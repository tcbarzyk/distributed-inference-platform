"""Shared SQLAlchemy database utilities.

This module centralizes reusable DB helpers for multiple services:
- Connection URL construction from environment variables.
- Engine factory.
- Session factory.
"""

import os
from urllib.parse import quote_plus
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.session import Session

def build_db_url() -> str:
    """Build a PostgreSQL SQLAlchemy URL from environment variables.

    `quote_plus` protects credentials with special characters so parsing
    remains valid (for example: '@', ':', '/', spaces).
    """
    user = os.getenv("POSTGRES_USER")
    password = os.getenv("POSTGRES_PASSWORD")
    if not user or not password:
        raise ValueError("POSTGRES_USER and POSTGRES_PASSWORD must be set.")

    host = os.getenv("POSTGRES_HOST", "postgres")
    port = os.getenv("POSTGRES_PORT", "5432")
    db_name = os.getenv("POSTGRES_DB", "platform")
    safe_user = quote_plus(user)
    safe_password = quote_plus(password)
    return f"postgresql+psycopg://{safe_user}:{safe_password}@{host}:{port}/{db_name}"


def make_engine(db_url: str | None = None, **engine_kwargs: Any) -> Engine:
    """Create a SQLAlchemy engine for PostgreSQL.

    Services should own their engine lifecycle; keeping this as a function avoids
    import-time side effects in shared code.
    """
    effective_url = db_url or build_db_url()
    return create_engine(
        effective_url,
        pool_pre_ping=True,  # Avoid stale pooled connections after DB/container restarts.
        **engine_kwargs,
    )


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create a session factory bound to the provided engine."""
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)
