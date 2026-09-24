from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from apps.api.main import AppServices, create_app
from apps.worker.main import main as worker_main
from backend.db.models import Base, ReconstructionJob
from backend.db.queue import JobQueue
from backend.jobs.development import DevelopmentJobService


class ApiWorkerFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data_root = self.root / "data"
        self.database_path = self.root / "platform.sqlite3"
        self.database_url = f"sqlite:///{self.database_path.as_posix()}"
        self.engine = create_engine(
            self.database_url,
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.queue = JobQueue(self.sessions)
        self.queue.ensure_resource("gpu:0")
        services = AppServices(
            self.engine,
            self.queue,
            DevelopmentJobService(self.queue, data_root=self.data_root),
        )
        self.client = TestClient(create_app(services=services, app_env="test"))
        self.user_id = uuid.uuid4()

    def tearDown(self) -> None:
        self.client.close()
        self.engine.dispose()
        self.temporary.cleanup()

    def create_job(self, idempotency_key: str) -> dict:
        response = self.client.post(
            "/api/v1/development/simulated-jobs",
            json={
                "user_id": str(self.user_id),
                "image_count": 3,
                "idempotency_key": idempotency_key,
            },
        )
        self.assertEqual(response.status_code, 201)
        return response.json()

    def test_api_to_database_to_worker_to_api_success(self) -> None:
        created = self.create_job("integration-success-001")
        job_id = created["job_id"]
        self.assertEqual(created["status"], "queued")
        self.assertFalse(created["reused"])

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = worker_main(
                [
                    "run-queued-once",
                    "--database-url",
                    self.database_url,
                    "--data-root",
                    str(self.data_root),
                    "--worker-id",
                    "api-flow-worker",
                    "--lease-seconds",
                    "5",
                    "--heartbeat-seconds",
                    "1",
                ]
            )
        self.assertEqual(exit_code, 0)
        worker_response = json.loads(stdout.getvalue())
        self.assertTrue(worker_response["claimed"])
        self.assertEqual(worker_response["job_id"], job_id)
        self.assertEqual(worker_response["status"], "succeeded")

        response = self.client.get(
            f"/api/v1/development/jobs/{job_id}",
            params={"user_id": str(self.user_id)},
        )
        self.assertEqual(response.status_code, 200)
        completed = response.json()
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["progress"], 100)
        for branch in ("A-v4", "B-v2", "C"):
            artifact = self.data_root / "jobs" / job_id / "output" / branch / "mesh.glb"
            self.assertEqual(artifact.read_bytes()[:4], b"glTF")
        evaluation = json.loads(
            (
                self.data_root / "jobs" / job_id / "reports" / "evaluation.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(evaluation["overall"]["status"], "not_available")
        self.assertIsNone(evaluation["overall"]["score"])

        repeated = self.client.post(
            "/api/v1/development/simulated-jobs",
            json={
                "user_id": str(self.user_id),
                "image_count": 3,
                "idempotency_key": "integration-success-001",
            },
        )
        self.assertEqual(repeated.status_code, 200)
        self.assertTrue(repeated.json()["reused"])
        self.assertEqual(repeated.json()["job_id"], job_id)
        with self.sessions() as session:
            count = session.execute(
                select(func.count()).select_from(ReconstructionJob)
            ).scalar_one()
        self.assertEqual(count, 1)

    def test_user_scope_cancel_and_production_route_guard(self) -> None:
        created = self.create_job("integration-cancel-001")
        job_id = created["job_id"]

        hidden = self.client.get(
            f"/api/v1/development/jobs/{job_id}",
            params={"user_id": str(uuid.uuid4())},
        )
        self.assertEqual(hidden.status_code, 404)
        cancelled = self.client.post(
            f"/api/v1/development/jobs/{job_id}/cancel",
            json={"user_id": str(self.user_id)},
        )
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["status"], "cancelled")

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = worker_main(
                [
                    "run-queued-once",
                    "--database-url",
                    self.database_url,
                    "--data-root",
                    str(self.data_root),
                    "--worker-id",
                    "api-flow-worker",
                    "--lease-seconds",
                    "5",
                    "--heartbeat-seconds",
                    "1",
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "idle")

        production = TestClient(create_app(app_env="production"))
        try:
            response = production.post(
                "/api/v1/development/simulated-jobs",
                json={
                    "user_id": str(self.user_id),
                    "image_count": 3,
                    "idempotency_key": "must-not-exist",
                },
            )
            self.assertEqual(response.status_code, 404)
        finally:
            production.close()


if __name__ == "__main__":
    unittest.main()
