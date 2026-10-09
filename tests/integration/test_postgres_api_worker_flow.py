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
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.api.main import AppServices, create_app
from apps.worker.main import main as worker_main
from backend.auth import AuthService, AuthSettings
from backend.db.models import (
    JobUpload,
    ReconstructionJob,
    RefreshSession,
    TaskActionEvent,
    User,
    WorkerLease,
)
from backend.db.queue import JobQueue
from backend.jobs import TaskSubmissionPolicy, TaskSubmissionSettings
from backend.jobs.development import DevelopmentJobService
from backend.uploads import UploadService
from tests.integration.postgres_support import validated_test_database_url


DATABASE_URL = validated_test_database_url()
TEST_SECRET = "postgres-api-flow-secret-" + "x" * 64


@unittest.skipUnless(DATABASE_URL, "RE3D_TEST_DATABASE_URL is not configured")
class PostgreSQLApiWorkerFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        assert DATABASE_URL is not None
        self.temporary = tempfile.TemporaryDirectory()
        self.data_root = Path(self.temporary.name) / "data"
        self.engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.queue = JobQueue(self.sessions)
        self.resource_key = f"gpu:test-api-{uuid.uuid4().hex[:8]}"
        self.queue.ensure_resource(self.resource_key)
        self.services = AppServices(
            self.engine,
            self.queue,
            DevelopmentJobService(self.queue, data_root=self.data_root),
            AuthService(
                self.sessions,
                AuthSettings(jwt_secret=TEST_SECRET, cookie_secure=False),
            ),
            UploadService(self.sessions, self.queue, data_root=self.data_root),
        )
        self.client = TestClient(create_app(services=self.services, app_env="test"))
        password = "correct horse battery staple"
        registered = self.client.post(
            "/api/v1/auth/register",
            json={
                "username": f"pg-user-{uuid.uuid4().hex[:8]}",
                "email": f"pg-{uuid.uuid4().hex[:12]}@example.com",
                "password": password,
            },
        )
        self.assertEqual(registered.status_code, 201)
        self.user_id = uuid.UUID(registered.json()["id"])
        with self.sessions.begin() as session:
            user = session.get(User, self.user_id)
            assert user is not None
            user.email_verified = True
        logged_in = self.client.post(
            "/api/v1/auth/login",
            json={
                "identifier": registered.json()["username"],
                "password": password,
            },
        )
        self.assertEqual(logged_in.status_code, 200)
        self.access_token = logged_in.json()["access_token"]
        self.job_ids: list[uuid.UUID] = []

    def tearDown(self) -> None:
        self.client.close()
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
            if self.job_ids:
                session.query(ReconstructionJob).filter(
                    ReconstructionJob.id.in_(self.job_ids)
                ).delete(synchronize_session=False)
            session.query(RefreshSession).filter(
                RefreshSession.user_id == self.user_id
            ).delete(synchronize_session=False)
            session.query(User).filter(User.id == self.user_id).delete(
                synchronize_session=False
            )
        self.engine.dispose()
        self.temporary.cleanup()

    def test_refresh_rotation_inserts_successor_before_linking_it(self) -> None:
        response = self.client.post("/api/v1/auth/refresh")
        self.assertEqual(response.status_code, 200, response.text)

        with self.sessions() as session:
            refresh_sessions = list(
                session.query(RefreshSession)
                .filter(RefreshSession.user_id == self.user_id)
                .all()
            )

        self.assertEqual(len(refresh_sessions), 2)
        active = [item for item in refresh_sessions if item.revoked_at is None]
        replaced = [item for item in refresh_sessions if item.revoked_at is not None]
        self.assertEqual(len(active), 1)
        self.assertEqual(len(replaced), 1)
        self.assertEqual(replaced[0].replaced_by_id, active[0].id)

    def test_production_upload_submission_defaults_to_real(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "API_IP_RATE_LIMIT_WINDOW_SECONDS": "60",
                "API_IP_RATE_LIMIT_MAX_REQUESTS": "300",
                "TRANSFER_WINDOW_SECONDS": "3600",
                "TRANSFER_UPLOAD_MAX_CONCURRENT": "2",
                "TRANSFER_UPLOAD_MAX_BYTES": str(2 * 1024**3),
                "TRANSFER_DOWNLOAD_MAX_CONCURRENT": "3",
                "TRANSFER_DOWNLOAD_MAX_BYTES": str(4 * 1024**3),
                "TRANSFER_LEASE_SECONDS": "14400",
            },
            clear=False,
        ):
            production_app = create_app(
                services=self.services,
                app_env="production",
            )
        production = TestClient(production_app)
        headers = {"Authorization": f"Bearer {self.access_token}"}
        try:
            created = production.post(
                "/api/v1/uploads",
                json={"idempotency_key": "postgres-production-real"},
                headers=headers,
            )
            self.assertEqual(created.status_code, 201, created.text)
            upload_id = created.json()["upload_id"]

            for index, color in enumerate(
                ((180, 30, 20), (20, 170, 60), (40, 80, 200))
            ):
                uploaded = production.post(
                    f"/api/v1/uploads/{upload_id}/images",
                    files={
                        "file": (
                            f"production-{index}.png",
                            self.png_bytes(color),
                            "image/png",
                        )
                    },
                    headers=headers,
                )
                self.assertEqual(uploaded.status_code, 201, uploaded.text)

            submitted = production.post(
                f"/api/v1/uploads/{upload_id}/submit",
                headers=headers,
            )
            self.assertEqual(submitted.status_code, 202, submitted.text)
            self.assertEqual(submitted.json()["execution_mode"], "real")
            self.job_ids.append(uuid.UUID(submitted.json()["job_id"]))
        finally:
            production.close()

    def test_api_postgresql_worker_and_artifacts_form_a_closed_loop(self) -> None:
        upload_response = self.client.post(
            "/api/v1/uploads",
            json={"idempotency_key": "postgres-upload-loop-001"},
            headers={"Authorization": f"Bearer {self.access_token}"},
        )
        self.assertEqual(
            upload_response.status_code,
            201,
            upload_response.text,
        )
        upload_id = upload_response.json()["upload_id"]
        for index, color in enumerate(
            ((180, 30, 20), (20, 170, 60), (40, 80, 200))
        ):
            image_response = self.client.post(
                f"/api/v1/uploads/{upload_id}/images",
                files={
                    "file": (
                        f"postgres-{index}.png",
                        self.png_bytes(color),
                        "image/png",
                    )
                },
                headers={"Authorization": f"Bearer {self.access_token}"},
            )
            self.assertEqual(image_response.status_code, 201)

        created_response = self.client.post(
            f"/api/v1/uploads/{upload_id}/submit",
            headers={"Authorization": f"Bearer {self.access_token}"},
        )
        self.assertEqual(created_response.status_code, 202)
        created = created_response.json()
        job_id = uuid.UUID(created["job_id"])
        self.job_ids.append(job_id)
        self.assertEqual(created["status"], "queued")

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = worker_main(
                [
                    "run-queued-once",
                    "--database-url",
                    DATABASE_URL,
                    "--data-root",
                    str(self.data_root),
                    "--worker-id",
                    "postgres-api-worker",
                    "--resource-key",
                    self.resource_key,
                    "--lease-seconds",
                    "5",
                    "--heartbeat-seconds",
                    "1",
                ]
            )
        self.assertEqual(exit_code, 0)
        worker_result = json.loads(stdout.getvalue())
        self.assertEqual(worker_result["job_id"], str(job_id))
        self.assertEqual(worker_result["status"], "succeeded")

        completed_response = self.client.get(
            f"/api/v1/jobs/{job_id}",
            headers={"Authorization": f"Bearer {self.access_token}"},
        )
        self.assertEqual(completed_response.status_code, 200)
        completed = completed_response.json()
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["progress"], 100)
        detail_response = self.client.get(
            f"/api/v1/jobs/{job_id}/detail",
            headers={"Authorization": f"Bearer {self.access_token}"},
        )
        self.assertEqual(detail_response.status_code, 200)
        detail = detail_response.json()
        self.assertEqual(detail["detail_state"], "available")
        self.assertEqual(detail["evaluation"]["overall_status"], "not_available")
        self.assertEqual(len(detail["result"]["branches"]), 3)
        artifact_url = detail["result"]["branches"][0]["artifacts"][0][
            "download_url"
        ]
        downloaded = self.client.get(
            artifact_url,
            headers={"Authorization": f"Bearer {self.access_token}"},
        )
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.content[:4], b"glTF")
        for branch in ("A-v4", "B-v2", "C"):
            artifact = (
                self.data_root
                / "jobs"
                / str(job_id)
                / "output"
                / branch
                / "mesh.glb"
            )
            self.assertEqual(artifact.read_bytes()[:4], b"glTF")
        evaluation = json.loads(
            (
                self.data_root
                / "jobs"
                / str(job_id)
                / "reports"
                / "evaluation.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(evaluation["job_id"], str(job_id))
        self.assertEqual(evaluation["overall"]["status"], "not_available")

    def test_postgresql_upload_delete_cancel_and_expiry_cleanup(self) -> None:
        headers = {"Authorization": f"Bearer {self.access_token}"}
        created = self.client.post(
            "/api/v1/uploads",
            json={"idempotency_key": "postgres-lifecycle-001"},
            headers=headers,
        ).json()
        upload_id = created["upload_id"]
        uploaded = self.client.post(
            f"/api/v1/uploads/{upload_id}/images",
            files={
                "file": (
                    "postgres-delete.png",
                    self.png_bytes((120, 40, 30)),
                    "image/png",
                )
            },
            headers=headers,
        ).json()
        deleted = self.client.delete(
            f"/api/v1/uploads/{upload_id}/images/{uploaded['images'][0]['id']}",
            headers=headers,
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()["image_count"], 0)
        cancelled = self.client.post(
            f"/api/v1/uploads/{upload_id}/cancel",
            headers=headers,
        )
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["cancellation_reason"], "user")
        self.assertTrue(cancelled.json()["storage_removed"])

        stale = self.client.post(
            "/api/v1/uploads",
            json={"idempotency_key": "postgres-lifecycle-stale-001"},
            headers=headers,
        ).json()
        now = datetime.now(timezone.utc)
        with self.sessions.begin() as session:
            record = session.get(JobUpload, uuid.UUID(stale["upload_id"]))
            assert record is not None
            record.updated_at = now - timedelta(hours=2)
        report = self.services.uploads.cleanup_stale(
            now=now,
            stale_after_hours=1,
        )
        self.assertEqual(report["expired"], 1)
        expired = self.client.get(
            f"/api/v1/uploads/{stale['upload_id']}",
            headers=headers,
        ).json()
        self.assertEqual(expired["status"], "cancelled")
        self.assertEqual(expired["cancellation_reason"], "expired")
        self.assertIsNotNone(expired["storage_cleaned_at"])

    def test_postgresql_submission_limits_and_task_audit(self) -> None:
        self.services.uploads.submission_policy = TaskSubmissionPolicy(
            TaskSubmissionSettings(
                max_pending_jobs=1,
                max_submissions_per_24h=2,
            )
        )
        headers = {"Authorization": f"Bearer {self.access_token}"}

        first_id = self.prepare_upload(
            token="postgres-limit-001",
            color_offset=10,
        )
        first = self.client.post(
            f"/api/v1/uploads/{first_id}/submit",
            headers=headers,
        )
        self.assertEqual(first.status_code, 202, first.text)
        self.job_ids.append(first_id)

        second_id = self.prepare_upload(
            token="postgres-limit-002",
            color_offset=30,
        )
        pending = self.client.post(
            f"/api/v1/uploads/{second_id}/submit",
            headers=headers,
        )
        self.assertEqual(pending.status_code, 429, pending.text)
        self.assertEqual(
            pending.headers["x-re3d-error-code"],
            "PENDING_JOB_LIMIT_REACHED",
        )
        self.assertNotIn("retry-after", pending.headers)
        with self.sessions() as session:
            self.assertIsNone(session.get(ReconstructionJob, second_id))
            second_upload = session.get(JobUpload, second_id)
            assert second_upload is not None
            self.assertEqual(second_upload.status, "uploading")

        first_cancelled = self.client.post(
            f"/api/v1/jobs/{first_id}/cancel",
            headers=headers,
        )
        self.assertEqual(first_cancelled.status_code, 200)
        self.assertEqual(first_cancelled.json()["status"], "cancelled")

        second = self.client.post(
            f"/api/v1/uploads/{second_id}/submit",
            headers=headers,
        )
        self.assertEqual(second.status_code, 202, second.text)
        self.job_ids.append(second_id)
        repeated = self.client.post(
            f"/api/v1/uploads/{second_id}/submit",
            headers=headers,
        )
        self.assertEqual(repeated.status_code, 202, repeated.text)
        self.assertEqual(repeated.json()["job_id"], str(second_id))

        second_cancelled = self.client.post(
            f"/api/v1/jobs/{second_id}/cancel",
            headers=headers,
        )
        self.assertEqual(second_cancelled.status_code, 200)

        third_id = self.prepare_upload(
            token="postgres-limit-003",
            color_offset=50,
        )
        daily = self.client.post(
            f"/api/v1/uploads/{third_id}/submit",
            headers=headers,
        )
        self.assertEqual(daily.status_code, 429, daily.text)
        self.assertEqual(
            daily.headers["x-re3d-error-code"],
            "SUBMISSION_WINDOW_LIMIT_REACHED",
        )
        self.assertGreater(int(daily.headers["retry-after"]), 0)

        with self.sessions() as session:
            events = list(
                session.query(TaskActionEvent)
                .filter(TaskActionEvent.user_id == self.user_id)
                .all()
            )
            jobs = list(
                session.query(ReconstructionJob)
                .filter(ReconstructionJob.user_id == self.user_id)
                .all()
            )
        self.assertEqual(len(jobs), 2)
        self.assertEqual(
            Counter(event.action for event in events),
            Counter({"job_submitted": 2, "cancel_requested": 2}),
        )

    def prepare_upload(self, *, token: str, color_offset: int) -> uuid.UUID:
        headers = {"Authorization": f"Bearer {self.access_token}"}
        created = self.client.post(
            "/api/v1/uploads",
            json={"idempotency_key": token},
            headers=headers,
        )
        self.assertEqual(created.status_code, 201, created.text)
        upload_id = uuid.UUID(created.json()["upload_id"])
        for index in range(3):
            uploaded = self.client.post(
                f"/api/v1/uploads/{upload_id}/images",
                files={
                    "file": (
                        f"postgres-limit-{index}.png",
                        self.png_bytes(
                            (
                                color_offset + index,
                                70 + index,
                                100 + index,
                            )
                        ),
                        "image/png",
                    )
                },
                headers=headers,
            )
            self.assertEqual(uploaded.status_code, 201, uploaded.text)
        return upload_id

    @staticmethod
    def png_bytes(color: tuple[int, int, int]) -> bytes:
        output = io.BytesIO()
        Image.new("RGB", (64, 48), color).save(output, format="PNG")
        return output.getvalue()


if __name__ == "__main__":
    unittest.main()
