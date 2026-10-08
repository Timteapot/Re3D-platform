from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session, sessionmaker

from backend.db.models import (
    ApiRateLimitBucket,
    AuthActionRequestBucket,
    AuthActionToken,
    AuthEvent,
    AuthRegistrationBucket,
    AuthThrottleBucket,
)


@dataclass(frozen=True)
class AuthMaintenanceSettings:
    event_retention_days: int = 90
    action_token_retention_days: int = 7
    throttle_retention_days: int = 7
    cleanup_batch_size: int = 1000

    def __post_init__(self) -> None:
        if not 1 <= self.event_retention_days <= 3650:
            raise ValueError("AUTH_EVENT_RETENTION_DAYS must be between 1 and 3650")
        if not 1 <= self.throttle_retention_days <= 365:
            raise ValueError(
                "AUTH_THROTTLE_RETENTION_DAYS must be between 1 and 365"
            )
        if not 1 <= self.action_token_retention_days <= 365:
            raise ValueError(
                "AUTH_ACTION_TOKEN_RETENTION_DAYS must be between 1 and 365"
            )
        if not 1 <= self.cleanup_batch_size <= 10000:
            raise ValueError("AUTH_CLEANUP_BATCH_SIZE must be between 1 and 10000")

    @classmethod
    def from_environment(cls) -> "AuthMaintenanceSettings":
        return cls(
            event_retention_days=_environment_integer(
                "AUTH_EVENT_RETENTION_DAYS",
                90,
            ),
            action_token_retention_days=_environment_integer(
                "AUTH_ACTION_TOKEN_RETENTION_DAYS",
                7,
            ),
            throttle_retention_days=_environment_integer(
                "AUTH_THROTTLE_RETENTION_DAYS",
                7,
            ),
            cleanup_batch_size=_environment_integer(
                "AUTH_CLEANUP_BATCH_SIZE",
                1000,
            ),
        )


