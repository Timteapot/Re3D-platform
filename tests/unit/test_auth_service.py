from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.auth import (
    AuthService,
    AuthSettings,
    DuplicateIdentityError,
    InvalidCredentialsError,
    InvalidTokenError,
)
from backend.db.models import Base, RefreshSession, User


TEST_SECRET = "auth-test-secret-" + "x" * 64


class AuthServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary.name) / "auth.sqlite3"
        self.engine = create_engine(f"sqlite:///{database_path.as_posix()}")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.settings = AuthSettings(jwt_secret=TEST_SECRET, cookie_secure=False)
        self.auth = AuthService(self.sessions, self.settings)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary.cleanup()

    def register_user(self) -> None:
        self.auth.register(
            username="Alice.Example",
            email="Alice@Example.COM",
            password="correct horse battery staple",
        )

    def test_registration_normalizes_identity_and_hashes_password(self) -> None:
        user = self.auth.register(
            username=" Alice.Example ",
            email="Alice@Example.COM",
            password="correct horse battery staple",
        )

        self.assertEqual(user.username, "alice.example")
        self.assertEqual(user.email, "alice@example.com")
        self.assertFalse(user.email_verified)
        with self.sessions() as session:
            stored = session.get(User, user.id)
            assert stored is not None
            self.assertTrue(stored.password_hash.startswith("$argon2id$"))
            self.assertNotIn("correct horse", stored.password_hash)

        with self.assertRaises(DuplicateIdentityError):
            self.auth.register(
                username="alice.example",
                email="different@example.com",
                password="another valid development password",
            )

    def test_login_access_refresh_rotation_and_logout(self) -> None:
        self.register_user()
        issued = self.auth.login(
            identifier="ALICE.EXAMPLE",
            password="correct horse battery staple",
        )
        current = self.auth.current_user(issued.access_token)
        self.assertEqual(current.username, "alice.example")
        self.assertIsNotNone(current.last_login_at)

        rotated = self.auth.refresh(issued.refresh_token)
        self.assertNotEqual(rotated.refresh_token, issued.refresh_token)
        self.assertEqual(rotated.refresh_expires_at, issued.refresh_expires_at)
        with self.assertRaises(InvalidTokenError):
            self.auth.refresh(issued.refresh_token)

        with self.assertRaises(InvalidTokenError):
            self.auth.refresh(rotated.refresh_token)
        self.auth.logout(rotated.refresh_token)
        with self.sessions() as session:
            sessions = session.execute(select(RefreshSession)).scalars().all()
        self.assertEqual(len(sessions), 2)
        self.assertEqual(len({item.family_id for item in sessions}), 1)
        self.assertNotIn(
            issued.refresh_token,
            {item.token_sha256 for item in sessions},
        )
        self.assertTrue(all(item.revoked_at is not None for item in sessions))

    def test_invalid_credentials_and_tampered_access_token_are_rejected(self) -> None:
        self.register_user()
        with self.assertRaises(InvalidCredentialsError):
            self.auth.login(
                identifier="missing@example.com",
                password="correct horse battery staple",
            )
        with self.assertRaises(InvalidCredentialsError):
            self.auth.login(
                identifier="alice.example",
                password="wrong password",
            )
        issued = self.auth.login(
            identifier="alice@example.com",
            password="correct horse battery staple",
        )
        tampered = issued.access_token[:-1] + (
            "a" if issued.access_token[-1] != "a" else "b"
        )
        with self.assertRaises(InvalidTokenError):
            self.auth.current_user(tampered)

    def test_access_token_allows_only_small_database_clock_skew(self) -> None:
        self.register_user()
        now = datetime.now(timezone.utc)
        with patch(
            "backend.auth.service._database_now",
            return_value=now + timedelta(seconds=4),
        ):
            within_tolerance = self.auth.login(
                identifier="alice.example",
                password="correct horse battery staple",
            )
        self.assertEqual(
            self.auth.current_user(within_tolerance.access_token).username,
            "alice.example",
        )

        with patch(
            "backend.auth.service._database_now",
            return_value=now + timedelta(seconds=10),
        ):
            outside_tolerance = self.auth.login(
                identifier="alice.example",
                password="correct horse battery staple",
            )
        with self.assertRaises(InvalidTokenError):
            self.auth.current_user(outside_tolerance.access_token)


class AuthSettingsTests(unittest.TestCase):
    def test_rejects_placeholder_and_insecure_production_cookie(self) -> None:
        with self.assertRaises(ValueError):
            AuthSettings(jwt_secret="replace-with-at-least-32-random-bytes")
        with patch.dict(
            "os.environ",
            {
                "JWT_SECRET": TEST_SECRET,
                "REFRESH_COOKIE_SECURE": "false",
            },
            clear=True,
        ):
            with self.assertRaises(ValueError):
                AuthSettings.from_environment(environment="production")

    def test_development_defaults_to_non_secure_cookie(self) -> None:
        with patch.dict("os.environ", {"JWT_SECRET": TEST_SECRET}, clear=True):
            settings = AuthSettings.from_environment(environment="development")
        self.assertFalse(settings.cookie_secure)


if __name__ == "__main__":
    unittest.main()
