from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.auth import (
    AuthRequestContext,
    AuthService,
    AuthSettings,
    DuplicateIdentityError,
    EmailDeliveryError,
    InvalidCredentialsError,
    InvalidActionTokenError,
    InvalidTokenError,
    RateLimitExceededError,
    resolve_client_ip,
)
from backend.db.models import (
    AuthActionRequestBucket,
    AuthActionToken,
    AuthEvent,
    AuthRegistrationBucket,
    AuthThrottleBucket,
    Base,
    RefreshSession,
    User,
)


TEST_SECRET = "auth-test-secret-" + "x" * 64


class RecordingEmailSender:
    def __init__(self) -> None:
        self.verification: list[tuple[str, str]] = []
        self.password_resets: list[tuple[str, str]] = []

    def send_email_verification(self, *, recipient: str, token: str) -> None:
        self.verification.append((recipient, token))

    def send_password_reset(self, *, recipient: str, token: str) -> None:
        self.password_resets.append((recipient, token))


class FailingEmailSender:
    def send_email_verification(self, *, recipient: str, token: str) -> None:
        raise EmailDeliveryError("simulated delivery failure")

    def send_password_reset(self, *, recipient: str, token: str) -> None:
        raise EmailDeliveryError("simulated delivery failure")


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

    def test_invalid_registration_does_not_create_audit_or_throttle_rows(self) -> None:
        with self.assertRaises(ValueError):
            self.auth.register(
                username="invalid username",
                email="valid@example.com",
                password="correct horse battery staple",
            )
        with self.sessions() as session:
            events = list(session.execute(select(AuthEvent)).scalars())
            buckets = list(
                session.execute(select(AuthRegistrationBucket)).scalars()
            )
        self.assertEqual(events, [])
        self.assertEqual(buckets, [])

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

    def test_authentication_events_do_not_store_raw_request_identity(self) -> None:
        context = AuthRequestContext(
            client_ip="203.0.113.25",
            user_agent="Example Browser/1.0",
        )
        user = self.auth.register(
            username="audit-user",
            email="audit-user@example.com",
            password="correct horse battery staple",
            context=context,
        )
        with self.assertRaises(InvalidCredentialsError):
            self.auth.login(
                identifier="audit-user@example.com",
                password="wrong password",
                context=context,
            )
        issued = self.auth.login(
            identifier="audit-user@example.com",
            password="correct horse battery staple",
            context=context,
        )
        rotated = self.auth.refresh(issued.refresh_token, context=context)
        with self.assertRaises(InvalidTokenError):
            self.auth.refresh(issued.refresh_token, context=context)
        self.auth.logout(rotated.refresh_token, context=context)

        with self.sessions() as session:
            events = session.execute(
                select(AuthEvent).order_by(AuthEvent.occurred_at, AuthEvent.action)
            ).scalars().all()
        self.assertEqual(
            {(event.action, event.outcome) for event in events},
            {
                ("register", "success"),
                ("login", "failure"),
                ("login", "success"),
                ("refresh", "success"),
                ("refresh", "reuse"),
                ("logout", "success"),
            },
        )
        self.assertTrue(any(event.user_id == user.id for event in events))
        self.assertTrue(
            all(
                event.client_ip_fingerprint is None
                or len(event.client_ip_fingerprint) == 64
                for event in events
            )
        )
        serialized = " ".join(
            str(
                (
                    event.identifier_fingerprint,
                    event.client_ip_fingerprint,
                    event.user_agent_sha256,
                    event.reason_code,
                )
            )
            for event in events
        )
        self.assertNotIn("audit-user@example.com", serialized)
        self.assertNotIn("203.0.113.25", serialized)
        self.assertNotIn("Example Browser", serialized)

    def test_account_login_throttle_blocks_and_expires(self) -> None:
        settings = AuthSettings(
            jwt_secret=TEST_SECRET,
            cookie_secure=False,
            login_account_max_failures=2,
            login_ip_max_failures=10,
            login_block_minutes=1,
        )
        auth = AuthService(self.sessions, settings)
        auth.register(
            username="limited-user",
            email="limited-user@example.com",
            password="correct horse battery staple",
        )
        now = datetime.now(timezone.utc) - timedelta(minutes=2)
        with patch("backend.auth.service._database_now", return_value=now):
            for identifier in ("limited-user", "limited-user@example.com"):
                with self.assertRaises(InvalidCredentialsError):
                    auth.login(
                        identifier=identifier,
                        password="wrong password",
                    )
            with self.assertRaises(RateLimitExceededError) as blocked:
                auth.login(
                    identifier="limited-user",
                    password="correct horse battery staple",
                )
        self.assertEqual(blocked.exception.retry_after_seconds, 60)

        with patch(
            "backend.auth.service._database_now",
            return_value=now + timedelta(seconds=61),
        ):
            issued = auth.login(
                identifier="limited-user",
                password="correct horse battery staple",
            )
        self.assertEqual(auth.current_user(issued.access_token).username, "limited-user")
        with self.sessions() as session:
            buckets = session.execute(select(AuthThrottleBucket)).scalars().all()
            blocked_events = session.execute(
                select(AuthEvent).where(AuthEvent.outcome == "blocked")
            ).scalars().all()
        self.assertEqual(len(buckets), 1)
        self.assertEqual(buckets[0].failure_count, 0)
        self.assertEqual(len(blocked_events), 1)

    def test_ip_throttle_aggregates_different_identifiers(self) -> None:
        settings = AuthSettings(
            jwt_secret=TEST_SECRET,
            cookie_secure=False,
            login_account_max_failures=2,
            login_ip_max_failures=2,
        )
        auth = AuthService(self.sessions, settings)
        auth.register(
            username="known-user",
            email="known-user@example.com",
            password="correct horse battery staple",
        )
        context = AuthRequestContext(client_ip="198.51.100.42")
        for identifier in ("missing-one", "missing-two"):
            with self.assertRaises(InvalidCredentialsError):
                auth.login(
                    identifier=identifier,
                    password="wrong password",
                    context=context,
                )
        with self.assertRaises(RateLimitExceededError):
            auth.login(
                identifier="known-user",
                password="correct horse battery staple",
                context=context,
            )

    def test_registration_throttle_counts_success_and_duplicate_attempts(self) -> None:
        settings = AuthSettings(
            jwt_secret=TEST_SECRET,
            cookie_secure=False,
            registration_identity_max_attempts=2,
            registration_ip_max_attempts=10,
            registration_block_minutes=1,
        )
        auth = AuthService(self.sessions, settings)
        now = datetime.now(timezone.utc) - timedelta(hours=2)
        with patch("backend.auth.service._database_now", return_value=now):
            auth.register(
                username="limited-registration",
                email="limited-registration@example.com",
                password="correct horse battery staple",
            )
            with self.assertRaises(DuplicateIdentityError):
                auth.register(
                    username="limited-registration",
                    email="another-registration@example.com",
                    password="correct horse battery staple",
                )
            with self.assertRaises(RateLimitExceededError) as blocked:
                auth.register(
                    username="limited-registration",
                    email="third-registration@example.com",
                    password="correct horse battery staple",
                )
        self.assertEqual(blocked.exception.retry_after_seconds, 60)

        with patch(
            "backend.auth.service._database_now",
            return_value=now + timedelta(minutes=61),
        ):
            with self.assertRaises(DuplicateIdentityError):
                auth.register(
                    username="limited-registration",
                    email="after-window@example.com",
                    password="correct horse battery staple",
                )

        with self.sessions() as session:
            buckets = session.execute(
                select(AuthRegistrationBucket)
            ).scalars().all()
            blocked_events = session.execute(
                select(AuthEvent).where(
                    AuthEvent.action == "register",
                    AuthEvent.outcome == "blocked",
                )
            ).scalars().all()
        self.assertGreaterEqual(len(buckets), 4)
        self.assertEqual(len(blocked_events), 1)

    def test_registration_ip_throttle_aggregates_unique_identities(self) -> None:
        settings = AuthSettings(
            jwt_secret=TEST_SECRET,
            cookie_secure=False,
            registration_identity_max_attempts=2,
            registration_ip_max_attempts=2,
        )
        auth = AuthService(self.sessions, settings)
        context = AuthRequestContext(client_ip="198.51.100.55")
        for index in range(2):
            auth.register(
                username=f"registration-ip-{index}",
                email=f"registration-ip-{index}@example.com",
                password="correct horse battery staple",
                context=context,
            )
        with self.assertRaises(RateLimitExceededError):
            auth.register(
                username="registration-ip-blocked",
                email="registration-ip-blocked@example.com",
                password="correct horse battery staple",
                context=context,
            )

    def test_email_verification_tokens_are_hashed_rotated_and_single_use(self) -> None:
        sender = RecordingEmailSender()
        auth = AuthService(self.sessions, self.settings, sender)
        user = auth.register(
            username="verify-user",
            email="verify-user@example.com",
            password="correct horse battery staple",
        )
        first = auth.request_email_verification(user_id=user.id)
        assert first is not None
        auth.deliver_auth_email(first)
        second = auth.request_email_verification(user_id=user.id)
        assert second is not None
        auth.deliver_auth_email(second)

        self.assertEqual(len(sender.verification), 2)
        self.assertNotIn(second.token, repr(second))
        with self.sessions() as session:
            tokens = session.execute(
                select(AuthActionToken).where(
                    AuthActionToken.purpose == "email_verification"
                )
            ).scalars().all()
        self.assertEqual(len(tokens), 2)
        self.assertTrue(any(token.revoked_at is not None for token in tokens))
        self.assertNotIn(second.token, {token.token_sha256 for token in tokens})
        self.assertIn(
            hashlib.sha256(second.token.encode("utf-8")).hexdigest(),
            {token.token_sha256 for token in tokens},
        )
        with self.assertRaises(InvalidActionTokenError):
            auth.confirm_email_verification(first.token)

        verified = auth.confirm_email_verification(second.token)
        self.assertTrue(verified.email_verified)
        with self.assertRaises(InvalidActionTokenError):
            auth.confirm_email_verification(second.token)

    def test_password_reset_revokes_sessions_and_verifies_email(self) -> None:
        sender = RecordingEmailSender()
        auth = AuthService(self.sessions, self.settings, sender)
        user = auth.register(
            username="reset-user",
            email="reset-user@example.com",
            password="correct horse battery staple",
        )
        issued = auth.login(
            identifier="reset-user",
            password="correct horse battery staple",
        )
        self.assertIsNone(
            auth.request_password_reset(email="missing-user@example.com")
        )
        pending = auth.request_password_reset(email="RESET-USER@example.com")
        assert pending is not None
        auth.deliver_auth_email(pending)
        self.assertEqual(sender.password_resets[0][0], "reset-user@example.com")

        auth.confirm_password_reset(
            token=pending.token,
            new_password="a different secure password",
        )
        with self.assertRaises(InvalidCredentialsError):
            auth.login(
                identifier="reset-user",
                password="correct horse battery staple",
            )
        logged_in = auth.login(
            identifier="reset-user",
            password="a different secure password",
        )
        self.assertTrue(logged_in.user.email_verified)
        with self.assertRaises(InvalidTokenError):
            auth.refresh(issued.refresh_token)
        with self.assertRaises(InvalidActionTokenError):
            auth.confirm_password_reset(
                token=pending.token,
                new_password="another different password",
            )

    def test_failed_email_delivery_revokes_action_token(self) -> None:
        auth = AuthService(self.sessions, self.settings, FailingEmailSender())
        user = auth.register(
            username="mail-failure",
            email="mail-failure@example.com",
            password="correct horse battery staple",
        )
        pending = auth.request_email_verification(user_id=user.id)
        assert pending is not None
        with self.assertLogs("backend.auth.service", level="ERROR"):
            auth.deliver_auth_email(pending)
        with self.assertRaises(InvalidActionTokenError):
            auth.confirm_email_verification(pending.token)
        with self.sessions() as session:
            stored = session.get(AuthActionToken, pending.token_id)
            assert stored is not None
            self.assertIsNotNone(stored.revoked_at)

    def test_auth_email_request_limit_is_shared_by_identity_and_ip(self) -> None:
        settings = AuthSettings(
            jwt_secret=TEST_SECRET,
            cookie_secure=False,
            action_request_identity_max_attempts=2,
            action_request_ip_max_attempts=2,
        )
        auth = AuthService(self.sessions, settings, RecordingEmailSender())
        user = auth.register(
            username="action-limit",
            email="action-limit@example.com",
            password="correct horse battery staple",
        )
        context = AuthRequestContext(client_ip="203.0.113.90")
        for _ in range(2):
            pending = auth.request_email_verification(
                user_id=user.id,
                context=context,
            )
            self.assertIsNotNone(pending)
        with self.assertRaises(RateLimitExceededError):
            auth.request_email_verification(
                user_id=user.id,
                context=context,
            )
        with self.sessions() as session:
            buckets = list(
                session.execute(select(AuthActionRequestBucket)).scalars()
            )
        self.assertEqual(len(buckets), 2)
        self.assertTrue(all(bucket.blocked_until is not None for bucket in buckets))


