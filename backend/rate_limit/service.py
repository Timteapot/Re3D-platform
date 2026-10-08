from __future__ import annotations

import hashlib
import hmac
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError as DatabaseIntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.db.models import ApiRateLimitBucket

from .settings import ApiRateLimitSettings


@dataclass(frozen=True)
class ApiRateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    reset_after_seconds: int


class ApiRateLimitService:
    """Consume one shared fixed-window request allowance per source IP."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        settings: ApiRateLimitSettings,
    ) -> None:
        self.sessions = sessions
        self.settings = settings
        self._secret = settings.fingerprint_secret.encode("utf-8")

    def consume(
        self,
        *,
        client_ip: str | None,
        now: datetime | None = None,
    ) -> ApiRateLimitDecision:
        key_hash = self._fingerprint(client_ip or "unresolved")
        window = timedelta(seconds=self.settings.window_seconds)

        with self.sessions.begin() as session:
            current = _as_utc(now) if now is not None else _database_now(session)
            bucket = self._get_or_create_bucket(
                session,
                key_hash=key_hash,
                now=current,
            )
            started_at = _as_utc(bucket.window_started_at)
            if started_at + window <= current:
                bucket.request_count = 0
                bucket.window_started_at = current
                started_at = current

            reset_after = max(
                1,
                math.ceil((started_at + window - current).total_seconds()),
            )
            bucket.updated_at = current
            if bucket.request_count >= self.settings.ip_max_requests:
                return ApiRateLimitDecision(
                    allowed=False,
                    limit=self.settings.ip_max_requests,
                    remaining=0,
                    reset_after_seconds=reset_after,
                )

            bucket.request_count += 1
            return ApiRateLimitDecision(
                allowed=True,
                limit=self.settings.ip_max_requests,
                remaining=max(
                    0,
                    self.settings.ip_max_requests - bucket.request_count,
                ),
                reset_after_seconds=reset_after,
            )

    def _get_or_create_bucket(
        self,
        session: Session,
        *,
        key_hash: str,
        now: datetime,
    ) -> ApiRateLimitBucket:
        bucket = session.execute(
            select(ApiRateLimitBucket)
            .where(ApiRateLimitBucket.key_hash == key_hash)
            .with_for_update()
        ).scalar_one_or_none()
        if bucket is not None:
            return bucket

        candidate = ApiRateLimitBucket(
            key_hash=key_hash,
            request_count=0,
            window_started_at=now,
            updated_at=now,
        )
        try:
            with session.begin_nested():
                session.add(candidate)
                session.flush()
            return candidate
        except DatabaseIntegrityError:
            return session.execute(
                select(ApiRateLimitBucket)
                .where(ApiRateLimitBucket.key_hash == key_hash)
                .with_for_update()
            ).scalar_one()

    def _fingerprint(self, client_ip: str) -> str:
        return hmac.new(
            self._secret,
            f"ordinary-api-ip:{client_ip}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()


def _database_now(session: Session) -> datetime:
    value = session.execute(select(func.current_timestamp())).scalar_one()
    return _as_utc(value)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
