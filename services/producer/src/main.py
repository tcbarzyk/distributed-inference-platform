"""
Redis producer service.

Flow:
1. Load and validate configuration.
2. Connect to Redis.
3. Choose source mode:
   - `sample_files`: discover disk frames and replay them.
   - `livestream`: read frames continuously from a live source.
4. For each frame, encode image bytes, write them to Redis with TTL, and enqueue
   a lightweight metadata job for workers.
5. Track and log run metrics.
"""

from pathlib import Path
import sys

# Support both direct-script and module execution.
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
SHARED_SRC = REPO_ROOT / "libs" / "platform_shared" / "src"
if str(SHARED_SRC) not in sys.path:
    sys.path.insert(0, str(SHARED_SRC))

import redis
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from frame_discovery import discover_frames, discover_frames_by_source
import cv2
from dataclasses import dataclass
from typing import Any

from platform_shared.schemas import QueueJob
from platform_shared.config import load_service_config
from platform_shared.observability.logging import get_logger, init_json_logging

CONFIG = load_service_config(caller_file=__file__)
init_json_logging(service_name="producer", log_level=CONFIG.log_level)
logger = get_logger("Producer")

def _log_extra(event: str, **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"event": event}
    for key, value in fields.items():
        if value is not None:
            payload[key] = value
    return payload


logger.info(
    "Producer will connect to Redis at %s:%s",
    CONFIG.redis_host,
    CONFIG.redis_port,
    extra=_log_extra("producer.startup.redis_target"),
)
SCHEMA_VERSION = 1


@dataclass
class ProducerMetrics:
    """Runtime counters used for progress and end-of-run summary logging."""

    discovered_frames: int = 0
    attempted_frames: int = 0
    published_frames: int = 0
    read_failures: int = 0
    encode_failures: int = 0
    redis_set_failures: int = 0
    queue_push_failures: int = 0
    dropped_frames: int = 0
    total_sleep_seconds: float = 0.0
    start_time_s: float = 0.0
    end_time_s: float = 0.0
    last_summary_log_s: float = 0.0


@dataclass(frozen=True)
class PublishRecord:
    """Minimal frame metadata required to publish one frame job."""

    frame_id: int
    source_id: str
    capture_ts_us: int
    path: str


def _build_job_id(record: PublishRecord) -> str:
    # Deterministic id keeps de-duplication simple in downstream systems.
    return f"{record.source_id}:{record.frame_id}:{record.capture_ts_us}"


def _publish_frame(r: redis.Redis, record: PublishRecord, image) -> tuple[bool, str | None]:
    """
    Encode and publish one frame.

    Returns:
    - (True, None) on success
    - (False, reason) on failure, where reason is one of:
      `encode_failed`, `redis_set_failed`, `queue_push_failed`
    """
    ok, encoded = cv2.imencode(
        ".jpg",
        image,
        [cv2.IMWRITE_JPEG_QUALITY, CONFIG.producer_jpeg_quality],
    )
    if not ok:
        return False, "encode_failed"

    frame_bytes = encoded.tobytes()
    frame_key = f"{CONFIG.producer_frame_key_prefix}:{record.source_id}:{record.frame_id}"

    try:
        r.set(
            name=frame_key,
            value=frame_bytes,
            ex=CONFIG.producer_frame_ttl_seconds,
        )
    except redis.RedisError as exc:
        logger.warning(
            "Redis SET failed for %s: %s",
            frame_key,
            exc,
            extra=_log_extra(
                "producer.frame.redis_set_failed",
                source_id=record.source_id,
                frame_id=record.frame_id,
                frame_key=frame_key,
            ),
        )
        return False, "redis_set_failed"

    job_data = QueueJob(
        schema_version=SCHEMA_VERSION,
        job_id=_build_job_id(record),
        frame_id=record.frame_id,
        source_id=record.source_id,
        capture_ts_us=record.capture_ts_us,
        frame_key=frame_key,
        enqueued_at_us=int(time.time() * 1_000_000),
    )

    try:
        r.lpush(CONFIG.queue_name, job_data.to_json_bytes())
    except redis.RedisError as exc:
        logger.warning(
            "Queue LPUSH failed for %s: %s",
            frame_key,
            exc,
            extra=_log_extra(
                "producer.queue.push_failed",
                source_id=record.source_id,
                frame_id=record.frame_id,
                frame_key=frame_key,
                job_id=job_data.job_id,
            ),
        )
        return False, "queue_push_failed"

    return True, None


