"""Add per-user transfer token buckets and concurrency leases.

Revision ID: 0015_transfer_limits
Revises: 0014_user_storage_quota
Create Date: 2026-10-08
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0015_transfer_limits"
down_revision: str | None = "0014_user_storage_quota"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_transfer_buckets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("capacity_bytes", sa.BigInteger(), nullable=False),
        sa.Column("available_bytes", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "direction IN ('upload', 'download')",
            name="ck_user_transfer_buckets_direction",
        ),
        sa.CheckConstraint(
            "capacity_bytes > 0",
            name="ck_user_transfer_buckets_capacity",
        ),
        sa.CheckConstraint(
            "available_bytes >= 0 AND available_bytes <= capacity_bytes",
            name="ck_user_transfer_buckets_available",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "direction",
            name="uq_user_transfer_buckets_user_direction",
        ),
    )
    op.create_table(
        "user_transfer_leases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("reserved_bytes", sa.BigInteger(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "direction IN ('upload', 'download')",
            name="ck_user_transfer_leases_direction",
        ),
        sa.CheckConstraint(
            "reserved_bytes >= 0",
            name="ck_user_transfer_leases_reserved_bytes",
        ),
        sa.CheckConstraint(
            "expires_at > acquired_at",
            name="ck_user_transfer_leases_expiry",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_user_transfer_leases_active",
        "user_transfer_leases",
        ["user_id", "direction", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_user_transfer_leases_active",
        table_name="user_transfer_leases",
    )
    op.drop_table("user_transfer_leases")
    op.drop_table("user_transfer_buckets")
