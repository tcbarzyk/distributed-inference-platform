from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from platform_shared.schemas import QueueJob


def _load_producer_main():
    module_name = "_test_producer_main"
    if module_name in sys.modules:
        return sys.modules[module_name]
    path = Path(__file__).resolve().parents[3] / "services" / "producer" / "src" / "main.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


producer_main = _load_producer_main()


def test_publish_frame_success_pushes_parseable_job(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeRedis:
        def __init__(self):
            self.set_calls = []
            self.push_calls = []

        def set(self, name, value, ex):
            self.set_calls.append((name, value, ex))

        def lpush(self, name, value):
            self.push_calls.append((name, value))

    fake = _FakeRedis()
    record = producer_main.PublishRecord(frame_id=7, source_id="cam-1", capture_ts_us=100, path="p")

    monkeypatch.setattr(producer_main.cv2, "imencode", lambda *_args, **_kwargs: (True, np.array([1, 2, 3], dtype=np.uint8)))
    monkeypatch.setattr(producer_main.time, "time", lambda: 1.0)

    ok, reason = producer_main._publish_frame(fake, record, np.zeros((2, 2), dtype=np.uint8))
    assert ok is True
    assert reason is None
    assert len(fake.set_calls) == 1
    assert len(fake.push_calls) == 1
    _, payload = fake.push_calls[0]
    job = QueueJob.from_json_bytes(payload)
    assert job.source_id == "cam-1"
    assert job.frame_id == 7


def test_publish_frame_handles_redis_set_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeRedis:
        def set(self, *_args, **_kwargs):
            raise producer_main.redis.RedisError("boom")

    record = producer_main.PublishRecord(frame_id=1, source_id="cam-1", capture_ts_us=1, path="p")
    monkeypatch.setattr(producer_main.cv2, "imencode", lambda *_args, **_kwargs: (True, np.array([1], dtype=np.uint8)))

    ok, reason = producer_main._publish_frame(_FakeRedis(), record, np.zeros((1, 1), dtype=np.uint8))
    assert ok is False
    assert reason == "redis_set_failed"


def test_publish_frame_handles_queue_push_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeRedis:
        def set(self, *_args, **_kwargs):
            return None

        def lpush(self, *_args, **_kwargs):
            raise producer_main.redis.RedisError("boom")

    record = producer_main.PublishRecord(frame_id=1, source_id="cam-1", capture_ts_us=1, path="p")
    monkeypatch.setattr(producer_main.cv2, "imencode", lambda *_args, **_kwargs: (True, np.array([1], dtype=np.uint8)))

    ok, reason = producer_main._publish_frame(_FakeRedis(), record, np.zeros((1, 1), dtype=np.uint8))
    assert ok is False
    assert reason == "queue_push_failed"


def test_run_source_frame_sequence_emits_interval_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"interval": 0}

    def _count_interval(*_args, **_kwargs):
        calls["interval"] += 1

    monkeypatch.setattr(producer_main, "_maybe_log_interval_summary", _count_interval)
    monkeypatch.setattr(producer_main, "_log_summary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(producer_main, "_sleep_for_replay_mode", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(producer_main, "_publish_frame", lambda *_args, **_kwargs: (True, None))
    monkeypatch.setattr(producer_main.cv2, "imread", lambda *_args, **_kwargs: np.zeros((2, 2), dtype=np.uint8))

    frames = [
        producer_main.PublishRecord(frame_id=0, source_id="cam-1", capture_ts_us=10, path="a"),
        producer_main.PublishRecord(frame_id=1, source_id="cam-1", capture_ts_us=20, path="b"),
    ]

    result = producer_main._run_source_frame_sequence(SimpleNamespace(), "cam-1", frames)
    assert result.published_frames == 2
    assert calls["interval"] >= len(frames)


def test_validate_producer_config_rejects_missing_camera_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        producer_main,
        "CONFIG",
        SimpleNamespace(
            producer_source_mode="sample_files",
            producer_sample_root=str(tmp_path),
            producer_camera_dirs=("missing",),
            producer_file_glob="*.bmp",
        ),
    )
    with pytest.raises(ValueError, match="Camera directory does not exist"):
        producer_main.validate_producer_config()
