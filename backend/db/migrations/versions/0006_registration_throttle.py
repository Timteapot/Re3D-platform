"""Add registration attempt throttle buckets.

Revision ID: 0006_registration_throttle
Revises: 0005_auth_audit_throttle
Create Date: 2026-09-29
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0006_registration_throttle"
down_revision: str | None = "0005_auth_audit_throttle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_registration_buckets",
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("dimension", sa.String(length=16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "dimension IN ('identity', 'ip')",
            name="ck_auth_registration_dimension",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_auth_registration_attempt_count",
        ),
        sa.PrimaryKeyConstraint("key_hash"),
    )
    op.create_index(
        "ix_auth_registration_blocked_until",
        "auth_registration_buckets",
        ["blocked_until"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_auth_registration_blocked_until",
        table_name="auth_registration_buckets",
    )
    op.drop_table("auth_registration_buckets")
