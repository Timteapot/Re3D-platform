from __future__ import annotations

import unittest

from backend.environment import (
    is_development_environment,
    is_loopback_host,
    normalize_environment,
    requires_explicit_operational_settings,
)


class EnvironmentTests(unittest.TestCase):
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
