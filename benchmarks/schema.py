"""Versioned benchmark result schema and helpers."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

BENCHMARK_RUN_SCHEMA_VERSION = 1

PROFILE_CHOICES = (
    "baseline",
    "worker-step",
    "api-step",
    "soak",
    "stress-destructive",
)

RUN_STATUS_CHOICES = (
    "planned",
    "running",
    "completed",
    "failed",
    "aborted",
)

CLASSIFICATION_CHOICES = (
    "unknown",
    "stable",
    "degrading",
    "saturated",
    "invalid",
)


def utc_now_iso() -> str:
    """Return the current UTC timestamp as an ISO 8601 string."""
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass(slots=True)
class GitMetadata:
    """Repository state captured at benchmark time."""

    commit: str | None = None
    branch: str | None = None
    dirty: bool | None = None


@dataclass(slots=True)
class EnvironmentMetadata:
    """Host and runtime metadata for a benchmark run."""

    hostname: str | None = None
    platform: str | None = None
    python_version: str | None = None
    cwd: str | None = None


@dataclass(slots=True)
class Thresholds:
    """Optional run thresholds used to classify the result."""

    max_failure_rate: float | None = None
    max_queue_depth_growth: float | None = None
    max_latency_p95_ms: float | None = None
    max_latency_p99_ms: float | None = None


@dataclass(slots=True)
class Summary:
    """Top-level human-friendly summary of a benchmark run."""

    duration_s: float | None = None
    classification: str = "unknown"
    success: bool | None = None
    headline: str | None = None
    primary_metrics: dict[str, float | int | str | None] = field(default_factory=dict)


@dataclass(slots=True)
class Artifacts:
    """References to output files associated with a run."""

    raw_outputs: list[str] = field(default_factory=list)
    related_audits: list[str] = field(default_factory=list)
    notes_path: str | None = None


@dataclass(slots=True)
class BenchmarkRun:
    """Canonical benchmark run document."""

    schema_version: int
    run_id: str
    profile: str
    status: str
    started_at_utc: str
    finished_at_utc: str | None = None
    label: str | None = None
    git: GitMetadata = field(default_factory=GitMetadata)
    environment: EnvironmentMetadata = field(default_factory=EnvironmentMetadata)
    inputs: dict[str, Any] = field(default_factory=dict)
    thresholds: Thresholds = field(default_factory=Thresholds)
    summary: Summary = field(default_factory=Summary)
    measurements: dict[str, Any] = field(default_factory=dict)
    verdicts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    artifacts: Artifacts = field(default_factory=Artifacts)
    comparisons: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def planned(
        cls,
        *,
        profile: str,
        label: str | None = None,
        inputs: Mapping[str, Any] | None = None,
        thresholds: Thresholds | None = None,
        git: GitMetadata | None = None,
        environment: EnvironmentMetadata | None = None,
        notes: list[str] | None = None,
    ) -> "BenchmarkRun":
        """Return a skeleton benchmark run document."""
        return cls(
            schema_version=BENCHMARK_RUN_SCHEMA_VERSION,
            run_id=f"bench_{uuid4().hex[:12]}",
            profile=profile,
            status="planned",
            started_at_utc=utc_now_iso(),
            label=label,
            git=git or GitMetadata(),
            environment=environment or EnvironmentMetadata(),
            inputs=dict(inputs or {}),
            thresholds=thresholds or Thresholds(),
            summary=Summary(classification="unknown"),
            notes=list(notes or []),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict."""
        return _to_dict(self)

    def write_json(self, path: Path) -> None:
        """Write this run document to disk."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")


def _to_dict(value: Any) -> Any:
    """Recursively convert dataclasses into plain Python containers."""
    if is_dataclass(value):
        return {k: _to_dict(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _to_dict(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_dict(v) for v in value]
    return value


def validate_run_document(doc: Mapping[str, Any]) -> list[str]:
    """Return a list of schema validation errors."""
    errors: list[str] = []

    def require_type(key: str, expected_type: type | tuple[type, ...]) -> None:
        value = doc.get(key)
        if not isinstance(value, expected_type):
            errors.append(f"'{key}' must be of type {expected_type}, got {type(value).__name__}")

    require_type("schema_version", int)
    require_type("run_id", str)
    require_type("profile", str)
    require_type("status", str)
    require_type("started_at_utc", str)
    require_type("git", dict)
    require_type("environment", dict)
    require_type("inputs", dict)
    require_type("thresholds", dict)
    require_type("summary", dict)
    require_type("measurements", dict)
    require_type("verdicts", list)
    require_type("errors", list)
    require_type("notes", list)
    require_type("artifacts", dict)
    require_type("comparisons", dict)

    profile = doc.get("profile")
    if isinstance(profile, str) and profile not in PROFILE_CHOICES:
        errors.append(f"'profile' must be one of {PROFILE_CHOICES}, got {profile!r}")

    status = doc.get("status")
    if isinstance(status, str) and status not in RUN_STATUS_CHOICES:
        errors.append(f"'status' must be one of {RUN_STATUS_CHOICES}, got {status!r}")

    if doc.get("schema_version") != BENCHMARK_RUN_SCHEMA_VERSION:
        errors.append(
            f"'schema_version' must equal {BENCHMARK_RUN_SCHEMA_VERSION}, got {doc.get('schema_version')!r}"
        )

    summary = doc.get("summary")
    if isinstance(summary, dict):
        classification = summary.get("classification")
        if classification is not None and classification not in CLASSIFICATION_CHOICES:
            errors.append(
                f"'summary.classification' must be one of {CLASSIFICATION_CHOICES}, got {classification!r}"
            )
        primary_metrics = summary.get("primary_metrics")
        if not isinstance(primary_metrics, dict):
            errors.append("'summary.primary_metrics' must be an object/dict")

    for list_key in ("verdicts", "errors", "notes"):
        items = doc.get(list_key)
        if isinstance(items, list) and not all(isinstance(item, str) for item in items):
            errors.append(f"'{list_key}' must contain only strings")

    return errors
