"""Add audited administrator role changes.

Revision ID: 0012_admin_audit
Revises: 0011_success_retention_runs
Create Date: 2026-10-01
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0012_admin_audit"
down_revision: str | None = "0011_success_retention_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admin_role_change_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_kind", sa.String(length=24), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("target_user_id", sa.Uuid(), nullable=False),
        sa.Column("previous_role", sa.String(length=16), nullable=False),
        sa.Column("new_role", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "actor_kind IN ('maintenance_cli', 'admin_api')",
            name="ck_admin_role_change_events_actor_kind",
        ),
        sa.CheckConstraint(
            "previous_role IN ('user', 'admin')",
            name="ck_admin_role_change_events_previous_role",
        ),
        sa.CheckConstraint(
            "new_role IN ('user', 'admin')",
            name="ck_admin_role_change_events_new_role",
        ),
        sa.CheckConstraint(
            "previous_role <> new_role",
            name="ck_admin_role_change_events_role_changed",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["target_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_admin_role_change_events_target_occurred",
        "admin_role_change_events",
        ["target_user_id", "occurred_at"],
    )
    op.create_index(
        "ix_admin_role_change_events_occurred",
        "admin_role_change_events",
        ["occurred_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_admin_role_change_events_occurred",
        table_name="admin_role_change_events",
    )
    op.drop_index(
        "ix_admin_role_change_events_target_occurred",
        table_name="admin_role_change_events",
    )
    op.drop_table("admin_role_change_events")