class AuthMaintenanceService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: AuthMaintenanceSettings | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings or AuthMaintenanceSettings()

    def cleanup(
        self,
        *,
        now: datetime | None = None,
        event_retention_days: int | None = None,
        action_token_retention_days: int | None = None,
        throttle_retention_days: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        configured_event_days = (
            self.settings.event_retention_days
            if event_retention_days is None
            else event_retention_days
        )
        configured_throttle_days = (
            self.settings.throttle_retention_days
            if throttle_retention_days is None
            else throttle_retention_days
        )
        configured_action_token_days = (
            self.settings.action_token_retention_days
            if action_token_retention_days is None
            else action_token_retention_days
        )
        configured_limit = (
            self.settings.cleanup_batch_size if limit is None else limit
        )
        if not 1 <= configured_event_days <= 3650:
            raise ValueError("event_retention_days must be between 1 and 3650")
        if not 1 <= configured_throttle_days <= 365:
            raise ValueError("throttle_retention_days must be between 1 and 365")
        if not 1 <= configured_action_token_days <= 365:
            raise ValueError("action_token_retention_days must be between 1 and 365")
        if not 1 <= configured_limit <= 10000:
            raise ValueError("cleanup limit must be between 1 and 10000")

        current = _as_utc(now or datetime.now(timezone.utc))
        event_cutoff = current - timedelta(days=configured_event_days)
        action_token_cutoff = current - timedelta(
            days=configured_action_token_days
        )
        throttle_cutoff = current - timedelta(days=configured_throttle_days)

        with self.session_factory.begin() as session:
            event_ids = list(
                session.execute(
                    select(AuthEvent.id)
                    .where(AuthEvent.occurred_at < event_cutoff)
                    .order_by(AuthEvent.occurred_at, AuthEvent.id)
                    .limit(configured_limit)
                ).scalars()
            )
            events_deleted = _delete_ids(
                session,
                AuthEvent,
                AuthEvent.id,
                event_ids,
            )

            action_token_ids = list(
                session.execute(
                    select(AuthActionToken.id)
                    .where(_action_token_cleanup_filter(action_token_cutoff))
                    .order_by(AuthActionToken.expires_at, AuthActionToken.id)
                    .limit(configured_limit)
                ).scalars()
            )
            action_tokens_deleted = _delete_ids(
                session,
                AuthActionToken,
                AuthActionToken.id,
                action_token_ids,
                _action_token_cleanup_filter(action_token_cutoff),
            )

            login_keys = list(
                session.execute(
                    select(AuthThrottleBucket.key_hash)
                    .where(
                        AuthThrottleBucket.updated_at < throttle_cutoff,
                        or_(
                            AuthThrottleBucket.blocked_until.is_(None),
                            AuthThrottleBucket.blocked_until <= current,
                        ),
                    )
                    .order_by(
                        AuthThrottleBucket.updated_at,
                        AuthThrottleBucket.key_hash,
                    )
                    .limit(configured_limit)
                ).scalars()
            )
            login_buckets_deleted = _delete_ids(
                session,
                AuthThrottleBucket,
                AuthThrottleBucket.key_hash,
                login_keys,
                AuthThrottleBucket.updated_at < throttle_cutoff,
                or_(
                    AuthThrottleBucket.blocked_until.is_(None),
                    AuthThrottleBucket.blocked_until <= current,
                ),
            )

            registration_keys = list(
                session.execute(
                    select(AuthRegistrationBucket.key_hash)
                    .where(
                        AuthRegistrationBucket.updated_at < throttle_cutoff,
                        or_(
                            AuthRegistrationBucket.blocked_until.is_(None),
                            AuthRegistrationBucket.blocked_until <= current,
                        ),
                    )
                    .order_by(
                        AuthRegistrationBucket.updated_at,
                        AuthRegistrationBucket.key_hash,
                    )
                    .limit(configured_limit)
                ).scalars()
            )
            registration_buckets_deleted = _delete_ids(
                session,
                AuthRegistrationBucket,
                AuthRegistrationBucket.key_hash,
                registration_keys,
                AuthRegistrationBucket.updated_at < throttle_cutoff,
                or_(
                    AuthRegistrationBucket.blocked_until.is_(None),
                    AuthRegistrationBucket.blocked_until <= current,
                ),
            )

            action_request_keys = list(
                session.execute(
                    select(AuthActionRequestBucket.key_hash)
                    .where(
                        AuthActionRequestBucket.updated_at < throttle_cutoff,
                        or_(
                            AuthActionRequestBucket.blocked_until.is_(None),
                            AuthActionRequestBucket.blocked_until <= current,
                        ),
                    )
                    .order_by(
                        AuthActionRequestBucket.updated_at,
                        AuthActionRequestBucket.key_hash,
                    )
                    .limit(configured_limit)
                ).scalars()
            )
            action_request_buckets_deleted = _delete_ids(
                session,
                AuthActionRequestBucket,
                AuthActionRequestBucket.key_hash,
                action_request_keys,
                AuthActionRequestBucket.updated_at < throttle_cutoff,
                or_(
                    AuthActionRequestBucket.blocked_until.is_(None),
                    AuthActionRequestBucket.blocked_until <= current,
                ),
            )

            api_rate_limit_keys = list(
                session.execute(
                    select(ApiRateLimitBucket.key_hash)
                    .where(ApiRateLimitBucket.updated_at < throttle_cutoff)
                    .order_by(
                        ApiRateLimitBucket.updated_at,
                        ApiRateLimitBucket.key_hash,
                    )
                    .limit(configured_limit)
                ).scalars()
            )
            api_rate_limit_buckets_deleted = _delete_ids(
                session,
                ApiRateLimitBucket,
                ApiRateLimitBucket.key_hash,
                api_rate_limit_keys,
                ApiRateLimitBucket.updated_at < throttle_cutoff,
            )

        return {
            "events_deleted": events_deleted,
            "action_tokens_deleted": action_tokens_deleted,
            "login_buckets_deleted": login_buckets_deleted,
            "registration_buckets_deleted": registration_buckets_deleted,
            "action_request_buckets_deleted": action_request_buckets_deleted,
            "api_rate_limit_buckets_deleted": api_rate_limit_buckets_deleted,
            "event_cutoff": event_cutoff.isoformat(),
            "action_token_cutoff": action_token_cutoff.isoformat(),
            "throttle_cutoff": throttle_cutoff.isoformat(),
        }


def _delete_ids(
    session: Session,
    model: type,
    id_column: Any,
    identifiers: list[Any],
    *criteria: Any,
) -> int:
    if not identifiers:
        return 0
    result = session.execute(
        delete(model).where(id_column.in_(identifiers), *criteria)
    )
    return result.rowcount or 0


def _environment_integer(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _action_token_cleanup_filter(cutoff: datetime):
    return or_(
        AuthActionToken.expires_at < cutoff,
        AuthActionToken.consumed_at < cutoff,
        AuthActionToken.revoked_at < cutoff,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
