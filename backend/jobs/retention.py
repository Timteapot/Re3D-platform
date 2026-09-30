from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.db.models import ReconstructionJob, SuccessRetentionRun
from backend.db.state_machine import JobStatus, assert_transition
from backend.re3d_adapter.errors import PathBoundaryError
from backend.re3d_adapter.paths import TaskLayout


RetentionTier = Literal["input", "runtime", "artifacts"]
RetentionCleanupResult = Literal["cleaned", "absent", "failed", "skipped"]
SUCCESS_RETENTION_STATUSES = (
    JobStatus.SUCCEEDED.value,
    JobStatus.EXPIRED.value,
)
TIER_PATHS: dict[RetentionTier, tuple[str, ...]] = {
    "input": ("input/staging", "input/images"),
    "runtime": ("runtime",),
    "artifacts": ("output",),
}
TIER_CLEANED_FIELDS: dict[RetentionTier, str] = {
    "input": "input_cleaned_at",
    "runtime": "runtime_cleaned_at",
    "artifacts": "artifacts_cleaned_at",
}
TIER_ERROR_CODES: dict[RetentionTier, str] = {
    "input": "SUCCESS_INPUT_REMOVE_FAILED",
    "runtime": "SUCCESS_RUNTIME_REMOVE_FAILED",
    "artifacts": "SUCCESS_ARTIFACT_REMOVE_FAILED",
}


@dataclass(frozen=True)
class SuccessRetentionSettings:
    """Optional per-tier retention; an unset tier is never selected."""

    input_retention_days: int | None = None
    runtime_retention_days: int | None = None
    artifact_retention_days: int | None = None
    cleanup_batch_size: int = 50
    scheduled_dry_run_enabled: bool = False
    scheduled_dry_run_interval_seconds: int = 86_400

    def __post_init__(self) -> None:
        for name, value in (
            ("SUCCESS_INPUT_RETENTION_DAYS", self.input_retention_days),
            ("SUCCESS_RUNTIME_RETENTION_DAYS", self.runtime_retention_days),
            ("SUCCESS_ARTIFACT_RETENTION_DAYS", self.artifact_retention_days),
        ):
            if value is not None and not 1 <= value <= 3650:
                raise ValueError(f"{name} must be between 1 and 3650")
        if not 1 <= self.cleanup_batch_size <= 1000:
            raise ValueError(
                "SUCCESS_RETENTION_CLEANUP_BATCH_SIZE must be between 1 and 1000"
            )
        if not 3600 <= self.scheduled_dry_run_interval_seconds <= 7 * 86_400:
            raise ValueError(
                "SUCCESS_RETENTION_DRY_RUN_INTERVAL_SECONDS must be between "
                "3600 and 604800"
            )
        if self.scheduled_dry_run_enabled and all(
            value is None
            for value in (
                self.input_retention_days,
                self.runtime_retention_days,
                self.artifact_retention_days,
            )
        ):
            raise ValueError(
                "scheduled retention dry-run requires at least one retention tier"
            )

    @classmethod
    def from_environment(cls) -> "SuccessRetentionSettings":
        return cls(
            input_retention_days=_optional_environment_integer(
                "SUCCESS_INPUT_RETENTION_DAYS"
            ),
            runtime_retention_days=_optional_environment_integer(
                "SUCCESS_RUNTIME_RETENTION_DAYS"
            ),
            artifact_retention_days=_optional_environment_integer(
                "SUCCESS_ARTIFACT_RETENTION_DAYS"
            ),
            cleanup_batch_size=_environment_integer(
                "SUCCESS_RETENTION_CLEANUP_BATCH_SIZE",
                50,
            ),
            scheduled_dry_run_enabled=_environment_boolean(
                "SUCCESS_RETENTION_DRY_RUN_ENABLED",
                False,
            ),
            scheduled_dry_run_interval_seconds=_environment_integer(
                "SUCCESS_RETENTION_DRY_RUN_INTERVAL_SECONDS",
                86_400,
            ),
        )

    def retention_days(self, tier: RetentionTier) -> int | None:
        return {
            "input": self.input_retention_days,
            "runtime": self.runtime_retention_days,
            "artifacts": self.artifact_retention_days,
        }[tier]


