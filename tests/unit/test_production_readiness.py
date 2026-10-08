from __future__ import annotations

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from apps.maintenance.readiness import (
    _check_production_storage,
    _required_environment_integer,
    _validate_production_database_identity,
    check_production_readiness,
)


class ProductionReadinessTests(unittest.TestCase):
    def test_requires_exact_production_environment(self) -> None:
        with patch.dict(os.environ, {"APP_ENV": "development"}, clear=True):
            with self.assertRaisesRegex(ValueError, "APP_ENV must be production"):
                check_production_readiness()

    def test_api_check_validates_public_dependencies_without_exposing_secrets(
        self,
    ) -> None:
        secret = "a-production-secret-that-must-never-be-returned"
        environment = {
            "APP_ENV": "production",
            "APP_PUBLIC_BASE_URL": "https://re3d.user-domain.cn",
            "AUTH_TRUSTED_PROXY_CIDRS": "127.0.0.1/32,::1/128",
            "API_IP_RATE_LIMIT_WINDOW_SECONDS": "60",
            "API_IP_RATE_LIMIT_MAX_REQUESTS": "300",
            "DATABASE_URL": (
                "postgresql+psycopg://re3d_runtime:database-secret@"
                "127.0.0.1:5432/re3d_platform"
            ),
            "JWT_SECRET": secret,
            "REFRESH_COOKIE_SECURE": "true",
            "RE3D_USER_MAX_PENDING_JOBS": "2",
            "RE3D_USER_MAX_SUBMISSIONS_PER_24H": "10",
            "RE3D_USER_STORAGE_QUOTA_BYTES": str(20 * 1024**3),
            "RE3D_JOB_STORAGE_RESERVATION_BYTES": str(2 * 1024**3),
            "SMTP_FROM": "no-reply@user-domain.cn",
            "SMTP_HOST": "smtp.mail-provider.cn",
            "SMTP_PASSWORD": "smtp-secret",
            "SMTP_PORT": "587",
            "SMTP_STARTTLS": "true",
            "SMTP_USERNAME": "mailer",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch(
                "apps.maintenance.readiness._check_production_database",
                return_value={
                    "name": "re3d_platform",
                    "role": "re3d_runtime",
                    "migration": "0014_user_storage_quota",
                },
            ),
            patch(
                "apps.maintenance.readiness._check_production_storage",
                return_value={
                    "data_root": "D:/Re3D-data-production",
                    "free_bytes": 100,
                    "minimum_free_bytes": 50,
                },
            ),
            patch(
                "apps.maintenance.readiness._check_smtp",
                return_value={
                    "host": "smtp.mail-provider.cn",
                    "port": 587,
                    "starttls": True,
                },
            ),
        ):
            report = check_production_readiness(component="api")

        serialized = json.dumps(report)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["component"], "api")
        self.assertNotIn("re3d", report)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("database-secret", serialized)
        self.assertNotIn("smtp-secret", serialized)
        self.assertEqual(
            report["task_submission"],
            {
                "max_pending_jobs": 2,
                "max_submissions_per_24h": 10,
            },
        )
        self.assertEqual(
            report["api_ip_rate_limit"],
            {
                "window_seconds": 60,
                "max_requests": 300,
                "backend": "database",
            },
        )
        self.assertEqual(
            report["user_storage_quota"],
            {
                "quota_bytes": 20 * 1024**3,
                "job_reservation_bytes": 2 * 1024**3,
                "accounting_backend": "database",
            },
        )

    def test_worker_check_does_not_require_web_or_email_secrets(self) -> None:
        environment = {
            "APP_ENV": "production",
            "DATABASE_URL": (
                "postgresql+psycopg://re3d_runtime:database-secret@"
                "127.0.0.1:5432/re3d_platform"
            ),
            "SUCCESS_INPUT_RETENTION_DAYS": "30",
            "SUCCESS_RUNTIME_RETENTION_DAYS": "30",
            "SUCCESS_ARTIFACT_RETENTION_DAYS": "30",
            "SUCCESS_RETENTION_DRY_RUN_ENABLED": "true",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch(
                "apps.maintenance.readiness._check_production_database",
                return_value={
                    "name": "re3d_platform",
                    "role": "re3d_runtime",
                    "migration": "0014_user_storage_quota",
                },
            ),
            patch(
                "apps.maintenance.readiness._check_production_storage",
                return_value={
                    "data_root": "D:/Re3D-data-production",
                    "free_bytes": 100,
                    "minimum_free_bytes": 50,
                },
            ),
            patch(
                "apps.maintenance.readiness._check_re3d_installation",
                return_value={
                    "root": "D:/Re3D",
                    "commit": "fixed-commit",
                    "tag": "fixed-tag",
                },
            ),
        ):
            report = check_production_readiness(component="worker")

        self.assertEqual(report["component"], "worker")
        self.assertIn("re3d", report)
        self.assertIn("scheduler", report)
        self.assertEqual(
            report["failed_job_cleanup"],
            {
                "grace_minutes": 5,
                "interval_seconds": 300,
                "batch_size": 100,
            },
        )
        self.assertEqual(
            report["resource_monitor"],
            {
                "enabled": True,
                "interval_seconds": 15,
                "nvidia_smi_configured": False,
            },
        )
        self.assertEqual(
            report["success_retention"],
            {
                "input_days": 30,
                "runtime_days": 30,
                "artifact_days": 30,
                "batch_size": 50,
                "scheduled_dry_run_enabled": True,
                "scheduled_dry_run_interval_seconds": 86400,
                "audit_backend": "database",
                "execution_requires_delete_confirmation": True,
            },
        )
        self.assertNotIn("authentication", report)
        self.assertNotIn("smtp", report)

    def test_worker_check_requires_fixed_retention_policy(self) -> None:
        environment = {
            "APP_ENV": "production",
            "DATABASE_URL": (
                "postgresql+psycopg://re3d_runtime:database-secret@"
                "127.0.0.1:5432/re3d_platform"
            ),
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch(
                "apps.maintenance.readiness._check_production_database",
                return_value={
                    "name": "re3d_platform",
                    "role": "re3d_runtime",
                    "migration": "0014_user_storage_quota",
                },
            ),
            patch(
                "apps.maintenance.readiness._check_production_storage",
                return_value={
                    "data_root": "D:/Re3D-data-production",
                    "free_bytes": 100,
                    "minimum_free_bytes": 50,
                },
            ),
        ):
            with self.assertRaisesRegex(ValueError, "retention tiers"):
                check_production_readiness(component="worker")

    def test_api_check_rejects_trusting_every_forwarded_address(self) -> None:
        environment = {
            "APP_ENV": "production",
            "AUTH_TRUSTED_PROXY_CIDRS": "0.0.0.0/0",
            "JWT_SECRET": "a" * 64,
            "REFRESH_COOKIE_SECURE": "true",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, "all-address network"):
                check_production_readiness(component="api")

    def test_api_check_rejects_example_domains_before_connecting(self) -> None:
        environment = {
            "APP_ENV": "production",
            "APP_PUBLIC_BASE_URL": "https://re3d.example.com",
            "AUTH_TRUSTED_PROXY_CIDRS": "127.0.0.1/32",
            "API_IP_RATE_LIMIT_WINDOW_SECONDS": "60",
            "API_IP_RATE_LIMIT_MAX_REQUESTS": "300",
            "JWT_SECRET": "a" * 64,
            "REFRESH_COOKIE_SECURE": "true",
            "SMTP_FROM": "no-reply@example.com",
            "SMTP_HOST": "smtp.example.com",
            "SMTP_STARTTLS": "true",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, "replace example domains"):
                check_production_readiness(component="api")

    def test_production_database_identity_rejects_development_or_admin(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-development"):
            _validate_production_database_identity(
                database_name="re3d_platform_dev",
                role_name="re3d_runtime",
                role_capabilities=(False, False, False, False, False),
            )
        with self.assertRaisesRegex(ValueError, "restricted runtime"):
            _validate_production_database_identity(
                database_name="re3d_platform",
                role_name="re3d_runtime",
                role_capabilities=(False, True, False, False, False),
            )
        with self.assertRaisesRegex(ValueError, "restricted runtime"):
            _validate_production_database_identity(
                database_name="re3d_platform",
                role_name="re3d_migrator",
                role_capabilities=(False,) * 8,
            )

    def test_storage_check_enforces_operator_selected_free_space_floor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = {
                "RE3D_DATA_ROOT": temporary,
                "RE3D_MIN_FREE_DISK_BYTES": str(2 * 1024**3),
            }
            with (
                patch.dict(os.environ, environment, clear=True),
                patch(
                    "apps.maintenance.readiness.shutil.disk_usage",
                    return_value=SimpleNamespace(free=1024**3),
                ),
            ):
                with self.assertRaisesRegex(ValueError, "does not satisfy"):
                    _check_production_storage()

    def test_required_integer_must_be_explicit_and_at_least_minimum(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "is required"):
                _required_environment_integer("RE3D_MIN_FREE_DISK_BYTES", minimum=10)
        with patch.dict(
            os.environ,
            {"RE3D_MIN_FREE_DISK_BYTES": "9"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "must be at least 10"):
                _required_environment_integer("RE3D_MIN_FREE_DISK_BYTES", minimum=10)


if __name__ == "__main__":
    unittest.main()
