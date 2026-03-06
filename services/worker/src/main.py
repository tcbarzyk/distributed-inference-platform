"""
Redis worker service.

Flow:
1. Connect to Redis and load model once at startup.
2. Block on queue for frame metadata jobs.
3. Fetch frame bytes from Redis, decode image, and run inference.
4. Publish inference results (JSONL, optional annotated frames).
5. Track metrics and emit interval/final summaries.
"""

import json
import random
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import redis
from prometheus_client import start_http_server

# Support both direct-script and module execution.
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
SHARED_SRC = REPO_ROOT / "libs" / "platform_shared" / "src"
if str(SHARED_SRC) not in sys.path:
    sys.path.insert(0, str(SHARED_SRC))

from inference import InferenceInput, get_model_device, load_model, run_inference
from platform_shared.config import load_service_config
from platform_shared.observability.logging import get_logger, init_json_logging
from platform_shared.observability.metrics import (
    MetricNames,
    STAGE_LABELS,
    make_counter,
    make_gauge,
    make_histogram,
)
from platform_shared.schemas import Detection, InferenceResult, QueueJob
from results import publish_result, encode_live_frame_jpeg

CONFIG = load_service_config(caller_file=__file__)
SCHEMA_VERSION = 1
init_json_logging(service_name="worker", log_level=CONFIG.log_level)
logger = get_logger("Worker")
_SHUTDOWN_REQUESTED = False
_LAST_MJPEG_PUBLISH_BY_SOURCE: dict[str, float] = {}
_REDIS_BRPOP_BACKOFF_BASE_S = 1.0
_REDIS_BRPOP_BACKOFF_MAX_S = 30.0
_REDIS_BRPOP_JITTER_FACTOR = 0.2

WORKER_JOBS_POPPED_TOTAL = make_counter(
    MetricNames.WORKER_JOBS_POPPED_TOTAL,
    "Total jobs popped by worker.",
)
WORKER_JOBS_PROCESSED_TOTAL = make_counter(
    MetricNames.WORKER_JOBS_PROCESSED_TOTAL,
    "Total jobs successfully processed by worker.",
)
WORKER_FAILURES_TOTAL = make_counter(
    MetricNames.WORKER_FAILURES_TOTAL,
    "Total worker failures by stage.",
    labelnames=STAGE_LABELS,
)
WORKER_QUEUE_LATENCY_MS = make_histogram(
    MetricNames.WORKER_QUEUE_LATENCY_MS,
    "Queue wait latency in milliseconds.",
)
WORKER_INFERENCE_DURATION_MS = make_histogram(
    MetricNames.WORKER_INFERENCE_DURATION_MS,
    "Model inference duration in milliseconds.",
)
WORKER_PIPELINE_DURATION_MS = make_histogram(
    MetricNames.WORKER_PIPELINE_DURATION_MS,
    "End-to-end pipeline duration in milliseconds.",
)
WORKER_QUEUE_DEPTH = make_gauge(
    MetricNames.WORKER_QUEUE_DEPTH,
    "Current queue depth seen by worker.",
)


def _log_extra(event: str, **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"event": event}
    for key, value in fields.items():
        if value is not None:
            payload[key] = value
    return payload


def _request_shutdown(signum: int, _frame) -> None:
    """Signal handler: request graceful loop stop at the next checkpoint."""
    del _frame
    global _SHUTDOWN_REQUESTED
    if _SHUTDOWN_REQUESTED:
        return
    _SHUTDOWN_REQUESTED = True
    logger.info(
        "Shutdown signal received (signal=%s). Finishing in-progress frame...",
        signum,
        extra=_log_extra("worker.shutdown.requested", signal=signum),
    )


def _register_signal_handlers() -> None:
    """Register service-level POSIX signal handlers."""
    signal.signal(signal.SIGTERM, _request_shutdown)


