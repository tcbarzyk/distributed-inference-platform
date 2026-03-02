"""FastAPI entrypoint for the API service.

Current responsibilities:
- Basic root endpoint.
- Liveness check (`/health/live`) for process health only.
- Readiness check (`/health/ready`) for Redis + Postgres dependency health.
- Read endpoint (`/results`) backed by PostgreSQL.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Generator

import redis
from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
import time

from platform_shared.config import load_service_config
from platform_shared.models import ResultModel, SourceModel

from .contracts import (
    ErrorResponse,
    HealthLiveResponse,
    HealthReadyResponse,
    LatestFrameResponse,
    ResultsListResponse,
    RootResponse,
    SourceDetailResponse,
    SourceEventsResponse,
    SourcesListResponse,
    SourceStatsResponse,
)
from .db import SessionLocal

app = FastAPI(title="Distributed Inference Platform API", version="0.1.0")
CONFIG = load_service_config(caller_file=__file__)
logging.basicConfig(
    level=CONFIG.log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("API")


def get_db() -> Generator[Session, None, None]:
    """Provide one SQLAlchemy session per request and always close it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def result_row_to_dict(row: ResultModel) -> dict[str, Any]:
    """Map `ResultModel` to a response object matching shared InferenceResult fields."""
    return {
        "id": row.id,
        "kind": _result_kind_from_row(row),
        "schema_version": row.schema_version,
        "job_id": row.job_id,
        "frame_id": row.frame_id,
        "source_id": row.source_id,
        "status": row.status,
        "model": row.model,
        "inference_ms": row.inference_ms,
        "pipeline_ms": row.pipeline_ms,
        "processed_at_us": row.processed_at_us,
        "detections": row.detections_json or [],
    }


def source_row_to_dict(
    row: SourceModel,
    *,
    is_active: bool = True,
    last_capture_ts_us: int | None = None,
    last_result_ts_us: int | None = None,
) -> dict[str, Any]:
    """Map `SourceModel` to source summary response fields.

    `last_capture_ts_us` and `last_result_ts_us` are optional derived values that
    can be populated from joins/subqueries when needed.
    """
    return {
        "source_id": row.source_id,
        "display_name": row.name,
        "kind": row.kind,
        "is_active": is_active,
        "last_capture_ts_us": last_capture_ts_us,
        "last_result_ts_us": last_result_ts_us,
    }


def _active_after_us() -> int:
    """Return recency cutoff timestamp (microseconds) for source activity."""
    return int(time.time() * 1_000_000) - (CONFIG.api_source_active_window_seconds * 1_000_000)


def _source_last_result_subquery(db: Session):
    """Build subquery: latest result timestamp per source."""
    return (
        db.query(
            ResultModel.source_id.label("source_id"),
            func.max(ResultModel.processed_at_us).label("last_result_ts_us"),
        )
        .group_by(ResultModel.source_id)
        .subquery()
    )


def _sources_with_activity_query(db: Session, last_result_subq):
    """Base query returning `(SourceModel, last_result_ts_us)` rows."""
    return (
        db.query(SourceModel, last_result_subq.c.last_result_ts_us)
        .outerjoin(last_result_subq, SourceModel.source_id == last_result_subq.c.source_id)
    )


def _is_source_active(last_result_ts_us: int | None, *, active_after_us: int) -> bool:
    """Evaluate source liveness from last result timestamp and recency threshold."""
    return last_result_ts_us is not None and last_result_ts_us >= active_after_us


def _safe_source_id(value: str) -> str:
    """Normalize source id for Redis key segments shared with worker writes."""
    return value.replace("/", "_").replace(":", "_")


def _live_meta_key_for_source(source_id: str) -> str:
    """Build Redis key for latest live-frame metadata for one source."""
    return f"{CONFIG.worker_live_meta_key_prefix}:{_safe_source_id(source_id)}"


def _live_frame_key_for_source(source_id: str) -> str:
    """Build Redis key for latest live-frame JPEG bytes for one source."""
    return f"{CONFIG.worker_live_frame_key_prefix}:{_safe_source_id(source_id)}"


