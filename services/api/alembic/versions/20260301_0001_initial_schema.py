"""Initial API schema.

Revision ID: 20260301_0001
Revises:
Create Date: 2026-03-01 17:20:00

Creates the first set of API tables:
- sources
- jobs
- results
with indexes and FK constraints matching `services/api/src/models.py`.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260301_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) Sources are the parent entity for jobs/results.
    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_sources_source_id"), "sources", ["source_id"], unique=True)

    # 2) Jobs link to sources and track frame-level processing state.
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.String(length=128), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("frame_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.source_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_jobs_job_id"), "jobs", ["job_id"], unique=True)
    op.create_index(op.f("ix_jobs_source_id"), "jobs", ["source_id"], unique=False)
    op.create_index(op.f("ix_jobs_status"), "jobs", ["status"], unique=False)

    # 3) Results store inference output and reference both job + source.
    op.create_table(
        "results",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.String(length=128), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("frame_id", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("inference_ms", sa.Float(), nullable=False),
        sa.Column("pipeline_ms", sa.Float(), nullable=False),
        sa.Column("processed_at_us", sa.BigInteger(), nullable=False),
        sa.Column("detections_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.job_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.source_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_results_frame_id"), "results", ["frame_id"], unique=False)
    op.create_index(op.f("ix_results_job_id"), "results", ["job_id"], unique=False)
    op.create_index(op.f("ix_results_processed_at_us"), "results", ["processed_at_us"], unique=False)
    op.create_index(op.f("ix_results_source_id"), "results", ["source_id"], unique=False)


def downgrade() -> None:
    # Drop in reverse dependency order: results -> jobs -> sources.
    op.drop_index(op.f("ix_results_source_id"), table_name="results")
    op.drop_index(op.f("ix_results_processed_at_us"), table_name="results")
    op.drop_index(op.f("ix_results_job_id"), table_name="results")
    op.drop_index(op.f("ix_results_frame_id"), table_name="results")
    op.drop_table("results")

    op.drop_index(op.f("ix_jobs_status"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_source_id"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_job_id"), table_name="jobs")
    op.drop_table("jobs")

    op.drop_index(op.f("ix_sources_source_id"), table_name="sources")
    op.drop_table("sources")
