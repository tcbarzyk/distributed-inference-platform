import json
import logging
import sys
import time
from dataclasses import dataclass
from json import JSONDecodeError
from pathlib import Path

import cv2
import numpy as np
import redis

# Support both direct-script and module execution.
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
SHARED_SRC = REPO_ROOT / "libs" / "platform_shared" / "src"
if str(SHARED_SRC) not in sys.path:
    sys.path.insert(0, str(SHARED_SRC))

from inference import InferenceInput, load_model, run_inference
from platform_shared.config import load_service_config
from results import publish_result

CONFIG = load_service_config(caller_file=__file__)

logging.basicConfig(
    level=CONFIG.log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("Worker")


@dataclass
class WorkerMetrics:
    jobs_popped: int = 0
    jobs_json_ok: int = 0
    jobs_invalid: int = 0
    missing_frame_key: int = 0
    blob_misses: int = 0
    decode_failures: int = 0
    inference_failures: int = 0
    publish_failures: int = 0
    redis_errors: int = 0
    processed_ok: int = 0
    deleted_blobs: int = 0
    queue_latency_samples: int = 0
    queue_latency_total_ms: float = 0.0
    start_time_s: float = 0.0
    end_time_s: float = 0.0
    last_summary_log_s: float = 0.0


def _safe_int(value):
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _fmt_latency(value):
    return f"{value:.2f}" if isinstance(value, (int, float)) else "n/a"


def _avg(total: float, samples: int) -> float:
    return (total / samples) if samples > 0 else 0.0


def _log_progress(metrics: WorkerMetrics) -> None:
    logger.info(
        (
            "Worker progress | popped=%d processed_ok=%d invalid=%d "
            "blob_misses=%d decode_failures=%d inference_failures=%d redis_errors=%d"
        ),
        metrics.jobs_popped,
        metrics.processed_ok,
        metrics.jobs_invalid,
        metrics.blob_misses,
        metrics.decode_failures,
        metrics.inference_failures,
        metrics.redis_errors,
    )


def _maybe_log_interval_summary(metrics: WorkerMetrics, *, force: bool = False) -> None:
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
        avg_queue_ms,
        throughput,
    )
    metrics.last_summary_log_s = now


