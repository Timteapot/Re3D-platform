from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class RestrictedDeploymentAssetTests(unittest.TestCase):
    def test_templates_keep_restricted_identities_and_loopback_scope(self) -> None:
        api = (ROOT / ".env.restricted.example").read_text(encoding="utf-8")
        worker = (ROOT / ".env.worker.restricted.example").read_text(
            encoding="utf-8"
        )
        for content in (api, worker):
            self.assertIn("APP_ENV=restricted", content)
            self.assertIn("re3d_restricted_runtime", content)
            self.assertIn("re3d_platform_restricted", content)
            self.assertIn("@127.0.0.1:5432/", content)
            self.assertIn("RE3D_DATA_ROOT=D:/3Dreconstruction/Re3D-data/restricted", content)
        self.assertIn("API_HOST=127.0.0.1", api)
        self.assertIn("APP_PUBLIC_BASE_URL=http://127.0.0.1:8080", api)
        self.assertIn("SMTP_HOST=127.0.0.1", api)
        self.assertIn("REFRESH_COOKIE_SECURE=false", api)
        self.assertNotIn("JWT_SECRET=", worker)
        self.assertNotIn("SMTP_HOST=", worker)

    def test_database_assets_preserve_migration_runtime_separation(self) -> None:
        restricted = ROOT / "deploy" / "postgres" / "restricted"
        bootstrap = (restricted / "bootstrap-restricted.psql").read_text(
            encoding="utf-8"
        )
        grants = (restricted / "grant-runtime.psql").read_text(
            encoding="utf-8"
        )
        verification = (restricted / "verify-runtime.psql").read_text(
            encoding="utf-8"
        )
        self.assertIn("OWNER = %I", bootstrap)
        self.assertIn("re3d_restricted_migrator", bootstrap)
        self.assertIn("re3d_restricted_runtime", bootstrap)
        self.assertIn("REVOKE CREATE, TEMPORARY", bootstrap)
        self.assertIn("REVOKE CREATE ON SCHEMA public", grants)
        self.assertIn("REVOKE INSERT, UPDATE, DELETE", grants)
        self.assertIn("current_database() = 're3d_platform_restricted'", verification)
        self.assertIn("current_user = 're3d_restricted_runtime'", verification)
        self.assertIn("NOT has_database_privilege", verification)

    def test_initializers_use_windows_powershell_compatible_randomness(self) -> None:
        for relative in (
            "deploy/development/initialize-local-env.ps1",
            "deploy/restricted/initialize-restricted-env.ps1",
        ):
            content = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("RandomNumberGenerator]::Create()", content)
            self.assertIn("GetBytes($secretBytes)", content)
            self.assertIn("BitConverter]::ToString", content)
            self.assertNotIn("RandomNumberGenerator]::Fill", content)
            self.assertNotIn("Convert]::ToHexString", content)

    def test_restricted_environment_files_receive_protected_acls(self) -> None:
        common = (ROOT / "deploy/restricted/common.ps1").read_text(encoding="utf-8")
        initializer = (
            ROOT / "deploy/restricted/initialize-restricted-env.ps1"
        ).read_text(encoding="utf-8")
        readiness = (
            ROOT / "deploy/restricted/check-restricted-readiness.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("SetAccessRuleProtection($true, $false)", common)
        self.assertIn("$acl.GetOwner", common)
        self.assertIn("Assert-Re3DRestrictedSecretFileAcl", common)
        self.assertIn("Set-Re3DRestrictedSecretFileAcl", initializer)
        self.assertIn("Assert-Re3DRestrictedSecretFileAcl", readiness)

    def test_standalone_mailpit_is_pinned_and_loopback_only(self) -> None:
        install = (ROOT / "deploy/mailpit/install-standalone.ps1").read_text(
            encoding="utf-8"
        )
        start = (ROOT / "deploy/mailpit/start-standalone.ps1").read_text(
            encoding="utf-8"
        )
        stop = (ROOT / "deploy/mailpit/stop-standalone.ps1").read_text(
            encoding="utf-8"
        )
        expected_executable_hash = (
            "ee0b025bc9f61e6856d6032128408ee6fe1627f510c118a4fa0faa7bfdb7cd33"
        )
        self.assertIn("mailpit/releases/download/v$version", install)
        self.assertIn(expected_executable_hash, install)
        self.assertIn(expected_executable_hash, start)
        self.assertIn('"--smtp=$smtpAddress"', start)
        self.assertIn('"--listen=$uiAddress"', start)
        self.assertIn('"127.0.0.1:1025"', start)
        self.assertIn('"127.0.0.1:8025"', start)
        self.assertNotIn("--pop3", start)
        self.assertIn("-WindowStyle Hidden", start)
        self.assertIn("[switch]$StayAttached", start)
        self.assertIn("Wait-Process -Id $process.Id", start)
        self.assertIn("PID $savedPid belongs to another executable", start)
        self.assertIn("PID $savedPid belongs to another executable", stop)


if __name__ == "__main__":
    unittest.main()
