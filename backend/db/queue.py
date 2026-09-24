from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError as DatabaseIntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .errors import (
    JobNotFoundError,
    LeaseLostError,
    QueueConflictError,
)
from .models import ReconstructionJob, WorkerLease
from .state_machine import JobStatus, TERMINAL_STATUSES, assert_transition


WORKER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RESOURCE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


@dataclass(frozen=True)
class JobClaim:
    job_id: uuid.UUID
    worker_id: str
    resource_key: str
    lease_token: uuid.UUID
    expires_at: datetime
    status: JobStatus
    attempt: int
    execution_mode: str
    recovered: bool


@dataclass(frozen=True)
class LeaseHeartbeat:
    expires_at: datetime
    cancel_requested: bool


class JobQueue:
    """Transactional task queue using one durable lease row per GPU resource."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def ensure_resource(self, resource_key: str = "gpu:0") -> None:
        _validate_resource_key(resource_key)
        with self.session_factory.begin() as session:
            if session.get(WorkerLease, resource_key) is None:
                session.add(WorkerLease(resource_key=resource_key))

    def enqueue(
        self,
        *,
        job_id: uuid.UUID,
        user_id: uuid.UUID,
        execution_mode: str,
        pipeline_tag: str,
        pipeline_commit: str,
        config_sha256: str,
        input_manifest_sha256: str,
        idempotency_key: str,
        priority: int = 0,
        attempt: int = 1,
    ) -> None:
        if execution_mode not in {"simulated", "real"}:
            raise ValueError("execution_mode must be simulated or real")
        if attempt < 1:
            raise ValueError("attempt must be at least 1")
        for name, digest in (
            ("config_sha256", config_sha256),
            ("input_manifest_sha256", input_manifest_sha256),
            ("idempotency_key", idempotency_key),
        ):
            _validate_sha256(name, digest)
        if not re.fullmatch(r"[a-f0-9]{40}", pipeline_commit):
            raise ValueError("pipeline_commit must be a lowercase 40-character Git SHA")

        try:
            with self.session_factory.begin() as session:
                now = _database_now(session)
                session.add(
                    ReconstructionJob(
                        id=job_id,
                        user_id=user_id,
                        status=JobStatus.QUEUED.value,
                        execution_mode=execution_mode,
                        priority=priority,
                        attempt=attempt,
                        progress=0,
                        pipeline_tag=pipeline_tag,
                        pipeline_commit=pipeline_commit,
                        config_sha256=config_sha256,
                        input_manifest_sha256=input_manifest_sha256,
                        idempotency_key=idempotency_key,
                        queued_at=now,
                        updated_at=now,
                    )
                )
        except DatabaseIntegrityError as exc:
            raise QueueConflictError(
                "job id or idempotency key already exists"
            ) from exc

    def claim_next(
        self,
        *,
        worker_id: str,
        resource_key: str = "gpu:0",
        lease_seconds: int = 60,
    ) -> JobClaim | None:
        _validate_worker_id(worker_id)
        _validate_resource_key(resource_key)
        _validate_lease_seconds(lease_seconds)
        with self.session_factory.begin() as session:
            now = _database_now(session)
            lease = session.execute(
                select(WorkerLease)
                .where(WorkerLease.resource_key == resource_key)
                .with_for_update()
            ).scalar_one_or_none()
            if lease is None:
                raise QueueConflictError(
                    f"worker resource {resource_key!r} has not been initialized"
                )
            if lease.job_id is not None and _is_active(lease, now):
                return None

            recovered_job = self._recover_expired_job(session, lease)
            recovered = recovered_job is not None
            job = recovered_job or session.execute(
                _queued_job_query(session).limit(1)
            ).scalar_one_or_none()
            if job is None:
                _clear_lease(lease, now)
                return None
            if not recovered:
                assert_transition(job.status, JobStatus.PREPARING)
                job.status = JobStatus.PREPARING.value
                job.started_at = job.started_at or now
                job.updated_at = now
                job.version += 1

            token = uuid.uuid4()
            expires_at = now + timedelta(seconds=lease_seconds)
            lease.job_id = job.id
            lease.worker_id = worker_id
            lease.lease_token = token
            lease.leased_at = now
            lease.heartbeat_at = now
            lease.expires_at = expires_at
            lease.updated_at = now
            return JobClaim(
                job_id=job.id,
                worker_id=worker_id,
                resource_key=resource_key,
                lease_token=token,
                expires_at=expires_at,
                status=JobStatus(job.status),
                attempt=job.attempt,
                execution_mode=job.execution_mode,
                recovered=recovered,
            )

    def heartbeat(
        self,
        claim: JobClaim,
        *,
        lease_seconds: int = 60,
    ) -> LeaseHeartbeat:
        _validate_lease_seconds(lease_seconds)
        with self.session_factory.begin() as session:
            now = _database_now(session)
            lease, job = _lock_owned_lease(session, claim, now)
            lease.heartbeat_at = now
            lease.expires_at = now + timedelta(seconds=lease_seconds)
            lease.updated_at = now
            return LeaseHeartbeat(
                expires_at=lease.expires_at,
                cancel_requested=job.cancel_requested,
            )

    def advance(
        self,
        claim: JobClaim,
        target: JobStatus | str,
        *,
        progress: int | None = None,
        error_code: str | None = None,
    ) -> JobStatus:
        target_status = JobStatus(target)
        if progress is not None and not 0 <= progress <= 100:
            raise ValueError("progress must be between 0 and 100")
        if error_code is not None and not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", error_code):
            raise ValueError("error_code must be a stable uppercase code")
        failure_statuses = {
            JobStatus.FAILED_INPUT,
            JobStatus.FAILED_PIPELINE,
            JobStatus.FAILED_EVALUATION,
        }
        if target_status in failure_statuses and error_code is None:
            raise ValueError("failed jobs require an error_code")
        if error_code is not None and target_status not in failure_statuses | {
            JobStatus.CANCELLED
        }:
            raise ValueError("error_code is only valid for failed or cancelled jobs")
        with self.session_factory.begin() as session:
            now = _database_now(session)
            lease, job = _lock_owned_lease(session, claim, now)
            assert_transition(job.status, target_status)
            if progress is not None and progress < job.progress:
                raise ValueError("job progress cannot decrease")
            job.status = target_status.value
            job.progress = progress if progress is not None else job.progress
            job.error_code = error_code
            job.updated_at = now
            job.version += 1
            if target_status in TERMINAL_STATUSES:
                job.finished_at = now
                if target_status is JobStatus.SUCCEEDED:
                    job.progress = 100
                _clear_lease(lease, now)
            return target_status

    def request_cancel(self, job_id: uuid.UUID) -> JobStatus:
        with self.session_factory.begin() as session:
            now = _database_now(session)
            job = session.execute(
                select(ReconstructionJob)
                .where(ReconstructionJob.id == job_id)
                .with_for_update()
            ).scalar_one_or_none()
            if job is None:
                raise JobNotFoundError(f"job {job_id} does not exist")
            status = JobStatus(job.status)
            if status in TERMINAL_STATUSES:
                return status
            if status in {
                JobStatus.DRAFT,
                JobStatus.UPLOADING,
                JobStatus.VALIDATING_INPUT,
                JobStatus.QUEUED,
            }:
                assert_transition(status, JobStatus.CANCELLED)
                job.status = JobStatus.CANCELLED.value
                job.finished_at = now
            else:
                job.cancel_requested = True
            job.updated_at = now
            job.version += 1
            return JobStatus(job.status)

    def get_job(self, job_id: uuid.UUID) -> dict[str, Any]:
        with self.session_factory() as session:
            job = session.get(ReconstructionJob, job_id)
            if job is None:
                raise JobNotFoundError(f"job {job_id} does not exist")
            return {
                "id": job.id,
                "user_id": job.user_id,
                "status": JobStatus(job.status),
                "execution_mode": job.execution_mode,
                "attempt": job.attempt,
                "progress": job.progress,
                "cancel_requested": job.cancel_requested,
                "error_code": job.error_code,
                "version": job.version,
            }

    @staticmethod
    def _recover_expired_job(
        session: Session,
        lease: WorkerLease,
    ) -> ReconstructionJob | None:
        if lease.job_id is None:
            return None
        job = session.execute(
            select(ReconstructionJob)
            .where(ReconstructionJob.id == lease.job_id)
            .with_for_update()
        ).scalar_one_or_none()
        if job is None or JobStatus(job.status) in TERMINAL_STATUSES:
            return None
        return job


def _queued_job_query(session: Session) -> Select[tuple[ReconstructionJob]]:
    query = (
        select(ReconstructionJob)
        .where(
            ReconstructionJob.status == JobStatus.QUEUED.value,
            ReconstructionJob.cancel_requested.is_(False),
        )
        .order_by(
            ReconstructionJob.priority.desc(),
            ReconstructionJob.queued_at,
            ReconstructionJob.created_at,
        )
    )
    return query.with_for_update(
        skip_locked=session.get_bind().dialect.name == "postgresql"
    )


def _lock_owned_lease(
    session: Session,
    claim: JobClaim,
    now: datetime,
) -> tuple[WorkerLease, ReconstructionJob]:
    lease = session.execute(
        select(WorkerLease)
        .where(WorkerLease.resource_key == claim.resource_key)
        .with_for_update()
    ).scalar_one_or_none()
    if (
        lease is None
        or lease.job_id != claim.job_id
        or lease.worker_id != claim.worker_id
        or lease.lease_token != claim.lease_token
        or not _is_active(lease, now)
    ):
        raise LeaseLostError("worker lease is missing, expired, or owned by another worker")
    job = session.execute(
        select(ReconstructionJob)
        .where(ReconstructionJob.id == claim.job_id)
        .with_for_update()
    ).scalar_one_or_none()
    if job is None or JobStatus(job.status) in TERMINAL_STATUSES:
        raise LeaseLostError("leased job is missing or already terminal")
    return lease, job


def _database_now(session: Session) -> datetime:
    value = session.execute(select(func.current_timestamp())).scalar_one()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_active(lease: WorkerLease, now: datetime) -> bool:
    if lease.expires_at is None:
        return False
    expires_at = lease.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at > now


def _clear_lease(lease: WorkerLease, now: datetime) -> None:
    lease.job_id = None
    lease.worker_id = None
    lease.lease_token = None
    lease.leased_at = None
    lease.heartbeat_at = None
    lease.expires_at = None
    lease.updated_at = now


def _validate_worker_id(worker_id: str) -> None:
    if not WORKER_ID_PATTERN.fullmatch(worker_id):
        raise ValueError("worker_id has an invalid format")


def _validate_resource_key(resource_key: str) -> None:
    if not RESOURCE_KEY_PATTERN.fullmatch(resource_key):
        raise ValueError("resource_key has an invalid format")


def _validate_lease_seconds(lease_seconds: int) -> None:
    if not 5 <= lease_seconds <= 3600:
        raise ValueError("lease_seconds must be between 5 and 3600")


def _validate_sha256(name: str, value: str) -> None:
    if not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
