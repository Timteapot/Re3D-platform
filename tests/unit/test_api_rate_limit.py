from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.db.models import ApiRateLimitBucket, Base
from backend.rate_limit import ApiRateLimitService, ApiRateLimitSettings


TEST_SECRET = "api-rate-limit-test-secret-" + "x" * 64


class ApiRateLimitSettingsTests(unittest.TestCase):
    def test_production_requires_explicit_operator_values(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(
                ValueError,
                "API_IP_RATE_LIMIT_WINDOW_SECONDS is required",
            ):
                ApiRateLimitSettings.from_environment(
                    environment="production",
                    fingerprint_secret=TEST_SECRET,
                )

    def test_development_defaults_and_ranges(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            settings = ApiRateLimitSettings.from_environment(
                environment="development",
                fingerprint_secret=TEST_SECRET,
            )
        self.assertEqual(settings.window_seconds, 60)
        self.assertEqual(settings.ip_max_requests, 300)
        with self.assertRaisesRegex(ValueError, "MAX_REQUESTS"):
            ApiRateLimitSettings(
                fingerprint_secret=TEST_SECRET,
                ip_max_requests=0,
            )


class ApiRateLimitServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary.name) / "rate-limit.sqlite3"
        self.engine = create_engine(f"sqlite:///{database_path.as_posix()}")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.service = ApiRateLimitService(
            self.sessions,
            ApiRateLimitSettings(
                fingerprint_secret=TEST_SECRET,
                window_seconds=60,
                ip_max_requests=2,
            ),
        )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary.cleanup()

    def test_fixed_window_blocks_then_resets_without_storing_raw_ip(self) -> None:
        started_at = datetime(2026, 10, 8, 8, 0, tzinfo=timezone.utc)
        first = self.service.consume(client_ip="203.0.113.10", now=started_at)
        second = self.service.consume(
            client_ip="203.0.113.10",
            now=started_at + timedelta(seconds=1),
        )
        blocked = self.service.consume(
            client_ip="203.0.113.10",
            now=started_at + timedelta(seconds=2),
        )

        self.assertTrue(first.allowed)
        self.assertEqual(first.remaining, 1)
        self.assertTrue(second.allowed)
        self.assertEqual(second.remaining, 0)
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reset_after_seconds, 58)

        with self.sessions() as session:
            bucket = session.execute(select(ApiRateLimitBucket)).scalar_one()
        self.assertEqual(bucket.request_count, 2)
        self.assertEqual(len(bucket.key_hash), 64)
        self.assertNotIn("203.0.113.10", bucket.key_hash)

        reset = self.service.consume(
            client_ip="203.0.113.10",
            now=started_at + timedelta(seconds=60),
        )
        self.assertTrue(reset.allowed)
        self.assertEqual(reset.remaining, 1)

    def test_different_sources_have_independent_buckets(self) -> None:
        now = datetime(2026, 10, 8, 8, 0, tzinfo=timezone.utc)
        for _ in range(2):
            self.assertTrue(
                self.service.consume(client_ip="198.51.100.1", now=now).allowed
            )
        self.assertFalse(
            self.service.consume(client_ip="198.51.100.1", now=now).allowed
        )
        self.assertTrue(
            self.service.consume(client_ip="198.51.100.2", now=now).allowed
        )


if __name__ == "__main__":
    unittest.main()