def _sleep_for_replay_mode(
    frames,
    index: int,
    metrics: ProducerMetrics,
    *,
    loop_started_s: float | None = None,
) -> None:
    """Apply pacing between frames based on replay mode."""
    if CONFIG.producer_replay_mode == "fixed":
        target_period_s = 1 / CONFIG.producer_fixed_fps
        # Sleep only for remaining budget after processing this frame.
        if loop_started_s is None:
            sleep_time = target_period_s
        else:
            elapsed_s = time.perf_counter() - loop_started_s
            sleep_time = max(0.0, target_period_s - elapsed_s)
        time.sleep(sleep_time)
        metrics.total_sleep_seconds += sleep_time
        return

    if CONFIG.producer_replay_mode == "realtime":
        # Sleep by source capture-time delta between this frame and the next.
        if index >= len(frames) - 1:
            return

        current_ts_us = frames[index].capture_ts_us
        next_ts_us = frames[index + 1].capture_ts_us
        delta_us = next_ts_us - current_ts_us
        if delta_us <= 0:
            logger.warning(
                "Non-positive timestamp delta at frame_id=%s (delta_us=%s); skipping sleep.",
                frames[index].frame_id,
                delta_us,
                extra=_log_extra(
                    "producer.replay.invalid_delta",
                    frame_id=frames[index].frame_id,
                    source_id=frames[index].source_id,
                    delta_us=delta_us,
                ),
            )
            return

        sleep_time = delta_us / 1_000_000
        time.sleep(sleep_time)
        metrics.total_sleep_seconds += sleep_time
        return

    # max mode: do not sleep


def _producer_redis_errors(metrics: ProducerMetrics) -> int:
    return metrics.redis_set_failures + metrics.queue_push_failures


def _maybe_log_interval_summary(metrics: ProducerMetrics, mode_label: str, *, force: bool = False) -> None:
    now = time.time()
    if not force and (now - metrics.last_summary_log_s) < CONFIG.producer_summary_log_interval_seconds:
        return

    elapsed = max(0.0, now - metrics.start_time_s)
    throughput = (metrics.published_frames / elapsed) if elapsed > 0 else 0.0
    logger.info(
        (
            "Summary interval | service=producer mode=%s elapsed_s=%.1f discovered=%d attempted=%d "
            "processed=%d dropped=%d redis_errors=%d throughput_fps=%.2f"
        ),
        mode_label,
        elapsed,
        metrics.discovered_frames,
        metrics.attempted_frames,
        metrics.published_frames,
        metrics.dropped_frames,
        _producer_redis_errors(metrics),
        throughput,
        extra=_log_extra("producer.summary.interval", mode=mode_label),
    )
    metrics.last_summary_log_s = now


def _log_summary(metrics: ProducerMetrics, mode_label: str) -> None:
    """Log final producer summary line."""
    elapsed = max(0.0, metrics.end_time_s - metrics.start_time_s)
    effective_fps = (metrics.published_frames / elapsed) if elapsed > 0 else 0.0
    logger.info(
        (
            "Summary final | service=producer mode=%s elapsed_s=%.3f discovered=%d attempted=%d "
            "processed=%d dropped=%d read_failures=%d encode_failures=%d redis_errors=%d "
            "total_sleep_s=%.3f throughput_fps=%.2f"
        ),
        mode_label,
        elapsed,
        metrics.discovered_frames,
        metrics.attempted_frames,
        metrics.published_frames,
        metrics.dropped_frames,
        metrics.read_failures,
        metrics.encode_failures,
        _producer_redis_errors(metrics),
        metrics.total_sleep_seconds,
        effective_fps,
        extra=_log_extra("producer.summary.final", mode=mode_label),
    )