@dataclass
class WorkerMetrics:
    """Runtime counters and timing aggregates for worker observability."""

    jobs_popped: int = 0
    jobs_json_ok: int = 0
    jobs_invalid: int = 0
    missing_frame_key: int = 0
    blob_misses: int = 0
    decode_failures: int = 0
    inference_failures: int = 0
    publish_failures: int = 0
    redis_errors: int = 0
    live_frame_plans: int = 0
    live_frame_skipped: int = 0
    live_frame_encode_failures: int = 0
    live_frame_write_failures: int = 0
    live_frame_written: int = 0
    mjpeg_publish_attempted: int = 0
    mjpeg_publish_sent: int = 0
    mjpeg_publish_skipped_rate_limit: int = 0
    mjpeg_publish_errors: int = 0
    processed_ok: int = 0
    deleted_blobs: int = 0
    queue_latency_samples: int = 0
    queue_latency_total_ms: float = 0.0
    start_time_s: float = 0.0
    end_time_s: float = 0.0
    last_summary_log_s: float = 0.0


def _fmt_latency(value):
    """Format latency value for logs; returns 'n/a' when missing."""
    return f"{value:.2f}" if isinstance(value, (int, float)) else "n/a"


def _avg(total: float, samples: int) -> float:
    """Safe average helper that avoids division by zero."""
    return (total / samples) if samples > 0 else 0.0


def _bound_redis_brpop_backoff_s(backoff_s: float) -> float:
    """Clamp Redis BRPOP backoff into configured base/max range."""
    return max(_REDIS_BRPOP_BACKOFF_BASE_S, min(backoff_s, _REDIS_BRPOP_BACKOFF_MAX_S))


def _compute_redis_brpop_retry_sleep_s(backoff_s: float) -> float:
    """Return a jittered retry sleep for Redis BRPOP errors."""
    bounded_backoff_s = _bound_redis_brpop_backoff_s(backoff_s)
    jitter_multiplier = 1.0 + random.uniform(-_REDIS_BRPOP_JITTER_FACTOR, _REDIS_BRPOP_JITTER_FACTOR)
    return min(_REDIS_BRPOP_BACKOFF_MAX_S, bounded_backoff_s * jitter_multiplier)


def _advance_redis_brpop_backoff_s(backoff_s: float, *, had_error: bool) -> float:
    """Return next Redis BRPOP retry backoff (doubles on error, resets on success)."""
    if not had_error:
        return _REDIS_BRPOP_BACKOFF_BASE_S
    bounded_backoff_s = _bound_redis_brpop_backoff_s(backoff_s)
    return min(bounded_backoff_s * 2.0, _REDIS_BRPOP_BACKOFF_MAX_S)


def _maybe_log_interval_summary(metrics: WorkerMetrics, *, force: bool = False) -> None:
    """Emit periodic summary logs on configured interval (or force)."""
    now = time.time()
    if not force and (now - metrics.last_summary_log_s) < CONFIG.worker_summary_log_interval_seconds:
        return

    elapsed = max(0.0, now - metrics.start_time_s)
    throughput = (metrics.processed_ok / elapsed) if elapsed > 0 else 0.0
    avg_queue_ms = _avg(metrics.queue_latency_total_ms, metrics.queue_latency_samples)
    logger.info(
        (
            "Summary interval | service=worker elapsed_s=%.1f popped=%d processed=%d invalid=%d "
            "blob_misses=%d decode_failures=%d inference_failures=%d publish_failures=%d redis_errors=%d "
            "live_plans=%d live_written=%d live_skipped=%d live_encode_failures=%d live_write_failures=%d "
            "mjpeg_attempted=%d mjpeg_sent=%d mjpeg_skipped_rate_limit=%d mjpeg_errors=%d "
            "avg_queue_ms=%.2f throughput_fps=%.2f"
        ),
        elapsed,
        metrics.jobs_popped,
        metrics.processed_ok,
        metrics.jobs_invalid,
        metrics.blob_misses,
        metrics.decode_failures,
        metrics.inference_failures,
        metrics.publish_failures,
        metrics.redis_errors,
        metrics.live_frame_plans,
        metrics.live_frame_written,
        metrics.live_frame_skipped,
        metrics.live_frame_encode_failures,
        metrics.live_frame_write_failures,
        metrics.mjpeg_publish_attempted,
        metrics.mjpeg_publish_sent,
        metrics.mjpeg_publish_skipped_rate_limit,
        metrics.mjpeg_publish_errors,
        avg_queue_ms,
        throughput,
        extra=_log_extra("worker.summary.interval"),
    )
    metrics.last_summary_log_s = now


