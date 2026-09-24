from __future__ import annotations

import os
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.db.errors import LeaseLostError
from backend.db.models import ReconstructionJob, WorkerLease
from backend.db.queue import JobClaim, JobQueue
from backend.db.state_machine import JobStatus


DATABASE_URL = os.environ.get("RE3D_TEST_DATABASE_URL")
PIPELINE_COMMIT = "2c5ba174dae9fe53dcec8f7d8466793fdebf0c58"
CONFIG_SHA256 = "e0b7ff8b5613884dba4eb6e86ff66419c4b5df96f3d89fa4b7756184fb055c69"


@unittest.skipUnless(DATABASE_URL, "RE3D_TEST_DATABASE_URL is not configured")
class PostgreSQLJobQueueIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        assert DATABASE_URL is not None
        self.engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.queue = JobQueue(self.sessions)
        self.resource_key = f"gpu:test-{uuid.uuid4().hex[:12]}"
        self.queue.ensure_resource(self.resource_key)

    def tearDown(self) -> None:
        with self.sessions.begin() as session:
            lease = session.get(WorkerLease, self.resource_key)
            if lease is not None:
                lease.job_id = None
                lease.worker_id = None
                lease.lease_token = None
                lease.leased_at = None
                lease.heartbeat_at = None
                lease.expires_at = None
                session.flush()
                session.delete(lease)
            session.query(ReconstructionJob).filter(
                ReconstructionJob.id.in_(getattr(self, "job_ids", []))
            ).delete(synchronize_session=False)
        self.engine.dispose()

    def enqueue(self, priority: int) -> uuid.UUID:
        job_id = uuid.uuid4()
        self.job_ids = getattr(self, "job_ids", []) + [job_id]
        self.queue.enqueue(
            job_id=job_id,
            user_id=uuid.uuid4(),
            execution_mode="simulated",
            pipeline_tag="re3d-pipeline-v1.1.0",
            pipeline_commit=PIPELINE_COMMIT,
            config_sha256=CONFIG_SHA256,
            input_manifest_sha256="a" * 64,
            idempotency_key=uuid.uuid4().hex * 2,
            priority=priority,
        )
        return job_id

    def test_concurrent_claim_and_expired_lease_takeover(self) -> None:
        high_priority = self.enqueue(priority=10)
        self.enqueue(priority=0)
        barrier = threading.Barrier(2)
        claims: list[JobClaim | None] = []
        failures: list[BaseException] = []

        def claim(worker_id: str) -> None:
            try:
                barrier.wait(timeout=5)
                claims.append(
                    self.queue.claim_next(
                        worker_id=worker_id,
                        resource_key=self.resource_key,
                    )
                )
            except BaseException as exc:  # pragma: no cover - diagnostic path
                failures.append(exc)

        workers = [
            threading.Thread(target=claim, args=(f"pg-worker-{index}",))
            for index in range(2)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)

        self.assertEqual(failures, [])
        claimed = [claim for claim in claims if claim is not None]
        self.assertEqual(len(claimed), 1)
        first_claim = claimed[0]
        self.assertEqual(first_claim.job_id, high_priority)

        with self.sessions.begin() as session:
            lease = session.execute(
                select(WorkerLease).where(
                    WorkerLease.resource_key == self.resource_key
                )
            ).scalar_one()
            lease.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

        recovered = self.queue.claim_next(
            worker_id="pg-recovery-worker",
            resource_key=self.resource_key,
        )
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered.job_id, high_priority)
        self.assertTrue(recovered.recovered)
        with self.assertRaises(LeaseLostError):
            self.queue.heartbeat(first_claim)
        self.queue.advance(recovered, JobStatus.SFM, progress=10)


if __name__ == "__main__":
    unittest.main()
