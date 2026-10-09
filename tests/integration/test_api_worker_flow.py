from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from apps.api.main import AppServices, create_app
from apps.worker.main import main as worker_main
from backend.auth import AuthService, AuthSettings
from backend.db.models import (
    AuthEvent,
    Base,
    ReconstructionJob,
    User,
    UserTransferLease,
)
from backend.db.queue import JobQueue
from backend.jobs import TaskSubmissionPolicy, TaskSubmissionSettings
from backend.jobs.development import DevelopmentJobService
from backend.uploads import (
    StorageQuotaExceededError,
    UploadService,
    UploadSettings,
)


TEST_SECRET = "api-flow-test-secret-" + "x" * 64
PRODUCTION_TRANSFER_ENV = {
    "TRANSFER_WINDOW_SECONDS": "3600",
    "TRANSFER_UPLOAD_MAX_CONCURRENT": "2",
    "TRANSFER_UPLOAD_MAX_BYTES": str(2 * 1024**3),
    "TRANSFER_DOWNLOAD_MAX_CONCURRENT": "3",
    "TRANSFER_DOWNLOAD_MAX_BYTES": str(4 * 1024**3),
    "TRANSFER_LEASE_SECONDS": "14400",
}


class RecordingEmailSender:
    def __init__(self) -> None:
        self.verification: list[tuple[str, str]] = []
        self.password_resets: list[tuple[str, str]] = []

    def send_email_verification(self, *, recipient: str, token: str) -> None:
        self.verification.append((recipient, token))

    def send_password_reset(self, *, recipient: str, token: str) -> None:
        self.password_resets.append((recipient, token))


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
        self.email_sender = RecordingEmailSender()
        self.services = AppServices(
            self.engine,
            self.queue,
            DevelopmentJobService(self.queue, data_root=self.data_root),
            AuthService(
                self.sessions,
                AuthSettings(jwt_secret=TEST_SECRET, cookie_secure=False),
                self.email_sender,
            ),
            UploadService(self.sessions, self.queue, data_root=self.data_root),
        )
        self.app = create_app(services=self.services, app_env="test")
        self.client = TestClient(self.app)
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

    def test_admin_audit_routes_require_role_and_return_no_store_pages(self) -> None:
        anonymous = self.client.get("/api/v1/admin/audit/auth-events")
        self.assertEqual(anonymous.status_code, 401)

        denied = self.client.get(
            "/api/v1/admin/audit/auth-events",
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(denied.status_code, 403)

        with self.sessions.begin() as session:
            user = session.get(User, self.user_id)
            assert user is not None
            user.role = "admin"

        allowed = self.client.get(
            "/api/v1/admin/audit/auth-events?limit=1&action=login",
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.headers["cache-control"], "no-store")
        page = allowed.json()
        self.assertEqual(page["limit"], 1)
        self.assertEqual(page["offset"], 0)
        self.assertEqual(len(page["items"]), 1)
        serialized = json.dumps(page)
        self.assertNotIn("api-owner@example.com", serialized)

        invalid_filter = self.client.get(
            "/api/v1/admin/audit/auth-events?action=not-an-action",
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(invalid_filter.status_code, 422)

    def test_ordinary_api_ip_limit_is_shared_and_health_is_exempt(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "API_IP_RATE_LIMIT_WINDOW_SECONDS": "60",
                "API_IP_RATE_LIMIT_MAX_REQUESTS": "2",
            },
            clear=False,
        ):
            limited_app = create_app(services=self.services, app_env="test")

        with TestClient(
            limited_app,
            client=("127.0.0.1", 51000),
        ) as limited_client:
            for _ in range(3):
                self.assertEqual(
                    limited_client.get("/health/live").status_code,
                    200,
                )

            first = limited_client.get(
                "/api/v1/jobs",
                headers=self.auth_headers(self.access_token),
            )
            second = limited_client.get(
                "/api/v1/jobs",
                headers=self.auth_headers(self.access_token),
            )
            blocked = limited_client.get(
                "/api/v1/jobs",
                headers=self.auth_headers(self.access_token),
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.headers["x-ratelimit-remaining"], "1")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.headers["x-ratelimit-remaining"], "0")
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(
            blocked.headers["x-re3d-error-code"],
            "API_IP_RATE_LIMITED",
        )
        self.assertEqual(blocked.headers["cache-control"], "no-store")
        self.assertGreaterEqual(int(blocked.headers["retry-after"]), 1)

    def test_upload_transfer_concurrency_limit_returns_stable_429(self) -> None:
        created = self.client.post(
            "/api/v1/uploads",
            json={"idempotency_key": "transfer-concurrency-upload"},
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(created.status_code, 201)
        upload_id = created.json()["upload_id"]
        held_leases = [
            self.app.state.transfer_limits.acquire(
                user_id=self.user_id,
                direction="upload",
                byte_count=0,
            )
            for _ in range(2)
        ]
        try:
            blocked = self.client.post(
                f"/api/v1/uploads/{upload_id}/images",
                files={
                    "file": (
                        "blocked.png",
                        self.png_bytes((30, 60, 90)),
                        "image/png",
                    )
                },
                headers=self.auth_headers(self.access_token),
            )
        finally:
            for held_lease in held_leases:
                self.app.state.transfer_limits.release(held_lease.id)

        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(
            blocked.headers["x-re3d-error-code"],
            "UPLOAD_CONCURRENCY_LIMIT_REACHED",
        )
        self.assertEqual(blocked.headers["cache-control"], "no-store")
        self.assertGreaterEqual(int(blocked.headers["retry-after"]), 1)
        self.assertEqual(
            self.client.get(
                f"/api/v1/uploads/{upload_id}",
                headers=self.auth_headers(self.access_token),
            ).json()["image_count"],
            0,
        )

    def register_and_login(
        self,
        *,
        username: str,
        email: str,
        verified: bool = True,
    ) -> tuple[uuid.UUID, str]:
        password = "correct horse battery staple"
        registered = self.client.post(
            "/api/v1/auth/register",
            json={"username": username, "email": email, "password": password},
        )
        self.assertEqual(registered.status_code, 201)
        user_id = uuid.UUID(registered.json()["id"])
        if verified:
            with self.sessions.begin() as session:
                user = session.get(User, user_id)
                assert user is not None
                user.email_verified = True
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
        return user_id, logged_in.json()["access_token"]

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
            f"/api/v1/jobs/{job_id}",
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(response.status_code, 200)
        completed = response.json()
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["progress"], 100)

        detail_response = self.client.get(
            f"/api/v1/jobs/{job_id}/detail",
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(detail_response.status_code, 200)
        detail = detail_response.json()
        self.assertEqual(detail["job"]["status"], "succeeded")
        self.assertEqual(detail["detail_state"], "available")
        self.assertEqual(detail["input"]["image_count"], 3)
        self.assertEqual(
            [branch["name"] for branch in detail["result"]["branches"]],
            ["A-v4", "B-v2", "C"],
        )
        self.assertEqual(detail["evaluation"]["overall_status"], "not_available")
        self.assertIsNone(detail["evaluation"]["score"])
        self.assertNotIn("path", detail["result"]["branches"][0]["artifacts"][0])
        self.assertNotIn("sha256", detail["result"]["branches"][0]["artifacts"][0])
        artifact_summary = detail["result"]["branches"][0]["artifacts"][0]
        artifact_url = (
            f"/api/v1/jobs/{job_id}/artifacts/A-v4/glb"
        )
        self.assertEqual(artifact_summary["download_url"], artifact_url)
        unauthenticated = self.client.get(artifact_url)
        self.assertEqual(unauthenticated.status_code, 401)
        held_leases = [
            self.app.state.transfer_limits.acquire(
                user_id=self.user_id,
                direction="download",
                byte_count=0,
            )
            for _ in range(3)
        ]
        blocked_download = self.client.get(
            artifact_url,
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(blocked_download.status_code, 429)
        self.assertEqual(
            blocked_download.headers["x-re3d-error-code"],
            "DOWNLOAD_CONCURRENCY_LIMIT_REACHED",
        )
        self.assertEqual(blocked_download.headers["cache-control"], "no-store")
        self.assertGreaterEqual(
            int(blocked_download.headers["retry-after"]),
            1,
        )
        for held_lease in held_leases:
            self.app.state.transfer_limits.release(held_lease.id)
        downloaded = self.client.get(
            artifact_url,
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.content[:4], b"glTF")
        self.assertEqual(downloaded.headers["content-type"], "model/gltf-binary")
        self.assertEqual(downloaded.headers["cache-control"], "private, no-store")
        self.assertEqual(downloaded.headers["x-content-type-options"], "nosniff")
        self.assertIn("attachment", downloaded.headers["content-disposition"])
        self.assertIn("A-v4-mesh.glb", downloaded.headers["content-disposition"])
        with self.sessions() as session:
            active_downloads = session.execute(
                select(func.count())
                .select_from(UserTransferLease)
                .where(
                    UserTransferLease.user_id == self.user_id,
                    UserTransferLease.direction == "download",
                )
            ).scalar_one()
        self.assertEqual(active_downloads, 0)

        stream = self.client.get(
            f"/api/v1/jobs/{job_id}/events",
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(stream.status_code, 200)
        self.assertEqual(stream.headers["content-type"], "text/event-stream; charset=utf-8")
        self.assertIn("event: job", stream.text)
        self.assertIn(f'"job_id": "{job_id}"', stream.text)
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

        artifact = self.data_root / "jobs" / job_id / "output" / "A-v4" / "mesh.glb"
        artifact.write_bytes(b"tampered")
        corrupted = self.client.get(
            artifact_url,
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(corrupted.status_code, 409)

    def test_user_scope_cancel_and_production_route_guard(self) -> None:
        created = self.create_job("integration-cancel-001")
        job_id = created["job_id"]

        _, other_access_token = self.register_and_login(
            username="different-owner",
            email="different-owner@example.com",
        )
        hidden = self.client.get(
            f"/api/v1/jobs/{job_id}",
            headers=self.auth_headers(other_access_token),
        )
        self.assertEqual(hidden.status_code, 404)
        hidden_detail = self.client.get(
            f"/api/v1/jobs/{job_id}/detail",
            headers=self.auth_headers(other_access_token),
        )
        self.assertEqual(hidden_detail.status_code, 404)
        hidden_stream = self.client.get(
            f"/api/v1/jobs/{job_id}/events",
            headers=self.auth_headers(other_access_token),
        )
        self.assertEqual(hidden_stream.status_code, 404)
        hidden_artifact = self.client.get(
            f"/api/v1/jobs/{job_id}/artifacts/A-v4/glb",
            headers=self.auth_headers(other_access_token),
        )
        self.assertEqual(hidden_artifact.status_code, 404)
        pending_artifact = self.client.get(
            f"/api/v1/jobs/{job_id}/artifacts/A-v4/glb",
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(pending_artifact.status_code, 409)
        cancelled = self.client.post(
            f"/api/v1/jobs/{job_id}/cancel",
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        legacy = self.client.get(
            f"/api/v1/development/jobs/{job_id}",
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(legacy.status_code, 200)
        self.assertEqual(legacy.json()["job_id"], job_id)
        development_paths = self.client.get("/openapi.json").json()["paths"]
        self.assertIn("/api/v1/jobs/{job_id}", development_paths)
        self.assertNotIn(
            "/api/v1/development/jobs/{job_id}",
            development_paths,
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
                    "api-flow-worker",
                    "--lease-seconds",
                    "5",
                    "--heartbeat-seconds",
                    "1",
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "idle")

        with patch.dict(
            "os.environ",
            {
                "API_IP_RATE_LIMIT_WINDOW_SECONDS": "60",
                "API_IP_RATE_LIMIT_MAX_REQUESTS": "300",
                **PRODUCTION_TRANSFER_ENV,
            },
            clear=False,
        ):
            production_app = create_app(
                services=self.services,
                app_env="production",
            )
        production = TestClient(production_app)
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
            stable_job = production.get(
                f"/api/v1/jobs/{job_id}",
                headers=self.auth_headers(self.access_token),
            )
            self.assertEqual(stable_job.status_code, 200)
            self.assertEqual(stable_job.json()["status"], "cancelled")
            artifact_response = production.get(
                f"/api/v1/jobs/{job_id}/artifacts/A-v4/glb",
                headers=self.auth_headers(self.access_token),
            )
            self.assertEqual(artifact_response.status_code, 409)
            legacy_job = production.get(
                f"/api/v1/development/jobs/{job_id}",
                headers=self.auth_headers(self.access_token),
            )
            self.assertEqual(legacy_job.status_code, 404)
            upload_response = production.post(
                "/api/v1/uploads",
                json={"idempotency_key": "production-upload-boundary"},
                headers=self.auth_headers(self.access_token),
            )
            self.assertEqual(upload_response.status_code, 201)
            openapi_paths = production.get("/openapi.json").json()["paths"]
            self.assertIn("/api/v1/jobs/{job_id}", openapi_paths)
            self.assertIn("/api/v1/uploads", openapi_paths)
            self.assertNotIn("/api/v1/development/jobs/{job_id}", openapi_paths)
            self.assertEqual(production.get("/api/v1/auth/me").status_code, 401)
        finally:
            production.close()

    def test_production_upload_submission_is_real_only(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "API_IP_RATE_LIMIT_WINDOW_SECONDS": "60",
                "API_IP_RATE_LIMIT_MAX_REQUESTS": "300",
                **PRODUCTION_TRANSFER_ENV,
            },
            clear=False,
        ):
            production_app = create_app(
                services=self.services,
                app_env="production",
            )
        production = TestClient(production_app)
        headers = self.auth_headers(self.access_token)
        try:
            created = production.post(
                "/api/v1/uploads",
                json={"idempotency_key": "production-real-upload"},
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

            simulated = production.post(
                f"/api/v1/uploads/{upload_id}/submit",
                json={"execution_mode": "simulated"},
                headers=headers,
            )
            self.assertEqual(simulated.status_code, 422, simulated.text)

            submitted = production.post(
                f"/api/v1/uploads/{upload_id}/submit",
                headers=headers,
            )
            self.assertEqual(submitted.status_code, 202, submitted.text)
            self.assertEqual(submitted.json()["execution_mode"], "real")
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

    def test_login_rate_limit_returns_retry_after_and_audits(self) -> None:
        password = "correct horse battery staple"
        registered = self.client.post(
            "/api/v1/auth/register",
            json={
                "username": "rate-limited-user",
                "email": "rate-limited-user@example.com",
                "password": password,
            },
        )
        self.assertEqual(registered.status_code, 201)

        for _ in range(self.services.auth.settings.login_account_max_failures):
            failed = self.client.post(
                "/api/v1/auth/login",
                json={
                    "identifier": "rate-limited-user",
                    "password": "wrong password",
                },
            )
            self.assertEqual(failed.status_code, 401)

        blocked = self.client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "rate-limited-user",
                "password": password,
            },
        )
        self.assertEqual(blocked.status_code, 429)
        self.assertGreaterEqual(int(blocked.headers["retry-after"]), 1)
        self.assertNotIn("rate-limited-user", blocked.text)

        with self.sessions() as session:
            outcomes = session.execute(
                select(AuthEvent.outcome).where(AuthEvent.action == "login")
            ).scalars().all()
        self.assertEqual(outcomes.count("failure"), 5)
        self.assertEqual(outcomes.count("blocked"), 1)

    def test_registration_rate_limit_returns_retry_after(self) -> None:
        payload = {
            "username": "registration-rate-limit",
            "email": "registration-rate-limit@example.com",
            "password": "correct horse battery staple",
        }
        created = self.client.post("/api/v1/auth/register", json=payload)
        self.assertEqual(created.status_code, 201)

        for _ in range(
            self.services.auth.settings.registration_identity_max_attempts - 1
        ):
            duplicate = self.client.post("/api/v1/auth/register", json=payload)
            self.assertEqual(duplicate.status_code, 409)

        blocked = self.client.post("/api/v1/auth/register", json=payload)
        self.assertEqual(blocked.status_code, 429)
        self.assertGreaterEqual(int(blocked.headers["retry-after"]), 1)
        self.assertNotIn(payload["email"], blocked.text)

        with self.sessions() as session:
            blocked_events = session.execute(
                select(AuthEvent).where(
                    AuthEvent.action == "register",
                    AuthEvent.outcome == "blocked",
                )
            ).scalars().all()
        self.assertEqual(len(blocked_events), 1)

    def test_email_verification_and_password_reset_api_flow(self) -> None:
        _, unverified_access_token = self.register_and_login(
            username="unverified-owner",
            email="unverified-owner@example.com",
            verified=False,
        )

        readable_jobs = self.client.get(
            "/api/v1/jobs",
            headers=self.auth_headers(unverified_access_token),
        )
        self.assertEqual(readable_jobs.status_code, 200)
        blocked_upload = self.client.post(
            "/api/v1/uploads",
            json={"idempotency_key": "unverified-upload-001"},
            headers=self.auth_headers(unverified_access_token),
        )
        self.assertEqual(blocked_upload.status_code, 403)
        self.assertEqual(blocked_upload.json()["detail"], "email verification is required")
        blocked_simulation = self.client.post(
            "/api/v1/development/simulated-jobs",
            json={"image_count": 3, "idempotency_key": "unverified-job-001"},
            headers=self.auth_headers(unverified_access_token),
        )
        self.assertEqual(blocked_simulation.status_code, 403)

        verification_request = self.client.post(
            "/api/v1/auth/email-verification/request",
            headers=self.auth_headers(unverified_access_token),
        )
        self.assertEqual(verification_request.status_code, 202)
        self.assertEqual(len(self.email_sender.verification), 1)
        verification_token = self.email_sender.verification[0][1]

        verified = self.client.post(
            "/api/v1/auth/email-verification/confirm",
            json={"token": verification_token},
        )
        self.assertEqual(verified.status_code, 200)
        self.assertTrue(verified.json()["email_verified"])
        replayed = self.client.post(
            "/api/v1/auth/email-verification/confirm",
            json={"token": verification_token},
        )
        self.assertEqual(replayed.status_code, 400)

        allowed_upload = self.client.post(
            "/api/v1/uploads",
            json={"idempotency_key": "verified-upload-001"},
            headers=self.auth_headers(unverified_access_token),
        )
        self.assertEqual(allowed_upload.status_code, 201)

        unknown = self.client.post(
            "/api/v1/auth/password-reset/request",
            json={"email": "missing-reset@example.com"},
        )
        known = self.client.post(
            "/api/v1/auth/password-reset/request",
            json={"email": "unverified-owner@example.com"},
        )
        self.assertEqual(unknown.status_code, 202)
        self.assertEqual(known.status_code, 202)
        self.assertEqual(unknown.json(), known.json())
        self.assertEqual(len(self.email_sender.password_resets), 1)

        reset_token = self.email_sender.password_resets[0][1]
        reset = self.client.post(
            "/api/v1/auth/password-reset/confirm",
            json={
                "token": reset_token,
                "new_password": "new correct horse battery staple",
            },
        )
        self.assertEqual(reset.status_code, 204)
        self.assertIn("max-age=0", reset.headers["set-cookie"].lower())
        self.assertEqual(self.client.post("/api/v1/auth/refresh").status_code, 401)
        old_password = self.client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "unverified-owner",
                "password": "correct horse battery staple",
            },
        )
        self.assertEqual(old_password.status_code, 401)
        new_password = self.client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "unverified-owner",
                "password": "new correct horse battery staple",
            },
        )
        self.assertEqual(new_password.status_code, 200)

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
            "/api/v1/jobs",
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
                "/api/v1/jobs",
                headers=self.auth_headers(self.access_token),
            ).json(),
            [],
        )

    def test_storage_capacity_floor_returns_stable_507_contract(self) -> None:
        minimum = 1024**3
        self.services.uploads.settings = UploadSettings(
            min_free_disk_bytes=minimum
        )
        with patch(
            "backend.uploads.service.shutil.disk_usage",
            return_value=SimpleNamespace(
                free=minimum + self.services.uploads.settings.max_file_bytes - 1
            ),
        ):
            response = self.client.post(
                "/api/v1/uploads",
                json={"idempotency_key": "capacity-api-upload-001"},
                headers=self.auth_headers(self.access_token),
            )

        self.assertEqual(response.status_code, 507)
        self.assertEqual(
            response.headers["x-re3d-error-code"],
            "STORAGE_CAPACITY_FLOOR_REACHED",
        )
        self.assertEqual(response.headers["retry-after"], "300")

    def test_storage_quota_endpoint_and_stable_507_contract(self) -> None:
        quota = self.client.get(
            "/api/v1/uploads/quota",
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(quota.status_code, 200)
        self.assertEqual(quota.headers["cache-control"], "no-store")
        self.assertEqual(quota.json()["used_bytes"], 0)
        self.assertGreater(quota.json()["job_reservation_bytes"], 0)

        created = self.client.post(
            "/api/v1/uploads",
            json={"idempotency_key": "quota-api-upload-001"},
            headers=self.auth_headers(self.access_token),
        )
        upload_id = created.json()["upload_id"]
        with patch.object(
            self.services.uploads.quota,
            "enforce_in_session",
            side_effect=StorageQuotaExceededError("user storage quota exceeded"),
        ):
            response = self.client.post(
                f"/api/v1/uploads/{upload_id}/images",
                files={
                    "file": (
                        "quota.png",
                        self.png_bytes((10, 20, 30)),
                        "image/png",
                    )
                },
                headers=self.auth_headers(self.access_token),
            )

        self.assertEqual(response.status_code, 507)
        self.assertEqual(
            response.headers["x-re3d-error-code"],
            "USER_STORAGE_QUOTA_EXCEEDED",
        )
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("retry-after", response.headers)

    def test_upload_submission_limits_return_stable_429_contract(self) -> None:
        self.services.uploads.submission_policy = TaskSubmissionPolicy(
            TaskSubmissionSettings(
                max_pending_jobs=1,
                max_submissions_per_24h=2,
            )
        )

        def prepare(token: str, color_offset: int) -> str:
            created = self.client.post(
                "/api/v1/uploads",
                json={"idempotency_key": token},
                headers=self.auth_headers(self.access_token),
            )
            self.assertEqual(created.status_code, 201)
            upload_id = created.json()["upload_id"]
            for index in range(3):
                uploaded = self.client.post(
                    f"/api/v1/uploads/{upload_id}/images",
                    files={
                        "file": (
                            f"limit-{index}.png",
                            self.png_bytes(
                                (
                                    color_offset + index,
                                    40 + index,
                                    80 + index,
                                )
                            ),
                            "image/png",
                        )
                    },
                    headers=self.auth_headers(self.access_token),
                )
                self.assertEqual(uploaded.status_code, 201)
            return upload_id

        first_id = prepare("limit-api-upload-001", 10)
        first = self.client.post(
            f"/api/v1/uploads/{first_id}/submit",
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(first.status_code, 202)

        second_id = prepare("limit-api-upload-002", 20)
        pending = self.client.post(
            f"/api/v1/uploads/{second_id}/submit",
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(pending.status_code, 429)
        self.assertEqual(
            pending.headers["x-re3d-error-code"],
            "PENDING_JOB_LIMIT_REACHED",
        )
        self.assertEqual(
            pending.json()["detail"],
            "pending reconstruction job limit reached",
        )

        cancelled = self.client.post(
            f"/api/v1/jobs/{first_id}/cancel",
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(cancelled.status_code, 200)
        second = self.client.post(
            f"/api/v1/uploads/{second_id}/submit",
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(second.status_code, 202)
        self.client.post(
            f"/api/v1/jobs/{second_id}/cancel",
            headers=self.auth_headers(self.access_token),
        )

        third_id = prepare("limit-api-upload-003", 30)
        daily = self.client.post(
            f"/api/v1/uploads/{third_id}/submit",
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(daily.status_code, 429)
        self.assertEqual(
            daily.headers["x-re3d-error-code"],
            "SUBMISSION_WINDOW_LIMIT_REACHED",
        )
        self.assertGreater(int(daily.headers["retry-after"]), 0)

    def test_real_submission_is_explicit_and_simulation_worker_does_not_claim_it(self) -> None:
        created = self.client.post(
            "/api/v1/uploads",
            json={"idempotency_key": "real-upload-flow-001"},
            headers=self.auth_headers(self.access_token),
        )
        upload_id = created.json()["upload_id"]
        for index, color in enumerate(((120, 30, 20), (20, 120, 30), (30, 20, 120))):
            uploaded = self.client.post(
                f"/api/v1/uploads/{upload_id}/images",
                files={
                    "file": (
                        f"real-{index}.png",
                        self.png_bytes(color),
                        "image/png",
                    )
                },
                headers=self.auth_headers(self.access_token),
            )
            self.assertEqual(uploaded.status_code, 201)

        submitted = self.client.post(
            f"/api/v1/uploads/{upload_id}/submit",
            json={"execution_mode": "real"},
            headers=self.auth_headers(self.access_token),
        )
        self.assertEqual(submitted.status_code, 202)
        self.assertEqual(submitted.json()["execution_mode"], "real")

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
                    "simulation-only-worker",
                    "--lease-seconds",
                    "5",
                    "--heartbeat-seconds",
                    "1",
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "idle")
        self.assertEqual(
            self.queue.get_job(uuid.UUID(upload_id))["status"],
            "queued",
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