def _log_summary(metrics: WorkerMetrics) -> None:
    """Emit end-of-run summary with throughput and error counters."""
    elapsed = max(0.0, metrics.end_time_s - metrics.start_time_s)
    throughput = (metrics.processed_ok / elapsed) if elapsed > 0 else 0.0
    avg_queue_ms = _avg(metrics.queue_latency_total_ms, metrics.queue_latency_samples)
    logger.info(
        (
            "Summary final | service=worker elapsed_s=%.3f popped=%d json_ok=%d processed=%d "
            "invalid=%d missing_frame_key=%d blob_misses=%d decode_failures=%d "
            "inference_failures=%d publish_failures=%d redis_errors=%d deleted_blobs=%d avg_queue_ms=%.2f "
            "live_plans=%d live_written=%d live_skipped=%d live_encode_failures=%d live_write_failures=%d "
            "mjpeg_attempted=%d mjpeg_sent=%d mjpeg_skipped_rate_limit=%d mjpeg_errors=%d "
            "throughput_fps=%.2f"
        ),
        elapsed,
        metrics.jobs_popped,
        metrics.jobs_json_ok,
        metrics.processed_ok,
        metrics.jobs_invalid,
        metrics.missing_frame_key,
        metrics.blob_misses,
        metrics.decode_failures,
        metrics.inference_failures,
        metrics.publish_failures,
        metrics.redis_errors,
        metrics.deleted_blobs,
        avg_queue_ms,
        metrics.live_frame_plans,
        metrics.live_frame_written,
        metrics.live_frame_skipped,
        metrics.live_frame_encode_failures,
        metrics.live_frame_write_failures,
        metrics.mjpeg_publish_attempted,
        metrics.mjpeg_publish_sent,
        metrics.mjpeg_publish_skipped_rate_limit,
        metrics.mjpeg_publish_errors,
        throughput,
        extra=_log_extra("worker.summary.final"),
    )


def _decode_job_payload(job_raw: bytes, metrics: WorkerMetrics):
    """Decode queue payload bytes into a validated QueueJob model."""
    try:
        job = QueueJob.from_json_bytes(job_raw)
    except ValueError as exc:
        metrics.jobs_invalid += 1
        WORKER_FAILURES_TOTAL.labels(stage="decode").inc()
        if "frame_key" in str(exc):
            metrics.missing_frame_key += 1
        logger.error("Failed to decode queue job: %s", exc, extra=_log_extra("worker.job.decode_failed"))
        return None

    metrics.jobs_json_ok += 1
    return job


def _fetch_frame_bytes(r: redis.Redis, frame_key: str, metrics: WorkerMetrics):
    """Fetch encoded frame bytes from Redis and track fetch failures."""
    try:
        frame_bytes = r.get(frame_key)
    except redis.RedisError as exc:
        metrics.redis_errors += 1
        WORKER_FAILURES_TOTAL.labels(stage="redis").inc()
        logger.error(
            "Redis GET failed for key=%s: %s",
            frame_key,
            exc,
            extra=_log_extra("worker.frame.fetch_failed", frame_key=frame_key),
        )
        return None

    if frame_bytes is None:
        metrics.blob_misses += 1
        WORKER_FAILURES_TOTAL.labels(stage="decode").inc()
        logger.warning(
            "Frame data missing or expired for key: %s",
            frame_key,
            extra=_log_extra("worker.frame.missing", frame_key=frame_key),
        )
        return None

    return frame_bytes


