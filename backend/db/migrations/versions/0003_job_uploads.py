"""Create job upload sessions and uploaded image metadata.

Revision ID: 0003_job_uploads
Revises: 0002_user_auth
Create Date: 2026-09-24
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0003_job_uploads"
down_revision: str | None = "0002_user_auth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job_uploads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("image_count", sa.Integer(), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('uploading', 'submitted', 'cancelled')",
            name="ck_job_uploads_status",
        ),
        sa.CheckConstraint(
            "image_count >= 0 AND image_count <= 150",
            name="ck_job_uploads_image_count",
        ),
        sa.CheckConstraint(
            "total_bytes >= 0",
            name="ck_job_uploads_total_bytes",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_job_uploads_user_status",
        "job_uploads",
        ["user_id", "status", "created_at"],
    )
    op.create_table(
        "job_upload_images",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("upload_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("stored_name", sa.String(length=64), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("sequence >= 0", name="ck_upload_images_sequence"),
        sa.CheckConstraint("size_bytes > 0", name="ck_upload_images_size"),
        sa.CheckConstraint(
            "width > 0 AND height > 0",
            name="ck_upload_images_dimensions",
        ),
        sa.ForeignKeyConstraint(
            ["upload_id"],
            ["job_uploads.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "upload_id",
            "sequence",
            name="uq_upload_images_sequence",
        ),
        sa.UniqueConstraint(
            "upload_id",
            "stored_name",
            name="uq_upload_images_name",
        ),
        sa.UniqueConstraint(
            "upload_id",
            "sha256",
            name="uq_upload_images_sha256",
        ),
    )
    op.create_index(
        "ix_job_upload_images_upload_id",
        "job_upload_images",
        ["upload_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_job_upload_images_upload_id", table_name="job_upload_images")
    op.drop_table("job_upload_images")
    op.drop_index("ix_job_uploads_user_status", table_name="job_uploads")
    op.drop_table("job_uploads")
