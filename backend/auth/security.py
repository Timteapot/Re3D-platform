from __future__ import annotations

import hashlib
import hmac
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError as DatabaseIntegrityError
from sqlalchemy.orm import Session

from backend.db.models import AuthEvent, AuthThrottleBucket

from .context import AuthRequestContext
from .settings import AuthSettings


@dataclass(frozen=True)
class LoginThrottleKey:
    dimension: str
    key_hash: str
    limit: int


class AuthSecurity:
    def __init__(self, settings: AuthSettings) -> None:
        self.settings = settings
        self._secret = settings.jwt_secret.encode("utf-8")

    def identifier_fingerprint(self, identifier: str) -> str:
        normalized = identifier.strip().lower()
        return self._fingerprint(f"identifier:{normalized}")

    def client_ip_fingerprint(self, context: AuthRequestContext | None) -> str | None:
        if context is None or context.client_ip is None:
            return None
        return self._fingerprint(f"client-ip:{context.client_ip}")

    def login_account_fingerprint(
        self,
        *,
        normalized_identifier: str,
        user_id: uuid.UUID | None,
    ) -> str:
        subject = f"user:{user_id}" if user_id is not None else (
            f"unknown:{normalized_identifier}"
        )
        return self._fingerprint(f"login-subject:{subject}")

    @staticmethod
    def user_agent_sha256(context: AuthRequestContext | None) -> str | None:
        if context is None or not context.user_agent:
            return None
        return hashlib.sha256(context.user_agent.encode("utf-8")).hexdigest()

    def login_keys(
        self,
        account_fingerprint: str,
        client_ip_fingerprint: str | None,
    ) -> tuple[LoginThrottleKey, ...]:
        keys = [
            LoginThrottleKey(
                dimension="account",
                key_hash=self._fingerprint(
                    f"login-account:{account_fingerprint}"
                ),
                limit=self.settings.login_account_max_failures,
            )
        ]
        if client_ip_fingerprint is not None:
            keys.append(
                LoginThrottleKey(
                    dimension="ip",
                    key_hash=self._fingerprint(
                        f"login-ip:{client_ip_fingerprint}"
                    ),
                    limit=self.settings.login_ip_max_failures,
                )
            )
        return tuple(sorted(keys, key=lambda key: key.key_hash))

    def login_retry_after(
        self,
        session: Session,
        keys: tuple[LoginThrottleKey, ...],
        *,
        now: datetime,
    ) -> int | None:
        retry_after = 0
        window = timedelta(minutes=self.settings.login_window_minutes)
        for key in keys:
            bucket = session.execute(
                select(AuthThrottleBucket)
                .where(AuthThrottleBucket.key_hash == key.key_hash)
                .with_for_update()
            ).scalar_one_or_none()
            if bucket is None:
                continue
            blocked_until = _as_utc_or_none(bucket.blocked_until)
            if blocked_until is not None and blocked_until > now:
                retry_after = max(
                    retry_after,
                    math.ceil((blocked_until - now).total_seconds()),
                )
                continue
            if _as_utc(bucket.window_started_at) + window <= now:
                bucket.failure_count = 0
                bucket.window_started_at = now
            bucket.blocked_until = None
            bucket.updated_at = now
        return retry_after or None

    def record_login_failure(
        self,
        session: Session,
        keys: tuple[LoginThrottleKey, ...],
        *,
        now: datetime,
    ) -> None:
        window = timedelta(minutes=self.settings.login_window_minutes)
        block = timedelta(minutes=self.settings.login_block_minutes)
        for key in keys:
            bucket = self._get_or_create_bucket(session, key, now=now)
            blocked_until = _as_utc_or_none(bucket.blocked_until)
            if blocked_until is not None and blocked_until > now:
                continue
            if _as_utc(bucket.window_started_at) + window <= now:
                bucket.failure_count = 0
                bucket.window_started_at = now
                bucket.blocked_until = None
            bucket.failure_count += 1
            bucket.updated_at = now
            if bucket.failure_count >= key.limit:
                bucket.blocked_until = now + block

    def reset_account_throttle(
        self,
        session: Session,
        keys: tuple[LoginThrottleKey, ...],
        *,
        now: datetime,
    ) -> None:
        for key in keys:
            if key.dimension != "account":
                continue
            bucket = session.execute(
                select(AuthThrottleBucket)
                .where(AuthThrottleBucket.key_hash == key.key_hash)
                .with_for_update()
            ).scalar_one_or_none()
            if bucket is not None:
                bucket.failure_count = 0
                bucket.window_started_at = now
                bucket.blocked_until = None
                bucket.updated_at = now

    def add_event(
        self,
        session: Session,
        *,
        action: str,
        outcome: str,
        context: AuthRequestContext | None,
        occurred_at: datetime,
        user_id: uuid.UUID | None = None,
        refresh_session_id: uuid.UUID | None = None,
        identifier_fingerprint: str | None = None,
        reason_code: str | None = None,
    ) -> None:
        session.add(
            AuthEvent(
                id=uuid.uuid4(),
                action=action,
                outcome=outcome,
                user_id=user_id,
                refresh_session_id=refresh_session_id,
                identifier_fingerprint=identifier_fingerprint,
                client_ip_fingerprint=self.client_ip_fingerprint(context),
                user_agent_sha256=self.user_agent_sha256(context),
                reason_code=reason_code,
                occurred_at=occurred_at,
            )
        )

    def _get_or_create_bucket(
        self,
        session: Session,
        key: LoginThrottleKey,
        *,
        now: datetime,
    ) -> AuthThrottleBucket:
        bucket = session.execute(
            select(AuthThrottleBucket)
            .where(AuthThrottleBucket.key_hash == key.key_hash)
            .with_for_update()
        ).scalar_one_or_none()
        if bucket is not None:
            return bucket

        candidate = AuthThrottleBucket(
            key_hash=key.key_hash,
            dimension=key.dimension,
            failure_count=0,
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
                select(AuthThrottleBucket)
                .where(AuthThrottleBucket.key_hash == key.key_hash)
                .with_for_update()
            ).scalar_one()

    def _fingerprint(self, value: str) -> str:
        return hmac.new(
            self._secret,
            value.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _as_utc_or_none(value: datetime | None) -> datetime | None:
    return _as_utc(value) if value is not None else None