def _decode_image(frame_bytes: bytes, frame_id, source_id, metrics: WorkerMetrics):
    """Decode encoded image bytes into an OpenCV image matrix."""
    arr = np.frombuffer(frame_bytes, dtype=np.uint8)
    image = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
    if image is None:
        metrics.decode_failures += 1
        WORKER_FAILURES_TOTAL.labels(stage="decode").inc()
        logger.error(
            "Failed to decode image for frame_id=%s source_id=%s",
            frame_id,
            source_id,
            extra=_log_extra("worker.frame.decode_failed", frame_id=frame_id, source_id=source_id),
        )
        return None
    return image


def _compute_queue_latency_ms(now_us: int, enqueued_at_us, metrics: WorkerMetrics):
    """Compute queue wait latency and update aggregate metrics."""
    queue_latency_ms = None
    if enqueued_at_us is not None:
        queue_latency_ms = (now_us - enqueued_at_us) / 1000.0
        if queue_latency_ms >= 0:
            metrics.queue_latency_samples += 1
            metrics.queue_latency_total_ms += queue_latency_ms
            WORKER_QUEUE_LATENCY_MS.observe(queue_latency_ms)
    return queue_latency_ms


def _compute_source_latency_ms(now_us: int, capture_ts_us):
    """Compute source-to-worker latency for per-frame debug logging."""
    source_latency_ms = None
    if capture_ts_us is not None:
        source_latency_ms = (now_us - capture_ts_us) / 1000.0
    return source_latency_ms


def _run_inference_safe(inference_input: InferenceInput, model, frame_key: str, metrics: WorkerMetrics):
    """Run inference with exception isolation so one frame cannot kill the loop."""
    try:
        inference_result = run_inference(inference_input, model)
    except Exception as exc:
        metrics.inference_failures += 1
        WORKER_FAILURES_TOTAL.labels(stage="inference").inc()
        logger.exception(
            "Inference failed for key=%s: %s",
            frame_key,
            exc,
            extra=_log_extra("worker.inference.failed", frame_key=frame_key),
        )
        return None
    return inference_result


def _build_result_model(job: QueueJob, inference_result: dict[str, Any], now_us: int) -> InferenceResult:
    """Normalize raw inference dict into the shared InferenceResult schema."""
    detections_raw = inference_result.get("detections", [])
    detections = tuple(Detection.from_dict(item) for item in detections_raw)

    status = inference_result.get("status")
    model = inference_result.get("model")
    inference_ms = float(inference_result.get("inference_ms", 0.0))
    pipeline_ms = float(inference_result.get("pipeline_ms", 0.0))
    if not isinstance(status, str) or not isinstance(model, str):
        raise ValueError("Inference output is missing required status/model fields.")

    return InferenceResult(
        schema_version=SCHEMA_VERSION,
        job_id=job.job_id,
        frame_id=job.frame_id,
        source_id=job.source_id,
        status=status,
        model=model,
        inference_ms=inference_ms,
        pipeline_ms=pipeline_ms,
        processed_at_us=now_us,
        detections=detections,
    )


