"""API contracts used by FastAPI route annotations and OpenAPI generation.

These models intentionally define the external HTTP/WebSocket interface
independently from ORM model classes.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    detail: str


class RootResponse(BaseModel):
    message: str


class HealthLiveResponse(BaseModel):
    status: Literal["ok"]


class DependencyCheck(BaseModel):
    ok: bool
    detail: str


class ReadinessChecks(BaseModel):
    redis: DependencyCheck
    postgres: DependencyCheck


class HealthReadyResponse(BaseModel):
    status: Literal["ok", "degraded"]
    checks: ReadinessChecks


class DetectionResponse(BaseModel):
    label: str
    class_id: int
    confidence: float
    bbox_xyxy: list[float] = Field(min_length=4, max_length=4)


class ResultResponse(BaseModel):
    id: int
    kind: Literal["detection", "unknown"]
    schema_version: int
    job_id: str
    frame_id: int
    source_id: str
    status: str
    model: str
    inference_ms: float
    pipeline_ms: float
    processed_at_us: int
    detections: list[DetectionResponse]


class ResultsListResponse(BaseModel):
    count: int
    items: list[ResultResponse]
    next_cursor: str | None = None


class SourceSummaryResponse(BaseModel):
    source_id: str
    display_name: str
    kind: str
    is_active: bool
    last_capture_ts_us: int | None = None
    last_result_ts_us: int | None = None


class SourcesListResponse(BaseModel):
    items: list[SourceSummaryResponse]
    next_cursor: str | None = None


class SourceDetailResponse(BaseModel):
    source_id: str
    display_name: str
    kind: str
    is_active: bool
    created_at: str | None = None
    last_capture_ts_us: int | None = None
    last_result_ts_us: int | None = None
    supports_live_stream: bool = False


class SourceEventsResponse(BaseModel):
    count: int
    items: list[ResultResponse]
    next_cursor: str | None = None


class LatestFrameResponse(BaseModel):
    source_id: str
    capture_ts_us: int
    processed_at_us: int | None = None
    job_id: str | None = None
    result_id: int | None = None
    annotated_image_url: str | None = None
    frame_key: str | None = None


class SourceStatsResponse(BaseModel):
    source_id: str
    input_fps: float | None = None
    processed_fps: float | None = None
    avg_latency_ms: float | None = None
    queue_depth: int | None = None
    last_result_ts_us: int | None = None


class WsResultPayload(BaseModel):
    job_id: str
    kind: str = "detection"
    annotated_image_url: str | None = None
    metrics: dict[str, float] = Field(default_factory=dict)


class WsMetricPayload(BaseModel):
    metrics: dict[str, float] = Field(default_factory=dict)


class WsHeartbeatPayload(BaseModel):
    message: str = "heartbeat"


class WsErrorPayload(BaseModel):
    detail: str


class WsEventEnvelope(BaseModel):
    type: Literal["result", "metric", "heartbeat", "error"]
    source_id: str
    capture_ts_us: int | None = None
    payload: dict[str, Any]
