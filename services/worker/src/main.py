"""
Redis worker service.

Flow:
1. Connect to Redis and load model once at startup.
2. Block on queue for frame metadata jobs.
3. Fetch frame bytes from Redis, decode image, and run inference.
4. Publish inference results (JSONL, optional annotated frames).
5. Track metrics and emit interval/final summaries.
"""

import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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

from inference import InferenceInput, get_model_device, load_model, run_inference
from platform_shared.config import load_service_config
from platform_shared.schemas import Detection, InferenceResult, QueueJob
from results import publish_result

CONFIG = load_service_config(caller_file=__file__)
SCHEMA_VERSION = 1



logging.basicConfig(
    level=CONFIG.log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("Worker")


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


def _log_progress(metrics: WorkerMetrics) -> None:
    """Legacy progress logger retained for debugging/compatibility."""
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
    """Emit end-of-run summary with throughput and error counters."""
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


def _decode_job_payload(job_raw: bytes, metrics: WorkerMetrics):
    """Decode queue payload bytes into a validated QueueJob model."""
    try:
        job = QueueJob.from_json_bytes(job_raw)
    except ValueError as exc:
        metrics.jobs_invalid += 1
        if "frame_key" in str(exc):
            metrics.missing_frame_key += 1
        logger.error("Failed to decode queue job: %s", exc)
        return None

    metrics.jobs_json_ok += 1
    return job


def _fetch_frame_bytes(r: redis.Redis, frame_key: str, metrics: WorkerMetrics):
    """Fetch encoded frame bytes from Redis and track fetch failures."""
    try:
        frame_bytes = r.get(frame_key)
    except redis.RedisError as exc:
        metrics.redis_errors += 1
        logger.error("Redis GET failed for key=%s: %s", frame_key, exc)
        return None

    if frame_bytes is None:
        metrics.blob_misses += 1
        logger.warning("Frame data missing or expired for key: %s", frame_key)
        return None

    return frame_bytes


def _decode_image(frame_bytes: bytes, frame_id, source_id, metrics: WorkerMetrics):
    """Decode encoded image bytes into an OpenCV image matrix."""
    arr = np.frombuffer(frame_bytes, dtype=np.uint8)
    image = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
    if image is None:
        metrics.decode_failures += 1
        logger.error(
            "Failed to decode image for frame_id=%s source_id=%s",
            frame_id,
            source_id,
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
        logger.exception("Inference failed for key=%s: %s", frame_key, exc)
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
        logger.debug(
            "Result published: file=%s detections=%d annotated=%s",
            publish_meta.get("results_file"),
            publish_meta.get("detections_count"),
            publish_meta.get("annotated_path"),
        )
    except Exception as exc:
        metrics.publish_failures += 1
        logger.exception("Result publish failed for frame_id=%s: %s", inference_result.frame_id, exc)


def _delete_frame_key(r: redis.Redis, frame_key: str, metrics: WorkerMetrics):
    """Best-effort cleanup of frame blob after processing."""
    try:
        deleted = r.delete(frame_key)
        if deleted:
            metrics.deleted_blobs += 1
    except redis.RedisError as exc:
        metrics.redis_errors += 1
        logger.error("Redis DEL failed for key=%s: %s", frame_key, exc)


def run_worker():
    """Main worker loop: consume jobs, process frames, publish results, log metrics."""
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
        model = load_model(CONFIG.worker_model_device)  # Preload model once to avoid first-frame latency.
        logger.info(
            "Model loaded and ready for inference. device_preference=%s resolved_device=%s",
            CONFIG.worker_model_device,
            get_model_device(),
        )

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
                    logger.error("Failed to build inference result schema: %s", exc)
                    continue


                metrics.processed_ok += 1
                logger.debug(
                    "Inference result: status=%s model=%s inference_ms=%.3f pipeline_ms=%.3f",
                    inference_result.status,
                    inference_result.model,
                    inference_result.inference_ms,
                    inference_result.pipeline_ms,
                )

                _publish_result_safe(image, inference_result, metrics)
                _delete_frame_key(r, job.frame_key, metrics)

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
