"""Result publishing helpers for worker outputs.

This module intentionally keeps publication side effects isolated from the main
consume/inference loop so failures can be handled centrally in worker metrics.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from sqlalchemy.orm import Session

from platform_shared.config import ServiceConfig
from platform_shared.models import JobModel, ResultModel, SourceModel
from platform_shared.schemas import InferenceResult

from db import SessionLocal


@dataclass(frozen=True)
class LiveFramePublishPlan:
    """Prepared plan for live-frame publication (no Redis side effects yet)."""

    enabled: bool
    should_publish: bool
    frame_key: str | None
    meta_key: str | None
    ttl_seconds: int
    reason: str
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _safe_source_id(value: str | None) -> str:
    """Normalize source id for Redis key segments."""
    return (value or "source").replace("/", "_").replace(":", "_")


def _build_live_frame_key(config: ServiceConfig, source_id: str) -> str:
    """Build Redis key where the latest annotated JPEG bytes will be stored."""
    return f"{config.worker_live_frame_key_prefix}:{_safe_source_id(source_id)}"


def _build_live_meta_key(config: ServiceConfig, source_id: str) -> str:
    """Build Redis key where the latest live-frame metadata JSON will be stored."""
    return f"{config.worker_live_meta_key_prefix}:{_safe_source_id(source_id)}"


def _should_publish_live_frame(config: ServiceConfig, frame_id: int | None) -> bool:
    """Determine whether this frame qualifies for live-frame publication cadence."""
    if not config.worker_live_frames_enabled:
        return False
    if frame_id is None:
        return False
    return frame_id % config.worker_live_frames_every_n == 0


def build_live_frame_publish_plan(
    *,
    config: ServiceConfig,
    inference_result: InferenceResult,
) -> LiveFramePublishPlan:
    """Prepare key names and metadata envelope for future Redis live-frame writes.

    This function is intentionally side-effect free. Redis SET/PUBLISH logic is
    added later, while callers can already rely on one stable plan shape.
    """
    source_id = inference_result.source_id
    frame_key = _build_live_frame_key(config, source_id)
    meta_key = _build_live_meta_key(config, source_id)
    should_publish = _should_publish_live_frame(config, inference_result.frame_id)
    reason = "ok" if should_publish else "disabled_or_filtered"
    metadata = {
        "source_id": source_id,
        "job_id": inference_result.job_id,
        "frame_id": inference_result.frame_id,
        "processed_at_us": inference_result.processed_at_us,
        "frame_key": frame_key,
        "content_type": "image/jpeg",
    }
    return LiveFramePublishPlan(
        enabled=config.worker_live_frames_enabled,
        should_publish=should_publish,
        frame_key=frame_key if should_publish else None,
        meta_key=meta_key if should_publish else None,
        ttl_seconds=config.worker_live_frame_ttl_seconds,
        reason=reason,
        metadata=metadata,
    )


def _ensure_dirs(config: ServiceConfig) -> tuple[Path, Path]:
    """Create local output directories used by JSONL mode if missing."""
    results_dir = Path(config.worker_results_dir)
    annotated_dir = results_dir / "annotated"
    results_dir.mkdir(parents=True, exist_ok=True)
    annotated_dir.mkdir(parents=True, exist_ok=True)
    return results_dir, annotated_dir


def _results_file_path(results_dir: Path) -> Path:
    # One file per UTC day keeps local output append-only and easy to rotate.
    day = datetime.now().strftime("%Y%m%d")
    return results_dir / f"results-{day}.jsonl"

def encode_live_frame_jpeg(*, image: np.ndarray, detections: list[dict[str, Any]], jpeg_quality: int) -> bytes | None:
    rendered = _draw_detections(image, detections)
    ok, encoded = cv2.imencode(".jpg", rendered, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
    if not ok:
        return None
    return encoded.tobytes()

def _draw_detections(image: np.ndarray, detections: list[dict[str, Any]]) -> np.ndarray:
    """Render bounding boxes and labels for optional debug snapshots."""
    canvas = image.copy()
    if len(canvas.shape) == 2:
        canvas = cv2.cvtColor(canvas, cv2.COLOR_GRAY2BGR)

    for det in detections:
        bbox = det.get("bbox_xyxy") or []
        if len(bbox) != 4:
            continue
        x1, y1, x2, y2 = [int(float(v)) for v in bbox]
        label = str(det.get("label", "obj"))
        conf = float(det.get("confidence", 0.0))
        text = f"{label} {conf:.2f}"
        cv2.rectangle(canvas, (x1, y1), (x2, y2), (0, 200, 0), 2)
        cv2.putText(canvas, text, (x1, max(0, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 0), 1)
    return canvas

def publish_to_json(*,
    config: ServiceConfig,
    inference_result: InferenceResult,
    image: np.ndarray,
) -> dict[str, Any]:
    """Persist one inference result to local JSONL (and optional annotated image)."""
    results_dir, annotated_dir = _ensure_dirs(config)
    results_file = _results_file_path(results_dir)
    live_frame_plan = build_live_frame_publish_plan(config=config, inference_result=inference_result)

    record = inference_result.to_dict()

    with results_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")

    annotated_path = None
    frame_id = inference_result.frame_id
    source_id = inference_result.source_id
    should_save_annotated = (
        config.worker_save_annotated
        and frame_id is not None
        and (frame_id % config.worker_annotated_every_n == 0)
    )
    if should_save_annotated:
        rendered = _draw_detections(image, record["detections"])
        # Keep output path safe across OS/filesystems and source naming styles.
        safe_source = (source_id or "source").replace("/", "_")
        annotated_path = annotated_dir / f"{safe_source}-frame-{frame_id}.jpg"
        cv2.imwrite(str(annotated_path), rendered)

    return {
        "results_file": str(results_file),
        "annotated_path": str(annotated_path) if annotated_path else None,
        "detections_count": len(record["detections"]),
        "live_frame_plan": live_frame_plan.to_dict(),
    }

def publish_to_postgres(*,
    config: ServiceConfig,
    inference_result: InferenceResult,
    image: np.ndarray,
) -> dict[str, Any]:
    """Persist one inference result to PostgreSQL.

    Transaction semantics:
    - ensure parent `source` exists
    - ensure parent `job` exists/updated
    - insert one `result` row
    - commit or rollback atomically
    """
    del image   # image is not needed for DB mode
    live_frame_plan = build_live_frame_publish_plan(config=config, inference_result=inference_result)

    session: Session = SessionLocal()
    try:
        # Source dimension row: create on first sight of a source_id.
        # Future improvement: move source registration to API/control-plane so
        # workers never infer metadata like `name/kind` at write time.
        source = (
            session.query(SourceModel)
            .filter(SourceModel.source_id == inference_result.source_id)
            .one_or_none()
        )
        if source is None:
            source = SourceModel(
                source_id=inference_result.source_id,
                name=inference_result.source_id,
                # Future improvement: populate from known source registry instead
                # of placeholder fallback values.
                kind="unknown",
            )
            session.add(source)

        # Job row is keyed by external job_id for cross-service traceability.
        # Future improvement: track full state transitions (queued/processing/done/failed)
        # and retry/attempt metadata in DB.
        job = (
            session.query(JobModel)
            .filter(JobModel.job_id == inference_result.job_id)
            .one_or_none()
        )
        if job is None:
            job = JobModel(
                job_id=inference_result.job_id,
                source_id=inference_result.source_id,
                frame_id=inference_result.frame_id,
                status=inference_result.status,
            )
            session.add(job)
        else:
            # Keep job row aligned with latest observed terminal result.
            job.source_id = inference_result.source_id
            job.frame_id = inference_result.frame_id
            job.status = inference_result.status

        # Results are append-only rows (many results may reference one job_id).
        # Future improvement: add idempotency/uniqueness policy if retries can
        # produce duplicate logical results.
        result_row = ResultModel(
            job_id=inference_result.job_id,
            source_id=inference_result.source_id,
            frame_id=inference_result.frame_id,
            schema_version=inference_result.schema_version,
            status=inference_result.status,
            model=inference_result.model,
            inference_ms=inference_result.inference_ms,
            pipeline_ms=inference_result.pipeline_ms,
            processed_at_us=inference_result.processed_at_us,
            detections_json=[det.to_dict() for det in inference_result.detections],
        )
        session.add(result_row)
        session.commit()
        return {
            "results_file": None,
            "annotated_path": None,
            "detections_count": len(result_row.detections_json),
            "live_frame_plan": live_frame_plan.to_dict(),
        }
    except Exception:
        # Roll back partial work so source/job/result remain transactionally consistent.
        session.rollback()
        raise
    finally:
        # Always release DB connection back to pool.
        session.close()

def publish_result(
    *,
    config: ServiceConfig,
    inference_result: InferenceResult,
    image: np.ndarray,
) -> dict[str, Any]:
    """
    Publish worker output.

    Current supported modes:
    - local_jsonl: append one JSON object per line and optionally save annotated image.
    - postgres: save results to a PostgreSQL database.
    """
    if config.worker_results_mode == "local_jsonl":
        return publish_to_json(config=config, inference_result=inference_result, image=image)
    elif config.worker_results_mode == "postgres":
        return publish_to_postgres(config=config, inference_result=inference_result, image=image)
    else:
        raise ValueError(f"Unsupported WORKER_RESULTS_MODE: {config.worker_results_mode}")
