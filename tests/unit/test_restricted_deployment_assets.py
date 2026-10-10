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


if __name__ == "__main__":
    unittest.main()
