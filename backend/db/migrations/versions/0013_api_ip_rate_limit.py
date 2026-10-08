"""Add shared ordinary API IP rate-limit buckets.

Revision ID: 0013_api_ip_rate_limit
Revises: 0012_admin_audit
Create Date: 2026-10-08
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0013_api_ip_rate_limit"
down_revision: str | None = "0012_admin_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_rate_limit_buckets",
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column(
            "window_started_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "request_count >= 0",
            name="ck_api_rate_limit_request_count",
        ),
        sa.PrimaryKeyConstraint("key_hash"),
    )
    op.create_index(
        "ix_api_rate_limit_updated_at",
        "api_rate_limit_buckets",
        ["updated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_api_rate_limit_updated_at",
        table_name="api_rate_limit_buckets",
    )
    op.drop_table("api_rate_limit_buckets")
