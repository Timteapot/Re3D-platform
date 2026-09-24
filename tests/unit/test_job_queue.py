from __future__ import annotations

import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.db.errors import (
    InvalidTransitionError,
    LeaseLostError,
    QueueConflictError,
)
from backend.db.heartbeat import LeaseHeartbeatLoop
from backend.db.models import Base, WorkerLease
from backend.db.queue import JobClaim, JobQueue, LeaseHeartbeat
from backend.db.runtime import DatabaseSettings, SchedulerSettings
from backend.db.state_machine import JobStatus, assert_transition, can_transition


PIPELINE_COMMIT = "2c5ba174dae9fe53dcec8f7d8466793fdebf0c58"
CONFIG_SHA256 = "e0b7ff8b5613884dba4eb6e86ff66419c4b5df96f3d89fa4b7756184fb055c69"


class StateMachineTests(unittest.TestCase):
    def test_normal_flow_and_expected_failure_paths(self) -> None:
        normal_flow = (
            JobStatus.DRAFT,
            JobStatus.UPLOADING,
            JobStatus.VALIDATING_INPUT,
            JobStatus.QUEUED,
            JobStatus.PREPARING,
            JobStatus.SFM,
            JobStatus.DENSE_RECONSTRUCTION,
            JobStatus.MESHING,
            JobStatus.TEXTURING,
            JobStatus.VALIDATING_OUTPUT,
            JobStatus.EVALUATING,
            JobStatus.SUCCEEDED,
            JobStatus.EXPIRED,
        )
        for current, target in zip(normal_flow, normal_flow[1:]):
            with self.subTest(current=current, target=target):
                self.assertTrue(can_transition(current, target))
                assert_transition(current, target)

        self.assertTrue(
            can_transition(JobStatus.VALIDATING_INPUT, JobStatus.FAILED_INPUT)
        )
        self.assertTrue(can_transition(JobStatus.SFM, JobStatus.FAILED_PIPELINE))
        self.assertTrue(
            can_transition(JobStatus.EVALUATING, JobStatus.FAILED_EVALUATION)
        )

    def test_rejects_skips_and_terminal_restarts(self) -> None:
        invalid = (
            (JobStatus.QUEUED, JobStatus.SFM),
            (JobStatus.SUCCEEDED, JobStatus.QUEUED),
            (JobStatus.FAILED_PIPELINE, JobStatus.PREPARING),
            (JobStatus.EXPIRED, JobStatus.SUCCEEDED),
        )
        for current, target in invalid:
            with self.subTest(current=current, target=target):
                with self.assertRaises(InvalidTransitionError):
                    assert_transition(current, target)


