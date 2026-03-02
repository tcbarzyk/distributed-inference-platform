"""Result publishing helpers for worker outputs.

This module intentionally keeps publication side effects isolated from the main
consume/inference loop so failures can be handled centrally in worker metrics.
"""

from __future__ import annotations

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
    del config  # kept for signature parity with other publish modes
    del image   # image is not needed for DB mode

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
