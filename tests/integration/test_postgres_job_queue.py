from __future__ import annotations

import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.db.errors import LeaseLostError
from backend.admin import AdminRoleService
from backend.db.models import (
    AdminRoleChangeEvent,
    ApiRateLimitBucket,
    JobUpload,
    ReconstructionJob,
    SuccessRetentionRun,
    User,
    WorkerLease,
)
from backend.db.queue import JobClaim, JobQueue
from backend.db.state_machine import JobStatus
from backend.jobs.cleanup import FailedJobCleanupSettings, FailedJobStorageCleaner
from backend.jobs.policy import (
    PENDING_LIMIT_CODE,
    TaskSubmissionLimitError,
    TaskSubmissionPolicy,
    TaskSubmissionSettings,
)
from backend.jobs.retention import SuccessRetentionRunner, SuccessRetentionSettings
from backend.rate_limit import ApiRateLimitService, ApiRateLimitSettings
from backend.uploads import (
    StorageQuotaExceededError,
    StorageQuotaSettings,
    UserStorageQuotaService,
)
from tests.integration.postgres_support import validated_test_database_url


DATABASE_URL = validated_test_database_url()
PIPELINE_COMMIT = "d5c51e580ddc54ac96bbb2635e5147713c2db4e7"
CONFIG_SHA256 = "9f8cbc83cb67b8313f624913686fbdc453e7572a18603d015bc198a9a92b8883"


class TestDatabaseGuardTests(unittest.TestCase):
    def test_rejects_development_database(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "RE3D_TEST_DATABASE_URL": (
                    "postgresql+psycopg://re3d_app:secret@127.0.0.1:5432/"
                    "re3d_platform_dev"
                )
            },
            clear=True,
        ):
            with self.assertRaises(RuntimeError):
                validated_test_database_url()

    def test_accepts_disposable_postgresql_test_database(self) -> None:
        expected = (
            "postgresql+psycopg://re3d_test:secret@127.0.0.1:55432/"
            "re3d_platform_test"
        )
        with patch.dict(
            "os.environ",
            {"RE3D_TEST_DATABASE_URL": expected},
            clear=True,
        ):
            self.assertEqual(validated_test_database_url(), expected)