def _publish_result_safe(
    r: redis.Redis,
    image,
    inference_result: InferenceResult,
    metrics: WorkerMetrics,
):
    """Publish inference result and isolate publisher errors from main loop."""
    try:
        publish_meta = publish_result(
            config=CONFIG,
            inference_result=inference_result,
            image=image,
        )
        _publish_live_frame_safe(
          r=r,
          inference_result=inference_result,
          image=image,
          publish_meta=publish_meta,
          metrics=metrics,
        )
        logger.debug(
            "Result published: file=%s detections=%d annotated=%s",
            publish_meta.get("results_file"),
            publish_meta.get("detections_count"),
            publish_meta.get("annotated_path"),
            extra=_log_extra(
                "worker.result.published",
                source_id=inference_result.source_id,
                frame_id=inference_result.frame_id,
                job_id=inference_result.job_id,
            ),
        )
    except Exception as exc:
        metrics.publish_failures += 1
        WORKER_FAILURES_TOTAL.labels(stage="publish").inc()
        logger.exception(
            "Result publish failed for frame_id=%s: %s",
            inference_result.frame_id,
            exc,
            extra=_log_extra(
                "worker.result.publish_failed",
                source_id=inference_result.source_id,
                frame_id=inference_result.frame_id,
                job_id=inference_result.job_id,
            ),
        )


def _mjpeg_channel_for_source(source_id: str) -> str:
    return f"{CONFIG.worker_mjpeg_channel_prefix}.{source_id}"


def _should_publish_mjpeg_for_source(source_id: str) -> bool:
    if not CONFIG.worker_mjpeg_publish_enabled:
        return False
    min_interval_s = 1.0 / float(CONFIG.worker_mjpeg_max_fps)
    now_s = time.time()
    last_s = _LAST_MJPEG_PUBLISH_BY_SOURCE.get(source_id)
    if last_s is not None and (now_s - last_s) < min_interval_s:
        return False
    _LAST_MJPEG_PUBLISH_BY_SOURCE[source_id] = now_s
    return True


def _delete_frame_key(r: redis.Redis, frame_key: str, metrics: WorkerMetrics):
    """Best-effort cleanup of frame blob after processing."""
    try:
        deleted = r.delete(frame_key)
        if deleted:
            metrics.deleted_blobs += 1
    except redis.RedisError as exc:
        metrics.redis_errors += 1
        WORKER_FAILURES_TOTAL.labels(stage="redis").inc()
        logger.error(
            "Redis DEL failed for key=%s: %s",
            frame_key,
            exc,
            extra=_log_extra("worker.frame.delete_failed", frame_key=frame_key),
        )


def _start_metrics_server() -> None:
    try:
        start_http_server(CONFIG.worker_metrics_port)
        logger.info(
            "Prometheus metrics server started on port %d",
            CONFIG.worker_metrics_port,
            extra=_log_extra("worker.metrics.server_started", port=CONFIG.worker_metrics_port),
        )
    except OSError as exc:
        logger.warning(
            "Prometheus metrics server could not start on port %d: %s",
            CONFIG.worker_metrics_port,
            exc,
            extra=_log_extra("worker.metrics.server_failed", port=CONFIG.worker_metrics_port),
        )


