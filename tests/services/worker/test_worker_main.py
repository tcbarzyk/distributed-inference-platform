from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from platform_shared.schemas import QueueJob


def _load_worker_main():
    module_name = "_test_worker_main"
    if module_name in sys.modules:
        return sys.modules[module_name]

    fake_db = types.ModuleType("db")
    fake_db.SessionLocal = lambda: None
    sys.modules["db"] = fake_db

    fake_inference = types.ModuleType("inference")
    fake_inference.InferenceInput = object
    fake_inference.get_model_device = lambda: "cpu"
    fake_inference.load_model = lambda *_args, **_kwargs: object()
    fake_inference.run_inference = lambda *_args, **_kwargs: {}
    sys.modules["inference"] = fake_inference

    path = Path(__file__).resolve().parents[3] / "services" / "worker" / "src" / "main.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


worker_main = _load_worker_main()


def test_decode_job_payload_invalid_marks_failure() -> None:
    metrics = worker_main.WorkerMetrics()
    result = worker_main._decode_job_payload(b"bad-json", metrics)
    assert result is None
    assert metrics.jobs_invalid == 1


def test_build_result_model_validates_required_fields() -> None:
    job = QueueJob(
        schema_version=1,
        job_id="j1",
        frame_id=1,
        source_id="cam-1",
        capture_ts_us=100,
        enqueued_at_us=101,
        frame_key="frame:1",
    )
    with pytest.raises(ValueError, match="status/model"):
        worker_main._build_result_model(job, {"inference_ms": 1.0, "pipeline_ms": 2.0, "detections": []}, 123)

    out = worker_main._build_result_model(
        job,
        {
            "status": "ok",
            "model": "yolo",
            "inference_ms": 1.5,
            "pipeline_ms": 2.5,
            "detections": [{"label": "person", "class_id": 0, "confidence": 0.9, "bbox_xyxy": [1, 2, 3, 4]}],
        },
        123,
    )
    assert out.job_id == "j1"
    assert len(out.detections) == 1


def test_should_publish_mjpeg_for_source_respects_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        worker_main,
        "CONFIG",
        SimpleNamespace(worker_mjpeg_publish_enabled=True, worker_mjpeg_max_fps=2),
    )
    worker_main._LAST_MJPEG_PUBLISH_BY_SOURCE.clear()
    clock = {"t": 100.0}
    monkeypatch.setattr(worker_main.time, "time", lambda: clock["t"])

    assert worker_main._should_publish_mjpeg_for_source("cam-1") is True
    assert worker_main._should_publish_mjpeg_for_source("cam-1") is False
    clock["t"] = 100.6
    assert worker_main._should_publish_mjpeg_for_source("cam-1") is True


def test_publish_mjpeg_safe_skips_when_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        worker_main,
        "CONFIG",
        SimpleNamespace(
            worker_mjpeg_publish_enabled=True,
            worker_live_frames_jpeg_quality=80,
            worker_mjpeg_channel_prefix="live.frames",
        ),
    )
    monkeypatch.setattr(worker_main, "_should_publish_mjpeg_for_source", lambda _sid: False)
    metrics = worker_main.WorkerMetrics()

    worker_main._publish_mjpeg_safe(
        r=SimpleNamespace(publish=lambda *_args, **_kwargs: 0),
        inference_result=SimpleNamespace(source_id="cam-1", frame_id=1, detections=[]),
        image=np.zeros((2, 2), dtype=np.uint8),
        metrics=metrics,
    )
    assert metrics.mjpeg_publish_attempted == 1
    assert metrics.mjpeg_publish_skipped_rate_limit == 1


def test_publish_mjpeg_safe_publishes_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeRedis:
        def __init__(self):
            self.calls = []

        def publish(self, channel: str, payload: bytes):
            self.calls.append((channel, payload))
            return 1

    monkeypatch.setattr(
        worker_main,
        "CONFIG",
        SimpleNamespace(
            worker_mjpeg_publish_enabled=True,
            worker_live_frames_jpeg_quality=80,
            worker_mjpeg_channel_prefix="live.frames",
        ),
    )
    monkeypatch.setattr(worker_main, "_should_publish_mjpeg_for_source", lambda _sid: True)
    monkeypatch.setattr(worker_main, "encode_live_frame_jpeg", lambda **_kwargs: b"jpeg")
    fake_redis = _FakeRedis()
    metrics = worker_main.WorkerMetrics()

    worker_main._publish_mjpeg_safe(
        r=fake_redis,
        inference_result=SimpleNamespace(source_id="cam-1", frame_id=1, detections=[]),
        image=np.zeros((2, 2), dtype=np.uint8),
        metrics=metrics,
    )
    assert metrics.mjpeg_publish_attempted == 1
    assert metrics.mjpeg_publish_sent == 1
    assert fake_redis.calls[0][0] == "live.frames.cam-1"
