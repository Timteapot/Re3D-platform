from __future__ import annotations

import os
import re
import tempfile
import unittest
import uuid
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.api.main import AppServices, create_app
from backend.auth import AuthEmailSettings, AuthService, AuthSettings, SmtpAuthEmailSender
from backend.db.models import Base
from backend.db.queue import JobQueue
from backend.jobs.development import DevelopmentJobService
from backend.uploads import UploadService


SMTP_HOST = os.environ.get("RE3D_TEST_MAILPIT_SMTP_HOST")
SMTP_PORT = int(os.environ.get("RE3D_TEST_MAILPIT_SMTP_PORT", "1025"))
MAILPIT_UI_URL = os.environ.get("RE3D_TEST_MAILPIT_UI_URL")
TEST_SECRET = "mailpit-auth-flow-secret-" + "x" * 64


@unittest.skipUnless(
    SMTP_HOST and MAILPIT_UI_URL,
    "Mailpit integration environment is not configured",
)
class MailpitAuthenticationFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        assert SMTP_HOST is not None
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.engine = create_engine(
            f"sqlite:///{(self.root / 'platform.sqlite3').as_posix()}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.queue = JobQueue(self.sessions)
        self.queue.ensure_resource("gpu:0")
        sender = SmtpAuthEmailSender(
            AuthEmailSettings(
                host=SMTP_HOST,
                port=SMTP_PORT,
                sender="no-reply@example.invalid",
                public_base_url="http://localhost:5173",
            )
        )
        services = AppServices(
            self.engine,
            self.queue,
            DevelopmentJobService(self.queue, data_root=self.root / "data"),
            AuthService(
                self.sessions,
                AuthSettings(jwt_secret=TEST_SECRET, cookie_secure=False),
                sender,
            ),
            UploadService(
                self.sessions,
                self.queue,
                data_root=self.root / "data",
            ),
        )
        self.client = TestClient(create_app(services=services, app_env="test"))

    def tearDown(self) -> None:
        self.client.close()
        self.engine.dispose()
        self.temporary.cleanup()

    def test_registration_verification_and_password_reset_via_smtp(self) -> None:
        suffix = uuid.uuid4().hex[:12]
        username = f"mailpit-{suffix}"
        email = f"mailpit-{suffix}@example.com"
        password = "correct horse battery staple"
        new_password = "new correct horse battery staple"

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
        headers = {
            "Authorization": f"Bearer {logged_in.json()['access_token']}"
        }

        blocked = self.client.post(
            "/api/v1/uploads",
            json={"idempotency_key": "mailpit-blocked-upload"},
            headers=headers,
        )
        self.assertEqual(blocked.status_code, 403)

        requested = self.client.post(
            "/api/v1/auth/email-verification/request",
            headers=headers,
        )
        self.assertEqual(requested.status_code, 202)
        verification_message = self._latest_message(email)
        verification_token = self._token_from_message(
            verification_message,
            path="verify-email",
        )
        verified = self.client.post(
            "/api/v1/auth/email-verification/confirm",
            json={"token": verification_token},
        )
        self.assertEqual(verified.status_code, 200)
        self.assertTrue(verified.json()["email_verified"])

        allowed = self.client.post(
            "/api/v1/uploads",
            json={"idempotency_key": "mailpit-allowed-upload"},
            headers=headers,
        )
        self.assertEqual(allowed.status_code, 201)

        reset_requested = self.client.post(
            "/api/v1/auth/password-reset/request",
            json={"email": email},
        )
        self.assertEqual(reset_requested.status_code, 202)
        reset_message = self._latest_message(email)
        reset_token = self._token_from_message(
            reset_message,
            path="reset-password",
        )
        reset = self.client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": reset_token, "new_password": new_password},
        )
        self.assertEqual(reset.status_code, 204)
        self.assertIn("max-age=0", reset.headers["set-cookie"].lower())

        old_login = self.client.post(
            "/api/v1/auth/login",
            json={"identifier": username, "password": password},
        )
        self.assertEqual(old_login.status_code, 401)
        new_login = self.client.post(
            "/api/v1/auth/login",
            json={"identifier": username, "password": new_password},
        )
        self.assertEqual(new_login.status_code, 200)

    def _latest_message(self, email: str) -> str:
        assert MAILPIT_UI_URL is not None
        query = urlencode({"query": f'to:"{email}"'})
        url = f"{MAILPIT_UI_URL.rstrip('/')}/view/latest.txt?{query}"
        with urlopen(url, timeout=5) as response:  # noqa: S310 - local test URL
            self.assertEqual(response.status, 200)
            return response.read().decode("utf-8")

    def _token_from_message(self, message: str, *, path: str) -> str:
        match = re.search(
            rf"http://localhost:5173/auth/{path}#token=([A-Za-z0-9_-]+)",
            message,
        )
        self.assertIsNotNone(match, message)
        assert match is not None
        return match.group(1)
