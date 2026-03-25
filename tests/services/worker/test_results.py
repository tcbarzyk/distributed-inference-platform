from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from platform_shared.schemas import Detection, InferenceResult


def _load_results_module():
    module_name = "results"
    if module_name in sys.modules:
        return sys.modules[module_name]

    fake_db = types.ModuleType("db")
    fake_db.SessionLocal = lambda: None
    sys.modules["db"] = fake_db

    return importlib.import_module(module_name)


results = _load_results_module()


def _inference_result(frame_id: int = 2) -> InferenceResult:
    return InferenceResult(
        schema_version=1,
        job_id="job-1",
        frame_id=frame_id,
        source_id="cam/1:2",
        status="ok",
        model="yolo",
        inference_ms=3.0,
        pipeline_ms=5.0,
        processed_at_us=123,
        detections=(Detection(label="person", class_id=0, confidence=0.9, bbox_xyxy=(1.0, 2.0, 3.0, 4.0)),),
    )


def test_build_live_frame_publish_plan_respects_cadence_and_keys() -> None:
    config = SimpleNamespace(
        worker_live_frames_enabled=True,
        worker_live_frames_every_n=2,
        worker_live_frame_key_prefix="live:frame:latest",
        worker_live_meta_key_prefix="live:meta:latest",
        worker_live_frame_ttl_seconds=15,
    )
    plan = results.build_live_frame_publish_plan(config=config, inference_result=_inference_result(frame_id=4))
    assert plan.enabled is True
    assert plan.should_publish is True
    assert plan.frame_key == "live:frame:latest:cam_1_2"
    assert plan.meta_key == "live:meta:latest:cam_1_2"
    assert plan.metadata["content_type"] == "image/jpeg"


def test_build_live_frame_publish_plan_skips_when_not_matching_cadence() -> None:
    config = SimpleNamespace(
        worker_live_frames_enabled=True,
        worker_live_frames_every_n=3,
        worker_live_frame_key_prefix="live:frame",
        worker_live_meta_key_prefix="live:meta",
        worker_live_frame_ttl_seconds=15,
    )
    plan = results.build_live_frame_publish_plan(config=config, inference_result=_inference_result(frame_id=2))
    assert plan.should_publish is False
    assert plan.frame_key is None
    assert plan.meta_key is None


def test_publish_result_rejects_unsupported_mode() -> None:
    with pytest.raises(ValueError, match="Unsupported WORKER_RESULTS_MODE"):
        results.publish_result(
            config=SimpleNamespace(worker_results_mode="bad"),
            inference_result=_inference_result(),
            image=np.zeros((2, 2), dtype=np.uint8),
            detection_dicts=[{"label": "person", "class_id": 0, "confidence": 0.9, "bbox_xyxy": [1, 2, 3, 4]}],
        )


def test_publish_to_json_writes_jsonl_record(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = SimpleNamespace(
        worker_results_mode="local_jsonl",
        worker_results_dir=str(tmp_path),
        worker_save_annotated=False,
        worker_annotated_every_n=1,
        worker_live_frames_enabled=False,
        worker_live_frames_every_n=1,
        worker_live_frame_key_prefix="live:frame",
        worker_live_meta_key_prefix="live:meta",
        worker_live_frame_ttl_seconds=5,
    )

    monkeypatch.setattr(results, "_results_file_path", lambda results_dir: results_dir / "results-test.jsonl")
    out = results.publish_to_json(
        config=config,
        inference_result=_inference_result(),
        image=np.zeros((5, 5), dtype=np.uint8),
        detection_dicts=[{"label": "person", "class_id": 0, "confidence": 0.9, "bbox_xyxy": [1, 2, 3, 4]}],
    )
    assert out["annotated_path"] is None
    assert out["detections_count"] == 1

    file_path = Path(out["results_file"])
    lines = file_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["job_id"] == "job-1"
    assert payload["detections"][0]["label"] == "person"
