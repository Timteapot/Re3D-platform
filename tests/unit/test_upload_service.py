from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.worker.main import main as worker_main
from backend.db.models import Base, JobUpload, TaskActionEvent, User
from backend.db.queue import JobQueue
from backend.jobs import (
    PENDING_LIMIT_CODE,
    SUBMISSION_WINDOW_LIMIT_CODE,
    TaskSubmissionLimitError,
    TaskSubmissionSettings,
)
from backend.uploads import (
    StorageQuotaExceededError,
    StorageQuotaSettings,
    UploadCapacityError,
    UploadConflictError,
    UploadNotFoundError,
    UploadService,
    UploadSettings,
    UploadTooLargeError,
    UploadValidationError,
)


class UploadServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data_root = self.root / "data"
        database_path = (self.root / "db.sqlite3").as_posix()
        self.database_url = f"sqlite:///{database_path}"
        self.engine = create_engine(self.database_url)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.user_id = uuid.uuid4()
        with self.sessions.begin() as session:
            session.add(
                User(
                    id=self.user_id,
                    username="upload-unit-user",
                    email="upload-unit@example.com",
                    password_hash="not-used-by-upload-tests",
                )
            )
        self.queue = JobQueue(self.sessions)
        self.service = UploadService(
            self.sessions,
            self.queue,
            data_root=self.data_root,
            settings=UploadSettings(
                max_file_bytes=4096,
                max_total_bytes=20_000,
                max_pixels=1_000_000,
            ),
        )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary.cleanup()

    def test_idempotent_creation_duplicate_and_size_limit(self) -> None:
        upload, reused = self.service.create(
            user_id=self.user_id,
            idempotency_token="upload-unit-001",
        )
        self.assertFalse(reused)
        repeated, reused = self.service.create(
            user_id=self.user_id,
            idempotency_token="upload-unit-001",
        )
        self.assertTrue(reused)
        self.assertEqual(repeated["upload_id"], upload["upload_id"])

        payload = self.png_bytes((100, 20, 30))
        current = self.service.add_image(
            upload_id=upload["upload_id"],
            user_id=self.user_id,
            source=io.BytesIO(payload),
            original_name="../camera.png",
        )
        self.assertEqual(current["image_count"], 1)
        self.assertEqual(current["images"][0]["original_name"], "camera.png")
        with self.assertRaises(UploadConflictError):
            self.service.add_image(
                upload_id=upload["upload_id"],
                user_id=self.user_id,
                source=io.BytesIO(payload),
                original_name="duplicate.png",
            )
        with self.assertRaises(UploadTooLargeError):
            self.service.add_image(
                upload_id=upload["upload_id"],
                user_id=self.user_id,
                source=io.BytesIO(b"x" * 4097),
                original_name="oversized.jpg",
            )
        staging = (
            self.data_root
            / "jobs"
            / str(upload["upload_id"])
            / "input"
            / "staging"
        )
        self.assertEqual(list(staging.iterdir()), [])

    def test_storage_floor_rejects_new_data_and_removes_staging_file(self) -> None:
        minimum = 1024**3
        service = UploadService(
            self.sessions,
            self.queue,
            data_root=self.data_root,
            settings=UploadSettings(
                max_file_bytes=4096,
                max_total_bytes=20_000,
                max_pixels=1_000_000,
                min_free_disk_bytes=minimum,
            ),
        )
        with patch(
            "backend.uploads.service.shutil.disk_usage",
            return_value=SimpleNamespace(free=minimum + 4095),
        ):
            with self.assertRaises(UploadCapacityError):
                service.create(
                    user_id=self.user_id,
                    idempotency_token="capacity-create-001",
                )

        with patch(
            "backend.uploads.service.shutil.disk_usage",
            return_value=SimpleNamespace(free=minimum + 4096),
        ):
            upload, _ = service.create(
                user_id=self.user_id,
                idempotency_token="capacity-upload-001",
            )

        with patch(
            "backend.uploads.service.shutil.disk_usage",
            side_effect=(
                SimpleNamespace(free=minimum + 4096),
                SimpleNamespace(free=minimum - 1),
            ),
        ):
            with self.assertRaises(UploadCapacityError):
                service.add_image(
                    upload_id=upload["upload_id"],
                    user_id=self.user_id,
                    source=io.BytesIO(self.png_bytes((50, 60, 70))),
                    original_name="capacity.png",
                )

        current = service.get(
            upload_id=upload["upload_id"],
            user_id=self.user_id,
        )
        self.assertEqual(current["image_count"], 0)
        staging = (
            self.data_root
            / "jobs"
            / str(upload["upload_id"])
            / "input"
            / "staging"
        )
        self.assertEqual(list(staging.iterdir()), [])

    def test_detects_actual_jpeg_and_rejects_unsupported_image_format(self) -> None:
        upload, _ = self.service.create(
            user_id=self.user_id,
            idempotency_token="upload-unit-formats",
        )
        jpeg = self.image_bytes((60, 90, 120), "JPEG")
        current = self.service.add_image(
            upload_id=upload["upload_id"],
            user_id=self.user_id,
            source=io.BytesIO(jpeg),
            original_name="claimed-as-png.png",
        )
        self.assertEqual(current["images"][0]["content_type"], "image/jpeg")

        gif = self.image_bytes((20, 40, 60), "GIF")
        with self.assertRaises(UploadValidationError):
            self.service.add_image(
                upload_id=upload["upload_id"],
                user_id=self.user_id,
                source=io.BytesIO(gif),
                original_name="unsupported.gif",
            )

    def test_submission_requires_three_untampered_images(self) -> None:
        upload, _ = self.service.create(
            user_id=self.user_id,
            idempotency_token="upload-unit-002",
        )
        for color in ((200, 10, 10), (10, 200, 10), (10, 10, 200)):
            self.service.add_image(
                upload_id=upload["upload_id"],
                user_id=self.user_id,
                source=io.BytesIO(self.png_bytes(color)),
                original_name="camera.png",
            )
        image_path = next(
            (
                self.data_root
                / "jobs"
                / str(upload["upload_id"])
                / "input"
                / "images"
            ).iterdir()
        )
        image_path.write_bytes(b"tampered")
        with self.assertRaises(UploadValidationError):
            self.service.submit(
                upload_id=upload["upload_id"],
                user_id=self.user_id,
            )
        current = self.service.get(
            upload_id=upload["upload_id"],
            user_id=self.user_id,
        )
        self.assertEqual(current["status"], "uploading")

    def test_submission_can_explicitly_create_a_real_queue_job(self) -> None:
        upload, _ = self.service.create(
            user_id=self.user_id,
            idempotency_token="upload-unit-real-mode",
        )
        for index, color in enumerate(((90, 10, 10), (10, 90, 10), (10, 10, 90))):
            self.service.add_image(
                upload_id=upload["upload_id"],
                user_id=self.user_id,
                source=io.BytesIO(self.png_bytes(color)),
                original_name=f"real-{index}.png",
            )

        job = self.service.submit(
            upload_id=upload["upload_id"],
            user_id=self.user_id,
            execution_mode="real",
        )

        self.assertEqual(job["execution_mode"], "real")
        request = json.loads(
            (
                self.data_root
                / "jobs"
                / str(upload["upload_id"])
                / "manifests"
                / "pipeline-request.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(request["execution_mode"], "real")
        with self.assertRaises(UploadConflictError):
            self.service.submit(
                upload_id=upload["upload_id"],
                user_id=self.user_id,
                execution_mode="simulated",
            )

    def test_submission_limits_and_task_action_audit(self) -> None:
        service = UploadService(
            self.sessions,
            self.queue,
            data_root=self.data_root,
            settings=UploadSettings(
                max_file_bytes=4096,
                max_total_bytes=20_000,
                max_pixels=1_000_000,
            ),
            submission_settings=TaskSubmissionSettings(
                max_pending_jobs=1,
                max_submissions_per_24h=2,
            ),
        )

        first = self._prepare_upload(service, "limited-submission-001")
        first_job = service.submit(
            upload_id=first["upload_id"],
            user_id=self.user_id,
        )

        second = self._prepare_upload(service, "limited-submission-002")
        with self.assertRaises(TaskSubmissionLimitError) as pending_context:
            service.submit(
                upload_id=second["upload_id"],
                user_id=self.user_id,
            )
        self.assertEqual(
            pending_context.exception.reason_code,
            PENDING_LIMIT_CODE,
        )
        self.assertEqual(
            service.get(
                upload_id=second["upload_id"],
                user_id=self.user_id,
            )["status"],
            "uploading",
        )

        self.queue.request_cancel(
            first_job["id"],
            user_id=self.user_id,
            audit=True,
        )
        second_job = service.submit(
            upload_id=second["upload_id"],
            user_id=self.user_id,
        )
        self.queue.request_cancel(
            second_job["id"],
            user_id=self.user_id,
            audit=True,
        )
        # Repeating an already-applied cancellation is idempotent and does not
        # append another audit row.
        self.queue.request_cancel(
            second_job["id"],
            user_id=self.user_id,
            audit=True,
        )

        third = self._prepare_upload(service, "limited-submission-003")
        with self.assertRaises(TaskSubmissionLimitError) as daily_context:
            service.submit(
                upload_id=third["upload_id"],
                user_id=self.user_id,
            )
        self.assertEqual(
            daily_context.exception.reason_code,
            SUBMISSION_WINDOW_LIMIT_CODE,
        )
        self.assertIsNotNone(daily_context.exception.retry_after_seconds)

        with self.sessions() as session:
            events = list(
                session.query(TaskActionEvent)
                .order_by(TaskActionEvent.occurred_at, TaskActionEvent.id)
            )
        self.assertEqual(
            Counter(event.action for event in events),
            Counter({"job_submitted": 2, "cancel_requested": 2}),
        )

    def test_user_storage_quota_aggregates_uploads_and_reserves_jobs(self) -> None:
        service = UploadService(
            self.sessions,
            self.queue,
            data_root=self.data_root,
            settings=UploadSettings(
                max_file_bytes=1024,
                max_total_bytes=1024,
                max_pixels=1_000_000,
            ),
            quota_settings=StorageQuotaSettings(
                quota_bytes=1536 * 1024**2,
                job_reservation_bytes=1024**3,
            ),
        )

        first = self._prepare_upload(service, "quota-first-upload")
        first = service.get(
            upload_id=first["upload_id"],
            user_id=self.user_id,
        )
        first_usage = service.storage_quota(user_id=self.user_id)
        self.assertEqual(first_usage["upload_bytes"], first["total_bytes"])
        self.assertEqual(first_usage["reserved_job_bytes"], 0)

        submitted = service.submit(
            upload_id=first["upload_id"],
            user_id=self.user_id,
        )
        self.assertEqual(submitted["storage_reserved_bytes"], 1024**3)
        submitted_usage = service.storage_quota(user_id=self.user_id)
        self.assertEqual(submitted_usage["upload_bytes"], 0)
        self.assertEqual(submitted_usage["reserved_job_bytes"], 1024**3)

        second = self._prepare_upload(service, "quota-second-upload")
        with self.assertRaises(StorageQuotaExceededError):
            service.submit(
                upload_id=second["upload_id"],
                user_id=self.user_id,
            )
        current = service.get(
            upload_id=second["upload_id"],
            user_id=self.user_id,
        )
        self.assertEqual(current["status"], "uploading")
        self.assertFalse(
            (
                self.data_root
                / "jobs"
                / str(second["upload_id"])
                / "manifests"
                / "pipeline-request.json"
            ).exists()
        )

        cancelled = service.cancel(
            upload_id=second["upload_id"],
            user_id=self.user_id,
        )
        self.assertTrue(cancelled["storage_removed"])
        released = service.storage_quota(user_id=self.user_id)
        self.assertEqual(released["used_bytes"], 1024**3)

    def test_quota_settings_are_explicit_in_production(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(
                ValueError,
                "RE3D_USER_STORAGE_QUOTA_BYTES",
            ):
                StorageQuotaSettings.from_environment(environment="production")

        with patch.dict(
            "os.environ",
            {
                "RE3D_USER_STORAGE_QUOTA_BYTES": "4096",
                "RE3D_JOB_STORAGE_RESERVATION_BYTES": "2048",
            },
            clear=True,
        ):
            settings = StorageQuotaSettings.from_environment(
                environment="production"
            )
        self.assertEqual(settings.quota_bytes, 4096)
        self.assertEqual(settings.job_reservation_bytes, 2048)

        with self.assertRaisesRegex(ValueError, "must not exceed"):
            StorageQuotaSettings(
                quota_bytes=1024,
                job_reservation_bytes=2048,
            )
        with self.assertRaisesRegex(ValueError, "at least 1 GiB"):
            UploadService(
                self.sessions,
                self.queue,
                data_root=self.data_root,
                settings=UploadSettings(
                    max_file_bytes=1024,
                    max_total_bytes=1024,
                    max_pixels=1_000_000,
                ),
                quota_settings=StorageQuotaSettings(
                    quota_bytes=4096,
                    job_reservation_bytes=2048,
                ),
            )

    def _prepare_upload(
        self,
        service: UploadService,
        idempotency_token: str,
    ) -> dict:
        upload, _ = service.create(
            user_id=self.user_id,
            idempotency_token=idempotency_token,
        )
        for index, color in enumerate(((70, 10, 10), (10, 70, 10), (10, 10, 70))):
            service.add_image(
                upload_id=upload["upload_id"],
                user_id=self.user_id,
                source=io.BytesIO(self.png_bytes(color)),
                original_name=f"limited-{index}.png",
            )
        return upload

    def test_delete_image_and_cancel_remove_mutable_upload_storage(self) -> None:
        upload, _ = self.service.create(
            user_id=self.user_id,
            idempotency_token="upload-unit-delete",
        )
        first = self.service.add_image(
            upload_id=upload["upload_id"],
            user_id=self.user_id,
            source=io.BytesIO(self.png_bytes((80, 20, 20))),
            original_name="first.png",
        )
        current = self.service.add_image(
            upload_id=upload["upload_id"],
            user_id=self.user_id,
            source=io.BytesIO(self.png_bytes((20, 80, 20))),
            original_name="second.png",
        )
        remaining = self.service.delete_image(
            upload_id=upload["upload_id"],
            image_id=first["images"][0]["id"],
            user_id=self.user_id,
        )
        self.assertEqual(remaining["image_count"], 1)
        self.assertEqual(remaining["total_bytes"], current["images"][1]["size_bytes"])
        with self.assertRaises(UploadNotFoundError):
            self.service.delete_image(
                upload_id=upload["upload_id"],
                image_id=uuid.uuid4(),
                user_id=self.user_id,
            )

        task_root = self.data_root / "jobs" / str(upload["upload_id"])
        cancelled = self.service.cancel(
            upload_id=upload["upload_id"],
            user_id=self.user_id,
        )
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["cancellation_reason"], "user")
        self.assertTrue(cancelled["storage_removed"])
        self.assertIsNotNone(cancelled["storage_cleaned_at"])
        self.assertFalse(task_root.exists())
        with self.assertRaises(UploadConflictError):
            self.service.add_image(
                upload_id=upload["upload_id"],
                user_id=self.user_id,
                source=io.BytesIO(self.png_bytes((20, 20, 80))),
                original_name="too-late.png",
            )

    def test_cleanup_expires_stale_uploads_and_retries_storage_removal(self) -> None:
        stale, _ = self.service.create(
            user_id=self.user_id,
            idempotency_token="upload-unit-stale",
        )
        recent, _ = self.service.create(
            user_id=self.user_id,
            idempotency_token="upload-unit-recent",
        )
        retry, _ = self.service.create(
            user_id=self.user_id,
            idempotency_token="upload-unit-retry",
        )
        retry = self.service.add_image(
            upload_id=retry["upload_id"],
            user_id=self.user_id,
            source=io.BytesIO(self.png_bytes((30, 40, 50))),
            original_name="retry.png",
        )
        now = datetime.now(timezone.utc)
        with self.sessions.begin() as session:
            stale_record = session.get(JobUpload, stale["upload_id"])
            assert stale_record is not None
            stale_record.updated_at = now - timedelta(hours=2)

        with self.assertLogs("backend.uploads.service", level="ERROR"):
            with patch(
                "backend.uploads.service._remove_upload_directory",
                side_effect=OSError("simulated cleanup failure"),
            ):
                cancelled = self.service.cancel(
                    upload_id=retry["upload_id"],
                    user_id=self.user_id,
                )
        self.assertFalse(cancelled["storage_removed"])
        self.assertIsNone(cancelled["storage_cleaned_at"])
        self.assertEqual(
            self.service.storage_quota(user_id=self.user_id)["upload_bytes"],
            retry["total_bytes"],
        )

        report = self.service.cleanup_stale(
            now=now,
            stale_after_hours=1,
            limit=10,
        )
        self.assertEqual(report["scanned"], 2)
        self.assertEqual(report["expired"], 1)
        self.assertEqual(report["storage_cleaned"], 2)
        self.assertEqual(report["storage_cleanup_failures"], [])
        self.assertEqual(
            self.service.storage_quota(user_id=self.user_id)["upload_bytes"],
            0,
        )
        self.assertEqual(
            self.service.get(
                upload_id=stale["upload_id"],
                user_id=self.user_id,
            )["cancellation_reason"],
            "expired",
        )
        self.assertEqual(
            self.service.get(
                upload_id=recent["upload_id"],
                user_id=self.user_id,
            )["status"],
            "uploading",
        )
        self.assertTrue(
            (self.data_root / "jobs" / str(recent["upload_id"])).is_dir()
        )

    def test_cleanup_command_reports_expired_upload(self) -> None:
        upload, _ = self.service.create(
            user_id=self.user_id,
            idempotency_token="upload-unit-cli-cleanup",
        )
        with self.sessions.begin() as session:
            record = session.get(JobUpload, upload["upload_id"])
            assert record is not None
            record.updated_at = datetime.now(timezone.utc) - timedelta(hours=2)

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = worker_main(
                [
                    "cleanup-stale-uploads",
                    "--database-url",
                    self.database_url,
                    "--data-root",
                    str(self.data_root),
                    "--stale-after-hours",
                    "1",
                    "--limit",
                    "10",
                ]
            )
        self.assertEqual(exit_code, 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["operation"], "cleanup-stale-uploads")
        self.assertEqual(report["expired"], 1)
        self.assertEqual(report["storage_cleaned"], 1)

    @staticmethod
    def png_bytes(color: tuple[int, int, int]) -> bytes:
        return UploadServiceTests.image_bytes(color, "PNG")

    @staticmethod
    def image_bytes(color: tuple[int, int, int], image_format: str) -> bytes:
        output = io.BytesIO()
        Image.new("RGB", (64, 48), color).save(output, format=image_format)
        return output.getvalue()


if __name__ == "__main__":
    unittest.main()
