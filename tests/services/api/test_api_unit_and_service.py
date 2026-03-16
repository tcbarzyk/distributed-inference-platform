from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from fastapi import HTTPException


def _load_api_main():
    api_src = Path(__file__).resolve().parents[3] / "services" / "api" / "src"
    package_name = "_test_api_service"

    if package_name not in sys.modules:
        pkg = types.ModuleType(package_name)
        pkg.__path__ = [str(api_src)]
        sys.modules[package_name] = pkg

    db_mod_name = f"{package_name}.db"
    if db_mod_name not in sys.modules:
        fake_db = types.ModuleType(db_mod_name)
        class _DummySession:
            def close(self):
                return None
        fake_db.SessionLocal = lambda: _DummySession()
        fake_db.engine = SimpleNamespace(connect=lambda: None)
        sys.modules[db_mod_name] = fake_db

    return importlib.import_module(f"{package_name}.main")


api_main = _load_api_main()
client = TestClient(api_main.app)


def test_validate_kind_filter_accepts_supported_values() -> None:
    assert api_main._validate_kind_filter(None) is None
    assert api_main._validate_kind_filter(" detection ") == "detection"
    assert api_main._validate_kind_filter("UNKNOWN") == "unknown"


def test_validate_kind_filter_rejects_unknown_kind() -> None:
    with pytest.raises(HTTPException, match="Unsupported kind filter"):
        api_main._validate_kind_filter("segmentation")


def test_root_endpoint_returns_request_id_header() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"message": "API online"}
    assert "X-Request-ID" in response.headers


def test_health_ready_degraded_when_dependency_checks_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_main, "_check_redis", lambda: (False, "redis down"))
    monkeypatch.setattr(api_main, "_check_postgres", lambda: (True, "ok"))
    response = client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["redis"]["ok"] is False
    assert body["checks"]["postgres"]["ok"] is True


def test_results_rejects_invalid_kind_without_db_query() -> None:
    response = client.get("/results", params={"kind": "not-valid"})
    assert response.status_code == 400
    assert "Unsupported kind filter" in response.json()["detail"]


def test_get_live_meta_for_source_sets_default_frame_key(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeRedis:
        def get(self, _key: str):
            return b'{"source_id":"cam-1","processed_at_us":123}'

    monkeypatch.setattr(api_main, "_redis_client", lambda: _FakeRedis())
    result = api_main._get_live_meta_for_source("cam-1")
    assert result is not None
    assert result["source_id"] == "cam-1"
    assert result["frame_key"] == api_main._live_frame_key_for_source("cam-1")


def test_get_live_meta_for_source_returns_none_for_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeRedis:
        def get(self, _key: str):
            return b"{bad json"

    monkeypatch.setattr(api_main, "_redis_client", lambda: _FakeRedis())
    assert api_main._get_live_meta_for_source("cam-1") is None


def test_mjpeg_frame_chunk_format() -> None:
    frame = b"\xff\xd8\xff"
    chunk = api_main._mjpeg_frame_chunk(frame, boundary="frame")
    assert chunk.startswith(b"--frame\r\n")
    assert b"Content-Type: image/jpeg\r\n" in chunk
    assert chunk.endswith(b"\r\n")


def test_iter_mjpeg_stream_stops_when_shutdown_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakePubSub:
        def __init__(self):
            self.message_calls = 0

        def subscribe(self, _channel: str):
            return None

        def get_message(self, timeout: float):
            self.message_calls += 1
            return None

        def unsubscribe(self, _channel: str):
            return None

        def close(self):
            return None

    fake_pubsub = _FakePubSub()
    monkeypatch.setattr(api_main, "_get_live_meta_for_source", lambda _source_id: None)
    monkeypatch.setattr(api_main, "_redis_client", lambda: SimpleNamespace(pubsub=lambda **_kwargs: fake_pubsub))
    monkeypatch.setattr(api_main, "_SHUTDOWN_REQUESTED", True)

    assert list(api_main._iter_mjpeg_stream("cam-1")) == []
    assert fake_pubsub.message_calls == 0
