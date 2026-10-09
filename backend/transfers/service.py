from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.db.models import User, UserTransferBucket, UserTransferLease

from .settings import TransferDirection, TransferLimitSettings


@dataclass(frozen=True)
class TransferLease:
    id: uuid.UUID
    user_id: uuid.UUID
    direction: TransferDirection
    reserved_bytes: int
    expires_at: datetime


class TransferLimitExceededError(RuntimeError):
    def __init__(
        self,
        *,
        direction: TransferDirection,
        limit_kind: str,
        retry_after_seconds: int | None,
    ) -> None:
        if limit_kind not in {"concurrency", "traffic"}:
            raise ValueError("unsupported transfer limit kind")
        self.direction = direction
        self.limit_kind = limit_kind
        self.retry_after_seconds = retry_after_seconds
        self.reason_code = (
            f"{direction.upper()}_{limit_kind.upper()}_LIMIT_REACHED"
        )
        super().__init__(
            f"{direction} {limit_kind} limit reached; retry later"
        )


class TransferLimitService:
    """Enforce database-shared per-user transfer admission limits.

    Concurrency is represented by expiring leases. Byte allowance uses a token
    bucket whose capacity is the configured bytes per window and whose refill
    rate is capacity/window. Locking the user row serializes all API instances.
    """

    def __init__(
        self,
        sessions: sessionmaker[Session],
        settings: TransferLimitSettings,
    ) -> None:
        self.sessions = sessions
        self.settings = settings

    def acquire(
        self,
        *,
        user_id: uuid.UUID,
        direction: TransferDirection,
        byte_count: int,
        now: datetime | None = None,
    ) -> TransferLease:
        if direction not in {"upload", "download"}:
            raise ValueError("transfer direction must be upload or download")
        if byte_count < 0:
            raise ValueError("transfer byte count cannot be negative")

        capacity = self.settings.max_bytes(direction)
        if byte_count > capacity:
            raise TransferLimitExceededError(
                direction=direction,
                limit_kind="traffic",
                retry_after_seconds=None,
            )

        denial: TransferLimitExceededError | None = None
        lease: TransferLease | None = None
        with self.sessions.begin() as session:
            current = _as_utc(now) if now is not None else _database_now(session)
            locked_user = session.execute(
                select(User.id).where(User.id == user_id).with_for_update()
            ).scalar_one_or_none()
            if locked_user is None:
                raise ValueError("transfer user does not exist")

            session.execute(
                delete(UserTransferLease).where(
                    UserTransferLease.user_id == user_id,
                    UserTransferLease.direction == direction,
                    UserTransferLease.expires_at <= current,
                )
            )
            active_count, earliest_expiry = session.execute(
                select(
                    func.count(UserTransferLease.id),
                    func.min(UserTransferLease.expires_at),
                ).where(
                    UserTransferLease.user_id == user_id,
                    UserTransferLease.direction == direction,
                    UserTransferLease.expires_at > current,
                )
            ).one()
            if active_count >= self.settings.max_concurrent(direction):
                expiry = _as_utc(earliest_expiry)
                denial = TransferLimitExceededError(
                    direction=direction,
                    limit_kind="concurrency",
                    retry_after_seconds=max(
                        1,
                        math.ceil((expiry - current).total_seconds()),
                    ),
                )
            else:
                bucket = self._get_or_create_bucket(
                    session,
                    user_id=user_id,
                    direction=direction,
                    capacity=capacity,
                    now=current,
                )
                available = self._refill_bucket(
                    bucket,
                    capacity=capacity,
                    now=current,
                )
                if byte_count > available:
                    deficit = byte_count - available
                    denial = TransferLimitExceededError(
                        direction=direction,
                        limit_kind="traffic",
                        retry_after_seconds=max(
                            1,
                            math.ceil(
                                deficit
                                * self.settings.window_seconds
                                / capacity
                            ),
                        ),
                    )
                else:
                    bucket.available_bytes = available - byte_count
                    lease_id = uuid.uuid4()
                    expires_at = current + timedelta(
                        seconds=self.settings.lease_seconds
                    )
                    session.add(
                        UserTransferLease(
                            id=lease_id,
                            user_id=user_id,
                            direction=direction,
                            reserved_bytes=byte_count,
                            acquired_at=current,
                            expires_at=expires_at,
                        )
                    )
                    lease = TransferLease(
                        id=lease_id,
                        user_id=user_id,
                        direction=direction,
                        reserved_bytes=byte_count,
                        expires_at=expires_at,
                    )

        if denial is not None:
            raise denial
        assert lease is not None
        return lease

    def release(self, lease_id: uuid.UUID) -> bool:
        with self.sessions.begin() as session:
            lease = session.get(UserTransferLease, lease_id)
            if lease is None:
                return False
            session.delete(lease)
            return True

    def _get_or_create_bucket(
        self,
        session: Session,
        *,
        user_id: uuid.UUID,
        direction: TransferDirection,
        capacity: int,
        now: datetime,
    ) -> UserTransferBucket:
        bucket = session.execute(
            select(UserTransferBucket).where(
                UserTransferBucket.user_id == user_id,
                UserTransferBucket.direction == direction,
            )
        ).scalar_one_or_none()
        if bucket is not None:
            return bucket
        bucket = UserTransferBucket(
            id=uuid.uuid4(),
            user_id=user_id,
            direction=direction,
            capacity_bytes=capacity,
            available_bytes=capacity,
            updated_at=now,
        )
        session.add(bucket)
        session.flush()
        return bucket

    def _refill_bucket(
        self,
        bucket: UserTransferBucket,
        *,
        capacity: int,
        now: datetime,
    ) -> int:
        available = bucket.available_bytes
        if bucket.capacity_bytes != capacity:
            if capacity > bucket.capacity_bytes:
                available += capacity - bucket.capacity_bytes
            available = min(capacity, available)
            bucket.capacity_bytes = capacity
            bucket.updated_at = now
        else:
            elapsed = max(
                0.0,
                (now - _as_utc(bucket.updated_at)).total_seconds(),
            )
            refill = math.floor(
                elapsed * capacity / self.settings.window_seconds
            )
            if refill > 0:
                available = min(capacity, available + refill)
                bucket.updated_at = now
        bucket.available_bytes = available
        return available


def _database_now(session: Session) -> datetime:
    value = session.execute(select(func.current_timestamp())).scalar_one()
    return _as_utc(value)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
