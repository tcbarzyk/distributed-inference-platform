import os
from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

def build_db_url() -> str:
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
    pass


engine = create_engine(
    build_db_url(),
    pool_pre_ping=True,  # Avoid stale pooled connections after DB/container restarts.
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
