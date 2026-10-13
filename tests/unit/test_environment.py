from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from backend.environment import (
    is_development_environment,
    is_loopback_host,
    normalize_environment,
    requires_explicit_operational_settings,
)


class EnvironmentTests(unittest.TestCase):
    def test_api_rejects_restricted_experiment_in_other_environments(self) -> None:
        from apps.api.main import create_app

        for environment in ("production", "development", "test"):
            with self.subTest(environment=environment), patch.dict(
                os.environ,
                {
                    "APP_ENV": "restricted",
                    "RE3D_EXECUTION_PROFILE": " GPU-EXPERIMENT-300-20 ",
                },
                clear=True,
            ), patch("apps.api.main.build_services") as build_services:
                with self.assertRaisesRegex(ValueError, "restricted-only"):
                    create_app(app_env=environment)
                build_services.assert_not_called()

    def test_normalizes_supported_environment_names(self) -> None:
        self.assertEqual(normalize_environment(" Restricted "), "restricted")
        self.assertTrue(is_development_environment("test"))
        self.assertTrue(requires_explicit_operational_settings("restricted"))
        self.assertTrue(requires_explicit_operational_settings("production"))
        self.assertFalse(
            requires_explicit_operational_settings("development")
        )

    def test_rejects_unknown_environment(self) -> None:
        with self.assertRaisesRegex(ValueError, "APP_ENV must be one of"):
            normalize_environment("staging")

    def test_identifies_only_loopback_hosts(self) -> None:
        for host in ("localhost", "127.0.0.1", "127.10.20.30", "::1", "[::1]"):
            with self.subTest(host=host):
                self.assertTrue(is_loopback_host(host))
        for host in (None, "", "0.0.0.0", "192.168.1.10", "example.com"):
            with self.subTest(host=host):
                self.assertFalse(is_loopback_host(host))


if __name__ == "__main__":
    unittest.main()
