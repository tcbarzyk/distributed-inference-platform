# db schema
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from .db import Base

class SourceModel(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # webcam|rtsp|file
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    jobs: Mapped[list["JobModel"]] = relationship(back_populates="source")
    results: Mapped[list["ResultModel"]] = relationship(back_populates="source")

class JobModel(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    source_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("sources.source_id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    frame_id: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False)  # queued|processing|done|failed
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    source: Mapped["SourceModel"] = relationship(back_populates="jobs")
    results: Mapped[list["ResultModel"]] = relationship(back_populates="job")

class ResultModel(Base):
    __tablename__ = "results"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("jobs.job_id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    source_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("sources.source_id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    frame_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    inference_ms: Mapped[float] = mapped_column(Float, nullable=False)
    pipeline_ms: Mapped[float] = mapped_column(Float, nullable=False)
    processed_at_us: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    detections_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    job: Mapped["JobModel"] = relationship(back_populates="results")
    source: Mapped["SourceModel"] = relationship(back_populates="results")