class AuthRequestContextTests(unittest.TestCase):
    def test_ignores_forwarded_header_from_untrusted_peer(self) -> None:
        self.assertEqual(
            resolve_client_ip(
                peer="198.51.100.8",
                forwarded_for="203.0.113.20",
                trusted_proxy_cidrs=("127.0.0.1/32",),
            ),
            "198.51.100.8",
        )

    def test_uses_first_untrusted_hop_behind_trusted_proxies(self) -> None:
        self.assertEqual(
            resolve_client_ip(
                peer="127.0.0.1",
                forwarded_for="203.0.113.20, 10.0.0.5",
                trusted_proxy_cidrs=("127.0.0.1/32", "10.0.0.0/8"),
            ),
            "203.0.113.20",
        )


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
        with patch.dict(
            "os.environ",
            {
                "JWT_SECRET": TEST_SECRET,
                "REFRESH_COOKIE_SECURE": "true",
            },
            clear=True,
        ):
            with self.assertRaises(ValueError):
                AuthSettings.from_environment(environment="production")

    def test_development_defaults_to_non_secure_cookie(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "JWT_SECRET": TEST_SECRET,
                "AUTH_TRUSTED_PROXY_CIDRS": "127.0.0.1/32,::1/128",
            },
            clear=True,
        ):
            settings = AuthSettings.from_environment(environment="development")
        self.assertFalse(settings.cookie_secure)
        self.assertEqual(
            settings.trusted_proxy_cidrs,
            ("127.0.0.1/32", "::1/128"),
        )

    def test_rejects_invalid_login_limits_and_proxy_networks(self) -> None:
        with self.assertRaises(ValueError):
            AuthSettings(
                jwt_secret=TEST_SECRET,
                login_account_max_failures=10,
                login_ip_max_failures=5,
            )
        with self.assertRaises(ValueError):
            AuthSettings(
                jwt_secret=TEST_SECRET,
                trusted_proxy_cidrs=("not-a-network",),
            )
        with self.assertRaises(ValueError):
            AuthSettings(
                jwt_secret=TEST_SECRET,
                registration_identity_max_attempts=10,
                registration_ip_max_attempts=5,
            )
        with self.assertRaises(ValueError):
            AuthSettings(
                jwt_secret=TEST_SECRET,
                action_request_identity_max_attempts=10,
                action_request_ip_max_attempts=5,
            )
        with self.assertRaises(ValueError):
            AuthSettings(
                jwt_secret=TEST_SECRET,
                password_reset_ttl_minutes=1,
            )


if __name__ == "__main__":
    unittest.main()