def run_worker():
    """Main worker loop: consume jobs, process frames, publish results, log metrics."""
    global _SHUTDOWN_REQUESTED
    _SHUTDOWN_REQUESTED = False
    _register_signal_handlers()
    _start_metrics_server()
    metrics = WorkerMetrics(start_time_s=time.time())
    metrics.last_summary_log_s = metrics.start_time_s
    logger.info(
        "Worker will connect to Redis at %s:%s",
        CONFIG.redis_host,
        CONFIG.redis_port,
        extra=_log_extra("worker.startup.redis_target"),
    )

    with redis.Redis(
        host=CONFIG.redis_host,
        port=CONFIG.redis_port,
        db=CONFIG.redis_db,
        password=CONFIG.redis_password,
        decode_responses=False,  # Keep binary data as bytes for image decode.
    ) as r:
        logger.info(
            "Worker online. Waiting on queue: %s",
            CONFIG.queue_name,
            extra=_log_extra("worker.startup.online", queue_name=CONFIG.queue_name),
        )
        model = load_model(CONFIG.worker_model_device)  # Preload model once to avoid first-frame latency.
        logger.info(
            "Model loaded and ready for inference. device_preference=%s resolved_device=%s",
            CONFIG.worker_model_device,
            get_model_device(),
            extra=_log_extra("worker.startup.model_ready"),
        )
        logger.info(
            (
                "Live frame settings | enabled=%s every_n=%d ttl_s=%d "
                "jpeg_quality=%d frame_key_prefix=%s meta_key_prefix=%s"
            ),
            CONFIG.worker_live_frames_enabled,
            CONFIG.worker_live_frames_every_n,
            CONFIG.worker_live_frame_ttl_seconds,
            CONFIG.worker_live_frames_jpeg_quality,
            CONFIG.worker_live_frame_key_prefix,
            CONFIG.worker_live_meta_key_prefix,
            extra=_log_extra("worker.config.live_frames"),
        )
        logger.info(
            "MJPEG PubSub settings | enabled=%s max_fps=%d channel_prefix=%s",
            CONFIG.worker_mjpeg_publish_enabled,
            CONFIG.worker_mjpeg_max_fps,
            CONFIG.worker_mjpeg_channel_prefix,
            extra=_log_extra("worker.config.mjpeg"),
        )

        redis_brpop_backoff_s = _REDIS_BRPOP_BACKOFF_BASE_S
        try:
            while not _SHUTDOWN_REQUESTED:
                _maybe_log_interval_summary(metrics)
                try:
                    result = r.brpop(CONFIG.queue_name, timeout=1)
                except redis.RedisError as exc:
                    metrics.redis_errors += 1
                    WORKER_FAILURES_TOTAL.labels(stage="redis").inc()
                    sleep_s = _compute_redis_brpop_retry_sleep_s(redis_brpop_backoff_s)
                    logger.error(
                        "Redis BRPOP failed: %s. Backing off for %.2fs",
                        exc,
                        sleep_s,
                        extra=_log_extra("worker.queue.pop_failed", retry_in_s=sleep_s),
                    )
                    time.sleep(sleep_s)
                    redis_brpop_backoff_s = _advance_redis_brpop_backoff_s(redis_brpop_backoff_s, had_error=True)
                    continue
                redis_brpop_backoff_s = _advance_redis_brpop_backoff_s(redis_brpop_backoff_s, had_error=False)

                if not result:
                    continue
                metrics.jobs_popped += 1
                WORKER_JOBS_POPPED_TOTAL.inc()
                _, job_raw = result
                try:
                    WORKER_QUEUE_DEPTH.set(float(r.llen(CONFIG.queue_name)))
                except redis.RedisError:
                    pass

                job = _decode_job_payload(job_raw, metrics)
                if job is None:
                    continue

                frame_bytes = _fetch_frame_bytes(r, job.frame_key, metrics)
                if frame_bytes is None:
                    continue

                image = _decode_image(frame_bytes, job.frame_id, job.source_id, metrics)
                if image is None:
                    continue

                now_us = int(time.time() * 1_000_000)
                queue_latency_ms = _compute_queue_latency_ms(now_us, job.enqueued_at_us, metrics)
                source_latency_ms = _compute_source_latency_ms(now_us, job.capture_ts_us)

                logger.debug(
                    "[OK] frame_id=%s source=%s shape=%s dtype=%s queue_latency_ms=%s source_latency_ms=%s",
                    job.frame_id,
                    job.source_id,
                    image.shape,
                    image.dtype,
                    _fmt_latency(queue_latency_ms),
                    _fmt_latency(source_latency_ms),
                    extra=_log_extra(
                        "worker.job.received",
                        job_id=job.job_id,
                        frame_id=job.frame_id,
                        source_id=job.source_id,
                        queue_latency_ms=queue_latency_ms,
                        source_latency_ms=source_latency_ms,
                    ),
                )

                inference_input = InferenceInput(
                    frame_id=job.frame_id,
                    source_id=job.source_id,
                    frame_key=job.frame_key,
                    capture_ts_us=job.capture_ts_us,
                    enqueued_at_us=job.enqueued_at_us,
                    received_at_us=now_us,
                    image=image,
                )

                inference_result_raw = _run_inference_safe(inference_input, model, job.frame_key, metrics)
                if inference_result_raw is None:
                    continue

                try:
                    inference_result = _build_result_model(job, inference_result_raw, now_us)
                except ValueError as exc:
                    metrics.publish_failures += 1
                    WORKER_FAILURES_TOTAL.labels(stage="publish").inc()
                    logger.error(
                        "Failed to build inference result schema: %s",
                        exc,
                        extra=_log_extra(
                            "worker.result.schema_build_failed",
                            job_id=job.job_id,
                            frame_id=job.frame_id,
                            source_id=job.source_id,
                        ),
                    )
                    continue


                metrics.processed_ok += 1
                WORKER_JOBS_PROCESSED_TOTAL.inc()
                logger.debug(
                    "Inference result: status=%s model=%s inference_ms=%.3f pipeline_ms=%.3f",
                    inference_result.status,
                    inference_result.model,
                    inference_result.inference_ms,
                    inference_result.pipeline_ms,
                    extra=_log_extra(
                        "worker.inference.completed",
                        job_id=inference_result.job_id,
                        frame_id=inference_result.frame_id,
                        source_id=inference_result.source_id,
                        inference_ms=inference_result.inference_ms,
                        pipeline_ms=inference_result.pipeline_ms,
                    ),
                )

                _publish_result_safe(r=r, image=image, inference_result=inference_result, metrics=metrics)
                _publish_mjpeg_safe(r=r, image=image, inference_result=inference_result, metrics=metrics)
                _delete_frame_key(r, job.frame_key, metrics)
                WORKER_INFERENCE_DURATION_MS.observe(float(inference_result.inference_ms))
                WORKER_PIPELINE_DURATION_MS.observe(float(inference_result.pipeline_ms))

                _maybe_log_interval_summary(metrics)

            if _SHUTDOWN_REQUESTED:
                logger.info("Worker shutting down...", extra=_log_extra("worker.shutdown"))
        except KeyboardInterrupt:
            logger.info("Worker shutting down...", extra=_log_extra("worker.shutdown"))
        finally:
            _maybe_log_interval_summary(metrics, force=True)
            metrics.end_time_s = time.time()
            _log_summary(metrics)

    logger.info("Worker connection closed.", extra=_log_extra("worker.closed"))

