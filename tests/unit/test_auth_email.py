from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from backend.auth import AuthEmailSettings, SmtpAuthEmailSender


class AuthEmailSettingsTests(unittest.TestCase):
    def test_development_allows_disabled_delivery(self) -> None:
        with patch.dict(
            "os.environ",
            {"APP_PUBLIC_BASE_URL": "http://localhost:5173"},
            clear=True,
        ):
            settings = AuthEmailSettings.from_environment(
                environment="development"
            )
        self.assertIsNone(settings.host)
        self.assertFalse(settings.starttls)

    def test_production_requires_smtp_and_https(self) -> None:
        with patch.dict(
            "os.environ",
            {"APP_PUBLIC_BASE_URL": "https://re3d.example.com"},
            clear=True,
        ):
            with self.assertRaises(ValueError):
                AuthEmailSettings.from_environment(environment="production")

    def test_restricted_accepts_loopback_mailpit_without_tls(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "SMTP_HOST": "127.0.0.1",
                "SMTP_FROM": "no-reply@re3d.local",
                "APP_PUBLIC_BASE_URL": "http://127.0.0.1:8080",
                "SMTP_STARTTLS": "false",
            },
            clear=True,
        ):
            settings = AuthEmailSettings.from_environment(
                environment="restricted"
            )
        self.assertFalse(settings.starttls)
        self.assertEqual(settings.host, "127.0.0.1")

    def test_restricted_rejects_remote_web_or_smtp_hosts(self) -> None:
        base = {
            "SMTP_HOST": "127.0.0.1",
            "SMTP_FROM": "no-reply@re3d.local",
            "APP_PUBLIC_BASE_URL": "http://192.168.1.10:8080",
        }
        with patch.dict("os.environ", base, clear=True):
            with self.assertRaisesRegex(ValueError, "loopback origin"):
                AuthEmailSettings.from_environment(environment="restricted")
        base.update(
            {
                "APP_PUBLIC_BASE_URL": "http://127.0.0.1:8080",
                "SMTP_HOST": "192.168.1.10",
            }
        )
        with patch.dict("os.environ", base, clear=True):
            with self.assertRaisesRegex(ValueError, "SMTP_HOST"):
                AuthEmailSettings.from_environment(environment="restricted")
        with patch.dict(
            "os.environ",
            {
                "SMTP_HOST": "smtp.example.com",
                "APP_PUBLIC_BASE_URL": "http://re3d.example.com",
            },
            clear=True,
        ):
            with self.assertRaises(ValueError):
                AuthEmailSettings.from_environment(environment="production")
        with patch.dict(
            "os.environ",
            {
                "SMTP_HOST": "smtp.example.com",
                "SMTP_FROM": "no-reply@example.com",
                "APP_PUBLIC_BASE_URL": "https://re3d.example.com",
                "SMTP_STARTTLS": "false",
            },
            clear=True,
        ):
            with self.assertRaises(ValueError):
                AuthEmailSettings.from_environment(environment="production")

    @patch("backend.auth.email.smtplib.SMTP")
    def test_smtp_sender_uses_fragment_links_and_starttls(self, smtp_type) -> None:
        smtp = MagicMock()
        smtp_type.return_value.__enter__.return_value = smtp
        sender = SmtpAuthEmailSender(
            AuthEmailSettings(
                host="smtp.example.com",
                port=587,
                sender="Re3D <no-reply@example.com>",
                username="smtp-user",
                password="smtp-password",
                starttls=True,
                public_base_url="https://re3d.example.com",
            )
        )

        sender.send_email_verification(
            recipient="user@example.com",
            token="secret-token",
        )

        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("smtp-user", "smtp-password")
        message = smtp.send_message.call_args.args[0]
        body = message.get_content()
        self.assertIn(
            "https://re3d.example.com/auth/verify-email#token=secret-token",
            body,
        )
        self.assertNotIn("?token=", body)


if __name__ == "__main__":
    unittest.main()
