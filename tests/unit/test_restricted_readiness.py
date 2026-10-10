from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from apps.maintenance.readiness import check_restricted_readiness


class RestrictedReadinessTests(unittest.TestCase):
    def test_requires_exact_restricted_environment(self) -> None:
        with patch.dict(os.environ, {"APP_ENV": "development"}, clear=True):
            with self.assertRaisesRegex(ValueError, "APP_ENV must be restricted"):
                check_restricted_readiness()

    def test_api_check_accepts_only_explicit_loopback_dependencies(self) -> None:
        secret = "restricted-secret-that-must-never-be-returned"
        environment = {
            "APP_ENV": "restricted",
            "APP_PUBLIC_BASE_URL": "http://127.0.0.1:8080",
            "API_HOST": "127.0.0.1",
            "AUTH_TRUSTED_PROXY_CIDRS": "127.0.0.1/32,::1/128",
            "API_IP_RATE_LIMIT_WINDOW_SECONDS": "60",
            "API_IP_RATE_LIMIT_MAX_REQUESTS": "300",
            "TRANSFER_WINDOW_SECONDS": "3600",
            "TRANSFER_UPLOAD_MAX_CONCURRENT": "2",
            "TRANSFER_UPLOAD_MAX_BYTES": str(2 * 1024**3),
            "TRANSFER_DOWNLOAD_MAX_CONCURRENT": "3",
            "TRANSFER_DOWNLOAD_MAX_BYTES": str(4 * 1024**3),
            "TRANSFER_LEASE_SECONDS": "14400",
            "DATABASE_URL": (
                "postgresql+psycopg://re3d_restricted_runtime:database-secret@"
                "127.0.0.1:5432/re3d_platform_restricted"
            ),
            "JWT_SECRET": secret,
            "REFRESH_COOKIE_SECURE": "false",
            "RE3D_USER_MAX_PENDING_JOBS": "3",
            "RE3D_USER_MAX_SUBMISSIONS_PER_24H": "20",
            "RE3D_USER_STORAGE_QUOTA_BYTES": str(10 * 1024**3),
            "RE3D_JOB_STORAGE_RESERVATION_BYTES": str(2 * 1024**3),
            "SMTP_FROM": "no-reply@re3d.local",
            "SMTP_HOST": "127.0.0.1",
            "SMTP_PORT": "1025",
            "SMTP_STARTTLS": "false",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch(
                "apps.maintenance.readiness._check_restricted_database",
                return_value={
                    "name": "re3d_platform_restricted",
                    "role": "re3d_restricted_runtime",
                    "migration": "0015_transfer_limits",
                },
            ),
            patch(
                "apps.maintenance.readiness._check_restricted_storage",
                return_value={
                    "data_root": "D:/3Dreconstruction/Re3D-data/restricted",
                    "free_bytes": 100,
                    "minimum_free_bytes": 50,
                },
            ),
            patch(
                "apps.maintenance.readiness._check_smtp",
                return_value={
                    "host": "127.0.0.1",
                    "port": 1025,
                    "starttls": False,
                },
            ),
        ):
            report = check_restricted_readiness(component="api")

        serialized = json.dumps(report)
        self.assertEqual(report["network_scope"], "loopback-only")
        self.assertFalse(report["authentication"]["cookie_secure"])
        self.assertNotIn("re3d", report)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("database-secret", serialized)

    def test_worker_check_does_not_require_api_or_email_configuration(self) -> None:
        environment = {
            "APP_ENV": "restricted",
            "DATABASE_URL": (
                "postgresql+psycopg://re3d_restricted_runtime:database-secret@"
                "127.0.0.1:5432/re3d_platform_restricted"
            ),
            "SUCCESS_INPUT_RETENTION_DAYS": "30",
            "SUCCESS_RUNTIME_RETENTION_DAYS": "30",
            "SUCCESS_ARTIFACT_RETENTION_DAYS": "30",
            "SUCCESS_RETENTION_DRY_RUN_ENABLED": "true",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch(
                "apps.maintenance.readiness._check_restricted_database",
                return_value={
                    "name": "re3d_platform_restricted",
                    "role": "re3d_restricted_runtime",
                    "migration": "0015_transfer_limits",
                },
            ),
            patch(
                "apps.maintenance.readiness._check_restricted_storage",
                return_value={
                    "data_root": "D:/3Dreconstruction/Re3D-data/restricted",
                    "free_bytes": 100,
                    "minimum_free_bytes": 50,
                },
            ),
            patch(
                "apps.maintenance.readiness._check_re3d_installation",
                return_value={
                    "root": "D:/3Dreconstruction/Re3D",
                    "commit": "fixed-commit",
                    "tag": "fixed-tag",
                },
            ),
        ):
            report = check_restricted_readiness(component="worker")

        self.assertEqual(report["component"], "worker")
        self.assertIn("re3d", report)
        self.assertNotIn("authentication", report)
        self.assertNotIn("smtp", report)

    def test_api_check_rejects_non_loopback_bind(self) -> None:
        with patch.dict(
            os.environ,
            {"APP_ENV": "restricted", "API_HOST": "0.0.0.0"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "API_HOST must be a loopback"):
                check_restricted_readiness(component="api")


if __name__ == "__main__":
    unittest.main()
