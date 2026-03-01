from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


def _require_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Field '{key}' must be an integer.")
    return value


def _require_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"Field '{key}' must be a non-empty string.")
    return value


@dataclass(frozen=True)
class QueueJob:
    schema_version: int
    job_id: str
    frame_id: int
    source_id: str
    capture_ts_us: int
    enqueued_at_us: int
    frame_key: str
    attempt: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "frame_id": self.frame_id,
            "source_id": self.source_id,
            "capture_ts_us": self.capture_ts_us,
            "enqueued_at_us": self.enqueued_at_us,
            "frame_key": self.frame_key,
            "attempt": self.attempt,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    def to_json_bytes(self) -> bytes:
        return self.to_json().encode("utf-8")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> QueueJob:
        if not isinstance(payload, dict):
            raise ValueError("QueueJob payload must be a JSON object.")

        attempt = payload.get("attempt", 0)
        if isinstance(attempt, bool) or not isinstance(attempt, int):
            raise ValueError("Field 'attempt' must be an integer.")

        return cls(
            schema_version=_require_int(payload, "schema_version"),
            job_id=_require_str(payload, "job_id"),
            frame_id=_require_int(payload, "frame_id"),
            source_id=_require_str(payload, "source_id"),
            capture_ts_us=_require_int(payload, "capture_ts_us"),
            enqueued_at_us=_require_int(payload, "enqueued_at_us"),
            frame_key=_require_str(payload, "frame_key"),
            attempt=attempt,
        )

    @classmethod
    def from_json(cls, payload: str) -> QueueJob:
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid QueueJob JSON payload.") from exc
        return cls.from_dict(raw)

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> QueueJob:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("QueueJob payload must be valid UTF-8 bytes.") from exc
        return cls.from_json(text)


@dataclass(frozen=True)
class Detection:
    label: str
    class_id: int
    confidence: float
    bbox_xyxy: tuple[float, float, float, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "class_id": self.class_id,
            "confidence": self.confidence,
            "bbox_xyxy": list(self.bbox_xyxy),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Detection:
        if not isinstance(payload, dict):
            raise ValueError("Detection payload must be a JSON object.")

        label = _require_str(payload, "label")
        class_id = _require_int(payload, "class_id")

        confidence_raw = payload.get("confidence")
        if isinstance(confidence_raw, bool) or not isinstance(confidence_raw, (int, float)):
            raise ValueError("Field 'confidence' must be numeric.")
        confidence = float(confidence_raw)

        bbox_raw = payload.get("bbox_xyxy")
        if not isinstance(bbox_raw, (list, tuple)) or len(bbox_raw) != 4:
            raise ValueError("Field 'bbox_xyxy' must contain exactly 4 numbers.")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in bbox_raw):
            raise ValueError("Field 'bbox_xyxy' must contain numeric values only.")
        bbox_xyxy = tuple(float(v) for v in bbox_raw)

        return cls(
            label=label,
            class_id=class_id,
            confidence=confidence,
            bbox_xyxy=(bbox_xyxy[0], bbox_xyxy[1], bbox_xyxy[2], bbox_xyxy[3]),
        )


@dataclass(frozen=True)
class InferenceResult:
    schema_version: int
    job_id: str
    frame_id: int
    source_id: str
    status: str
    model: str
    inference_ms: float
    pipeline_ms: float
    processed_at_us: int
    detections: tuple[Detection, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "frame_id": self.frame_id,
            "source_id": self.source_id,
            "status": self.status,
            "model": self.model,
            "inference_ms": self.inference_ms,
            "pipeline_ms": self.pipeline_ms,
            "processed_at_us": self.processed_at_us,
            "detections": [det.to_dict() for det in self.detections],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    def to_json_bytes(self) -> bytes:
        return self.to_json().encode("utf-8")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> InferenceResult:
        if not isinstance(payload, dict):
            raise ValueError("InferenceResult payload must be a JSON object.")

        inference_ms_raw = payload.get("inference_ms")
        if isinstance(inference_ms_raw, bool) or not isinstance(inference_ms_raw, (int, float)):
            raise ValueError("Field 'inference_ms' must be numeric.")

        pipeline_ms_raw = payload.get("pipeline_ms")
        if isinstance(pipeline_ms_raw, bool) or not isinstance(pipeline_ms_raw, (int, float)):
            raise ValueError("Field 'pipeline_ms' must be numeric.")

        detections_raw = payload.get("detections", [])
        if not isinstance(detections_raw, list):
            raise ValueError("Field 'detections' must be a list.")

        return cls(
            schema_version=_require_int(payload, "schema_version"),
            job_id=_require_str(payload, "job_id"),
            frame_id=_require_int(payload, "frame_id"),
            source_id=_require_str(payload, "source_id"),
            status=_require_str(payload, "status"),
            model=_require_str(payload, "model"),
            inference_ms=float(inference_ms_raw),
            pipeline_ms=float(pipeline_ms_raw),
            processed_at_us=_require_int(payload, "processed_at_us"),
            detections=tuple(Detection.from_dict(item) for item in detections_raw),
        )

    @classmethod
    def from_json(cls, payload: str) -> InferenceResult:
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid InferenceResult JSON payload.") from exc
        return cls.from_dict(raw)

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> InferenceResult:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("InferenceResult payload must be valid UTF-8 bytes.") from exc
        return cls.from_json(text)
