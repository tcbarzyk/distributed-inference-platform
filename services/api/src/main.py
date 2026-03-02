"""FastAPI entrypoint for the API service.

Current responsibilities:
- Basic root endpoint.
- Liveness check (`/health/live`) for process health only.
- Readiness check (`/health/ready`) for Redis + Postgres dependency health.
- Read endpoint (`/results`) backed by PostgreSQL.
"""

from __future__ import annotations

import logging
from typing import Any, Generator

import redis
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from platform_shared.config import load_service_config
from platform_shared.models import ResultModel

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


@app.get("/results")
async def list_results(
    source_id: str | None = Query(default=None),
    since_us: int | None = Query(default=None, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return recent inference results with optional source/time filtering."""
    try:
        q = db.query(ResultModel)
        if source_id:
            q = q.filter(ResultModel.source_id == source_id)
        if since_us is not None:
            q = q.filter(ResultModel.processed_at_us >= since_us)

        # Newest-first ordering makes polling clients consume recent data quickly.
        rows = q.order_by(ResultModel.processed_at_us.desc()).limit(limit).all()

        items = [result_row_to_dict(r) for r in rows]
        logger.info(
            "GET /results source_id=%s since_us=%s limit=%d count=%d",
            source_id,
            since_us,
            limit,
            len(items),
        )
        return {"count": len(items), "items": items}
    except SQLAlchemyError as exc:
        logger.exception("Database query failed for /results: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc


@app.get("/")
async def root() -> dict[str, str]:
    logger.debug("Root endpoint called.")
    return {"message": "API online"}


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


@app.get("/health/live")
async def health_live() -> dict[str, str]:
    # Liveness should stay lightweight and avoid external dependencies.
    logger.debug("Liveness check called.")
    return {"status": "ok"}


@app.get("/health/ready")
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