class SuccessJobStorageCleaner:
    """Dry-run or remove successful job data in independently audited tiers."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        data_root: Path,
        settings: SuccessRetentionSettings | None = None,
    ) -> None:
        self.sessions = sessions
        self.data_root = data_root.expanduser().resolve()
        self.settings = settings or SuccessRetentionSettings.from_environment()

    def cleanup_due_jobs(
        self,
        *,
        execute: bool = False,
        now: datetime | None = None,
        limit: int | None = None,
    ) -> dict[str, object]:
        current = _normalize_time(now or datetime.now(timezone.utc))
        configured_limit = self.settings.cleanup_batch_size if limit is None else limit
        if not 1 <= configured_limit <= 1000:
            raise ValueError("cleanup limit must be between 1 and 1000")

        tier_reports: dict[str, dict[str, object]] = {}
        for tier in ("input", "runtime", "artifacts"):
            retention_days = self.settings.retention_days(tier)
            if retention_days is None:
                tier_reports[tier] = {
                    "enabled": False,
                    "retention_days": None,
                    "candidates": 0,
                    "cleaned": 0,
                    "already_absent": 0,
                    "failed": 0,
                    "skipped": 0,
                }
                continue

            cutoff = current - timedelta(days=retention_days)
            candidates = self._candidate_ids(
                tier,
                cutoff=cutoff,
                limit=configured_limit,
            )
            counts = {
                "cleaned": 0,
                "absent": 0,
                "failed": 0,
                "skipped": 0,
            }
            if execute:
                for job_id in candidates:
                    result = self.cleanup_job_tier(
                        job_id,
                        tier=tier,
                        retention_days=retention_days,
                        now=current,
                    )
                    counts[result] += 1
            tier_reports[tier] = {
                "enabled": True,
                "retention_days": retention_days,
                "cutoff": cutoff.isoformat(),
                "candidates": len(candidates),
                "cleaned": counts["cleaned"],
                "already_absent": counts["absent"],
                "failed": counts["failed"],
                "skipped": counts["skipped"],
            }

        return {
            "mode": "execute" if execute else "dry_run",
            "batch_size_per_tier": configured_limit,
            "tiers": tier_reports,
        }

    def cleanup_job_tier(
        self,
        job_id: uuid.UUID,
        *,
        tier: RetentionTier,
        retention_days: int,
        now: datetime | None = None,
    ) -> RetentionCleanupResult:
        if tier not in TIER_PATHS:
            raise ValueError("retention tier must be input, runtime, or artifacts")
        if not 1 <= retention_days <= 3650:
            raise ValueError("retention_days must be between 1 and 3650")
        current = _normalize_time(now or datetime.now(timezone.utc))
        cutoff = current - timedelta(days=retention_days)
        cleaned_field = TIER_CLEANED_FIELDS[tier]
        error_code = TIER_ERROR_CODES[tier]

        with self.sessions.begin() as session:
            job = session.execute(
                select(ReconstructionJob)
                .where(ReconstructionJob.id == job_id)
                .with_for_update()
            ).scalar_one_or_none()
            if (
                job is None
                or job.status not in SUCCESS_RETENTION_STATUSES
                or job.finished_at is None
                or _normalize_time(job.finished_at) > cutoff
                or getattr(job, cleaned_field) is not None
            ):
                return "skipped"

            job.retention_cleanup_attempts += 1
            job.retention_cleanup_attempted_at = current
            job.updated_at = current
            job.version += 1
            try:
                existed = _remove_retention_tier(
                    self.data_root,
                    job_id,
                    tier=tier,
                )
            except OSError:
                job.retention_cleanup_last_error = error_code
                return "failed"

            if tier == "artifacts" and job.status == JobStatus.SUCCEEDED.value:
                assert_transition(job.status, JobStatus.EXPIRED)
                job.status = JobStatus.EXPIRED.value
            setattr(job, cleaned_field, current)
            if job.retention_cleanup_last_error == error_code:
                job.retention_cleanup_last_error = None
            return "cleaned" if existed else "absent"

    def _candidate_ids(
        self,
        tier: RetentionTier,
        *,
        cutoff: datetime,
        limit: int,
    ) -> list[uuid.UUID]:
        cleaned_column = getattr(
            ReconstructionJob,
            TIER_CLEANED_FIELDS[tier],
        )
        with self.sessions() as session:
            return list(
                session.execute(
                    select(ReconstructionJob.id)
                    .where(
                        ReconstructionJob.status.in_(SUCCESS_RETENTION_STATUSES),
                        ReconstructionJob.finished_at.is_not(None),
                        ReconstructionJob.finished_at <= cutoff,
                        cleaned_column.is_(None),
                    )
                    .order_by(
                        ReconstructionJob.finished_at,
                        ReconstructionJob.id,
                    )
                    .limit(limit)
                ).scalars()
            )


class SuccessRetentionRunner:
    """Run one retention cycle and persist a secret-free database audit."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        data_root: Path,
        settings: SuccessRetentionSettings | None = None,
    ) -> None:
        self.sessions = sessions
        self.settings = settings or SuccessRetentionSettings.from_environment()
        self.cleaner = SuccessJobStorageCleaner(
            sessions,
            data_root=data_root,
            settings=self.settings,
        )

    def run(
        self,
        *,
        execute: bool = False,
        trigger: str = "manual",
        now: datetime | None = None,
        limit: int | None = None,
    ) -> dict[str, object]:
        if trigger not in {"manual", "scheduled"}:
            raise ValueError("retention trigger must be manual or scheduled")
        started_at = _normalize_time(now or datetime.now(timezone.utc))
        batch_size = self.settings.cleanup_batch_size if limit is None else limit
        if not 1 <= batch_size <= 1000:
            raise ValueError("cleanup limit must be between 1 and 1000")
        run_id = uuid.uuid4()
        with self.sessions.begin() as session:
            session.add(
                SuccessRetentionRun(
                    id=run_id,
                    trigger=trigger,
                    mode="execute" if execute else "dry_run",
                    status="running",
                    input_retention_days=self.settings.input_retention_days,
                    runtime_retention_days=self.settings.runtime_retention_days,
                    artifact_retention_days=self.settings.artifact_retention_days,
                    batch_size=batch_size,
                    started_at=started_at,
                )
            )

        try:
            report = self.cleaner.cleanup_due_jobs(
                execute=execute,
                now=started_at,
                limit=batch_size,
            )
        except Exception:
            self._finish_failed(run_id, now=now)
            raise

        finished_at = _normalize_time(now or datetime.now(timezone.utc))
        with self.sessions.begin() as session:
            record = session.get(SuccessRetentionRun, run_id)
            if record is None:
                raise RuntimeError("retention audit record disappeared")
            record.status = "succeeded"
            record.finished_at = finished_at
            record.report = report
        return {
            "run_id": str(run_id),
            "trigger": trigger,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            **report,
        }

    def _finish_failed(
        self,
        run_id: uuid.UUID,
        *,
        now: datetime | None,
    ) -> None:
        finished_at = _normalize_time(now or datetime.now(timezone.utc))
        with self.sessions.begin() as session:
            record = session.get(SuccessRetentionRun, run_id)
            if record is None:
                return
            record.status = "failed"
            record.finished_at = finished_at
            record.error_code = "SUCCESS_RETENTION_RUN_FAILED"


