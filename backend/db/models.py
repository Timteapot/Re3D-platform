from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .state_machine import JobStatus


class Base(DeclarativeBase):
    pass


STATUS_VALUES = ", ".join(f"'{status.value}'" for status in JobStatus)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'admin')", name="ck_users_role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    username: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    email_verified: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    jobs: Mapped[list[ReconstructionJob]] = relationship(back_populates="user")
    refresh_sessions: Mapped[list[RefreshSession]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    uploads: Mapped[list[JobUpload]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )


class RefreshSession(Base):
    __tablename__ = "refresh_sessions"
    __table_args__ = (
        Index("ix_refresh_sessions_user_expires", "user_id", "expires_at"),
        Index("ix_refresh_sessions_family_id", "family_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    family_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    token_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("refresh_sessions.id", ondelete="SET NULL"),
    )

    user: Mapped[User] = relationship(back_populates="refresh_sessions")


class JobUpload(Base):
    __tablename__ = "job_uploads"
    __table_args__ = (
        CheckConstraint(
            "status IN ('uploading', 'submitted', 'cancelled')",
            name="ck_job_uploads_status",
        ),
        CheckConstraint(
            "image_count >= 0 AND image_count <= 150",
            name="ck_job_uploads_image_count",
        ),
        CheckConstraint("total_bytes >= 0", name="ck_job_uploads_total_bytes"),
        Index("ix_job_uploads_user_status", "user_id", "status", "created_at"),
        Index(
            "ix_job_uploads_cleanup",
            "status",
            "updated_at",
            "storage_cleaned_at",
        ),
        CheckConstraint(
            "(status = 'cancelled' AND cancelled_at IS NOT NULL "
            "AND cancellation_reason IN ('user', 'expired')) OR "
            "(status <> 'cancelled' AND cancelled_at IS NULL "
            "AND cancellation_reason IS NULL AND storage_cleaned_at IS NULL)",
            name="ck_job_uploads_cancellation",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
    )
    image_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_reason: Mapped[str | None] = mapped_column(String(16))
    storage_cleaned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    user: Mapped[User] = relationship(back_populates="uploads")
    images: Mapped[list[JobUploadImage]] = relationship(
        back_populates="upload",
        cascade="all, delete-orphan",
        order_by="JobUploadImage.sequence",
    )


class JobUploadImage(Base):
    __tablename__ = "job_upload_images"
    __table_args__ = (
        UniqueConstraint("upload_id", "sequence", name="uq_upload_images_sequence"),
        UniqueConstraint("upload_id", "stored_name", name="uq_upload_images_name"),
        UniqueConstraint("upload_id", "sha256", name="uq_upload_images_sha256"),
        CheckConstraint("sequence >= 0", name="ck_upload_images_sequence"),
        CheckConstraint("size_bytes > 0", name="ck_upload_images_size"),
        CheckConstraint(
            "width > 0 AND height > 0",
            name="ck_upload_images_dimensions",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    upload_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("job_uploads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    stored_name: Mapped[str] = mapped_column(String(64), nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    upload: Mapped[JobUpload] = relationship(back_populates="images")


class ReconstructionJob(Base):
    __tablename__ = "reconstruction_jobs"
    __table_args__ = (
        CheckConstraint(f"status IN ({STATUS_VALUES})", name="ck_jobs_status"),
        CheckConstraint(
            "execution_mode IN ('simulated', 'real')",
            name="ck_jobs_execution_mode",
        ),
        CheckConstraint("progress >= 0 AND progress <= 100", name="ck_jobs_progress"),
        CheckConstraint("attempt >= 1", name="ck_jobs_attempt"),
        Index(
            "ix_jobs_queue_order",
            "status",
            "priority",
            "queued_at",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=JobStatus.DRAFT.value
    )
    execution_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    pipeline_tag: Mapped[str] = mapped_column(String(128), nullable=False)
    pipeline_commit: Mapped[str] = mapped_column(String(40), nullable=False)
    config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    input_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    user: Mapped[User] = relationship(back_populates="jobs")
    lease: Mapped[WorkerLease | None] = relationship(back_populates="job")


class WorkerLease(Base):
    __tablename__ = "worker_leases"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_worker_leases_job_id"),
        UniqueConstraint("lease_token", name="uq_worker_leases_token"),
        CheckConstraint(
            "(job_id IS NULL AND worker_id IS NULL AND lease_token IS NULL "
            "AND leased_at IS NULL AND heartbeat_at IS NULL AND expires_at IS NULL) "
            "OR (job_id IS NOT NULL AND worker_id IS NOT NULL "
            "AND lease_token IS NOT NULL AND leased_at IS NOT NULL "
            "AND heartbeat_at IS NOT NULL AND expires_at IS NOT NULL)",
            name="ck_worker_leases_all_or_none",
        ),
    )

    resource_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("reconstruction_jobs.id", ondelete="RESTRICT"),
    )
    worker_id: Mapped[str | None] = mapped_column(String(64))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    leased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    job: Mapped[ReconstructionJob | None] = relationship(back_populates="lease")