def _merge_metrics(total: ProducerMetrics, part: ProducerMetrics) -> None:
    """Aggregate one per-source metrics object into the service-level total."""
    total.discovered_frames += part.discovered_frames
    total.attempted_frames += part.attempted_frames
    total.published_frames += part.published_frames
    total.read_failures += part.read_failures
    total.encode_failures += part.encode_failures
    total.redis_set_failures += part.redis_set_failures
    total.queue_push_failures += part.queue_push_failures
    total.dropped_frames += part.dropped_frames
    total.total_sleep_seconds += part.total_sleep_seconds


def _run_source_frame_sequence(r: redis.Redis, source_id: str, frames) -> ProducerMetrics:
    """Publish one source's frame sequence while preserving source-local order/timing."""
    metrics = ProducerMetrics(start_time_s=time.time())
    metrics.last_summary_log_s = metrics.start_time_s
    metrics.discovered_frames = len(frames)

    for idx, record in enumerate(frames):
        loop_started_s = time.perf_counter()
        metrics.attempted_frames += 1
        image = cv2.imread(str(record.path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            metrics.read_failures += 1
            metrics.dropped_frames += 1
            logger.warning(
                "Failed to read image at %s for source=%s, skipping.",
                record.path,
                source_id,
                extra=_log_extra(
                    "producer.frame.read_failed",
                    source_id=source_id,
                    frame_id=record.frame_id,
                    path=str(record.path),
                ),
            )
            continue

        published, reason = _publish_frame(r, record, image)
        if not published:
            metrics.dropped_frames += 1
            if reason == "encode_failed":
                metrics.encode_failures += 1
            elif reason == "redis_set_failed":
                metrics.redis_set_failures += 1
            elif reason == "queue_push_failed":
                metrics.queue_push_failures += 1
            continue

        metrics.published_frames += 1
        _sleep_for_replay_mode(frames, idx, metrics, loop_started_s=loop_started_s)

    metrics.end_time_s = time.time()
    return metrics


def _run_sample_file_mode_parallel_sources(r: redis.Redis) -> None:
    """Replay sample-file frames with one producer loop per source in parallel."""
    metrics = ProducerMetrics(start_time_s=time.time())
    metrics.last_summary_log_s = metrics.start_time_s
    frames_by_source = discover_frames_by_source()
    source_count = len(frames_by_source)
    max_workers = min(CONFIG.producer_parallel_max_workers, max(1, source_count))
    logger.info(
        "Starting parallel sample-file replay: sources=%d max_workers=%d",
        source_count,
        max_workers,
        extra=_log_extra("producer.mode.sample_files_parallel.start"),
    )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_run_source_frame_sequence, r, source_id, frames): source_id
            for source_id, frames in frames_by_source.items()
        }
        for future in as_completed(futures):
            source_id = futures[future]
            try:
                source_metrics = future.result()
                _merge_metrics(metrics, source_metrics)
                logger.info(
                    (
                        "Parallel source complete | source=%s discovered=%d attempted=%d "
                        "processed=%d dropped=%d redis_errors=%d"
                    ),
                    source_id,
                    source_metrics.discovered_frames,
                    source_metrics.attempted_frames,
                    source_metrics.published_frames,
                    source_metrics.dropped_frames,
                    _producer_redis_errors(source_metrics),
                    extra=_log_extra("producer.source.complete", source_id=source_id),
                )
            except Exception as exc:
                logger.exception(
                    "Parallel source task failed for source=%s: %s",
                    source_id,
                    exc,
                    extra=_log_extra("producer.source.failed", source_id=source_id),
                )

    metrics.end_time_s = time.time()
    _maybe_log_interval_summary(metrics, "sample_files_parallel", force=True)
    _log_summary(metrics, "sample_files_parallel")


