from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
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
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
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
