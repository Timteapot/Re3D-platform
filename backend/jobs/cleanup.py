from __future__ import annotations

import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.db.models import ReconstructionJob
from backend.db.state_machine import JobStatus
from backend.re3d_adapter.errors import PathBoundaryError
from backend.re3d_adapter.paths import TaskLayout


LOGGER = logging.getLogger(__name__)
CLEANABLE_JOB_STATUSES = frozenset(
    {
        JobStatus.FAILED_INPUT.value,
        JobStatus.FAILED_PIPELINE.value,
        JobStatus.FAILED_EVALUATION.value,
        JobStatus.CANCELLED.value,
    }
)
CleanupResult = Literal["cleaned", "absent", "failed", "skipped"]


@dataclass(frozen=True)
class FailedJobCleanupSettings:
    grace_minutes: int = 5
    interval_seconds: int = 300
    batch_size: int = 100

    def __post_init__(self) -> None:
        if not 0 <= self.grace_minutes <= 24 * 60:
            raise ValueError(
                "RE3D_FAILED_JOB_CLEANUP_GRACE_MINUTES must be between 0 and 1440"
            )
        if not 30 <= self.interval_seconds <= 24 * 60 * 60:
            raise ValueError(
                "RE3D_FAILED_JOB_CLEANUP_INTERVAL_SECONDS must be between 30 and 86400"
            )
        if not 1 <= self.batch_size <= 1000:
            raise ValueError(
                "RE3D_FAILED_JOB_CLEANUP_BATCH_SIZE must be between 1 and 1000"
            )

    @classmethod
    def from_environment(cls) -> "FailedJobCleanupSettings":
        return cls(
            grace_minutes=_read_int(
                "RE3D_FAILED_JOB_CLEANUP_GRACE_MINUTES",
                5,
            ),
            interval_seconds=_read_int(
                "RE3D_FAILED_JOB_CLEANUP_INTERVAL_SECONDS",
                300,
            ),
            batch_size=_read_int(
                "RE3D_FAILED_JOB_CLEANUP_BATCH_SIZE",
                100,
            ),
        )


class FailedJobStorageCleaner:
    """Remove only failed/cancelled task directories and retain database audit."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        data_root: Path,
        settings: FailedJobCleanupSettings | None = None,
    ) -> None:
        self.sessions = sessions
        self.data_root = data_root.expanduser().resolve()
        self.settings = settings or FailedJobCleanupSettings.from_environment()

    def cleanup_terminal_jobs(
        self,
        *,
        now: datetime | None = None,
        grace_minutes: int | None = None,
        limit: int | None = None,
    ) -> dict[str, object]:
        configured_grace = (
            self.settings.grace_minutes
            if grace_minutes is None
            else grace_minutes
        )
        configured_limit = self.settings.batch_size if limit is None else limit
        if not 0 <= configured_grace <= 24 * 60:
            raise ValueError("cleanup grace_minutes must be between 0 and 1440")
        if not 1 <= configured_limit <= 1000:
            raise ValueError("cleanup limit must be between 1 and 1000")
        current = _normalize_time(now or datetime.now(timezone.utc))
        cutoff = current - timedelta(minutes=configured_grace)
        with self.sessions() as session:
            candidate_ids = list(
                session.execute(
                    select(ReconstructionJob.id)
                    .where(
                        ReconstructionJob.status.in_(CLEANABLE_JOB_STATUSES),
                        ReconstructionJob.finished_at.is_not(None),
                        ReconstructionJob.finished_at <= cutoff,
                        ReconstructionJob.storage_cleaned_at.is_(None),
                    )
                    .order_by(
                        ReconstructionJob.finished_at,
                        ReconstructionJob.id,
                    )
                    .limit(configured_limit)
                ).scalars()
            )

        counts = {"cleaned": 0, "absent": 0, "failed": 0, "skipped": 0}
        failures: list[str] = []
        for job_id in candidate_ids:
            result = self.cleanup_job(job_id, now=current)
            counts[result] += 1
            if result == "failed":
                failures.append(str(job_id))
        return {
            "scanned": len(candidate_ids),
            "storage_cleaned": counts["cleaned"],
            "storage_already_absent": counts["absent"],
            "storage_cleanup_failures": failures,
            "skipped": counts["skipped"],
            "cutoff": cutoff.isoformat(),
        }

    def cleanup_job(
        self,
        job_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> CleanupResult:
        current = _normalize_time(now or datetime.now(timezone.utc))
        with self.sessions.begin() as session:
            job = session.execute(
                select(ReconstructionJob)
                .where(ReconstructionJob.id == job_id)
                .with_for_update()
            ).scalar_one_or_none()
            if (
                job is None
                or job.status not in CLEANABLE_JOB_STATUSES
                or job.finished_at is None
                or job.storage_cleaned_at is not None
            ):
                return "skipped"

            job.storage_cleanup_attempts += 1
            job.storage_cleanup_attempted_at = current
            job.updated_at = current
            job.version += 1
            try:
                existed = _remove_job_directory(self.data_root, job_id)
            except OSError:
                job.storage_cleanup_last_error = "TASK_DIRECTORY_REMOVE_FAILED"
                LOGGER.exception(
                    "Could not remove terminal task directory for %s",
                    job_id,
                )
                return "failed"

            job.storage_cleaned_at = current
            job.storage_released_at = current
            job.storage_cleanup_last_error = None
            return "cleaned" if existed else "absent"


def _remove_job_directory(data_root: Path, job_id: uuid.UUID) -> bool:
    resolved_data_root = data_root.expanduser().resolve()
    jobs_root = (resolved_data_root / "jobs").resolve()
    candidate = jobs_root / str(job_id)
    is_symlink = candidate.is_symlink()
    is_junction = getattr(candidate, "is_junction", lambda: False)()
    if is_symlink or is_junction:
        raise OSError("task cleanup target is a link or junction")
    if not candidate.exists():
        return False
    if not candidate.is_dir():
        raise OSError("task cleanup target is not a real directory")
    try:
        layout = TaskLayout.from_data_root(resolved_data_root, str(job_id))
    except PathBoundaryError as exc:
        raise OSError("task cleanup target failed its path boundary") from exc
    if layout.root != candidate:
        raise OSError("task cleanup target resolves through a link or junction")
    shutil.rmtree(layout.root)
    return True


def _normalize_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _read_int(name: str, default: int) -> int:
    configured = os.environ.get(name)
    if configured is None:
        return default
    try:
        return int(configured)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
