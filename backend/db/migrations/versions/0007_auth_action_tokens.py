"""Add email verification and password reset token state.

Revision ID: 0007_auth_action_tokens
Revises: 0006_registration_throttle
Create Date: 2026-09-29
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0007_auth_action_tokens"
down_revision: str | None = "0006_registration_throttle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_auth_events_action", "auth_events", type_="check")
    op.alter_column(
        "auth_events",
        "action",
        existing_type=sa.String(length=16),
        type_=sa.String(length=24),
        existing_nullable=False,
    )
    op.create_check_constraint(
        "ck_auth_events_action",
        "auth_events",
        "action IN ('register', 'login', 'refresh', 'logout', "
        "'email_verification', 'password_reset')",
    )

    op.create_table(
        "auth_action_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(length=24), nullable=False),
        sa.Column("token_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "purpose IN ('email_verification', 'password_reset')",
            name="ck_auth_action_tokens_purpose",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_sha256"),
    )
    op.create_index(
        "ix_auth_action_tokens_user_purpose",
        "auth_action_tokens",
        ["user_id", "purpose", "created_at"],
    )
    op.create_index(
        "ix_auth_action_tokens_expires",
        "auth_action_tokens",
        ["expires_at"],
    )

    op.create_table(
        "auth_action_request_buckets",
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("purpose", sa.String(length=24), nullable=False),
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
            "purpose IN ('email_verification', 'password_reset')",
            name="ck_auth_action_request_purpose",
        ),
        sa.CheckConstraint(
            "dimension IN ('identity', 'ip')",
            name="ck_auth_action_request_dimension",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_auth_action_request_attempt_count",
        ),
        sa.PrimaryKeyConstraint("key_hash"),
    )
    op.create_index(
        "ix_auth_action_request_blocked_until",
        "auth_action_request_buckets",
        ["blocked_until"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_auth_action_request_blocked_until",
        table_name="auth_action_request_buckets",
    )
    op.drop_table("auth_action_request_buckets")
    op.drop_index("ix_auth_action_tokens_expires", table_name="auth_action_tokens")
    op.drop_index(
        "ix_auth_action_tokens_user_purpose",
        table_name="auth_action_tokens",
    )
    op.drop_table("auth_action_tokens")

    op.execute(
        "DELETE FROM auth_events WHERE action IN "
        "('email_verification', 'password_reset')"
    )
    op.drop_constraint("ck_auth_events_action", "auth_events", type_="check")
    op.alter_column(
        "auth_events",
        "action",
        existing_type=sa.String(length=24),
        type_=sa.String(length=16),
        existing_nullable=False,
    )
    op.create_check_constraint(
        "ck_auth_events_action",
        "auth_events",
        "action IN ('register', 'login', 'refresh', 'logout')",
    )
