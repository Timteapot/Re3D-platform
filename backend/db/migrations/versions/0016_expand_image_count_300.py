"""Expand upload image count limit from 150 to 300.

Revision ID: 0016_expand_image_count_300
Revises: 0015_transfer_limits
Create Date: 2026-10-12
"""

from collections.abc import Sequence

from alembic import op


revision: str = "0016_expand_image_count_300"
down_revision: str | None = "0015_transfer_limits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_job_uploads_image_count",
        "job_uploads",
        type_="check",
    )
    op.create_check_constraint(
        "ck_job_uploads_image_count",
        "job_uploads",
        "image_count >= 0 AND image_count <= 300",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_job_uploads_image_count",
        "job_uploads",
        type_="check",
    )
    op.create_check_constraint(
        "ck_job_uploads_image_count",
        "job_uploads",
        "image_count >= 0 AND image_count <= 150",
    )