class SchedulerSettingsTests(unittest.TestCase):
    def test_reads_safe_defaults_and_rejects_invalid_heartbeat_ratio(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            settings = SchedulerSettings.from_environment()
        self.assertEqual(settings.resource_key, "gpu:0")
        self.assertEqual(settings.lease_seconds, 60)
        self.assertEqual(settings.heartbeat_seconds, 20)

        with patch.dict(
            "os.environ",
            {"RE3D_LEASE_SECONDS": "30", "RE3D_HEARTBEAT_SECONDS": "15"},
            clear=True,
        ):
            with self.assertRaises(ValueError):
                SchedulerSettings.from_environment()

    def test_database_url_is_required_and_backend_is_limited(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ValueError):
                DatabaseSettings.from_environment()
        with self.assertRaises(ValueError):
            DatabaseSettings.from_environment("mysql://localhost/re3d")
        configured = DatabaseSettings.from_environment("sqlite:///:memory:")
        self.assertEqual(configured.url, "sqlite:///:memory:")


class HeartbeatLoopTests(unittest.TestCase):
    def claim(self) -> JobClaim:
        return JobClaim(
            job_id=uuid.uuid4(),
            worker_id="worker-a",
            resource_key="gpu:0",
            lease_token=uuid.uuid4(),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
            status=JobStatus.PREPARING,
            attempt=1,
            execution_mode="simulated",
            recovered=False,
        )

    def test_periodic_heartbeat_surfaces_cancellation(self) -> None:
        called = threading.Event()

        class FakeQueue:
            def heartbeat(
                self,
                claim: JobClaim,
                *,
                lease_seconds: int,
            ) -> LeaseHeartbeat:
                called.set()
                return LeaseHeartbeat(
                    expires_at=datetime.now(timezone.utc)
                    + timedelta(seconds=lease_seconds),
                    cancel_requested=True,
                )

        loop = LeaseHeartbeatLoop(
            FakeQueue(),  # type: ignore[arg-type]
            self.claim(),
            interval_seconds=0.01,
            lease_seconds=5,
        )
        with loop:
            self.assertTrue(called.wait(timeout=1))
        self.assertTrue(loop.cancel_requested)
        self.assertIsNotNone(loop.latest)

    def test_background_lease_failure_is_raised_to_worker(self) -> None:
        called = threading.Event()

        class FailingQueue:
            def heartbeat(
                self,
                claim: JobClaim,
                *,
                lease_seconds: int,
            ) -> LeaseHeartbeat:
                called.set()
                raise LeaseLostError("lease replaced")

        loop = LeaseHeartbeatLoop(
            FailingQueue(),  # type: ignore[arg-type]
            self.claim(),
            interval_seconds=0.01,
            lease_seconds=5,
        )
        with self.assertRaises(LeaseLostError):
            with loop:
                self.assertTrue(called.wait(timeout=1))


class JobQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary.name) / "queue.sqlite3"
        self.engine = create_engine(f"sqlite:///{database_path}")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.queue = JobQueue(self.sessions)
        self.queue.ensure_resource("gpu:0")

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary.cleanup()

    def enqueue(self, *, priority: int = 0) -> uuid.UUID:
        job_id = uuid.uuid4()
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

    def test_claim_enforces_one_gpu_slot_and_priority_order(self) -> None:
        low_priority = self.enqueue(priority=0)
        high_priority = self.enqueue(priority=10)

        claim = self.queue.claim_next(worker_id="worker-a")
        self.assertIsNotNone(claim)
        assert claim is not None
        self.assertEqual(claim.job_id, high_priority)
        self.assertEqual(claim.status, JobStatus.PREPARING)
        self.assertFalse(claim.recovered)
        self.assertIsNone(self.queue.claim_next(worker_id="worker-b"))
        self.assertEqual(self.queue.get_job(low_priority)["status"], JobStatus.QUEUED)

    def test_heartbeat_reports_cancellation_and_terminal_releases_slot(self) -> None:
        job_id = self.enqueue()
        claim = self.queue.claim_next(worker_id="worker-a")
        assert claim is not None

        status = self.queue.request_cancel(job_id)
        self.assertEqual(status, JobStatus.PREPARING)
        heartbeat = self.queue.heartbeat(claim)
        self.assertTrue(heartbeat.cancel_requested)

        self.queue.advance(
            claim,
            JobStatus.CANCELLED,
            error_code="USER_CANCELLED",
        )
        job = self.queue.get_job(job_id)
        self.assertEqual(job["status"], JobStatus.CANCELLED)
        self.assertIsNone(self.queue.claim_next(worker_id="worker-b"))

    def test_expired_lease_is_recovered_and_old_token_is_fenced(self) -> None:
        job_id = self.enqueue()
        stale_claim = self.queue.claim_next(worker_id="worker-a")
        assert stale_claim is not None
        with self.sessions.begin() as session:
            lease = session.execute(
                select(WorkerLease).where(WorkerLease.resource_key == "gpu:0")
            ).scalar_one()
            lease.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

        recovered = self.queue.claim_next(worker_id="worker-b")
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered.job_id, job_id)
        self.assertTrue(recovered.recovered)
        self.assertNotEqual(recovered.lease_token, stale_claim.lease_token)
        with self.assertRaises(LeaseLostError):
            self.queue.heartbeat(stale_claim)
        self.queue.heartbeat(recovered)

    def test_full_worker_flow_is_monotonic_and_releases_next_job(self) -> None:
        first = self.enqueue(priority=1)
        second = self.enqueue(priority=0)
        claim = self.queue.claim_next(worker_id="worker-a")
        assert claim is not None
        self.assertEqual(claim.job_id, first)

        stages = (
            (JobStatus.SFM, 10),
            (JobStatus.DENSE_RECONSTRUCTION, 35),
            (JobStatus.MESHING, 60),
            (JobStatus.TEXTURING, 80),
            (JobStatus.VALIDATING_OUTPUT, 90),
            (JobStatus.EVALUATING, 95),
            (JobStatus.SUCCEEDED, 100),
        )
        for status, progress in stages:
            self.queue.advance(claim, status, progress=progress)

        completed = self.queue.get_job(first)
        self.assertEqual(completed["status"], JobStatus.SUCCEEDED)
        self.assertEqual(completed["progress"], 100)
        next_claim = self.queue.claim_next(worker_id="worker-b")
        self.assertIsNotNone(next_claim)
        assert next_claim is not None
        self.assertEqual(next_claim.job_id, second)

    def test_rejects_progress_regression_and_duplicate_idempotency(self) -> None:
        job_id = self.enqueue()
        claim = self.queue.claim_next(worker_id="worker-a")
        assert claim is not None
        self.queue.advance(claim, JobStatus.SFM, progress=20)
        with self.assertRaises(ValueError):
            self.queue.advance(
                claim,
                JobStatus.DENSE_RECONSTRUCTION,
                progress=10,
            )
        with self.assertRaises(ValueError):
            self.queue.advance(claim, JobStatus.FAILED_PIPELINE)

        with self.assertRaises(QueueConflictError):
            self.queue.enqueue(
                job_id=uuid.uuid4(),
                user_id=uuid.uuid4(),
                execution_mode="simulated",
                pipeline_tag="re3d-pipeline-v1.1.0",
                pipeline_commit=PIPELINE_COMMIT,
                config_sha256=CONFIG_SHA256,
                input_manifest_sha256="b" * 64,
                idempotency_key=self._idempotency_key(job_id),
            )

    def test_queued_cancellation_never_claims_job(self) -> None:
        job_id = self.enqueue()
        self.assertEqual(self.queue.request_cancel(job_id), JobStatus.CANCELLED)
        self.assertIsNone(self.queue.claim_next(worker_id="worker-a"))

    def _idempotency_key(self, job_id: uuid.UUID) -> str:
        with self.sessions() as session:
            from backend.db.models import ReconstructionJob

            return session.get(ReconstructionJob, job_id).idempotency_key


if __name__ == "__main__":
    unittest.main()
