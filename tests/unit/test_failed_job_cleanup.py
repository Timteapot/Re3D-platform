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

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.worker.main import main as worker_main
from backend.db.models import Base, ReconstructionJob
from backend.db.queue import JobQueue
from backend.db.state_machine import JobStatus
from backend.jobs.cleanup import (
    FailedJobCleanupSettings,
    FailedJobStorageCleaner,
)
from backend.jobs.details import JobDetailReader


PIPELINE_COMMIT = "2c5ba174dae9fe53dcec8f7d8466793fdebf0c58"


class FailedJobStorageCleanerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data_root = self.root / "data"
        self.jobs_root = self.data_root / "jobs"
        self.jobs_root.mkdir(parents=True)
        database_path = self.root / "cleanup.sqlite3"
        self.database_url = f"sqlite:///{database_path.as_posix()}"
        self.engine = create_engine(self.database_url)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.queue = JobQueue(self.sessions)
        self.queue.ensure_resource("gpu:0")
        self.cleaner = FailedJobStorageCleaner(
            self.sessions,
            data_root=self.data_root,
            settings=FailedJobCleanupSettings(
                grace_minutes=5,
                interval_seconds=300,
                batch_size=100,
            ),
        )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary.cleanup()

    def enqueue(self) -> uuid.UUID:
        job_id = uuid.uuid4()
        self.queue.enqueue(
            job_id=job_id,
            user_id=uuid.uuid4(),
            execution_mode="real",
            pipeline_tag="re3d-pipeline-v1.1.0",
            pipeline_commit=PIPELINE_COMMIT,
            config_sha256="a" * 64,
            input_manifest_sha256="b" * 64,
            idempotency_key=uuid.uuid4().hex * 2,
            storage_reserved_bytes=2048,
        )
        task_root = self.jobs_root / str(job_id)
        task_root.mkdir()
        (task_root / "private.log").write_text("diagnostic", encoding="utf-8")
        return job_id

    def finish(self, job_id: uuid.UUID, status: JobStatus) -> None:
        if status is JobStatus.CANCELLED:
            self.queue.request_cancel(job_id)
            return
        claim = self.queue.claim_next(worker_id=f"worker-{str(job_id)[:8]}")
        self.assertIsNotNone(claim)
        assert claim is not None
        self.assertEqual(claim.job_id, job_id)
        self.queue.advance(
            claim,
            status,
            error_code="TEST_TERMINAL_FAILURE",
        )

    def age(self, job_id: uuid.UUID, finished_at: datetime) -> None:
        with self.sessions.begin() as session:
            job = session.get(ReconstructionJob, job_id)
            assert job is not None
            job.finished_at = finished_at

    def test_batch_cleans_only_old_failed_and_cancelled_jobs(self) -> None:
        now = datetime.now(timezone.utc)
        failed = self.enqueue()
        self.finish(failed, JobStatus.FAILED_PIPELINE)
        self.age(failed, now - timedelta(minutes=10))

        cancelled = self.enqueue()
        self.finish(cancelled, JobStatus.CANCELLED)
        self.age(cancelled, now - timedelta(minutes=10))

        recent_failure = self.enqueue()
        self.finish(recent_failure, JobStatus.FAILED_PIPELINE)
        self.age(recent_failure, now - timedelta(minutes=1))

        succeeded = self.enqueue()
        claim = self.queue.claim_next(worker_id="worker-success")
        self.assertIsNotNone(claim)
        assert claim is not None
        self.queue.advance(claim, JobStatus.SFM, progress=10)
        self.queue.advance(claim, JobStatus.DENSE_RECONSTRUCTION, progress=30)
        self.queue.advance(claim, JobStatus.MESHING, progress=60)
        self.queue.advance(claim, JobStatus.TEXTURING, progress=80)
        self.queue.advance(claim, JobStatus.VALIDATING_OUTPUT, progress=90)
        self.queue.advance(claim, JobStatus.EVALUATING, progress=95)
        self.queue.advance(claim, JobStatus.SUCCEEDED, progress=100)
        self.age(succeeded, now - timedelta(minutes=10))

        report = self.cleaner.cleanup_terminal_jobs(now=now)

        self.assertEqual(report["scanned"], 2)
        self.assertEqual(report["storage_cleaned"], 2)
        self.assertEqual(report["storage_cleanup_failures"], [])
        self.assertFalse((self.jobs_root / str(failed)).exists())
        self.assertFalse((self.jobs_root / str(cancelled)).exists())
        self.assertTrue((self.jobs_root / str(recent_failure)).is_dir())
        self.assertTrue((self.jobs_root / str(succeeded)).is_dir())
        with self.sessions() as session:
            cleaned = session.get(ReconstructionJob, failed)
            success = session.get(ReconstructionJob, succeeded)
            assert cleaned is not None and success is not None
            self.assertIsNotNone(cleaned.storage_cleaned_at)
            self.assertIsNotNone(cleaned.storage_released_at)
            self.assertEqual(cleaned.storage_cleanup_attempts, 1)
            self.assertIsNone(cleaned.storage_cleanup_last_error)
            self.assertIsNone(success.storage_cleaned_at)
        snapshot = self.queue.get_job(failed)
        detail = JobDetailReader(self.queue, data_root=self.data_root).read(
            failed,
            user_id=snapshot["user_id"],
        )
        self.assertEqual(detail.detail_state, "not_available")
        self.assertIsNone(detail.warning_code)

    def test_cleanup_failure_is_audited_and_retried(self) -> None:
        job_id = self.enqueue()
        self.finish(job_id, JobStatus.FAILED_PIPELINE)
        with (
            self.assertLogs("backend.jobs.cleanup", level="ERROR"),
            patch(
                "backend.jobs.cleanup._remove_job_directory",
                side_effect=OSError("simulated removal failure"),
            ),
        ):
            self.assertEqual(self.cleaner.cleanup_job(job_id), "failed")

        with self.sessions() as session:
            failed = session.get(ReconstructionJob, job_id)
            assert failed is not None
            self.assertEqual(failed.storage_cleanup_attempts, 1)
            self.assertEqual(
                failed.storage_cleanup_last_error,
                "TASK_DIRECTORY_REMOVE_FAILED",
            )
            self.assertIsNone(failed.storage_cleaned_at)
            self.assertIsNone(failed.storage_released_at)

        self.assertEqual(self.cleaner.cleanup_job(job_id), "cleaned")
        with self.sessions() as session:
            cleaned = session.get(ReconstructionJob, job_id)
            assert cleaned is not None
            self.assertEqual(cleaned.storage_cleanup_attempts, 2)
            self.assertIsNone(cleaned.storage_cleanup_last_error)
            self.assertIsNotNone(cleaned.storage_cleaned_at)
            self.assertIsNotNone(cleaned.storage_released_at)

    def test_non_directory_target_is_rejected_without_deleting_it(self) -> None:
        job_id = self.enqueue()
        self.finish(job_id, JobStatus.CANCELLED)
        task_root = self.jobs_root / str(job_id)
        for child in task_root.iterdir():
            child.unlink()
        task_root.rmdir()
        task_root.write_text("do not delete", encoding="utf-8")

        with self.assertLogs("backend.jobs.cleanup", level="ERROR"):
            self.assertEqual(self.cleaner.cleanup_job(job_id), "failed")
        self.assertEqual(task_root.read_text(encoding="utf-8"), "do not delete")

    def test_missing_directory_is_recorded_as_cleaned(self) -> None:
        job_id = self.enqueue()
        self.finish(job_id, JobStatus.CANCELLED)
        task_root = self.jobs_root / str(job_id)
        for child in task_root.iterdir():
            child.unlink()
        task_root.rmdir()

        self.assertEqual(self.cleaner.cleanup_job(job_id), "absent")
        snapshot = self.queue.get_job(job_id)
        self.assertIsNotNone(snapshot["storage_cleaned_at"])
        self.assertIsNotNone(snapshot["storage_released_at"])

    def test_cleanup_command_outputs_safe_report(self) -> None:
        job_id = self.enqueue()
        self.finish(job_id, JobStatus.CANCELLED)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = worker_main(
                [
                    "cleanup-failed-job-storage",
                    "--database-url",
                    self.database_url,
                    "--data-root",
                    str(self.data_root),
                    "--grace-minutes",
                    "0",
                    "--limit",
                    "10",
                ]
            )
        self.assertEqual(exit_code, 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["operation"], "cleanup-failed-job-storage")
        self.assertEqual(report["storage_cleaned"], 1)
        self.assertNotIn("private.log", report)

    def test_settings_read_environment_and_reject_unsafe_ranges(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "RE3D_FAILED_JOB_CLEANUP_GRACE_MINUTES": "10",
                "RE3D_FAILED_JOB_CLEANUP_INTERVAL_SECONDS": "60",
                "RE3D_FAILED_JOB_CLEANUP_BATCH_SIZE": "25",
            },
            clear=True,
        ):
            settings = FailedJobCleanupSettings.from_environment()
        self.assertEqual(settings.grace_minutes, 10)
        self.assertEqual(settings.interval_seconds, 60)
        self.assertEqual(settings.batch_size, 25)

        with self.assertRaisesRegex(ValueError, "INTERVAL_SECONDS"):
            FailedJobCleanupSettings(interval_seconds=0)


if __name__ == "__main__":
    unittest.main()
