"""Track tiered cleanup of successful task storage.

Revision ID: 0010_success_storage_retention
Revises: 0009_task_submission_policy
Create Date: 2026-09-30
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0010_success_storage_retention"
down_revision: str | None = "0009_task_submission_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for column_name in (
        "input_cleaned_at",
        "runtime_cleaned_at",
        "artifacts_cleaned_at",
        "retention_cleanup_attempted_at",
    ):
        op.add_column(
            "reconstruction_jobs",
            sa.Column(
                column_name,
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
    op.add_column(
        "reconstruction_jobs",
        sa.Column(
            "retention_cleanup_attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "reconstruction_jobs",
        sa.Column(
            "retention_cleanup_last_error",
            sa.String(length=64),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_jobs_retention_cleanup_attempts",
        "reconstruction_jobs",
        "retention_cleanup_attempts >= 0",
    )
    op.create_check_constraint(
        "ck_jobs_input_retention_status",
        "reconstruction_jobs",
        "input_cleaned_at IS NULL OR status IN ('succeeded', 'expired')",
    )
    op.create_check_constraint(
        "ck_jobs_runtime_retention_status",
        "reconstruction_jobs",
        "runtime_cleaned_at IS NULL OR status IN ('succeeded', 'expired')",
    )
    op.create_check_constraint(
        "ck_jobs_artifact_retention_status",
        "reconstruction_jobs",
        "artifacts_cleaned_at IS NULL OR status = 'expired'",
    )
    op.create_index(
        "ix_jobs_success_retention",
        "reconstruction_jobs",
        ["status", "finished_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_jobs_success_retention",
        table_name="reconstruction_jobs",
    )
    op.drop_constraint(
        "ck_jobs_artifact_retention_status",
        "reconstruction_jobs",
        type_="check",
    )
    op.drop_constraint(
        "ck_jobs_runtime_retention_status",
        "reconstruction_jobs",
        type_="check",
    )
    op.drop_constraint(
        "ck_jobs_input_retention_status",
        "reconstruction_jobs",
        type_="check",
    )
    op.drop_constraint(
        "ck_jobs_retention_cleanup_attempts",
        "reconstruction_jobs",
        type_="check",
    )
    op.drop_column("reconstruction_jobs", "retention_cleanup_last_error")
    op.drop_column("reconstruction_jobs", "retention_cleanup_attempts")
    op.drop_column("reconstruction_jobs", "retention_cleanup_attempted_at")
    op.drop_column("reconstruction_jobs", "artifacts_cleaned_at")
    op.drop_column("reconstruction_jobs", "runtime_cleaned_at")
    op.drop_column("reconstruction_jobs", "input_cleaned_at")
