from __future__ import annotations

import pytest

from platform_shared.schemas import Detection, InferenceResult, QueueJob

from contracts import ResultResponse


def test_queue_job_roundtrip_bytes() -> None:
    job = QueueJob(
        schema_version=1,
        job_id="cam-1:10:100",
        frame_id=10,
        source_id="cam-1",
        capture_ts_us=100,
        enqueued_at_us=110,
        frame_key="frame:cam-1:10",
        attempt=2,
    )

    decoded = QueueJob.from_json_bytes(job.to_json_bytes())
    assert decoded == job


def test_queue_job_invalid_utf8_raises() -> None:
    with pytest.raises(ValueError, match="UTF-8"):
        QueueJob.from_json_bytes(b"\xff\xfe\x00")


def test_detection_from_dict_requires_bbox_len_four() -> None:
    with pytest.raises(ValueError, match="exactly 4"):
        Detection.from_dict(
            {
                "label": "person",
                "class_id": 0,
                "confidence": 0.9,
                "bbox_xyxy": [1, 2, 3],
            }
        )


def test_inference_result_requires_numeric_inference_ms() -> None:
    with pytest.raises(ValueError, match="inference_ms"):
        InferenceResult.from_dict(
            {
                "schema_version": 1,
                "job_id": "j1",
                "frame_id": 1,
                "source_id": "cam-1",
                "status": "ok",
                "model": "yolo",
                "inference_ms": "bad",
                "pipeline_ms": 10.0,
                "processed_at_us": 123,
                "detections": [],
            }
        )


def test_shared_result_payload_validates_against_api_contract() -> None:
    result = InferenceResult(
        schema_version=1,
        job_id="cam-1:1:100",
        frame_id=1,
        source_id="cam-1",
        status="ok",
        model="yolov8n",
        inference_ms=4.5,
        pipeline_ms=9.2,
        processed_at_us=123456,
        detections=(
            Detection(label="person", class_id=0, confidence=0.95, bbox_xyxy=(1.0, 2.0, 3.0, 4.0)),
        ),
    )
    payload = {"id": 1, "kind": "detection", **result.to_dict()}
    validated = ResultResponse.model_validate(payload)
    assert validated.kind == "detection"
    assert validated.frame_id == 1
    assert validated.detections[0].bbox_xyxy == [1.0, 2.0, 3.0, 4.0]
