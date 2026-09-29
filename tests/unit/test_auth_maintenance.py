from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from apps.maintenance.main import main as maintenance_main
from backend.auth import AuthMaintenanceService, AuthMaintenanceSettings
from backend.db.models import (
    AuthActionRequestBucket,
    AuthActionToken,
    AuthEvent,
    AuthRegistrationBucket,
    AuthThrottleBucket,
    Base,
)


class AuthMaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary.name) / "maintenance.sqlite3"
        self.database_url = f"sqlite:///{database_path.as_posix()}"
        self.engine = create_engine(self.database_url)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary.cleanup()

    def test_cleanup_removes_expired_records_but_preserves_active_blocks(self) -> None:
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=100)
        recent = now - timedelta(days=1)
        with self.sessions.begin() as session:
            session.add_all(
                [
                    _event(old, "old"),
                    _event(recent, "recent"),
                    _login_bucket("old-login", old),
                    _login_bucket(
                        "active-login",
                        old,
                        blocked_until=now + timedelta(hours=1),
                    ),
                    _login_bucket("recent-login", recent),
                    _registration_bucket("old-registration", old),
                    _registration_bucket(
                        "active-registration",
                        old,
                        blocked_until=now + timedelta(hours=1),
                    ),
                    _registration_bucket("recent-registration", recent),
                    _action_token(old, "a" * 64, expired=True),
                    _action_token(recent, "b" * 64, expired=False),
                    _action_request_bucket("old-action-request", old),
                    _action_request_bucket(
                        "active-action-request",
                        old,
                        blocked_until=now + timedelta(hours=1),
                    ),
                ]
            )

        report = AuthMaintenanceService(
            self.sessions,
            AuthMaintenanceSettings(
                event_retention_days=90,
                throttle_retention_days=7,
                cleanup_batch_size=10,
            ),
        ).cleanup(now=now)

        self.assertEqual(report["events_deleted"], 1)
        self.assertEqual(report["action_tokens_deleted"], 1)
        self.assertEqual(report["login_buckets_deleted"], 1)
        self.assertEqual(report["registration_buckets_deleted"], 1)
        self.assertEqual(report["action_request_buckets_deleted"], 1)
        with self.sessions() as session:
            event_reasons = set(session.execute(select(AuthEvent.reason_code)).scalars())
            login_keys = set(
                session.execute(select(AuthThrottleBucket.key_hash)).scalars()
            )
            registration_keys = set(
                session.execute(select(AuthRegistrationBucket.key_hash)).scalars()
            )
            action_tokens = list(
                session.execute(select(AuthActionToken)).scalars()
            )
            action_request_keys = set(
                session.execute(select(AuthActionRequestBucket.key_hash)).scalars()
            )
        self.assertEqual(event_reasons, {"recent"})
        self.assertEqual(login_keys, {"active-login", "recent-login"})
        self.assertEqual(
            registration_keys,
            {"active-registration", "recent-registration"},
        )
        self.assertEqual(len(action_tokens), 1)
        self.assertEqual(action_tokens[0].token_sha256, "b" * 64)
        self.assertEqual(action_request_keys, {"active-action-request"})

    def test_cleanup_batch_limit_and_command_output(self) -> None:
        old = datetime.now(timezone.utc) - timedelta(days=365)
        with self.sessions.begin() as session:
            session.add_all([_event(old, "first"), _event(old, "second")])

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = maintenance_main(
                [
                    "cleanup-auth-security",
                    "--database-url",
                    self.database_url,
                    "--event-retention-days",
                    "90",
                    "--throttle-retention-days",
                    "7",
                    "--limit",
                    "1",
                ]
            )
        self.assertEqual(exit_code, 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["operation"], "cleanup-auth-security")
        self.assertEqual(report["events_deleted"], 1)
        with self.sessions() as session:
            remaining = list(session.execute(select(AuthEvent)).scalars())
        self.assertEqual(len(remaining), 1)

    def test_settings_read_environment_and_reject_invalid_limits(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "AUTH_EVENT_RETENTION_DAYS": "30",
                "AUTH_ACTION_TOKEN_RETENTION_DAYS": "4",
                "AUTH_THROTTLE_RETENTION_DAYS": "2",
                "AUTH_CLEANUP_BATCH_SIZE": "50",
            },
            clear=True,
        ):
            settings = AuthMaintenanceSettings.from_environment()
        self.assertEqual(settings.event_retention_days, 30)
        self.assertEqual(settings.action_token_retention_days, 4)
        self.assertEqual(settings.throttle_retention_days, 2)
        self.assertEqual(settings.cleanup_batch_size, 50)
        with self.assertRaises(ValueError):
            AuthMaintenanceSettings(event_retention_days=0)


def _event(occurred_at: datetime, reason_code: str) -> AuthEvent:
    return AuthEvent(
        id=uuid.uuid4(),
        action="login",
        outcome="failure",
        reason_code=reason_code,
        occurred_at=occurred_at,
    )


def _login_bucket(
    key_hash: str,
    updated_at: datetime,
    *,
    blocked_until: datetime | None = None,
) -> AuthThrottleBucket:
    return AuthThrottleBucket(
        key_hash=key_hash,
        dimension="account",
        failure_count=1,
        window_started_at=updated_at,
        blocked_until=blocked_until,
        updated_at=updated_at,
    )


def _registration_bucket(
    key_hash: str,
    updated_at: datetime,
    *,
    blocked_until: datetime | None = None,
) -> AuthRegistrationBucket:
    return AuthRegistrationBucket(
        key_hash=key_hash,
        dimension="identity",
        attempt_count=1,
        window_started_at=updated_at,
        blocked_until=blocked_until,
        updated_at=updated_at,
    )


def _action_token(
    created_at: datetime,
    token_sha256: str,
    *,
    expired: bool,
) -> AuthActionToken:
    return AuthActionToken(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        purpose="password_reset",
        token_sha256=token_sha256,
        created_at=created_at,
        expires_at=(
            created_at + timedelta(minutes=30)
            if expired
            else datetime.now(timezone.utc) + timedelta(days=1)
        ),
    )


def _action_request_bucket(
    key_hash: str,
    updated_at: datetime,
    *,
    blocked_until: datetime | None = None,
) -> AuthActionRequestBucket:
    return AuthActionRequestBucket(
        key_hash=key_hash,
        purpose="password_reset",
        dimension="identity",
        attempt_count=1,
        window_started_at=updated_at,
        blocked_until=blocked_until,
        updated_at=updated_at,
    )


if __name__ == "__main__":
    unittest.main()
