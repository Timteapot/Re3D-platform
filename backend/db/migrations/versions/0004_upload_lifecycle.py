"""Add upload cancellation and storage cleanup audit fields.

Revision ID: 0004_upload_lifecycle
Revises: 0003_job_uploads
Create Date: 2026-09-24
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0004_upload_lifecycle"
down_revision: str | None = "0003_job_uploads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "job_uploads",
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "job_uploads",
        sa.Column("cancellation_reason", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "job_uploads",
        sa.Column("storage_cleaned_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE job_uploads "
        "SET cancelled_at = updated_at, cancellation_reason = 'user' "
        "WHERE status = 'cancelled'"
    )
    op.create_check_constraint(
        "ck_job_uploads_cancellation",
        "job_uploads",
        "(status = 'cancelled' AND cancelled_at IS NOT NULL "
        "AND cancellation_reason IN ('user', 'expired')) OR "
        "(status <> 'cancelled' AND cancelled_at IS NULL "
        "AND cancellation_reason IS NULL AND storage_cleaned_at IS NULL)",
    )
    op.create_index(
        "ix_job_uploads_cleanup",
        "job_uploads",
        ["status", "updated_at", "storage_cleaned_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_job_uploads_cleanup", table_name="job_uploads")
    op.drop_constraint(
        "ck_job_uploads_cancellation",
        "job_uploads",
        type_="check",
    )
    op.drop_column("job_uploads", "storage_cleaned_at")
    op.drop_column("job_uploads", "cancellation_reason")
    op.drop_column("job_uploads", "cancelled_at")
