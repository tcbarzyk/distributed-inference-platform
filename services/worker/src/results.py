"""Result publishing helpers for worker outputs."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from platform_shared.config import ServiceConfig
from platform_shared.schemas import InferenceResult


def _ensure_dirs(config: ServiceConfig) -> tuple[Path, Path]:
    results_dir = Path(config.worker_results_dir)
    annotated_dir = results_dir / "annotated"
    results_dir.mkdir(parents=True, exist_ok=True)
    annotated_dir.mkdir(parents=True, exist_ok=True)
    return results_dir, annotated_dir


def _results_file_path(results_dir: Path) -> Path:
    # One results file per day keeps local output manageable.
    day = datetime.now().strftime("%Y%m%d")
    return results_dir / f"results-{day}.jsonl"


def _draw_detections(image: np.ndarray, detections: list[dict[str, Any]]) -> np.ndarray:
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
    raise NotImplementedError("PostgreSQL result persistence not implemented yet.")

def publish_result(
    *,
    config: ServiceConfig,
    inference_result: InferenceResult,
    image: np.ndarray,
) -> dict[str, Any]:
    """
    Publish worker output.

    Current supported mode:
    - local_jsonl: append one JSON object per line and optionally save annotated image.
    - postgres: save results to a PostgreSQL database.
    """
    if config.worker_results_mode == "local_jsonl":
        return publish_to_json(config=config, inference_result=inference_result, image=image)
    elif config.worker_results_mode == "postgres":
        return publish_to_postgres(config=config, inference_result=inference_result, image=image)
    else:
        raise ValueError(f"Unsupported WORKER_RESULTS_MODE: {config.worker_results_mode}")