def _log_summary(metrics: WorkerMetrics) -> None:
    elapsed = max(0.0, metrics.end_time_s - metrics.start_time_s)
    throughput = (metrics.processed_ok / elapsed) if elapsed > 0 else 0.0
    avg_queue_ms = _avg(metrics.queue_latency_total_ms, metrics.queue_latency_samples)
    logger.info(
        (
            "Summary final | service=worker elapsed_s=%.3f popped=%d json_ok=%d processed=%d "
            "invalid=%d missing_frame_key=%d blob_misses=%d decode_failures=%d "
            "inference_failures=%d publish_failures=%d redis_errors=%d deleted_blobs=%d avg_queue_ms=%.2f "
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
        throughput,
    )


def run_worker():
    metrics = WorkerMetrics(start_time_s=time.time())
    metrics.last_summary_log_s = metrics.start_time_s
    logger.info("Worker will connect to Redis at %s:%s", CONFIG.redis_host, CONFIG.redis_port)

    with redis.Redis(
        host=CONFIG.redis_host,
        port=CONFIG.redis_port,
        db=CONFIG.redis_db,
        password=CONFIG.redis_password,
        decode_responses=False,  # Keep binary data as bytes for image decode.
    ) as r:
        logger.info("Worker online. Waiting on queue: %s", CONFIG.queue_name)
        model = load_model()  # Preload model once to avoid first-frame latency.
        logger.info("Model loaded and ready for inference.")

        try:
            while True:
                _maybe_log_interval_summary(metrics)
                try:
                    result = r.brpop(CONFIG.queue_name, timeout=0)
                except redis.RedisError as exc:
                    metrics.redis_errors += 1
                    logger.error("Redis BRPOP failed: %s", exc)
                    continue

                if not result:
                    continue
                metrics.jobs_popped += 1
                _, job_raw = result

                try:
                    job = json.loads(job_raw.decode("utf-8"))
                except (UnicodeDecodeError, JSONDecodeError) as exc:
                    metrics.jobs_invalid += 1
                    logger.error("Failed to decode job JSON: %s", exc)
                    continue

                if not isinstance(job, dict):
                    metrics.jobs_invalid += 1
                    logger.warning("Job payload is not a JSON object: %s", type(job))
                    continue
                metrics.jobs_json_ok += 1

                frame_id = _safe_int(job.get("frame_id"))
                source_id = job.get("source_id")
                frame_key = job.get("frame_key")
                capture_ts_us = _safe_int(job.get("capture_ts_us"))
                enqueued_at_us = _safe_int(job.get("enqueued_at_us"))

                if not isinstance(frame_key, str) or frame_key.strip() == "":
                    metrics.jobs_invalid += 1
                    metrics.missing_frame_key += 1
                    logger.warning("Received job without valid frame_key; skipping. job=%s", job)
                    continue

                try:
                    frame_bytes = r.get(frame_key)
                except redis.RedisError as exc:
                    metrics.redis_errors += 1
                    logger.error("Redis GET failed for key=%s: %s", frame_key, exc)
                    continue

                if frame_bytes is None:
                    metrics.blob_misses += 1
                    logger.warning("Frame data missing or expired for key: %s", frame_key)
                    continue

                arr = np.frombuffer(frame_bytes, dtype=np.uint8)
                image = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
                if image is None:
                    metrics.decode_failures += 1
                    logger.error(
                        "Failed to decode image for frame_id=%s source_id=%s",
                        frame_id,
                        source_id,
                    )
                    continue

                now_us = int(time.time() * 1_000_000)
                queue_latency_ms = None
                if enqueued_at_us is not None:
                    queue_latency_ms = (now_us - enqueued_at_us) / 1000.0
                    if queue_latency_ms >= 0:
                        metrics.queue_latency_samples += 1
                        metrics.queue_latency_total_ms += queue_latency_ms

                source_latency_ms = None
                if capture_ts_us is not None:
                    source_latency_ms = (now_us - capture_ts_us) / 1000.0

                logger.debug(
                    "[OK] frame_id=%s source=%s shape=%s dtype=%s queue_latency_ms=%s source_latency_ms=%s",
                    frame_id,
                    source_id,
                    image.shape,
                    image.dtype,
                    _fmt_latency(queue_latency_ms),
                    _fmt_latency(source_latency_ms),
                )

                inference_input = InferenceInput(
                    frame_id=frame_id,
                    source_id=source_id,
                    frame_key=frame_key,
                    capture_ts_us=capture_ts_us,
                    enqueued_at_us=enqueued_at_us,
                    received_at_us=now_us,
                    image=image,
                )

                try:
                    inference_result = run_inference(inference_input, model)
                except Exception as exc:
                    metrics.inference_failures += 1
                    logger.exception("Inference failed for key=%s: %s", frame_key, exc)
                    continue

                metrics.processed_ok += 1
                logger.debug(
                    "Inference result: status=%s model=%s inference_ms=%.3f pipeline_ms=%.3f",
                    inference_result.get("status"),
                    inference_result.get("model"),
                    float(inference_result.get("inference_ms", 0.0)),
                    float(inference_result.get("pipeline_ms", 0.0)),
                )

                try:
                    publish_meta = publish_result(
                        config=CONFIG,
                        inference_result=inference_result,
                        image=image,
                        frame_id=frame_id,
                        source_id=source_id,
                    )
                    logger.debug(
                        "Result published: file=%s detections=%d annotated=%s",
                        publish_meta.get("results_file"),
                        publish_meta.get("detections_count"),
                        publish_meta.get("annotated_path"),
                    )
                except Exception as exc:
                    metrics.publish_failures += 1
                    logger.exception("Result publish failed for frame_id=%s: %s", frame_id, exc)

                try:
                    deleted = r.delete(frame_key)
                    if deleted:
                        metrics.deleted_blobs += 1
                except redis.RedisError as exc:
                    metrics.redis_errors += 1
                    logger.error("Redis DEL failed for key=%s: %s", frame_key, exc)

                _maybe_log_interval_summary(metrics)

        except KeyboardInterrupt:
            logger.info("Worker shutting down...")
        finally:
            _maybe_log_interval_summary(metrics, force=True)
            metrics.end_time_s = time.time()
            _log_summary(metrics)

    logger.info("Worker connection closed.")


if __name__ == "__main__":
    run_worker()