def _remove_retention_tier(
    data_root: Path,
    job_id: uuid.UUID,
    *,
    tier: RetentionTier,
) -> bool:
    resolved_data_root = data_root.expanduser().resolve()
    jobs_root = (resolved_data_root / "jobs").resolve()
    task_root = jobs_root / str(job_id)
    if task_root.is_symlink() or getattr(task_root, "is_junction", lambda: False)():
        raise OSError("successful task retention target is a link or junction")
    if not task_root.exists():
        return False
    if not task_root.is_dir():
        raise OSError("successful task retention target is not a directory")
    try:
        layout = TaskLayout.from_data_root(resolved_data_root, str(job_id))
    except PathBoundaryError as exc:
        raise OSError("successful task retention target is unsafe") from exc
    if layout.root != task_root:
        raise OSError("successful task retention target resolves through a link")

    existed = False
    for relative in TIER_PATHS[tier]:
        candidate = layout.root
        for part in PurePosixPath(relative).parts:
            candidate = candidate / part
            if candidate.is_symlink() or getattr(
                candidate,
                "is_junction",
                lambda: False,
            )():
                raise OSError("retention subpath is a link or junction")
        if not candidate.exists():
            continue
        if not candidate.is_dir():
            raise OSError("retention subpath is not a directory")
        resolved = candidate.resolve()
        if resolved != layout.resolve(relative):
            raise OSError("retention subpath resolves outside its expected location")
        shutil.rmtree(candidate)
        existed = True
    return existed


def _normalize_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _optional_environment_integer(name: str) -> int | None:
    configured = os.environ.get(name)
    if configured is None or not configured.strip():
        return None
    try:
        return int(configured)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _environment_integer(name: str, default: int) -> int:
    configured = os.environ.get(name)
    if configured is None:
        return default
    try:
        return int(configured)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _environment_boolean(name: str, default: bool) -> bool:
    configured = os.environ.get(name)
    if configured is None:
        return default
    normalized = configured.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")
