from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from apps.worker.main import main as worker_main
from backend.db.models import Base, ReconstructionJob, SuccessRetentionRun
from backend.db.queue import JobQueue
from backend.db.state_machine import JobStatus
from backend.jobs import (
    SuccessJobStorageCleaner,
    SuccessRetentionRunner,
    SuccessRetentionSettings,
)


PIPELINE_COMMIT = "2c5ba174dae9fe53dcec8f7d8466793fdebf0c58"


class SuccessRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data_root = self.root / "data"
        self.jobs_root = self.data_root / "jobs"
        self.jobs_root.mkdir(parents=True)
        database_path = self.root / "retention.sqlite3"
        self.database_url = f"sqlite:///{database_path.as_posix()}"
        self.engine = create_engine(self.database_url)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.queue = JobQueue(self.sessions)
        self.queue.ensure_resource("gpu:0")
        self.settings = SuccessRetentionSettings(
            input_retention_days=1,
            runtime_retention_days=1,
            artifact_retention_days=1,
            cleanup_batch_size=10,
        )
        self.cleaner = SuccessJobStorageCleaner(
            self.sessions,
            data_root=self.data_root,
            settings=self.settings,
        )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary.cleanup()

    def create_success(self, *, finished_at: datetime) -> uuid.UUID:
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
        claim = self.queue.claim_next(worker_id=f"retention-{job_id.hex[:8]}")
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
        with self.sessions.begin() as session:
            job = session.get(ReconstructionJob, job_id)
            assert job is not None
            job.finished_at = finished_at

        task_root = self.jobs_root / str(job_id)
        for relative in (
            "input/images",
            "input/staging",
            "runtime/logs",
            "output/A-v4",
            "reports",
            "manifests",
        ):
            (task_root / relative).mkdir(parents=True, exist_ok=True)
        (task_root / "input/images/camera.jpg").write_bytes(b"image")
        (task_root / "input/input-manifest.json").write_text("{}", encoding="utf-8")
        (task_root / "runtime/logs/re3d.log").write_text("log", encoding="utf-8")
        (task_root / "output/A-v4/mesh.glb").write_bytes(b"glTF")
        (task_root / "reports/evaluation.json").write_text("{}", encoding="utf-8")
        (task_root / "manifests/pipeline-result.json").write_text(
            "{}",
            encoding="utf-8",
        )
        return job_id

    def test_dry_run_then_execute_deletes_only_configured_tiers(self) -> None:
        now = datetime.now(timezone.utc)
        job_id = self.create_success(finished_at=now - timedelta(days=2))
        task_root = self.jobs_root / str(job_id)

        preview = self.cleaner.cleanup_due_jobs(now=now)
        self.assertEqual(preview["mode"], "dry_run")
        self.assertEqual(preview["tiers"]["input"]["candidates"], 1)
        self.assertTrue((task_root / "input/images").is_dir())
        self.assertTrue((task_root / "runtime").is_dir())
        self.assertTrue((task_root / "output").is_dir())

        result = self.cleaner.cleanup_due_jobs(execute=True, now=now)
        self.assertEqual(result["mode"], "execute")
        self.assertEqual(result["tiers"]["input"]["cleaned"], 1)
        self.assertEqual(result["tiers"]["runtime"]["cleaned"], 1)
        self.assertEqual(result["tiers"]["artifacts"]["cleaned"], 1)
        self.assertFalse((task_root / "input/images").exists())
        self.assertFalse((task_root / "input/staging").exists())
        self.assertFalse((task_root / "runtime").exists())
        self.assertFalse((task_root / "output").exists())
        self.assertTrue((task_root / "input/input-manifest.json").is_file())
        self.assertTrue((task_root / "reports/evaluation.json").is_file())
        self.assertTrue((task_root / "manifests/pipeline-result.json").is_file())

        snapshot = self.queue.get_job(job_id)
        self.assertEqual(snapshot["status"], JobStatus.EXPIRED)
        self.assertIsNotNone(snapshot["input_cleaned_at"])
        self.assertIsNotNone(snapshot["runtime_cleaned_at"])
        self.assertIsNotNone(snapshot["artifacts_cleaned_at"])
        self.assertIsNotNone(snapshot["storage_released_at"])
        self.assertEqual(snapshot["retention_cleanup_attempts"], 3)
        self.assertIsNone(snapshot["retention_cleanup_last_error"])

        repeated = self.cleaner.cleanup_due_jobs(execute=True, now=now)
        self.assertEqual(repeated["tiers"]["input"]["candidates"], 0)
        self.assertEqual(repeated["tiers"]["runtime"]["candidates"], 0)
        self.assertEqual(repeated["tiers"]["artifacts"]["candidates"], 0)

    def test_failure_is_audited_and_same_tier_retry_clears_error(self) -> None:
        now = datetime.now(timezone.utc)
        job_id = self.create_success(finished_at=now - timedelta(days=2))
        with patch(
            "backend.jobs.retention._remove_retention_tier",
            side_effect=OSError("simulated retention failure"),
        ):
            result = self.cleaner.cleanup_job_tier(
                job_id,
                tier="input",
                retention_days=1,
                now=now,
            )
        self.assertEqual(result, "failed")
        snapshot = self.queue.get_job(job_id)
        self.assertEqual(snapshot["retention_cleanup_attempts"], 1)
        self.assertEqual(
            snapshot["retention_cleanup_last_error"],
            "SUCCESS_INPUT_REMOVE_FAILED",
        )
        self.assertIsNone(snapshot["input_cleaned_at"])

        self.assertEqual(
            self.cleaner.cleanup_job_tier(
                job_id,
                tier="input",
                retention_days=1,
                now=now,
            ),
            "cleaned",
        )
        retried = self.queue.get_job(job_id)
        self.assertEqual(retried["retention_cleanup_attempts"], 2)
        self.assertIsNone(retried["retention_cleanup_last_error"])
        self.assertIsNotNone(retried["input_cleaned_at"])
        self.assertIsNone(retried["storage_released_at"])

    def test_reservation_is_released_only_after_every_tier_is_cleaned(self) -> None:
        now = datetime.now(timezone.utc)
        job_id = self.create_success(finished_at=now - timedelta(days=2))

        for tier in ("input", "runtime"):
            self.assertEqual(
                self.cleaner.cleanup_job_tier(
                    job_id,
                    tier=tier,
                    retention_days=1,
                    now=now,
                ),
                "cleaned",
            )
            self.assertIsNone(self.queue.get_job(job_id)["storage_released_at"])

        self.assertEqual(
            self.cleaner.cleanup_job_tier(
                job_id,
                tier="artifacts",
                retention_days=1,
                now=now,
            ),
            "cleaned",
        )
        self.assertIsNotNone(self.queue.get_job(job_id)["storage_released_at"])

    def test_non_directory_retention_target_is_preserved_and_audited(self) -> None:
        now = datetime.now(timezone.utc)
        job_id = self.create_success(finished_at=now - timedelta(days=2))
        runtime = self.jobs_root / str(job_id) / "runtime"
        shutil.rmtree(runtime)
        runtime.write_text("do not delete", encoding="utf-8")

        result = self.cleaner.cleanup_job_tier(
            job_id,
            tier="runtime",
            retention_days=1,
            now=now,
        )

        self.assertEqual(result, "failed")
        self.assertEqual(runtime.read_text(encoding="utf-8"), "do not delete")
        snapshot = self.queue.get_job(job_id)
        self.assertEqual(
            snapshot["retention_cleanup_last_error"],
            "SUCCESS_RUNTIME_REMOVE_FAILED",
        )
        self.assertIsNone(snapshot["runtime_cleaned_at"])

    def test_command_is_dry_run_without_explicit_execute(self) -> None:
        now = datetime.now(timezone.utc)
        job_id = self.create_success(finished_at=now - timedelta(days=2))
        images = self.jobs_root / str(job_id) / "input/images"
        environment = {
            "SUCCESS_INPUT_RETENTION_DAYS": "1",
            "SUCCESS_RETENTION_CLEANUP_BATCH_SIZE": "10",
        }
        stdout = io.StringIO()
        with (
            patch.dict("os.environ", environment, clear=True),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = worker_main(
                [
                    "cleanup-success-job-storage",
                    "--database-url",
                    self.database_url,
                    "--data-root",
                    str(self.data_root),
                ]
            )
        self.assertEqual(exit_code, 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["mode"], "dry_run")
        self.assertEqual(report["tiers"]["input"]["candidates"], 1)
        self.assertTrue(images.is_dir())
        with self.sessions() as session:
            audit = session.execute(select(SuccessRetentionRun)).scalar_one()
        self.assertEqual(audit.trigger, "manual")
        self.assertEqual(audit.mode, "dry_run")
        self.assertEqual(audit.status, "succeeded")
        self.assertEqual(audit.input_retention_days, 1)
        self.assertEqual(audit.report["tiers"]["input"]["candidates"], 1)

    def test_execute_requires_separate_delete_confirmation(self) -> None:
        now = datetime.now(timezone.utc)
        job_id = self.create_success(finished_at=now - timedelta(days=2))
        output = self.jobs_root / str(job_id) / "output"
        stderr = io.StringIO()
        with (
            patch.dict(
                "os.environ",
                {"SUCCESS_ARTIFACT_RETENTION_DAYS": "1"},
                clear=True,
            ),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = worker_main(
                [
                    "cleanup-success-job-storage",
                    "--database-url",
                    self.database_url,
                    "--data-root",
                    str(self.data_root),
                    "--execute",
                ]
            )
        self.assertEqual(exit_code, 2)
        self.assertIn("--confirm-delete", stderr.getvalue())
        self.assertTrue(output.is_dir())
        with self.sessions() as session:
            audits = list(session.execute(select(SuccessRetentionRun)).scalars())
        self.assertEqual(audits, [])

    def test_confirmed_execute_deletes_and_audits_the_run(self) -> None:
        now = datetime.now(timezone.utc)
        job_id = self.create_success(finished_at=now - timedelta(days=2))
        task_root = self.jobs_root / str(job_id)
        stdout = io.StringIO()
        with (
            patch.dict(
                "os.environ",
                {
                    "SUCCESS_INPUT_RETENTION_DAYS": "1",
                    "SUCCESS_RUNTIME_RETENTION_DAYS": "1",
                    "SUCCESS_ARTIFACT_RETENTION_DAYS": "1",
                },
                clear=True,
            ),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = worker_main(
                [
                    "cleanup-success-job-storage",
                    "--database-url",
                    self.database_url,
                    "--data-root",
                    str(self.data_root),
                    "--trigger",
                    "scheduled",
                    "--execute",
                    "--confirm-delete",
                ]
            )
        self.assertEqual(exit_code, 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["mode"], "execute")
        self.assertEqual(report["tiers"]["artifacts"]["cleaned"], 1)
        self.assertFalse((task_root / "input/images").exists())
        self.assertFalse((task_root / "runtime").exists())
        self.assertFalse((task_root / "output").exists())
        with self.sessions() as session:
            audit = session.execute(select(SuccessRetentionRun)).scalar_one()
        self.assertEqual(audit.trigger, "scheduled")
        self.assertEqual(audit.mode, "execute")
        self.assertEqual(audit.status, "succeeded")
        self.assertEqual(audit.report["tiers"]["artifacts"]["cleaned"], 1)

    def test_env_file_supports_scheduled_dry_run_without_scripts(self) -> None:
        now = datetime.now(timezone.utc)
        self.create_success(finished_at=now - timedelta(days=2))
        env_file = self.root / "retention.env"
        env_file.write_text(
            "\n".join(
                (
                    f"DATABASE_URL={self.database_url}",
                    f"RE3D_DATA_ROOT={self.data_root}",
                    "SUCCESS_INPUT_RETENTION_DAYS=1",
                    "SUCCESS_RUNTIME_RETENTION_DAYS=1",
                    "SUCCESS_ARTIFACT_RETENTION_DAYS=1",
                    "SUCCESS_RETENTION_CLEANUP_BATCH_SIZE=10",
                )
            ),
            encoding="utf-8",
        )
        stdout = io.StringIO()
        with (
            patch.dict("os.environ", {}, clear=True),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = worker_main(
                [
                    "cleanup-success-job-storage",
                    "--env-file",
                    str(env_file),
                    "--trigger",
                    "scheduled",
                ]
            )
        self.assertEqual(exit_code, 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["trigger"], "scheduled")
        self.assertEqual(report["mode"], "dry_run")
        with self.sessions() as session:
            audit = session.execute(select(SuccessRetentionRun)).scalar_one()
        self.assertEqual(audit.trigger, "scheduled")
        self.assertEqual(audit.status, "succeeded")

    def test_runner_audits_stable_failure_without_exception_text(self) -> None:
        runner = SuccessRetentionRunner(
            self.sessions,
            data_root=self.data_root,
            settings=self.settings,
        )
        with (
            patch.object(
                runner.cleaner,
                "cleanup_due_jobs",
                side_effect=OSError("private filesystem detail"),
            ),
            self.assertRaises(OSError),
        ):
            runner.run(trigger="scheduled")
        with self.sessions() as session:
            audit = session.execute(select(SuccessRetentionRun)).scalar_one()
        self.assertEqual(audit.status, "failed")
        self.assertEqual(audit.error_code, "SUCCESS_RETENTION_RUN_FAILED")
        self.assertIsNone(audit.report)

    def test_settings_are_optional_and_validate_ranges(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            settings = SuccessRetentionSettings.from_environment()
        self.assertIsNone(settings.input_retention_days)
        self.assertIsNone(settings.runtime_retention_days)
        self.assertIsNone(settings.artifact_retention_days)
        self.assertFalse(settings.scheduled_dry_run_enabled)
        self.assertEqual(settings.scheduled_dry_run_interval_seconds, 86_400)

        with patch.dict(
            "os.environ",
            {
                "SUCCESS_INPUT_RETENTION_DAYS": "30",
                "SUCCESS_RETENTION_DRY_RUN_ENABLED": "true",
                "SUCCESS_RETENTION_DRY_RUN_INTERVAL_SECONDS": "3600",
            },
            clear=True,
        ):
            scheduled = SuccessRetentionSettings.from_environment()
        self.assertTrue(scheduled.scheduled_dry_run_enabled)
        self.assertEqual(scheduled.scheduled_dry_run_interval_seconds, 3600)

        with self.assertRaisesRegex(ValueError, "SUCCESS_ARTIFACT"):
            SuccessRetentionSettings(artifact_retention_days=0)
        with self.assertRaisesRegex(ValueError, "DRY_RUN_INTERVAL"):
            SuccessRetentionSettings(scheduled_dry_run_interval_seconds=60)
        with self.assertRaisesRegex(ValueError, "at least one retention tier"):
            SuccessRetentionSettings(scheduled_dry_run_enabled=True)


if __name__ == "__main__":
    unittest.main()