def _publish_live_frame_safe(
  *,
  r: redis.Redis,
  inference_result: InferenceResult,
  image,
  publish_meta: dict[str, Any],
  metrics: WorkerMetrics,
) -> None:
    plan = publish_meta.get("live_frame_plan") or {}
    metrics.live_frame_plans += 1
    if not plan.get("enabled") or not plan.get("should_publish"):
        metrics.live_frame_skipped += 1
        logger.debug(
            "Live frame skipped frame_id=%s source_id=%s reason=%s enabled=%s should_publish=%s",
            inference_result.frame_id,
            inference_result.source_id,
            plan.get("reason"),
            plan.get("enabled"),
            plan.get("should_publish"),
            extra=_log_extra(
                "worker.live_frame.skipped",
                source_id=inference_result.source_id,
                frame_id=inference_result.frame_id,
            ),
        )
        return
    frame_key = plan["frame_key"]
    meta_key = plan["meta_key"]
    ttl = int(plan["ttl_seconds"])
    metadata = dict(plan.get("metadata") or {})

    jpeg_bytes = encode_live_frame_jpeg(
        image=image,
        detections=[d.to_dict() for d in inference_result.detections],
        jpeg_quality=CONFIG.worker_live_frames_jpeg_quality,
    )
    if jpeg_bytes is None:
        metrics.live_frame_encode_failures += 1
        WORKER_FAILURES_TOTAL.labels(stage="publish").inc()
        logger.error(
            "Failed to encode live frame JPEG for frame_id=%s",
            inference_result.frame_id,
            extra=_log_extra(
                "worker.live_frame.encode_failed",
                source_id=inference_result.source_id,
                frame_id=inference_result.frame_id,
            ),
        )
        return
    metadata["byte_length"] = len(jpeg_bytes)
    metadata["written_at_us"] = int(time.time() * 1_000_000)
    try:
        pipe = r.pipeline(transaction=True)
        pipe.set(frame_key, jpeg_bytes, ex=ttl)
        pipe.set(meta_key, json.dumps(metadata), ex=ttl)
        # Optional for future WS/MJPEG fanout:
        # pipe.publish(f"live.frames.{inference_result.source_id}", jpeg_bytes)
        # pipe.publish(f"live.meta.{inference_result.source_id}", json.dumps(metadata))
        pipe.execute()
        metrics.live_frame_written += 1
        logger.debug(
            "Live frame written source_id=%s frame_id=%s frame_key=%s meta_key=%s bytes=%d ttl_s=%d",
            inference_result.source_id,
            inference_result.frame_id,
            frame_key,
            meta_key,
            len(jpeg_bytes),
            ttl,
            extra=_log_extra(
                "worker.live_frame.written",
                source_id=inference_result.source_id,
                frame_id=inference_result.frame_id,
                frame_key=frame_key,
                meta_key=meta_key,
            ),
        )
    except redis.RedisError as exc:
        metrics.redis_errors += 1
        metrics.live_frame_write_failures += 1
        WORKER_FAILURES_TOTAL.labels(stage="redis").inc()
        logger.warning(
            "Live frame Redis publish failed source=%s: %s",
            inference_result.source_id,
            exc,
            extra=_log_extra(
                "worker.live_frame.publish_failed",
                source_id=inference_result.source_id,
                frame_id=inference_result.frame_id,
            ),
        )


