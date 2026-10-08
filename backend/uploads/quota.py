from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.db.models import JobUpload, ReconstructionJob, User


USER_STORAGE_QUOTA_CODE = "USER_STORAGE_QUOTA_EXCEEDED"


class StorageQuotaExceededError(RuntimeError):
    """A user cannot reserve additional task storage."""

    reason_code = USER_STORAGE_QUOTA_CODE


@dataclass(frozen=True)
class StorageQuotaSettings:
    """Logical per-user storage limits enforced through database state."""

    quota_bytes: int = 10 * 1024**3
    job_reservation_bytes: int = 2 * 1024**3

    def __post_init__(self) -> None:
        if self.quota_bytes < 1:
            raise ValueError("RE3D_USER_STORAGE_QUOTA_BYTES must be positive")
        if self.job_reservation_bytes < 1:
            raise ValueError("RE3D_JOB_STORAGE_RESERVATION_BYTES must be positive")
        if self.job_reservation_bytes > self.quota_bytes:
            raise ValueError(
                "RE3D_JOB_STORAGE_RESERVATION_BYTES must not exceed "
                "RE3D_USER_STORAGE_QUOTA_BYTES"
            )

    @classmethod
    def from_environment(
        cls,
        *,
        environment: str = "development",
    ) -> "StorageQuotaSettings":
        production = environment.strip().lower() == "production"
        return cls(
            quota_bytes=_read_integer(
                "RE3D_USER_STORAGE_QUOTA_BYTES",
                default=10 * 1024**3,
                required=production,
            ),
            job_reservation_bytes=_read_integer(
                "RE3D_JOB_STORAGE_RESERVATION_BYTES",
                default=2 * 1024**3,
                required=production,
            ),
        )


@dataclass(frozen=True)
class StorageQuotaSnapshot:
    quota_bytes: int
    used_bytes: int
    remaining_bytes: int
    over_quota_bytes: int
    upload_bytes: int
    reserved_job_bytes: int
    job_reservation_bytes: int

    def as_dict(self) -> dict[str, int]:
        return {
            "quota_bytes": self.quota_bytes,
            "used_bytes": self.used_bytes,
            "remaining_bytes": self.remaining_bytes,
            "over_quota_bytes": self.over_quota_bytes,
            "upload_bytes": self.upload_bytes,
            "reserved_job_bytes": self.reserved_job_bytes,
            "job_reservation_bytes": self.job_reservation_bytes,
        }


class UserStorageQuotaService:
    """Calculate and transactionally enforce one user's logical storage usage."""

    def __init__(self, settings: StorageQuotaSettings | None = None) -> None:
        self.settings = settings or StorageQuotaSettings()

    def snapshot(self, session: Session, *, user_id: uuid.UUID) -> StorageQuotaSnapshot:
        upload_bytes = int(
            session.execute(
                select(func.coalesce(func.sum(JobUpload.total_bytes), 0)).where(
                    JobUpload.user_id == user_id,
                    JobUpload.status != "submitted",
                    JobUpload.storage_cleaned_at.is_(None),
                )
            ).scalar_one()
        )
        reserved_job_bytes = int(
            session.execute(
                select(
                    func.coalesce(
                        func.sum(ReconstructionJob.storage_reserved_bytes),
                        0,
                    )
                ).where(
                    ReconstructionJob.user_id == user_id,
                    ReconstructionJob.storage_released_at.is_(None),
                )
            ).scalar_one()
        )
        used_bytes = upload_bytes + reserved_job_bytes
        return StorageQuotaSnapshot(
            quota_bytes=self.settings.quota_bytes,
            used_bytes=used_bytes,
            remaining_bytes=max(0, self.settings.quota_bytes - used_bytes),
            over_quota_bytes=max(0, used_bytes - self.settings.quota_bytes),
            upload_bytes=upload_bytes,
            reserved_job_bytes=reserved_job_bytes,
            job_reservation_bytes=self.settings.job_reservation_bytes,
        )

    def enforce_in_session(
        self,
        session: Session,
        *,
        user_id: uuid.UUID,
        additional_bytes: int,
        replaced_upload_bytes: int = 0,
    ) -> StorageQuotaSnapshot:
        if additional_bytes < 0 or replaced_upload_bytes < 0:
            raise ValueError("storage quota deltas must not be negative")
        user = session.execute(
            select(User).where(User.id == user_id).with_for_update()
        ).scalar_one_or_none()
        if user is None:
            raise RuntimeError("storage quota user does not exist")

        current = self.snapshot(session, user_id=user_id)
        projected = current.used_bytes - replaced_upload_bytes + additional_bytes
        if projected > self.settings.quota_bytes:
            raise StorageQuotaExceededError("user storage quota exceeded")
        return current


def _read_integer(name: str, *, default: int, required: bool) -> int:
    configured = os.environ.get(name)
    if configured is None or not configured.strip():
        if required:
            raise ValueError(f"{name} is required in production")
        return default
    try:
        return int(configured)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
