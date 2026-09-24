"""Create reconstruction jobs and single-GPU worker leases.

Revision ID: 0001_job_queue
Revises:
Create Date: 2026-09-24
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0001_job_queue"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


STATUS_VALUES = (
    "'draft', 'uploading', 'validating_input', 'queued', 'preparing', 'sfm', "
    "'dense_reconstruction', 'meshing', 'texturing', 'validating_output', "
    "'evaluating', 'succeeded', 'failed_input', 'failed_pipeline', "
    "'failed_evaluation', 'cancelled', 'expired'"
)


def upgrade() -> None:
    op.create_table(
        "reconstruction_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("execution_mode", sa.String(length=16), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("pipeline_tag", sa.String(length=128), nullable=False),
        sa.Column("pipeline_commit", sa.String(length=40), nullable=False),
        sa.Column("config_sha256", sa.String(length=64), nullable=False),
        sa.Column("input_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(f"status IN ({STATUS_VALUES})", name="ck_jobs_status"),
        sa.CheckConstraint(
            "execution_mode IN ('simulated', 'real')",
            name="ck_jobs_execution_mode",
        ),
        sa.CheckConstraint(
            "progress >= 0 AND progress <= 100",
            name="ck_jobs_progress",
        ),
        sa.CheckConstraint("attempt >= 1", name="ck_jobs_attempt"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_reconstruction_jobs_user_id",
        "reconstruction_jobs",
        ["user_id"],
    )
    op.create_index(
        "ix_jobs_queue_order",
        "reconstruction_jobs",
        ["status", "priority", "queued_at", "created_at"],
    )
    op.create_table(
        "worker_leases",
        sa.Column("resource_key", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("worker_id", sa.String(length=64), nullable=True),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("leased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(job_id IS NULL AND worker_id IS NULL AND lease_token IS NULL "
            "AND leased_at IS NULL AND heartbeat_at IS NULL AND expires_at IS NULL) "
            "OR (job_id IS NOT NULL AND worker_id IS NOT NULL "
            "AND lease_token IS NOT NULL AND leased_at IS NOT NULL "
            "AND heartbeat_at IS NOT NULL AND expires_at IS NOT NULL)",
            name="ck_worker_leases_all_or_none",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["reconstruction_jobs.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("resource_key"),
        sa.UniqueConstraint("job_id", name="uq_worker_leases_job_id"),
        sa.UniqueConstraint("lease_token", name="uq_worker_leases_token"),
    )
    op.execute(
        sa.text("INSERT INTO worker_leases (resource_key) VALUES ('gpu:0')")
    )


def downgrade() -> None:
    op.drop_table("worker_leases")
    op.drop_index("ix_jobs_queue_order", table_name="reconstruction_jobs")
    op.drop_index("ix_reconstruction_jobs_user_id", table_name="reconstruction_jobs")
    op.drop_table("reconstruction_jobs")
