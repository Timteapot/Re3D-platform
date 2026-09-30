"""Audit every successful-task retention run.

Revision ID: 0011_success_retention_runs
Revises: 0010_success_storage_retention
Create Date: 2026-09-30
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0011_success_retention_runs"
down_revision: str | None = "0010_success_storage_retention"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "success_retention_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("input_retention_days", sa.Integer(), nullable=True),
        sa.Column("runtime_retention_days", sa.Integer(), nullable=True),
        sa.Column("artifact_retention_days", sa.Integer(), nullable=True),
        sa.Column("batch_size", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("report", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "trigger IN ('manual', 'scheduled')",
            name="ck_success_retention_runs_trigger",
        ),
        sa.CheckConstraint(
            "mode IN ('dry_run', 'execute')",
            name="ck_success_retention_runs_mode",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_success_retention_runs_status",
        ),
        sa.CheckConstraint(
            "input_retention_days IS NULL OR "
            "input_retention_days BETWEEN 1 AND 3650",
            name="ck_success_retention_runs_input_days",
        ),
        sa.CheckConstraint(
            "runtime_retention_days IS NULL OR "
            "runtime_retention_days BETWEEN 1 AND 3650",
            name="ck_success_retention_runs_runtime_days",
        ),
        sa.CheckConstraint(
            "artifact_retention_days IS NULL OR "
            "artifact_retention_days BETWEEN 1 AND 3650",
            name="ck_success_retention_runs_artifact_days",
        ),
        sa.CheckConstraint(
            "batch_size BETWEEN 1 AND 1000",
            name="ck_success_retention_runs_batch_size",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND finished_at IS NULL "
            "AND report IS NULL AND error_code IS NULL) OR "
            "(status = 'succeeded' AND finished_at IS NOT NULL "
            "AND report IS NOT NULL AND error_code IS NULL) OR "
            "(status = 'failed' AND finished_at IS NOT NULL "
            "AND report IS NULL AND error_code IS NOT NULL)",
            name="ck_success_retention_runs_result",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_success_retention_runs_started",
        "success_retention_runs",
        ["started_at", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_success_retention_runs_started",
        table_name="success_retention_runs",
    )
    op.drop_table("success_retention_runs")
