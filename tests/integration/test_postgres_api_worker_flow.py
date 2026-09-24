from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.api.main import AppServices, create_app
from apps.worker.main import main as worker_main
from backend.auth import AuthService, AuthSettings
from backend.db.models import ReconstructionJob, RefreshSession, User, WorkerLease
from backend.db.queue import JobQueue
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

    def test_api_postgresql_worker_and_artifacts_form_a_closed_loop(self) -> None:
        upload_response = self.client.post(
            "/api/v1/uploads",
            json={"idempotency_key": "postgres-upload-loop-001"},
            headers={"Authorization": f"Bearer {self.access_token}"},
        )
        self.assertEqual(upload_response.status_code, 201)
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
            f"/api/v1/development/jobs/{job_id}",
            headers={"Authorization": f"Bearer {self.access_token}"},
        )
        self.assertEqual(completed_response.status_code, 200)
        completed = completed_response.json()
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["progress"], 100)
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

    @staticmethod
    def png_bytes(color: tuple[int, int, int]) -> bytes:
        output = io.BytesIO()
        Image.new("RGB", (64, 48), color).save(output, format="PNG")
        return output.getvalue()


if __name__ == "__main__":
    unittest.main()