def _publish_mjpeg_safe(
  *,
  r: redis.Redis,
  inference_result: InferenceResult,
  image,
  metrics: WorkerMetrics,
) -> None:
    if not CONFIG.worker_mjpeg_publish_enabled:
        return
    metrics.mjpeg_publish_attempted += 1
    if not _should_publish_mjpeg_for_source(inference_result.source_id):
        metrics.mjpeg_publish_skipped_rate_limit += 1
        return

    jpeg_bytes = encode_live_frame_jpeg(
        image=image,
        detections=[d.to_dict() for d in inference_result.detections],
        jpeg_quality=CONFIG.worker_live_frames_jpeg_quality,
    )
    if jpeg_bytes is None:
        metrics.mjpeg_publish_errors += 1
        return

    channel = _mjpeg_channel_for_source(inference_result.source_id)
    try:
        sent = r.publish(channel, jpeg_bytes)
        metrics.mjpeg_publish_sent += 1
        logger.debug(
            "MJPEG frame published source_id=%s frame_id=%s channel=%s subscribers=%d bytes=%d",
            inference_result.source_id,
            inference_result.frame_id,
            channel,
            sent,
            len(jpeg_bytes),
            extra=_log_extra(
                "worker.mjpeg.published",
                source_id=inference_result.source_id,
                frame_id=inference_result.frame_id,
                channel=channel,
                subscribers=sent,
            ),
        )
    except redis.RedisError as exc:
        metrics.redis_errors += 1
        metrics.mjpeg_publish_errors += 1
        WORKER_FAILURES_TOTAL.labels(stage="redis").inc()
        logger.warning(
            "MJPEG publish failed source=%s frame_id=%s channel=%s: %s",
            inference_result.source_id,
            inference_result.frame_id,
            channel,
            exc,
            extra=_log_extra(
                "worker.mjpeg.publish_failed",
                source_id=inference_result.source_id,
                frame_id=inference_result.frame_id,
                channel=channel,
            ),
        )


if __name__ == "__main__":
    run_worker()
