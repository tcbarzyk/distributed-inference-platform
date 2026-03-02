"""SQLAlchemy setup for the worker service.

This module centralizes:
- Engine/session creation by reusing shared DB utilities.
"""

from platform_shared.db_utils import make_engine, make_session_factory

# Service-local engine/session objects built from shared utility helpers.
engine = make_engine()
SessionLocal = make_session_factory(engine)
