"""Inference scaffold for worker-side ML integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import time
import cv2
import numpy as np
from ultralytics import YOLO

_MODEL = None


def load_model() -> YOLO:
    """Load and cache the pretrained model."""
    global _MODEL
    if _MODEL is None:
        _MODEL = YOLO("yolov8n.pt")
    return _MODEL


@dataclass(frozen=True)
class InferenceInput:
    """Normalized payload handed from ingest/decode to inference."""

    frame_id: int | None
    source_id: str | None
    frame_key: str
    capture_ts_us: int | None
    enqueued_at_us: int | None
    received_at_us: int
    image: np.ndarray


def preprocess_image(image: np.ndarray, img_size: int = 640) -> np.ndarray:
    """Convert decoded OpenCV frame into model input format."""
    if image is None:
        raise ValueError("preprocess_image: input image is None")
    # If source is grayscale (H, W), convert to 3-channel BGR.
    if len(image.shape) == 2:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    # If source has 4 channels (BGRA), convert to 3-channel BGR.
    elif len(image.shape) == 3 and image.shape[2] == 4:
        image = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
    image = cv2.resize(image, (img_size, img_size), interpolation=cv2.INTER_LINEAR)
    return image


def infer_image(model: YOLO, preprocessed_image: np.ndarray) -> tuple[Any, float]:
    """
      Run forward pass on one preprocessed image and return:
      - raw model output
      - inference time in milliseconds
    """
    started = time.perf_counter()
    raw_result = model(preprocessed_image, verbose=False)
    inference_ms = (time.perf_counter() - started) * 1000.0
    return raw_result, inference_ms


def postprocess_detections(raw_result: Any, conf_threshold: float = 0.25) -> list[dict[str, Any]]:
    """
    Convert model-specific output into a stable list of detections:
    [
      {
        "label": "person",
        "class_id": 0,
        "confidence": 0.91,
        "bbox_xyxy": [x1, y1, x2, y2]
      },
      ...
    ]
    """
    detections: list[dict[str, Any]] = []

    if not raw_result:
        return detections

    result = raw_result[0]  # Assuming batch size of 1
    boxes = result.boxes
    if boxes is None:
        return detections

    names = result.names or {}
    xyxy_list = boxes.xyxy.cpu().tolist()
    conf_list = boxes.conf.cpu().tolist()
    cls_list = boxes.cls.cpu().tolist()

    for xyxy, conf, cls_id_float in zip(xyxy_list, conf_list, cls_list):
        confidence = float(conf)
        if confidence < conf_threshold:
            continue
        class_id = int(cls_id_float)
        label = names.get(class_id, str(class_id))
        detections.append(
            {
                "label": label,
                "class_id": class_id,
                "confidence": round(confidence, 4),
                "bbox_xyxy": [round(float(v), 2) for v in xyxy],
            }
        )
    return detections


def run_inference(payload: InferenceInput, model: YOLO) -> dict[str, Any]:
    """
    Run preprocess + inference + postprocess for a single frame payload.
    """
    started = time.perf_counter()

    # Preprocess -> infer -> postprocess
    preprocessed_image = preprocess_image(payload.image)
    raw_result, inference_ms = infer_image(model, preprocessed_image)
    detections: list[dict[str, Any]] = postprocess_detections(raw_result)

    pipeline_ms = (time.perf_counter() - started) * 1000.0
    return {
        "status": "ok",
        "model": "yolov8n.pt",
        "frame_id": payload.frame_id,
        "source_id": payload.source_id,
        "detections": detections,
        "inference_ms": inference_ms,
        "pipeline_ms": pipeline_ms,
    }
