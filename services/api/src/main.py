from __future__ import annotations

import logging

import redis
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from platform_shared.config import load_service_config

app = FastAPI(title="Distributed Inference Platform API", version="0.1.0")
CONFIG = load_service_config(caller_file=__file__)
logging.basicConfig(
    level=CONFIG.log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("API")


@app.get("/")
async def root() -> dict[str, str]:
    logger.debug("Root endpoint called.")
    return {"message": "API online"}


def _check_redis() -> tuple[bool, str]:
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
    logger.debug("Liveness check called.")
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready() -> JSONResponse:
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
