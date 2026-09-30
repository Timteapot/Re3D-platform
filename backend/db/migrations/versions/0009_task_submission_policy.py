"""Add task action audit events for submission policy enforcement.

Revision ID: 0009_task_submission_policy
Revises: 0008_failed_job_storage_cleanup
Create Date: 2026-09-30
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0009_task_submission_policy"
down_revision: str | None = "0008_failed_job_storage_cleanup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_action_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("execution_mode", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('job_submitted', 'cancel_requested')",
            name="ck_task_action_events_action",
        ),
        sa.CheckConstraint(
            "outcome IN ('success', 'blocked', 'reuse')",
            name="ck_task_action_events_outcome",
        ),
        sa.CheckConstraint(
            "execution_mode IN ('simulated', 'real')",
            name="ck_task_action_events_execution_mode",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_task_action_events_user_occurred",
        "task_action_events",
        ["user_id", "occurred_at"],
    )
    op.create_index(
        "ix_task_action_events_job_occurred",
        "task_action_events",
        ["job_id", "occurred_at"],
    )
    op.create_index(
        "ix_task_action_events_action_occurred",
        "task_action_events",
        ["action", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_task_action_events_action_occurred",
        table_name="task_action_events",
    )
    op.drop_index(
        "ix_task_action_events_job_occurred",
        table_name="task_action_events",
    )
    op.drop_index(
        "ix_task_action_events_user_occurred",
        table_name="task_action_events",
    )
    op.drop_table("task_action_events")
