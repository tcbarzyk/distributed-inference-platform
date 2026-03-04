"""Shared logging helpers for platform services."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any


_RESERVED_LOG_RECORD_FIELDS = set(logging.makeLogRecord({}).__dict__.keys())


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JsonLogFormatter(logging.Formatter):
    """Format stdlib log records as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _utc_timestamp(),
            "level": record.levelname,
            "logger": record.name,
            "service": getattr(record, "service", "unknown"),
            "env": getattr(record, "env", "dev"),
            "event": getattr(record, "event", "log"),
            "msg": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key in _RESERVED_LOG_RECORD_FIELDS:
                continue
            if key in {"service", "env", "event"}:
                continue
            payload[key] = value

        if record.exc_info:
            payload["error.stack"] = self.formatException(record.exc_info)
            exc = record.exc_info[1]
            if exc is not None:
                payload.setdefault("error.type", type(exc).__name__)
                payload.setdefault("error.message", str(exc))

        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


class _ServiceContextFilter(logging.Filter):
    """Attach shared service-level context to each log record."""

    def __init__(self, *, service_name: str, env: str) -> None:
        super().__init__()
        self._service_name = service_name
        self._env = env

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "service"):
            record.service = self._service_name
        if not hasattr(record, "env"):
            record.env = self._env
        return True


def init_json_logging(*, service_name: str, log_level: str = "INFO", env: str | None = None) -> None:
    """Initialize root logging for one service process.

    Uses JSON logs by default and can be switched to plain text with
    `LOG_FORMAT=text`.
    """

    effective_env = (env or os.getenv("APP_ENV") or "dev").strip() or "dev"
    effective_format = (os.getenv("LOG_FORMAT") or "json").strip().lower()
    level = getattr(logging, (log_level or "INFO").upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_ServiceContextFilter(service_name=service_name, env=effective_env))
    if effective_format == "text":
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
        )
    else:
        handler.setFormatter(JsonLogFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(handler)


def get_logger(name: str, **context: Any) -> logging.Logger | logging.LoggerAdapter[logging.Logger]:
    """Return a logger optionally wrapped with static context fields."""

    logger = logging.getLogger(name)
    if not context:
        return logger
    return logging.LoggerAdapter(logger, context)