@unittest.skipUnless(DATABASE_URL, "RE3D_TEST_DATABASE_URL is not configured")
class PostgreSQLJobQueueIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        assert DATABASE_URL is not None
        self.engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.queue = JobQueue(self.sessions)
        self.resource_key = f"gpu:test-{uuid.uuid4().hex[:12]}"
        self.queue.ensure_resource(self.resource_key)
        self.temporary = tempfile.TemporaryDirectory()
        self.data_root = Path(self.temporary.name) / "data"
        self.user_id = uuid.uuid4()
        with self.sessions.begin() as session:
            session.add(
                User(
                    id=self.user_id,
                    username=f"queue-{self.user_id.hex[:12]}",
                    email=f"queue-{self.user_id.hex[:12]}@example.com",
                    password_hash="test-only-not-a-login-hash",
                    role="user",
                    is_active=True,
                    email_verified=False,
                )
            )

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
            session.query(SuccessRetentionRun).filter(
                SuccessRetentionRun.id.in_(
                    getattr(self, "retention_run_ids", [])
                )
            ).delete(synchronize_session=False)
            role_user_ids = getattr(self, "role_user_ids", [])
            if role_user_ids:
                session.query(AdminRoleChangeEvent).filter(
                    AdminRoleChangeEvent.target_user_id.in_(role_user_ids)
                ).delete(synchronize_session=False)
                session.query(User).filter(User.id.in_(role_user_ids)).delete(
                    synchronize_session=False
                )
            rate_limit_started_at = getattr(self, "rate_limit_started_at", None)
            if rate_limit_started_at is not None:
                session.query(ApiRateLimitBucket).filter(
                    ApiRateLimitBucket.window_started_at == rate_limit_started_at
                ).delete(synchronize_session=False)
            session.query(User).filter(User.id == self.user_id).delete(
                synchronize_session=False
            )
        self.engine.dispose()
        self.temporary.cleanup()

    def enqueue(self, priority: int) -> uuid.UUID:
        job_id = uuid.uuid4()
        self.job_ids = getattr(self, "job_ids", []) + [job_id]
        self.queue.enqueue(
            job_id=job_id,
            user_id=self.user_id,
            execution_mode="simulated",
            pipeline_tag="re3d-pipeline-v1.1.1",
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

    def test_concurrent_user_submissions_are_serialized_by_policy(self) -> None:
        policy = TaskSubmissionPolicy(
            TaskSubmissionSettings(
                max_pending_jobs=1,
                max_submissions_per_24h=2,
            )
        )
        first_ready = threading.Event()
        second_started = threading.Event()
        release_first = threading.Event()
        results: list[str] = []
        failures: list[BaseException] = []
        job_ids = [uuid.uuid4(), uuid.uuid4()]
        self.job_ids = getattr(self, "job_ids", []) + job_ids

        def enqueue_with_policy(index: int) -> None:
            try:
                if index == 1:
                    first_ready.wait(timeout=5)
                    second_started.set()
                with self.sessions.begin() as session:
                    policy.enforce_in_session(session, user_id=self.user_id)
                    self.queue.enqueue_in_session(
                        session,
                        job_id=job_ids[index],
                        user_id=self.user_id,
                        execution_mode="simulated",
                        pipeline_tag="re3d-pipeline-v1.1.1",
                        pipeline_commit=PIPELINE_COMMIT,
                        config_sha256=CONFIG_SHA256,
                        input_manifest_sha256="b" * 64,
                        idempotency_key=uuid.uuid4().hex * 2,
                    )
                    if index == 0:
                        first_ready.set()
                        release_first.wait(timeout=5)
                results.append("accepted")
            except TaskSubmissionLimitError as exc:
                results.append(exc.reason_code)
            except BaseException as exc:  # pragma: no cover - diagnostic path
                failures.append(exc)

        workers = [
            threading.Thread(target=enqueue_with_policy, args=(index,))
            for index in range(2)
        ]
        for worker in workers:
            worker.start()
        self.assertTrue(first_ready.wait(timeout=5))
        self.assertTrue(second_started.wait(timeout=5))
        release_first.set()
        for worker in workers:
            worker.join(timeout=10)

        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(failures, [])
        self.assertCountEqual(results, ["accepted", PENDING_LIMIT_CODE])

    def test_concurrent_storage_reservations_cannot_oversell_user_quota(
        self,
    ) -> None:
        quota = UserStorageQuotaService(
            StorageQuotaSettings(
                quota_bytes=1024,
                job_reservation_bytes=1,
            )
        )
        first_ready = threading.Event()
        second_started = threading.Event()
        release_first = threading.Event()
        results: list[str] = []
        failures: list[BaseException] = []

        def reserve(index: int) -> None:
            upload_id = uuid.uuid4()
            try:
                if index == 1:
                    first_ready.wait(timeout=5)
                    second_started.set()
                with self.sessions.begin() as session:
                    quota.enforce_in_session(
                        session,
                        user_id=self.user_id,
                        additional_bytes=700,
                    )
                    session.add(
                        JobUpload(
                            id=upload_id,
                            user_id=self.user_id,
                            status="uploading",
                            idempotency_key=uuid.uuid4().hex * 2,
                            image_count=1,
                            total_bytes=700,
                        )
                    )
                    if index == 0:
                        first_ready.set()
                        release_first.wait(timeout=5)
                results.append("accepted")
            except StorageQuotaExceededError:
                results.append("quota_exceeded")
            except BaseException as exc:  # pragma: no cover - diagnostic path
                failures.append(exc)

        workers = [
            threading.Thread(target=reserve, args=(index,))
            for index in range(2)
        ]
        for worker in workers:
            worker.start()
        self.assertTrue(first_ready.wait(timeout=5))
        self.assertTrue(second_started.wait(timeout=5))
        release_first.set()
        for worker in workers:
            worker.join(timeout=10)

        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(failures, [])
        self.assertCountEqual(results, ["accepted", "quota_exceeded"])

    def test_concurrent_admin_demotions_preserve_one_active_admin(self) -> None:
        self.role_user_ids = [uuid.uuid4(), uuid.uuid4()]
        with self.sessions.begin() as session:
            session.add_all(
                User(
                    id=user_id,
                    username=f"admin-{user_id.hex[:12]}",
                    email=f"admin-{user_id.hex[:12]}@example.com",
                    password_hash="test-only-not-a-login-hash",
                    role="admin",
                    is_active=True,
                    email_verified=True,
                )
                for user_id in self.role_user_ids
            )

        barrier = threading.Barrier(2)
        results: list[str] = []
        failures: list[BaseException] = []

        def demote(user_id: uuid.UUID) -> None:
            try:
                barrier.wait(timeout=5)
                AdminRoleService(self.sessions).set_role(
                    user_id=user_id,
                    role="user",
                    confirmed=True,
                )
                results.append("changed")
            except ValueError as exc:
                results.append(str(exc))
            except BaseException as exc:  # pragma: no cover - diagnostic path
                failures.append(exc)

        workers = [
            threading.Thread(target=demote, args=(user_id,))
            for user_id in self.role_user_ids
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)

        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(failures, [])
        self.assertEqual(results.count("changed"), 1)
        self.assertEqual(
            results.count("the last active administrator cannot be demoted"),
            1,
        )
        with self.sessions() as session:
            active_admin_count = session.execute(
                select(func.count(User.id)).where(
                    User.id.in_(self.role_user_ids),
                    User.role == "admin",
                    User.is_active.is_(True),
                )
            ).scalar_one()
            role_event_count = session.execute(
                select(func.count(AdminRoleChangeEvent.id)).where(
                    AdminRoleChangeEvent.target_user_id.in_(self.role_user_ids)
                )
            ).scalar_one()
        self.assertEqual(active_admin_count, 1)
        self.assertEqual(role_event_count, 1)

    def test_concurrent_api_rate_limit_consumption_is_shared(self) -> None:
        self.rate_limit_started_at = datetime(
            2099,
            1,
            1,
            tzinfo=timezone.utc,
        )
        service = ApiRateLimitService(
            self.sessions,
            ApiRateLimitSettings(
                fingerprint_secret="postgres-rate-limit-test-" + "x" * 64,
                window_seconds=60,
                ip_max_requests=1,
            ),
        )
        source = f"integration-test-{uuid.uuid4()}"
        barrier = threading.Barrier(2)
        decisions: list[bool] = []
        failures: list[BaseException] = []

        def consume() -> None:
            try:
                barrier.wait(timeout=5)
                decision = service.consume(
                    client_ip=source,
                    now=self.rate_limit_started_at,
                )
                decisions.append(decision.allowed)
            except BaseException as exc:  # pragma: no cover - diagnostic path
                failures.append(exc)

        workers = [threading.Thread(target=consume) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)

        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(failures, [])
        self.assertCountEqual(decisions, [True, False])
        with self.sessions() as session:
            bucket = session.execute(
                select(ApiRateLimitBucket).where(
                    ApiRateLimitBucket.window_started_at
                    == self.rate_limit_started_at
                )
            ).scalar_one()
        self.assertEqual(bucket.request_count, 1)

    def test_failed_job_storage_cleanup_is_audited(self) -> None:
        job_id = self.enqueue(priority=1)
        task_root = self.data_root / "jobs" / str(job_id)
        task_root.mkdir(parents=True)
        (task_root / "private.log").write_text("private", encoding="utf-8")
        claim = self.queue.claim_next(
            worker_id="pg-cleanup-worker",
            resource_key=self.resource_key,
        )
        self.assertIsNotNone(claim)
        assert claim is not None
        self.queue.advance(
            claim,
            JobStatus.FAILED_PIPELINE,
            error_code="POSTGRES_CLEANUP_TEST",
        )
        terminal_snapshot = self.queue.get_job(job_id)
        finished_at = terminal_snapshot["finished_at"]
        assert finished_at is not None

        cleaner = FailedJobStorageCleaner(
            self.sessions,
            data_root=self.data_root,
            settings=FailedJobCleanupSettings(
                grace_minutes=0,
                interval_seconds=300,
                batch_size=10,
            ),
        )
        report = cleaner.cleanup_terminal_jobs(
            now=finished_at + timedelta(seconds=1),
            grace_minutes=0,
        )

        self.assertEqual(report["storage_cleaned"], 1)
        self.assertFalse(task_root.exists())
        snapshot = self.queue.get_job(job_id)
        self.assertIsNotNone(snapshot["storage_cleaned_at"])
        self.assertEqual(snapshot["storage_cleanup_attempts"], 1)
        self.assertIsNone(snapshot["storage_cleanup_last_error"])

    def test_success_retention_dry_run_is_audited_as_json(self) -> None:
        job_id = self.enqueue(priority=1)
        claim = self.queue.claim_next(
            worker_id="pg-retention-worker",
            resource_key=self.resource_key,
        )
        self.assertIsNotNone(claim)
        assert claim is not None
        for status, progress in (
            (JobStatus.SFM, 10),
            (JobStatus.DENSE_RECONSTRUCTION, 30),
            (JobStatus.MESHING, 60),
            (JobStatus.TEXTURING, 80),
            (JobStatus.VALIDATING_OUTPUT, 90),
            (JobStatus.EVALUATING, 95),
            (JobStatus.SUCCEEDED, 100),
        ):
            self.queue.advance(claim, status, progress=progress)
        now = datetime.now(timezone.utc)
        with self.sessions.begin() as session:
            job = session.get(ReconstructionJob, job_id)
            assert job is not None
            job.finished_at = now - timedelta(days=31)
        images = self.data_root / "jobs" / str(job_id) / "input" / "images"
        images.mkdir(parents=True)
        (images / "camera.jpg").write_bytes(b"image")

        report = SuccessRetentionRunner(
            self.sessions,
            data_root=self.data_root,
            settings=SuccessRetentionSettings(
                input_retention_days=30,
                runtime_retention_days=30,
                artifact_retention_days=30,
                cleanup_batch_size=10,
            ),
        ).run(trigger="scheduled", now=now)
        run_id = uuid.UUID(report["run_id"])
        self.retention_run_ids = [run_id]

        self.assertEqual(report["mode"], "dry_run")
        self.assertEqual(report["tiers"]["input"]["candidates"], 1)
        self.assertTrue(images.is_dir())
        with self.sessions() as session:
            audit = session.get(SuccessRetentionRun, run_id)
            assert audit is not None
            self.assertEqual(audit.status, "succeeded")
            self.assertEqual(audit.trigger, "scheduled")
            self.assertEqual(audit.report["tiers"]["input"]["candidates"], 1)


if __name__ == "__main__":
    unittest.main()
