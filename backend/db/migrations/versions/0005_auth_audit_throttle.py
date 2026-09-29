"""Add authentication audit events and login throttle buckets.

Revision ID: 0005_auth_audit_throttle
Revises: 0004_upload_lifecycle
Create Date: 2026-09-29
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0005_auth_audit_throttle"
down_revision: str | None = "0004_upload_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_throttle_buckets",
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("dimension", sa.String(length=16), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "dimension IN ('account', 'ip')",
            name="ck_auth_throttle_dimension",
        ),
        sa.CheckConstraint(
            "failure_count >= 0",
            name="ck_auth_throttle_failure_count",
        ),
        sa.PrimaryKeyConstraint("key_hash"),
    )
    op.create_index(
        "ix_auth_throttle_blocked_until",
        "auth_throttle_buckets",
        ["blocked_until"],
    )

    op.create_table(
        "auth_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("refresh_session_id", sa.Uuid(), nullable=True),
        sa.Column("identifier_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("client_ip_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("user_agent_sha256", sa.String(length=64), nullable=True),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('register', 'login', 'refresh', 'logout')",
            name="ck_auth_events_action",
        ),
        sa.CheckConstraint(
            "outcome IN ('success', 'failure', 'blocked', 'reuse')",
            name="ck_auth_events_outcome",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_auth_events_action_occurred",
        "auth_events",
        ["action", "occurred_at"],
    )
    op.create_index(
        "ix_auth_events_user_occurred",
        "auth_events",
        ["user_id", "occurred_at"],
    )
    op.create_index(
        "ix_auth_events_identifier_occurred",
        "auth_events",
        ["identifier_fingerprint", "occurred_at"],
    )
    op.create_index(
        "ix_auth_events_ip_occurred",
        "auth_events",
        ["client_ip_fingerprint", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_auth_events_ip_occurred", table_name="auth_events")
    op.drop_index("ix_auth_events_identifier_occurred", table_name="auth_events")
    op.drop_index("ix_auth_events_user_occurred", table_name="auth_events")
    op.drop_index("ix_auth_events_action_occurred", table_name="auth_events")
    op.drop_table("auth_events")
    op.drop_index(
        "ix_auth_throttle_blocked_until",
        table_name="auth_throttle_buckets",
    )
    op.drop_table("auth_throttle_buckets")
