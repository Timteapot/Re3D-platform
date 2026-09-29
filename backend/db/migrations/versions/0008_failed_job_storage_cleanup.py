"""Track failed and cancelled job storage cleanup.

Revision ID: 0008_failed_job_storage_cleanup
Revises: 0007_auth_action_tokens
Create Date: 2026-09-30
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0008_failed_job_storage_cleanup"
down_revision: str | None = "0007_auth_action_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "reconstruction_jobs",
        sa.Column(
            "storage_cleaned_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "reconstruction_jobs",
        sa.Column(
            "storage_cleanup_attempted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "reconstruction_jobs",
        sa.Column(
            "storage_cleanup_attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "reconstruction_jobs",
        sa.Column(
            "storage_cleanup_last_error",
            sa.String(length=64),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_jobs_storage_cleanup_attempts",
        "reconstruction_jobs",
        "storage_cleanup_attempts >= 0",
    )
    op.create_check_constraint(
        "ck_jobs_storage_cleanup_terminal",
        "reconstruction_jobs",
        "storage_cleaned_at IS NULL OR "
        "(status IN ('failed_input', 'failed_pipeline', "
        "'failed_evaluation', 'cancelled') "
        "AND storage_cleanup_attempts > 0)",
    )
    op.create_check_constraint(
        "ck_jobs_storage_cleanup_result",
        "reconstruction_jobs",
        "storage_cleaned_at IS NULL OR storage_cleanup_last_error IS NULL",
    )
    op.create_index(
        "ix_jobs_storage_cleanup",
        "reconstruction_jobs",
        ["status", "finished_at", "storage_cleaned_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_jobs_storage_cleanup",
        table_name="reconstruction_jobs",
    )
    op.drop_constraint(
        "ck_jobs_storage_cleanup_result",
        "reconstruction_jobs",
        type_="check",
    )
    op.drop_constraint(
        "ck_jobs_storage_cleanup_terminal",
        "reconstruction_jobs",
        type_="check",
    )
    op.drop_constraint(
        "ck_jobs_storage_cleanup_attempts",
        "reconstruction_jobs",
        type_="check",
    )
    op.drop_column("reconstruction_jobs", "storage_cleanup_last_error")
    op.drop_column("reconstruction_jobs", "storage_cleanup_attempts")
    op.drop_column("reconstruction_jobs", "storage_cleanup_attempted_at")
    op.drop_column("reconstruction_jobs", "storage_cleaned_at")
