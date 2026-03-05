"""Shared Prometheus metric helpers and naming conventions."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

DEFAULT_NAMESPACE: Final[str] = "dip"

LATENCY_BUCKETS_MS: Final[tuple[float, ...]] = (
    1.0,
    5.0,
    10.0,
    25.0,
    50.0,
    100.0,
    250.0,
    500.0,
    1000.0,
    2500.0,
    5000.0,
)

RESULT_LABELS: Final[tuple[str, ...]] = ("result",)
STAGE_LABELS: Final[tuple[str, ...]] = ("stage",)
HTTP_ROUTE_LABELS: Final[tuple[str, ...]] = ("route", "method")
HTTP_STATUS_LABELS: Final[tuple[str, ...]] = ("route", "method", "status_code")


class MetricNames:
    """Shared metric names used across platform services."""

    PRODUCER_FRAMES_PUBLISHED_TOTAL = "producer_frames_published_total"
    PRODUCER_FRAMES_DROPPED_TOTAL = "producer_frames_dropped_total"
    PRODUCER_FAILURES_TOTAL = "producer_failures_total"
    PRODUCER_PUBLISH_LATENCY_MS = "producer_publish_latency_ms"

    WORKER_JOBS_POPPED_TOTAL = "worker_jobs_popped_total"
    WORKER_JOBS_PROCESSED_TOTAL = "worker_jobs_processed_total"
    WORKER_FAILURES_TOTAL = "worker_failures_total"
    WORKER_QUEUE_LATENCY_MS = "worker_queue_latency_ms"
    WORKER_INFERENCE_DURATION_MS = "worker_inference_duration_ms"
    WORKER_PIPELINE_DURATION_MS = "worker_pipeline_duration_ms"
    WORKER_QUEUE_DEPTH = "worker_queue_depth"

    API_REQUESTS_TOTAL = "api_requests_total"
    API_REQUEST_DURATION_MS = "api_request_duration_ms"
    API_DEPENDENCY_CHECKS_TOTAL = "api_dependency_checks_total"


_SAFE_LABEL_PATTERN = re.compile(r"[^a-zA-Z0-9_:.-]")


def sanitize_label_value(value: object, *, default: str = "unknown") -> str:
    """Normalize dynamic label values into a bounded, safe string."""

    if value is None:
        return default
    text = str(value).strip()
    if text == "":
        return default
    return _SAFE_LABEL_PATTERN.sub("_", text)


def create_registry() -> CollectorRegistry:
    """Create a service-local metric registry."""

    return CollectorRegistry()


def make_counter(
    name: str,
    documentation: str,
    *,
    labelnames: Sequence[str] = (),
    namespace: str = DEFAULT_NAMESPACE,
    registry: CollectorRegistry | None = None,
) -> Counter:
    kwargs = {
        "name": name,
        "documentation": documentation,
        "labelnames": tuple(labelnames),
        "namespace": namespace,
    }
    if registry is not None:
        kwargs["registry"] = registry
    return Counter(**kwargs)


def make_gauge(
    name: str,
    documentation: str,
    *,
    labelnames: Sequence[str] = (),
    namespace: str = DEFAULT_NAMESPACE,
    registry: CollectorRegistry | None = None,
) -> Gauge:
    kwargs = {
        "name": name,
        "documentation": documentation,
        "labelnames": tuple(labelnames),
        "namespace": namespace,
    }
    if registry is not None:
        kwargs["registry"] = registry
    return Gauge(**kwargs)


def make_histogram(
    name: str,
    documentation: str,
    *,
    labelnames: Sequence[str] = (),
    buckets: Sequence[float] = LATENCY_BUCKETS_MS,
    namespace: str = DEFAULT_NAMESPACE,
    registry: CollectorRegistry | None = None,
) -> Histogram:
    kwargs = {
        "name": name,
        "documentation": documentation,
        "labelnames": tuple(labelnames),
        "buckets": tuple(float(v) for v in buckets),
        "namespace": namespace,
    }
    if registry is not None:
        kwargs["registry"] = registry
    return Histogram(**kwargs)
