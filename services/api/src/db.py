"""Shared SQLAlchemy database setup for the API service.

This module centralizes:
- Connection URL construction from environment variables.
- Declarative base class used by ORM models.
- Engine and session factory used by request handlers/migrations.
"""

import os
from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

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

class Base(DeclarativeBase):
    """Declarative base for all ORM models in `services/api/src/models.py`."""
    pass


# Engine is process-wide and manages the connection pool.
engine = create_engine(
    build_db_url(),
    pool_pre_ping=True,  # Avoid stale pooled connections after DB/container restarts.
)
# Session factory; each request/unit of work should use its own session instance.
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
