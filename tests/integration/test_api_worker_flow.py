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
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from apps.api.main import AppServices, create_app
from apps.worker.main import main as worker_main
from backend.auth import AuthService, AuthSettings
from backend.db.models import Base, ReconstructionJob
from backend.db.queue import JobQueue
from backend.jobs.development import DevelopmentJobService
from backend.uploads import UploadService


TEST_SECRET = "api-flow-test-secret-" + "x" * 64


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
        self.user_id, self.access_token = self.register_and_login(
            username="api-owner",
            email="api-owner@example.com",
        )

    def tearDown(self) -> None:
        self.client.close()
        self.engine.dispose()
        self.temporary.cleanup()

    def create_job(self, idempotency_key: str) -> dict:
        response = self.client.post(
            "/api/v1/development/simulated-jobs",
            json={
                "image_count": 3,
                "idempotency_key": idempotency_key,
            },
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(response.status_code, 201)
        return response.json()

    def register_and_login(self, *, username: str, email: str) -> tuple[uuid.UUID, str]:
        password = "correct horse battery staple"
        registered = self.client.post(
            "/api/v1/auth/register",
            json={"username": username, "email": email, "password": password},
        )
        self.assertEqual(registered.status_code, 201)
        logged_in = self.client.post(
            "/api/v1/auth/login",
            json={"identifier": username, "password": password},
        )
        self.assertEqual(logged_in.status_code, 200)
        self.assertNotIn("refresh_token", logged_in.json())
        refresh_cookie = logged_in.headers["set-cookie"].lower()
        self.assertIn("httponly", refresh_cookie)
        self.assertIn("samesite=lax", refresh_cookie)
        self.assertIn("path=/api/v1/auth", refresh_cookie)
        return uuid.UUID(registered.json()["id"]), logged_in.json()["access_token"]

    @staticmethod
    def auth_headers(access_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token}"}

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
            headers=self.auth_headers(self.access_token),
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
                "image_count": 3,
                "idempotency_key": "integration-success-001",
            },
            headers=self.auth_headers(self.access_token),
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

        _, other_access_token = self.register_and_login(
            username="different-owner",
            email="different-owner@example.com",
        )
        hidden = self.client.get(
            f"/api/v1/development/jobs/{job_id}",
            headers=self.auth_headers(other_access_token),
        )
        self.assertEqual(hidden.status_code, 404)
        cancelled = self.client.post(
            f"/api/v1/development/jobs/{job_id}/cancel",
            headers=self.auth_headers(self.access_token),
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

        production = TestClient(
            create_app(services=self.services, app_env="production")
        )
        try:
            response = production.post(
                "/api/v1/development/simulated-jobs",
                json={
                    "image_count": 3,
                    "idempotency_key": "must-not-exist",
                },
                headers=self.auth_headers(self.access_token),
            )
            self.assertEqual(response.status_code, 404)
            upload_response = production.post(
                "/api/v1/uploads",
                json={"idempotency_key": "must-not-upload"},
                headers=self.auth_headers(self.access_token),
            )
            self.assertEqual(upload_response.status_code, 404)
            self.assertEqual(production.get("/api/v1/auth/me").status_code, 401)
        finally:
            production.close()

    def test_auth_refresh_rotation_logout_and_required_bearer(self) -> None:
        missing = self.client.post(
            "/api/v1/development/simulated-jobs",
            json={"image_count": 3, "idempotency_key": "missing-auth-001"},
        )
        self.assertEqual(missing.status_code, 401)

        me = self.client.get(
            "/api/v1/auth/me",
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(me.status_code, 200)
        self.assertEqual(uuid.UUID(me.json()["id"]), self.user_id)

        cookie_name = self.services.auth.settings.refresh_cookie_name
        old_refresh = self.client.cookies.get(cookie_name)
        self.assertIsNotNone(old_refresh)
        refreshed = self.client.post("/api/v1/auth/refresh")
        self.assertEqual(refreshed.status_code, 200)
        self.assertNotEqual(self.client.cookies.get(cookie_name), old_refresh)
        self.assertEqual(refreshed.headers["cache-control"], "no-store")

        replay = TestClient(create_app(services=self.services, app_env="test"))
        try:
            replay.cookies.set(
                cookie_name,
                old_refresh,
                path="/api/v1/auth",
            )
            self.assertEqual(replay.post("/api/v1/auth/refresh").status_code, 401)
        finally:
            replay.close()

        logged_out = self.client.post("/api/v1/auth/logout")
        self.assertEqual(logged_out.status_code, 204)
        self.assertIsNone(self.client.cookies.get(cookie_name))
        self.assertEqual(self.client.post("/api/v1/auth/refresh").status_code, 401)

    def test_authenticated_image_upload_submission_and_user_scope(self) -> None:
        created = self.client.post(
            "/api/v1/uploads",
            json={"idempotency_key": "browser-upload-flow-001"},
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(created.status_code, 201)
        upload_id = created.json()["upload_id"]
        first_payload = self.png_bytes((190, 40, 30))

        for index, payload in enumerate(
            (
                first_payload,
                self.png_bytes((30, 160, 90)),
                self.png_bytes((50, 90, 210)),
            )
        ):
            uploaded = self.client.post(
                f"/api/v1/uploads/{upload_id}/images",
                files={
                    "file": (
                        f"../../camera-{index}.png",
                        payload,
                        "image/png",
                    )
                },
                headers=self.auth_headers(self.access_token),
            )
            self.assertEqual(uploaded.status_code, 201)
            self.assertEqual(uploaded.json()["image_count"], index + 1)

        duplicate = self.client.post(
            f"/api/v1/uploads/{upload_id}/images",
            files={"file": ("duplicate.png", first_payload, "image/png")},
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(duplicate.status_code, 409)

        _, other_access_token = self.register_and_login(
            username="upload-outsider",
            email="upload-outsider@example.com",
        )
        hidden = self.client.get(
            f"/api/v1/uploads/{upload_id}",
            headers=self.auth_headers(other_access_token),
        )
        self.assertEqual(hidden.status_code, 404)

        submitted = self.client.post(
            f"/api/v1/uploads/{upload_id}/submit",
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(submitted.status_code, 202)
        self.assertEqual(submitted.json()["job_id"], upload_id)
        self.assertEqual(submitted.json()["status"], "queued")
        self.assertEqual(submitted.json()["execution_mode"], "simulated")

        resubmitted = self.client.post(
            f"/api/v1/uploads/{upload_id}/submit",
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(resubmitted.status_code, 202)
        self.assertEqual(resubmitted.json()["job_id"], upload_id)
        late_image = self.client.post(
            f"/api/v1/uploads/{upload_id}/images",
            files={
                "file": (
                    "late.png",
                    self.png_bytes((10, 10, 10)),
                    "image/png",
                )
            },
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(late_image.status_code, 409)

        listed = self.client.get(
            "/api/v1/development/jobs",
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(listed.status_code, 200)
        self.assertIn(upload_id, [job["job_id"] for job in listed.json()])

        task_root = self.data_root / "jobs" / upload_id
        manifest = json.loads(
            (task_root / "input" / "input-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(manifest["source"], "user_upload")
        self.assertFalse(manifest["simulation"])
        self.assertEqual(len(manifest["images"]), 3)
        self.assertTrue(
            all("/" not in image["original_name"] for image in manifest["images"])
        )

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
                    "uploaded-input-worker",
                    "--lease-seconds",
                    "5",
                    "--heartbeat-seconds",
                    "1",
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "succeeded")

    def test_invalid_or_incomplete_upload_is_not_queued(self) -> None:
        created = self.client.post(
            "/api/v1/uploads",
            json={"idempotency_key": "invalid-upload-flow-001"},
            headers=self.auth_headers(self.access_token),
        )
        upload_id = created.json()["upload_id"]
        invalid = self.client.post(
            f"/api/v1/uploads/{upload_id}/images",
            files={"file": ("not-image.jpg", b"not an image", "image/jpeg")},
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(invalid.status_code, 422)
        current = self.client.get(
            f"/api/v1/uploads/{upload_id}",
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(current.json()["image_count"], 0)
        submitted = self.client.post(
            f"/api/v1/uploads/{upload_id}/submit",
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(submitted.status_code, 422)
        self.assertEqual(
            self.client.get(
                "/api/v1/development/jobs",
                headers=self.auth_headers(self.access_token),
            ).json(),
            [],
        )

    def test_upload_image_delete_cancel_and_owner_scope(self) -> None:
        created = self.client.post(
            "/api/v1/uploads",
            json={"idempotency_key": "cancel-upload-flow-001"},
            headers=self.auth_headers(self.access_token),
        )
        upload_id = created.json()["upload_id"]
        uploaded = self.client.post(
            f"/api/v1/uploads/{upload_id}/images",
            files={
                "file": (
                    "removable.png",
                    self.png_bytes((90, 30, 20)),
                    "image/png",
                )
            },
            headers=self.auth_headers(self.access_token),
        )
        image_id = uploaded.json()["images"][0]["id"]

        _, other_access_token = self.register_and_login(
            username="upload-delete-outsider",
            email="upload-delete-outsider@example.com",
        )
        hidden = self.client.delete(
            f"/api/v1/uploads/{upload_id}/images/{image_id}",
            headers=self.auth_headers(other_access_token),
        )
        self.assertEqual(hidden.status_code, 404)

        deleted = self.client.delete(
            f"/api/v1/uploads/{upload_id}/images/{image_id}",
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()["image_count"], 0)

        cancelled = self.client.post(
            f"/api/v1/uploads/{upload_id}/cancel",
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        self.assertTrue(cancelled.json()["storage_removed"])
        task_root = self.data_root / "jobs" / upload_id
        self.assertFalse(task_root.exists())

        repeated = self.client.post(
            f"/api/v1/uploads/{upload_id}/cancel",
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(repeated.status_code, 200)
        self.assertTrue(repeated.json()["storage_removed"])
        rejected = self.client.post(
            f"/api/v1/uploads/{upload_id}/images",
            files={
                "file": (
                    "late.png",
                    self.png_bytes((20, 30, 90)),
                    "image/png",
                )
            },
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(rejected.status_code, 409)

    @staticmethod
    def png_bytes(color: tuple[int, int, int]) -> bytes:
        output = io.BytesIO()
        Image.new("RGB", (64, 48), color).save(output, format="PNG")
        return output.getvalue()


if __name__ == "__main__":
    unittest.main()
