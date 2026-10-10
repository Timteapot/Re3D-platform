from __future__ import annotations

import math
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.db.models import ReconstructionJob, TaskActionEvent, User
from backend.db.state_machine import TERMINAL_STATUSES
from backend.environment import requires_explicit_operational_settings


PENDING_LIMIT_CODE = "PENDING_JOB_LIMIT_REACHED"
SUBMISSION_WINDOW_LIMIT_CODE = "SUBMISSION_WINDOW_LIMIT_REACHED"


class TaskSubmissionLimitError(RuntimeError):
    """A user-scoped task submission limit rejected a new job."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class TaskSubmissionSettings:
    """Limits for stable upload submissions, shared through database state."""

    max_pending_jobs: int = 3
    max_submissions_per_24h: int = 20

    def __post_init__(self) -> None:
        if not 1 <= self.max_pending_jobs <= 1000:
            raise ValueError("RE3D_USER_MAX_PENDING_JOBS must be between 1 and 1000")
        if not 1 <= self.max_submissions_per_24h <= 10_000:
            raise ValueError(
                "RE3D_USER_MAX_SUBMISSIONS_PER_24H must be between 1 and 10000"
            )
        if self.max_submissions_per_24h < self.max_pending_jobs:
            raise ValueError(
                "RE3D_USER_MAX_SUBMISSIONS_PER_24H must be at least "
                "RE3D_USER_MAX_PENDING_JOBS"
            )

    @classmethod
    def from_environment(
        cls,
        *,
        environment: str = "development",
    ) -> "TaskSubmissionSettings":
        explicit = requires_explicit_operational_settings(environment)
        return cls(
            max_pending_jobs=_read_integer(
                "RE3D_USER_MAX_PENDING_JOBS",
                default=3,
                required=explicit,
            ),
            max_submissions_per_24h=_read_integer(
                "RE3D_USER_MAX_SUBMISSIONS_PER_24H",
                default=20,
                required=explicit,
            ),
        )


class TaskSubmissionPolicy:
    """Enforce task limits and append task lifecycle audit events."""

    def __init__(self, settings: TaskSubmissionSettings | None = None) -> None:
        self.settings = settings or TaskSubmissionSettings()

    def enforce_in_session(
        self,
        session: Session,
        *,
        user_id: uuid.UUID,
    ) -> None:
        """Lock one user, then evaluate both limits in the caller's transaction."""

        user = session.execute(
            select(User).where(User.id == user_id).with_for_update()
        ).scalar_one_or_none()
        if user is None:
            raise RuntimeError("task submission user does not exist")

        now = _database_now(session)
        terminal_values = tuple(status.value for status in TERMINAL_STATUSES)
        pending_jobs = session.execute(
            select(func.count())
            .select_from(ReconstructionJob)
            .where(
                ReconstructionJob.user_id == user_id,
                ReconstructionJob.status.not_in(terminal_values),
            )
        ).scalar_one()
        if pending_jobs >= self.settings.max_pending_jobs:
            raise TaskSubmissionLimitError(
                "pending reconstruction job limit reached",
                reason_code=PENDING_LIMIT_CODE,
            )

        window_start = now - timedelta(hours=24)
        submission_count = session.execute(
            select(func.count(ReconstructionJob.id)).where(
                ReconstructionJob.user_id == user_id,
                ReconstructionJob.created_at >= window_start,
            )
        ).scalar_one()
        if submission_count >= self.settings.max_submissions_per_24h:
            release_offset = (
                submission_count - self.settings.max_submissions_per_24h
            )
            release_created_at = session.execute(
                select(ReconstructionJob.created_at)
                .where(
                    ReconstructionJob.user_id == user_id,
                    ReconstructionJob.created_at >= window_start,
                )
                .order_by(
                    ReconstructionJob.created_at,
                    ReconstructionJob.id,
                )
                .offset(release_offset)
                .limit(1)
            ).scalar_one()
            release_time = _as_utc(release_created_at) + timedelta(hours=24)
            retry_after_seconds = max(
                1,
                math.ceil((release_time - now).total_seconds()),
            )
            raise TaskSubmissionLimitError(
                "24-hour reconstruction submission limit reached",
                reason_code=SUBMISSION_WINDOW_LIMIT_CODE,
                retry_after_seconds=retry_after_seconds,
            )

    @staticmethod
    def record_in_session(
        session: Session,
        *,
        action: str,
        user_id: uuid.UUID,
        job_id: uuid.UUID,
        execution_mode: str,
        reason_code: str | None = None,
    ) -> None:
        session.add(
            TaskActionEvent(
                id=uuid.uuid4(),
                action=action,
                outcome="success",
                user_id=user_id,
                job_id=job_id,
                execution_mode=execution_mode,
                reason_code=reason_code,
            )
        )


def _database_now(session: Session) -> datetime:
    value = session.execute(select(func.current_timestamp())).scalar_one()
    return _as_utc(value)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _read_integer(name: str, *, default: int, required: bool) -> int:
    configured = os.environ.get(name)
    if configured is None or not configured.strip():
        if required:
            raise ValueError(f"{name} is required for restricted and production")
        return default
    try:
        return int(configured)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