def _get_live_meta_for_source(source_id: str) -> dict[str, Any] | None:
    """Fetch and parse live-frame metadata from Redis for one source.

    Returns `None` when metadata does not exist, is invalid, or Redis is
    unavailable. Callers can then fall back to DB-backed behavior.
    """
    meta_key = _live_meta_key_for_source(source_id)
    try:
        with redis.Redis(
            host=CONFIG.redis_host,
            port=CONFIG.redis_port,
            db=CONFIG.redis_db,
            password=CONFIG.redis_password,
            socket_timeout=1,
            decode_responses=True,
        ) as r:
            raw = r.get(meta_key)
    except redis.RedisError as exc:
        logger.warning("Redis live-meta read failed for key=%s: %s", meta_key, exc)
        return None

    if not raw:
        return None

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("Invalid live-meta JSON for key=%s: %s", meta_key, exc)
        return None

    if not isinstance(parsed, dict):
        logger.warning("Invalid live-meta payload type for key=%s", meta_key)
        return None

    # Ensure frame_key exists for downstream image fetch paths.
    if not parsed.get("frame_key"):
        parsed["frame_key"] = _live_frame_key_for_source(source_id)
    return parsed


def _get_live_frame_bytes_for_key(frame_key: str) -> bytes | None:
    """Fetch latest live-frame bytes from Redis by frame key."""
    try:
        with redis.Redis(
            host=CONFIG.redis_host,
            port=CONFIG.redis_port,
            db=CONFIG.redis_db,
            password=CONFIG.redis_password,
            socket_timeout=1,
            decode_responses=False,
        ) as r:
            raw = r.get(frame_key)
    except redis.RedisError as exc:
        logger.warning("Redis live-frame read failed for key=%s: %s", frame_key, exc)
        return None

    if raw is None or not isinstance(raw, (bytes, bytearray)):
        return None
    return bytes(raw)


def _result_kind_from_row(row: ResultModel) -> str:
    """Derive lightweight result kind from detection payload presence."""
    return "detection" if (row.detections_json or []) else "unknown"


def _validate_kind_filter(kind: str | None) -> str | None:
    """Validate optional kind filter for result-style endpoints."""
    if kind is None:
        return None
    normalized = kind.strip().lower()
    if normalized not in {"detection", "unknown"}:
        raise HTTPException(status_code=400, detail="Unsupported kind filter. Use: detection, unknown")
    return normalized