def _run_sample_file_mode(r: redis.Redis) -> None:
    """
    Replay discovered sample-file frames through Redis.

    Steps per frame:
    - read image from disk
    - publish frame bytes + metadata
    - apply replay sleep policy
    """
    metrics = ProducerMetrics(start_time_s=time.time())
    metrics.last_summary_log_s = metrics.start_time_s
    frames = discover_frames()
    metrics.discovered_frames = len(frames)
    logger.info(
        "Total frames discovered: %d",
        metrics.discovered_frames,
        extra=_log_extra("producer.discovery.complete"),
    )

    for idx, record in enumerate(frames):
        loop_started_s = time.perf_counter()
        _maybe_log_interval_summary(metrics, "sample_files")
        metrics.attempted_frames += 1
        image = cv2.imread(str(record.path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            metrics.read_failures += 1
            metrics.dropped_frames += 1
            logger.warning(
                "Failed to read image at %s, skipping.",
                record.path,
                extra=_log_extra(
                    "producer.frame.read_failed",
                    source_id=record.source_id,
                    frame_id=record.frame_id,
                    path=str(record.path),
                ),
            )
            continue

        published, reason = _publish_frame(r, record, image)
        if not published:
            metrics.dropped_frames += 1
            if reason == "encode_failed":
                metrics.encode_failures += 1
            elif reason == "redis_set_failed":
                metrics.redis_set_failures += 1
            elif reason == "queue_push_failed":
                metrics.queue_push_failures += 1
            continue

        metrics.published_frames += 1
        _sleep_for_replay_mode(frames, idx, metrics, loop_started_s=loop_started_s)
        _maybe_log_interval_summary(metrics, "sample_files")

    metrics.end_time_s = time.time()
    _maybe_log_interval_summary(metrics, "sample_files", force=True)
    _log_summary(metrics, "sample_files")


def _run_livestream_mode(r: redis.Redis) -> None:
    """
    Produce jobs from a live video source (RTSP/RTMP/webcam URL/device path).

    End-to-end flow per frame:
    1) Read frame from OpenCV capture.
    2) Build a capture timestamp in microseconds.
    3) Encode frame and write bytes to Redis with TTL.
    4) Push a lightweight metadata job to the Redis queue.
    5) Repeat until stream read fails or user interrupts.
    """
    metrics = ProducerMetrics(start_time_s=time.time())
    metrics.last_summary_log_s = metrics.start_time_s
    source_id = CONFIG.producer_stream_source_id or "stream-1"
    logger.info(
        "Starting livestream producer for source_id=%s",
        source_id,
        extra=_log_extra("producer.mode.livestream.start", source_id=source_id),
    )

    # OpenCV handles opening camera devices and network stream URLs.
    # Examples:
    # - 0 (webcam index)
    # - "rtsp://user:pass@camera-ip/stream"
    # - "rtmp://..."
    cap = cv2.VideoCapture(CONFIG.producer_stream_url)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open livestream source: {CONFIG.producer_stream_url}")

    frame_id = 0
    try:
        while True:
            loop_started_s = time.perf_counter()
            _maybe_log_interval_summary(metrics, "livestream")
            # Pull the next frame from the stream.
            # ok=False or image=None usually means source interruption/end/failure.
            ok, image = cap.read()
            if not ok or image is None:
                logger.warning(
                    "Livestream frame read failed; stopping stream loop.",
                    extra=_log_extra("producer.livestream.read_failed", source_id=source_id, frame_id=frame_id),
                )
                break

            metrics.attempted_frames += 1
            metrics.discovered_frames += 1

            # For live input, capture time is "now" at ingest.
            # Stored in microseconds to align with sample-file timestamp units.
            capture_ts_us = int(time.time() * 1_000_000)
            record = PublishRecord(
                frame_id=frame_id,
                source_id=source_id,
                capture_ts_us=capture_ts_us,
                path="<livestream>",
            )

            # Reuse the same publish routine as sample mode:
            # - JPEG encode
            # - Redis SET with TTL for frame bytes
            # - Queue LPUSH with metadata payload
            published, reason = _publish_frame(r, record, image)
            if not published:
                metrics.dropped_frames += 1
                if reason == "encode_failed":
                    metrics.encode_failures += 1
                elif reason == "redis_set_failed":
                    metrics.redis_set_failures += 1
                elif reason == "queue_push_failed":
                    metrics.queue_push_failures += 1
            else:
                metrics.published_frames += 1

            frame_id += 1

            # Replay mode behavior for live streams:
            # - fixed: throttle intentionally to configured FPS.
            # - realtime: do not sleep; the stream cadence is already real-time.
            # - max: do not sleep.
            if CONFIG.producer_replay_mode == "fixed":
                target_period_s = 1 / CONFIG.producer_fixed_fps
                elapsed_s = time.perf_counter() - loop_started_s
                sleep_time = max(0.0, target_period_s - elapsed_s)
                time.sleep(sleep_time)
                metrics.total_sleep_seconds += sleep_time
            elif CONFIG.producer_replay_mode == "realtime":
                # For live input, frames already arrive in real time, so no additional sleep.
                pass
            # max mode: no sleep
            _maybe_log_interval_summary(metrics, "livestream")
    except KeyboardInterrupt:
        logger.info(
            "Livestream shutdown signal received.",
            extra=_log_extra("producer.livestream.shutdown", source_id=source_id),
        )
    finally:
        # Always release capture handles and log a final run summary.
        cap.release()
        metrics.end_time_s = time.time()
        _maybe_log_interval_summary(metrics, "livestream", force=True)
        _log_summary(metrics, "livestream")


def validate_producer_config() -> None:
    """
    Perform producer-side runtime validation.

    Shared config already validates env types/ranges. This function validates
    the filesystem assumptions needed for file-based replay.
    """
    if CONFIG.producer_source_mode == "livestream":
        logger.info("Config validation passed for livestream mode.", extra=_log_extra("producer.config.validated"))
        return

    if CONFIG.producer_source_mode != "sample_files":
        raise ValueError(f"Unsupported PRODUCER_SOURCE_MODE: {CONFIG.producer_source_mode}")

    if not CONFIG.producer_sample_root:
        raise ValueError("PRODUCER_SAMPLE_ROOT is required for sample_files mode.")

    sample_root = Path(CONFIG.producer_sample_root)
    if not sample_root.exists():
        raise ValueError(f"Sample root does not exist: {sample_root}")
    if not sample_root.is_dir():
        raise ValueError(f"Sample root is not a directory: {sample_root}")

    if not CONFIG.producer_camera_dirs:
        raise ValueError("PRODUCER_CAMERA_DIRS must include at least one directory.")

    matched_total = 0
    for camera_dir in CONFIG.producer_camera_dirs:
        camera_path = sample_root / camera_dir
        if not camera_path.exists():
            raise ValueError(f"Camera directory does not exist: {camera_path}")
        if not camera_path.is_dir():
            raise ValueError(f"Camera path is not a directory: {camera_path}")

        match_count = len(list(camera_path.glob(CONFIG.producer_file_glob)))
        if match_count == 0:
            raise ValueError(
                f"No files matched glob '{CONFIG.producer_file_glob}' in {camera_path}"
            )
        matched_total += match_count

    logger.info(
        "Config validation passed for sample_files mode: root=%s, cameras=%s, matched_files=%d",
        sample_root,
        ",".join(CONFIG.producer_camera_dirs),
        matched_total,
        extra=_log_extra("producer.config.validated"),
    )


def run_producer():
    """
    Entry point for producer execution.

    Mode dispatch:
    - sample_files: reads indexed files from disk and replays them.
    - livestream: reads frames continuously from a live source.
    """
    validate_producer_config()

    with redis.Redis(
        host=CONFIG.redis_host,
        port=CONFIG.redis_port,
        db=CONFIG.redis_db,
        password=CONFIG.redis_password,
    ) as r:
        try:
            r.ping()
            logger.info(
                "Connected to Redis at %s:%s",
                CONFIG.redis_host,
                CONFIG.redis_port,
                extra=_log_extra("producer.redis.connected"),
            )
            if CONFIG.producer_source_mode == "sample_files":
                if CONFIG.producer_parallel_sources and len(CONFIG.producer_camera_dirs) > 1:
                    _run_sample_file_mode_parallel_sources(r)
                else:
                    _run_sample_file_mode(r)
            elif CONFIG.producer_source_mode == "livestream":
                _run_livestream_mode(r)
            else:
                raise ValueError(f"Unsupported PRODUCER_SOURCE_MODE: {CONFIG.producer_source_mode}")

        except redis.ConnectionError:
            logger.error(
                "Could not connect to Redis. Is the Docker container running?",
                extra=_log_extra("producer.redis.connect_failed"),
            )
        except KeyboardInterrupt:
            logger.info("Shutdown signal received.", extra=_log_extra("producer.shutdown"))
    
    # Once we exit the 'with' block, the connection is already closed.
    logger.info("Producer connection closed safely.", extra=_log_extra("producer.closed"))

if __name__ == "__main__":
    run_producer()
