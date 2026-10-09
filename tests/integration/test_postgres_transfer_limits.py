from __future__ import annotations

import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.models import User
from backend.transfers import (
    TransferLimitExceededError,
    TransferLimitService,
    TransferLimitSettings,
)
from tests.integration.postgres_support import validated_test_database_url


DATABASE_URL = validated_test_database_url()


@unittest.skipUnless(DATABASE_URL, "RE3D_TEST_DATABASE_URL is not configured")
class PostgreSQLTransferLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        assert DATABASE_URL is not None
        self.engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.user_id = uuid.uuid4()
        with self.sessions.begin() as session:
            session.add(
                User(
                    id=self.user_id,
                    username=f"transfer-{uuid.uuid4().hex[:12]}",
                    email=f"transfer-{uuid.uuid4().hex[:12]}@example.com",
                    password_hash="not-used",
                    role="user",
                    is_active=True,
                    email_verified=True,
                )
            )

    def tearDown(self) -> None:
        with self.sessions.begin() as session:
            user = session.get(User, self.user_id)
            if user is not None:
                session.delete(user)
        self.engine.dispose()

    def test_concurrent_api_instances_do_not_over_allocate_byte_tokens(self) -> None:
        service = TransferLimitService(
            self.sessions,
            TransferLimitSettings(
                window_seconds=3600,
                upload_max_concurrent=2,
                upload_max_bytes=100,
                download_max_concurrent=2,
                download_max_bytes=100,
                lease_seconds=60,
            ),
        )
        barrier = Barrier(2)
        now = datetime(2026, 10, 8, 8, 0, tzinfo=timezone.utc)

        def compete() -> tuple[str, uuid.UUID | None]:
            barrier.wait()
            try:
                lease = service.acquire(
                    user_id=self.user_id,
                    direction="upload",
                    byte_count=60,
                    now=now,
                )
                return "allowed", lease.id
            except TransferLimitExceededError as exc:
                return exc.reason_code, None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: compete(), range(2)))

        self.assertEqual(
            [result[0] for result in results].count("allowed"),
            1,
        )
        self.assertEqual(
            [result[0] for result in results].count(
                "UPLOAD_TRAFFIC_LIMIT_REACHED"
            ),
            1,
        )
        for _, lease_id in results:
            if lease_id is not None:
                service.release(lease_id)


if __name__ == "__main__":
    unittest.main()