@app.get(
    "/results",
    response_model=ResultsListResponse,
    responses={400: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
async def list_results(
    source_id: str | None = Query(default=None),
    since_us: int | None = Query(default=None, ge=0),
    until_us: int | None = Query(default=None, ge=0),
    kind: str | None = Query(default=None),
    cursor: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> ResultsListResponse:
    """Return recent inference results with optional source/time filtering."""
    try:
        normalized_kind = _validate_kind_filter(kind)
        q = db.query(ResultModel)
        if source_id:
            q = q.filter(ResultModel.source_id == source_id)
        if since_us is not None:
            q = q.filter(ResultModel.processed_at_us >= since_us)
        if until_us is not None:
            q = q.filter(ResultModel.processed_at_us <= until_us)
        if normalized_kind == "detection":
            q = q.filter(func.json_array_length(ResultModel.detections_json) > 0)
        elif normalized_kind == "unknown":
            q = q.filter(func.json_array_length(ResultModel.detections_json) == 0)
        if cursor is not None:
            # Keyset pagination cursor for newest-first traversal.
            q = q.filter(ResultModel.id < cursor)

        # Newest-first ordering by PK id for stable keyset pagination.
        rows = q.order_by(ResultModel.id.desc()).limit(limit + 1).all()
        page_rows = rows[:limit]
        has_more = len(rows) > limit

        items = [result_row_to_dict(r) for r in page_rows]
        next_cursor = str(page_rows[-1].id) if has_more and page_rows else None
        logger.info(
            "GET /results source_id=%s since_us=%s until_us=%s kind=%s cursor=%s limit=%d count=%d",
            source_id,
            since_us,
            until_us,
            kind,
            cursor,
            limit,
            len(items),
        )
        return ResultsListResponse(count=len(items), items=items, next_cursor=next_cursor)
    except SQLAlchemyError as exc:
        logger.exception("Database query failed for /results: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc


@app.get("/", response_model=RootResponse)
async def root() -> RootResponse:
    logger.debug("Root endpoint called.")
    return RootResponse(message="API online")


def _check_redis() -> tuple[bool, str]:
    """Return `(ok, detail)` for Redis dependency readiness."""
    client = redis.Redis(
        host=CONFIG.redis_host,
        port=CONFIG.redis_port,
        db=CONFIG.redis_db,
        password=CONFIG.redis_password,
        socket_timeout=1,
    )
    try:
        client.ping()
    except redis.RedisError as exc:
        logger.warning("Redis readiness check failed: %s", exc)
        return False, str(exc)
    finally:
        client.close()
    logger.debug("Redis readiness check passed.")
    return True, "ok"


def _check_postgres() -> tuple[bool, str]:
    """Return `(ok, detail)` for Postgres dependency readiness."""
    try:
        from sqlalchemy import text
        from .db import engine
    except Exception as exc:
        logger.warning("Postgres import/readiness bootstrap failed: %s", exc)
        return False, f"db_import_failed: {exc}"

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("Postgres readiness check failed: %s", exc)
        return False, str(exc)
    logger.debug("Postgres readiness check passed.")
    return True, "ok"


@app.get("/health/live", response_model=HealthLiveResponse)
async def health_live() -> HealthLiveResponse:
    # Liveness should stay lightweight and avoid external dependencies.
    logger.debug("Liveness check called.")
    return HealthLiveResponse(status="ok")


@app.get("/health/ready", response_model=HealthReadyResponse)
async def health_ready() -> JSONResponse:
    # Readiness validates required dependencies before routing traffic.
    redis_ok, redis_msg = _check_redis()
    db_ok, db_msg = _check_postgres()
    ready = redis_ok and db_ok
    code = 200 if ready else 503
    logger.info(
        "Readiness check complete: ready=%s redis_ok=%s postgres_ok=%s",
        ready,
        redis_ok,
        db_ok,
    )
    return JSONResponse(
        status_code=code,
        content={
            "status": "ok" if ready else "degraded",
            "checks": {
                "redis": {"ok": redis_ok, "detail": redis_msg},
                "postgres": {"ok": db_ok, "detail": db_msg},
            },
        },
    )


@app.get("/health")
async def health() -> JSONResponse:
    # Backward-compatible alias.
    return await health_ready()


@app.get(
    "/sources",
    response_model=SourcesListResponse,
    responses={503: {"model": ErrorResponse}},
)
async def list_sources(
    active_only: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> SourcesListResponse:
    """List registered sources for the source selector UI."""
    try:
        active_after_us = _active_after_us()
        last_result_subq = _source_last_result_subquery(db)
        q = _sources_with_activity_query(db, last_result_subq)

        if active_only:
            q = q.filter(last_result_subq.c.last_result_ts_us.is_not(None))
            q = q.filter(last_result_subq.c.last_result_ts_us >= active_after_us)
        if cursor:
            # If cursor is provided, filter sources that come after the cursor value.
            q = q.filter(SourceModel.source_id > cursor)
        sources = q.order_by(SourceModel.source_id.asc()).limit(limit + 1).all()
        page_rows = sources[:limit]
        items = [
            source_row_to_dict(
                row=source_row,
                is_active=_is_source_active(last_ts, active_after_us=active_after_us),
                last_result_ts_us=last_ts,
            )
            for source_row, last_ts in page_rows
        ]
        next_cursor = None
        if len(sources) > limit and items:
            next_cursor = items[-1]["source_id"]
        return SourcesListResponse(items=items, next_cursor=next_cursor)
    except SQLAlchemyError as exc:
        logger.exception("Database query failed for /sources: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc


@app.get(
    "/sources/{source_id}",
    response_model=SourceDetailResponse,
    responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
async def get_source(source_id: str, db: Session = Depends(get_db)) -> SourceDetailResponse:
    """Return one source with metadata used by source details UI."""
    try:
        active_after_us = _active_after_us()
        last_result_subq = _source_last_result_subquery(db)
        row = (
            _sources_with_activity_query(db, last_result_subq)
            .filter(SourceModel.source_id == source_id)
            .one_or_none()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Source not found")

        source_row, last_result_ts_us = row
        return SourceDetailResponse(
            source_id=source_row.source_id,
            display_name=source_row.name,
            kind=source_row.kind,
            is_active=_is_source_active(last_result_ts_us, active_after_us=active_after_us),
            created_at=source_row.created_at.isoformat() if source_row.created_at else None,
            last_capture_ts_us=None,
            last_result_ts_us=last_result_ts_us,
            supports_live_stream=source_row.kind in {"webcam", "rtsp"},
        )
    except SQLAlchemyError as exc:
        logger.exception("Database query failed for /sources/%s: %s", source_id, exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc


@app.get(
    "/sources/{source_id}/events",
    response_model=SourceEventsResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
async def list_source_events(
    source_id: str,
    since_us: int | None = Query(default=None, ge=0),
    until_us: int | None = Query(default=None, ge=0),
    kind: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    cursor: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
) -> SourceEventsResponse:
    """Return source-scoped events for timeline/dashboard views."""
    try:
        normalized_kind = _validate_kind_filter(kind)
        # Ensure source exists for a clear 404 on unknown source ids.
        source_exists = (
            db.query(SourceModel.source_id)
            .filter(SourceModel.source_id == source_id)
            .first()
            is not None
        )
        if not source_exists:
            raise HTTPException(status_code=404, detail="Source not found")

        q = db.query(ResultModel).filter(ResultModel.source_id == source_id)
        if since_us is not None:
            q = q.filter(ResultModel.processed_at_us >= since_us)
        if until_us is not None:
            q = q.filter(ResultModel.processed_at_us <= until_us)
        if cursor is not None:
            q = q.filter(ResultModel.id < cursor)
        if normalized_kind == "detection":
            q = q.filter(func.json_array_length(ResultModel.detections_json) > 0)
        elif normalized_kind == "unknown":
            q = q.filter(func.json_array_length(ResultModel.detections_json) == 0)

        rows = q.order_by(ResultModel.id.desc()).limit(limit + 1).all()
        page_rows = rows[:limit]
        has_more = len(rows) > limit
        items = [result_row_to_dict(r) for r in page_rows]
        next_cursor = str(page_rows[-1].id) if has_more and page_rows else None
        return SourceEventsResponse(count=len(items), items=items, next_cursor=next_cursor)
    except SQLAlchemyError as exc:
        logger.exception("Database query failed for /sources/%s/events: %s", source_id, exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc


@app.get(
    "/sources/{source_id}/latest-frame",
    response_model=LatestFrameResponse,
    responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
async def get_latest_frame(source_id: str, db: Session = Depends(get_db)) -> LatestFrameResponse:
    """Return latest frame metadata for live-feed bootstrap render."""
    try:
        source_exists = (
            db.query(SourceModel.source_id)
            .filter(SourceModel.source_id == source_id)
            .first()
            is not None
        )
        if not source_exists:
            raise HTTPException(status_code=404, detail="Source not found")

        live_meta = _get_live_meta_for_source(source_id)
        if live_meta is not None:
            processed_at_us_raw = live_meta.get("processed_at_us")
            capture_ts_us_raw = live_meta.get("capture_ts_us", processed_at_us_raw)
            try:
                processed_at_us = int(processed_at_us_raw) if processed_at_us_raw is not None else None
            except (TypeError, ValueError):
                processed_at_us = None
            try:
                capture_ts_us = int(capture_ts_us_raw) if capture_ts_us_raw is not None else None
            except (TypeError, ValueError):
                capture_ts_us = None

            if capture_ts_us is not None:
                return LatestFrameResponse(
                    source_id=source_id,
                    capture_ts_us=capture_ts_us,
                    processed_at_us=processed_at_us,
                    job_id=(str(live_meta.get("job_id")) if live_meta.get("job_id") is not None else None),
                    result_id=None,
                    annotated_image_url=f"/sources/{source_id}/frame/latest.jpg",
                    frame_key=(str(live_meta.get("frame_key")) if live_meta.get("frame_key") else None),
                )

        row = (
            db.query(ResultModel)
            .filter(ResultModel.source_id == source_id)
            .order_by(ResultModel.id.desc())
            .first()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="No results available for source")

        # v1 fallback: capture timestamp is not yet stored separately in DB.
        return LatestFrameResponse(
            source_id=source_id,
            capture_ts_us=row.processed_at_us,
            processed_at_us=row.processed_at_us,
            job_id=row.job_id,
            result_id=row.id,
            annotated_image_url=None,
            frame_key=None,
        )
    except SQLAlchemyError as exc:
        logger.exception("Database query failed for /sources/%s/latest-frame: %s", source_id, exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc


@app.get(
    "/sources/{source_id}/frame/latest.jpg",
    responses={200: {"content": {"image/jpeg": {}}}, 404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
async def get_latest_frame_image(source_id: str, db: Session = Depends(get_db)) -> Response:
    """Return latest live annotated frame bytes for one source (JPEG)."""
    try:
        source_exists = (
            db.query(SourceModel.source_id)
            .filter(SourceModel.source_id == source_id)
            .first()
            is not None
        )
        if not source_exists:
            raise HTTPException(status_code=404, detail="Source not found")

        live_meta = _get_live_meta_for_source(source_id)
        if live_meta is None:
            raise HTTPException(status_code=404, detail="No live frame available for source")

        frame_key = str(live_meta.get("frame_key") or _live_frame_key_for_source(source_id))
        frame_bytes = _get_live_frame_bytes_for_key(frame_key)
        if frame_bytes is None:
            raise HTTPException(status_code=404, detail="Live frame expired or unavailable")

        content_type = str(live_meta.get("content_type") or "image/jpeg")
        return Response(
            content=frame_bytes,
            media_type=content_type,
            headers={"Cache-Control": "no-store"},
        )
    except SQLAlchemyError as exc:
        logger.exception("Database query failed for /sources/%s/frame/latest.jpg: %s", source_id, exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc


@app.get(
    "/sources/{source_id}/stats",
    response_model=SourceStatsResponse,
    responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
async def get_source_stats(source_id: str, db: Session = Depends(get_db)) -> SourceStatsResponse:
    """Return rolling source stats for KPI cards."""
    try:
        source_exists = (
            db.query(SourceModel.source_id)
            .filter(SourceModel.source_id == source_id)
            .first()
            is not None
        )
        if not source_exists:
            raise HTTPException(status_code=404, detail="Source not found")

        last_result_ts_us = (
            db.query(func.max(ResultModel.processed_at_us))
            .filter(ResultModel.source_id == source_id)
            .scalar()
        )
        if last_result_ts_us is None:
            return SourceStatsResponse(
                source_id=source_id,
                input_fps=None,
                processed_fps=0.0,
                avg_latency_ms=None,
                queue_depth=None,
                last_result_ts_us=None,
            )

        window_s = CONFIG.api_source_active_window_seconds
        since_us = int(time.time() * 1_000_000) - (window_s * 1_000_000)
        recent_count = (
            db.query(func.count(ResultModel.id))
            .filter(ResultModel.source_id == source_id)
            .filter(ResultModel.processed_at_us >= since_us)
            .scalar()
            or 0
        )
        avg_latency_ms = (
            db.query(func.avg(ResultModel.pipeline_ms))
            .filter(ResultModel.source_id == source_id)
            .filter(ResultModel.processed_at_us >= since_us)
            .scalar()
        )
        processed_fps = float(recent_count) / float(window_s) if window_s > 0 else 0.0

        queue_depth: int | None = None
        try:
            with redis.Redis(
                host=CONFIG.redis_host,
                port=CONFIG.redis_port,
                db=CONFIG.redis_db,
                password=CONFIG.redis_password,
                socket_timeout=1,
            ) as r:
                queue_depth = int(r.llen(CONFIG.queue_name))
        except redis.RedisError:
            queue_depth = None

        return SourceStatsResponse(
            source_id=source_id,
            input_fps=None,
            processed_fps=processed_fps,
            avg_latency_ms=float(avg_latency_ms) if avg_latency_ms is not None else None,
            queue_depth=queue_depth,
            last_result_ts_us=int(last_result_ts_us),
        )
    except SQLAlchemyError as exc:
        logger.exception("Database query failed for /sources/%s/stats: %s", source_id, exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc


@app.websocket("/ws/sources/{source_id}")
async def ws_source_events(websocket: WebSocket, source_id: str) -> None:
    """WebSocket contract endpoint reserved for live source updates."""
    await websocket.accept()
    await websocket.send_json(
        {
            "type": "error",
            "source_id": source_id,
            "capture_ts_us": None,
            "payload": {"detail": "Not implemented yet"},
        }
    )
    await websocket.close(code=1013, reason="Not implemented")
