"""Add per-user task storage reservations.

Revision ID: 0014_user_storage_quota
Revises: 0013_api_ip_rate_limit
Create Date: 2026-10-08
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0014_user_storage_quota"
down_revision: str | None = "0013_api_ip_rate_limit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "reconstruction_jobs",
        sa.Column(
            "storage_reserved_bytes",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "reconstruction_jobs",
        sa.Column(
            "storage_released_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.execute(
        """
        UPDATE reconstruction_jobs AS jobs
        SET storage_reserved_bytes = uploads.total_bytes
        FROM job_uploads AS uploads
        WHERE uploads.id = jobs.id
          AND uploads.status = 'submitted'
        """
    )
    op.execute(
        """
        UPDATE reconstruction_jobs
        SET storage_released_at = CASE
            WHEN storage_cleaned_at IS NOT NULL THEN storage_cleaned_at
            WHEN input_cleaned_at IS NOT NULL
             AND runtime_cleaned_at IS NOT NULL
             AND artifacts_cleaned_at IS NOT NULL
                THEN artifacts_cleaned_at
            ELSE NULL
        END
        """
    )
    op.create_check_constraint(
        "ck_jobs_storage_reserved_bytes",
        "reconstruction_jobs",
        "storage_reserved_bytes >= 0",
    )
    op.create_check_constraint(
        "ck_jobs_storage_release_cleanup",
        "reconstruction_jobs",
        "storage_released_at IS NULL OR storage_cleaned_at IS NOT NULL OR "
        "(input_cleaned_at IS NOT NULL AND runtime_cleaned_at IS NOT NULL "
        "AND artifacts_cleaned_at IS NOT NULL)",
    )
    op.create_index(
        "ix_jobs_user_storage_reservation",
        "reconstruction_jobs",
        ["user_id", "storage_released_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_jobs_user_storage_reservation",
        table_name="reconstruction_jobs",
    )
    op.drop_constraint(
        "ck_jobs_storage_release_cleanup",
        "reconstruction_jobs",
        type_="check",
    )
    op.drop_constraint(
        "ck_jobs_storage_reserved_bytes",
        "reconstruction_jobs",
        type_="check",
    )
    op.drop_column("reconstruction_jobs", "storage_released_at")
    op.drop_column("reconstruction_jobs", "storage_reserved_bytes")
