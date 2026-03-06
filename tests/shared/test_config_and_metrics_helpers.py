from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from prometheus_client import CollectorRegistry, generate_latest

from platform_shared.config import load_service_config
from platform_shared.observability.metrics import (
    make_counter,
    sanitize_label_value,
)


def _worker_main_path() -> str:
    return str(Path(__file__).resolve().parents[2] / "services" / "worker" / "src" / "main.py")


def test_load_service_config_rejects_invalid_metrics_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRODUCER_METRICS_PORT", "70000")
    with pytest.raises(ValueError, match="PRODUCER_METRICS_PORT"):
        load_service_config(caller_file=_worker_main_path())


def test_load_service_config_rejects_invalid_fps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRODUCER_FIXED_FPS", "0")
    with pytest.raises(ValueError, match="PRODUCER_FIXED_FPS"):
        load_service_config(caller_file=_worker_main_path())


def test_make_counter_registers_in_default_registry_when_registry_not_provided() -> None:
    name = f"test_counter_{uuid4().hex}"
    counter = make_counter(name, "test counter registration")
    counter.inc()
    text = generate_latest().decode("utf-8")
    assert f"dip_{name}_total" in text


def test_make_counter_can_use_custom_registry() -> None:
    name = f"test_counter_custom_{uuid4().hex}"
    reg = CollectorRegistry()
    counter = make_counter(name, "test counter custom registry", registry=reg)
    counter.inc()
    text = generate_latest(reg).decode("utf-8")
    assert f"dip_{name}_total" in text


def test_sanitize_label_value_normalizes_invalid_chars() -> None:
    assert sanitize_label_value("GET /results?x=1") == "GET__results_x_1"
    assert sanitize_label_value(None) == "unknown"
    assert sanitize_label_value("   ") == "unknown"
