from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.db.models import (
    Base,
    User,
    UserTransferBucket,
    UserTransferLease,
)
from backend.transfers import (
    TransferLimitExceededError,
    TransferLimitService,
    TransferLimitSettings,
)


class TransferLimitSettingsTests(unittest.TestCase):
    def test_production_requires_explicit_operator_values(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(
                ValueError,
                "TRANSFER_WINDOW_SECONDS is required",
            ):
                TransferLimitSettings.from_environment(
                    environment="production"
                )

    def test_restricted_requires_explicit_operator_values(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "WINDOW_SECONDS is required"):
                TransferLimitSettings.from_environment(
                    environment="restricted"
                )

    def test_development_uses_bounded_defaults(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            settings = TransferLimitSettings.from_environment(
                environment="development"
            )
        self.assertEqual(settings.window_seconds, 3600)
        self.assertEqual(settings.upload_max_concurrent, 2)
        self.assertEqual(settings.upload_max_bytes, 2 * 1024**3)
        self.assertEqual(settings.download_max_concurrent, 3)
        self.assertEqual(settings.download_max_bytes, 4 * 1024**3)
        with self.assertRaisesRegex(ValueError, "MAX_CONCURRENT"):
            TransferLimitSettings(upload_max_concurrent=0)


class TransferLimitServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary.name) / "transfers.sqlite3"
        self.engine = create_engine(f"sqlite:///{database_path.as_posix()}")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.user_id = uuid.uuid4()
        self.other_user_id = uuid.uuid4()
        with self.sessions.begin() as session:
            for user_id, suffix in (
                (self.user_id, "one"),
                (self.other_user_id, "two"),
            ):
                session.add(
                    User(
                        id=user_id,
                        username=f"transfer-{suffix}",
                        email=f"transfer-{suffix}@example.com",
                        password_hash="not-used",
                        role="user",
                        is_active=True,
                        email_verified=True,
                    )
                )
        self.settings = TransferLimitSettings(
            window_seconds=100,
            upload_max_concurrent=1,
            upload_max_bytes=100,
            download_max_concurrent=1,
            download_max_bytes=200,
            lease_seconds=60,
        )
        self.service = TransferLimitService(self.sessions, self.settings)
        self.started_at = datetime(2026, 10, 8, 8, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary.cleanup()

    def test_token_bucket_refills_and_isolated_users_have_full_capacity(self) -> None:
        first = self.service.acquire(
            user_id=self.user_id,
            direction="upload",
            byte_count=60,
            now=self.started_at,
        )
        self.assertTrue(self.service.release(first.id))

        with self.assertRaises(TransferLimitExceededError) as blocked:
            self.service.acquire(
                user_id=self.user_id,
                direction="upload",
                byte_count=41,
                now=self.started_at,
            )
        self.assertEqual(blocked.exception.reason_code, "UPLOAD_TRAFFIC_LIMIT_REACHED")
        self.assertEqual(blocked.exception.retry_after_seconds, 1)

        refilled = self.service.acquire(
            user_id=self.user_id,
            direction="upload",
            byte_count=41,
            now=self.started_at + timedelta(seconds=1),
        )
        self.assertTrue(self.service.release(refilled.id))
        independent = self.service.acquire(
            user_id=self.other_user_id,
            direction="upload",
            byte_count=100,
            now=self.started_at,
        )
        self.assertTrue(self.service.release(independent.id))

        with self.sessions() as session:
            buckets = session.execute(
                select(UserTransferBucket).order_by(UserTransferBucket.user_id)
            ).scalars().all()
        self.assertEqual(len(buckets), 2)

    def test_concurrency_lease_releases_or_expires(self) -> None:
        first = self.service.acquire(
            user_id=self.user_id,
            direction="download",
            byte_count=0,
            now=self.started_at,
        )
        with self.assertRaises(TransferLimitExceededError) as blocked:
            self.service.acquire(
                user_id=self.user_id,
                direction="download",
                byte_count=0,
                now=self.started_at + timedelta(seconds=1),
            )
        self.assertEqual(
            blocked.exception.reason_code,
            "DOWNLOAD_CONCURRENCY_LIMIT_REACHED",
        )
        self.assertEqual(blocked.exception.retry_after_seconds, 59)

        self.assertTrue(self.service.release(first.id))
        self.assertFalse(self.service.release(first.id))
        second = self.service.acquire(
            user_id=self.user_id,
            direction="download",
            byte_count=0,
            now=self.started_at + timedelta(seconds=2),
        )
        expired_replacement = self.service.acquire(
            user_id=self.user_id,
            direction="download",
            byte_count=0,
            now=self.started_at + timedelta(seconds=62),
        )
        self.assertNotEqual(second.id, expired_replacement.id)

        with self.sessions() as session:
            lease_count = session.execute(
                select(func.count()).select_from(UserTransferLease)
            ).scalar_one()
        self.assertEqual(lease_count, 1)

    def test_single_transfer_larger_than_bucket_capacity_is_rejected(self) -> None:
        with self.assertRaises(TransferLimitExceededError) as blocked:
            self.service.acquire(
                user_id=self.user_id,
                direction="download",
                byte_count=201,
                now=self.started_at,
            )
        self.assertEqual(blocked.exception.reason_code, "DOWNLOAD_TRAFFIC_LIMIT_REACHED")
        self.assertIsNone(blocked.exception.retry_after_seconds)


if __name__ == "__main__":
    unittest.main()
