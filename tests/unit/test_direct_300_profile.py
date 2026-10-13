from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/restricted/set-direct-300-profile.ps1"
PROFILE = ROOT / "deploy/restricted/profiles/direct-300.json"
PWSH = shutil.which("pwsh") or shutil.which("pwsh.exe")
if not PWSH:
    bundled = (
        Path.home()
        / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native/powershell/pwsh.exe"
    )
    if bundled.is_file():
        PWSH = str(bundled)


def ps_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


class Direct300ProfileAssetTests(unittest.TestCase):
    def test_reviewed_limits_are_shared_between_api_and_worker(self) -> None:
        profile = json.loads(PROFILE.read_text(encoding="utf-8"))
        self.assertEqual(profile["schema_version"], 1)
        self.assertEqual(profile["name"], "direct-300")
        for role in ("api", "worker"):
            self.assertEqual(profile[role]["RE3D_EXECUTION_PROFILE"], "gpu-large-300")
            self.assertEqual(profile[role]["RE3D_JOB_STORAGE_RESERVATION_BYTES"], str(64 * 2**30))
            self.assertEqual(profile[role]["RE3D_MIN_FREE_DISK_BYTES"], str(75 * 2**30))
        self.assertEqual(profile["api"]["RE3D_USER_STORAGE_QUOTA_BYTES"], str(128 * 2**30))
        self.assertEqual(profile["api"]["RE3D_JOB_TIMEOUT_SECONDS"], "14400")
        self.assertEqual(profile["api"]["RE3D_USER_MAX_PENDING_JOBS"], "1")
        self.assertEqual(profile["api"]["UPLOAD_MAX_FILE_BYTES"], str(25 * 2**20))
        self.assertEqual(profile["api"]["UPLOAD_MAX_TOTAL_BYTES"], str(2**30))
        self.assertEqual(profile["worker"]["RE3D_RESOURCE_SAMPLE_INTERVAL_SECONDS"], "5")
        self.assertEqual(profile["worker"]["RE3D_TASK_STORAGE_SAMPLE_INTERVAL_SECONDS"], "15")
        self.assertNotIn("DATABASE_URL", profile["api"])
        self.assertNotIn("JWT_SECRET", profile["api"])

    def test_profile_is_accepted_by_the_actual_api_and_worker_settings(self) -> None:
        from backend.monitoring import ResourceMonitorSettings
        from backend.uploads import StorageQuotaSettings, UploadSettings
        from backend.worker.preflight import WorkerExecutionProfile

        profile = json.loads(PROFILE.read_text(encoding="utf-8"))
        with patch.dict(os.environ, profile["api"], clear=True):
            uploads = UploadSettings.from_environment()
            quota = StorageQuotaSettings.from_environment(environment="restricted")
            api_profile = WorkerExecutionProfile.from_environment()
        self.assertEqual(uploads.job_timeout_seconds, 14400)
        self.assertEqual((uploads.min_images, uploads.max_images), (3, 300))
        self.assertEqual(uploads.max_file_bytes, 25 * 2**20)
        self.assertEqual(uploads.max_total_bytes, 2**30)
        self.assertEqual(uploads.min_free_disk_bytes, 75 * 2**30)
        self.assertEqual(quota.quota_bytes, 128 * 2**30)
        self.assertEqual(quota.job_reservation_bytes, 64 * 2**30)
        with patch.dict(os.environ, profile["worker"], clear=True):
            worker_profile = WorkerExecutionProfile.from_environment()
            monitoring = ResourceMonitorSettings.from_environment()
        self.assertEqual(api_profile, worker_profile)
        self.assertEqual(worker_profile.name, "gpu-large-300")
        self.assertEqual(worker_profile.target_image_count, 300)
        self.assertEqual(worker_profile.mapanything_batch_size, 4)
        self.assertTrue(monitoring.enabled)
        self.assertEqual(monitoring.interval_seconds, 5)
        self.assertEqual(monitoring.task_storage_interval_seconds, 15)


@unittest.skipUnless(os.name == "nt" and PWSH, "Windows and PowerShell 7 are required")
class Direct300ProfilePowerShellTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="re3d-direct300-")
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name) / "platform"
        self.project.mkdir()
        self.bundle = Path(self.temporary.name) / "bundle"
        self.api = self.project / ".env.restricted"
        self.worker = self.project / ".env.worker.restricted"
        self.marker = uuid.uuid4().hex + uuid.uuid4().hex
        self.api_bytes = (
            "# synthetic fixture only\r\nAPP_ENV=restricted\r\n"
            f"JWT_SECRET={self.marker}\r\n"
            "UNMANAGED_MULTILINE=\"first line\r\n"
            "RE3D_JOB_TIMEOUT_SECONDS=123\r\n"
            "last line\"\r\n"
            "RE3D_EXECUTION_PROFILE=baseline # keep comment\r\n"
            "RE3D_USER_MAX_SUBMISSIONS_PER_24H=20\r\n"
            "RE3D_USER_STORAGE_QUOTA_BYTES=34359738368\r\n"
        ).encode("utf-8")
        self.worker_bytes = (
            "APP_ENV=restricted\nRE3D_EXECUTION_PROFILE=baseline\n"
            f"UNMANAGED_FIELD='{self.marker}'\n"
            "RE3D_RESOURCE_SAMPLE_INTERVAL_SECONDS='15'\n"
            "RE3D_TASK_STORAGE_SAMPLE_INTERVAL_SECONDS=60"
        ).encode("utf-8")
        self.api.write_bytes(self.api_bytes)
        self.worker.write_bytes(self.worker_bytes)

    def run_profile(
        self,
        *,
        apply: bool = False,
        service_status: str | None = None,
        fail_verification: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        mock = "function global:Get-Service { param([object]$ErrorAction) @() }\n"
        if service_status:
            mock = (
                "function global:Get-Service { param([object]$ErrorAction) "
                "[pscustomobject]@{Name='Re3DRestrictedWorker';Status="
                + ps_literal(service_status)
                + "} }\n"
            )
        if fail_verification:
            mock += (
                "$global:sourceAclReads=0;\n"
                "function global:Get-Acl { param([string]$LiteralPath) "
                "if ($LiteralPath -ceq " + ps_literal(self.api) + ") { "
                "$global:sourceAclReads++; if ($global:sourceAclReads -eq 2) { "
                "return [pscustomobject]@{Sddl='synthetic-verification-failure'} } }; "
                "Microsoft.PowerShell.Security\\Get-Acl -LiteralPath $LiteralPath }\n"
            )
        command = (
            "$ErrorActionPreference='Stop';\n"
            + mock
            + f"& {ps_literal(SCRIPT)} -ProjectRoot {ps_literal(self.project)} "
            + f"-BundleRoot {ps_literal(self.bundle)}"
            + (" -Apply" if apply else "")
        )
        return subprocess.run(
            [str(PWSH), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )

    def get_security(self, path: Path) -> dict:
        command = (
            f"$acl=Get-Acl -LiteralPath {ps_literal(path)}; "
            "@{sddl=$acl.Sddl;protected=$acl.AreAccessRulesProtected;"
            "sids=@($acl.Access | ForEach-Object { "
            "$_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value })} "
            "| ConvertTo-Json -Depth 4 -Compress"
        )
        result = subprocess.run(
            [str(PWSH), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=True,
        )
        return json.loads(result.stdout)

    def test_dry_run_has_no_mutations_and_does_not_print_unmanaged_values(self) -> None:
        self.bundle.mkdir()
        result = self.run_profile()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["mode"], "dry-run")
        self.assertTrue(report["changed"])
        self.assertFalse(report["applied"])
        self.assertTrue(report["requires_bundle_regeneration"])
        self.assertIsNone(report["backup_directory"])
        self.assertEqual(self.api.read_bytes(), self.api_bytes)
        self.assertEqual(self.worker.read_bytes(), self.worker_bytes)
        self.assertNotIn(self.marker, result.stdout + result.stderr)
        self.assertNotIn("UNMANAGED_MULTILINE", result.stdout)
        self.assertFalse((self.project / ".restricted-profile-backups").exists())

    def test_apply_preserves_secrets_multiline_bytes_and_source_security(self) -> None:
        from backend.monitoring import ResourceMonitorSettings
        from backend.uploads import UploadSettings
        from backend.worker.preflight import WorkerExecutionProfile
        from dotenv import dotenv_values

        original_api_security = self.get_security(self.api)["sddl"]
        original_worker_security = self.get_security(self.worker)["sddl"]
        result = self.run_profile(apply=True, service_status="Stopped")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["applied"])
        self.assertFalse(report["requires_bundle_regeneration"])
        self.assertNotIn(self.marker, result.stdout + result.stderr)
        api = self.api.read_bytes()
        self.assertIn(f"JWT_SECRET={self.marker}\r\n".encode(), api)
        self.assertIn(b'UNMANAGED_MULTILINE="first line\r\nRE3D_JOB_TIMEOUT_SECONDS=123\r\nlast line"\r\n', api)
        self.assertIn(b"RE3D_EXECUTION_PROFILE=gpu-large-300 # keep comment\r\n", api)
        self.assertIn(b"RE3D_JOB_TIMEOUT_SECONDS=14400\r\n", api)
        self.assertIn(b"RE3D_USER_MAX_SUBMISSIONS_PER_24H=20\r\n", api)
        self.assertEqual(self.get_security(self.api)["sddl"], original_api_security)
        self.assertEqual(self.get_security(self.worker)["sddl"], original_worker_security)
        with patch.dict(os.environ, dotenv_values(self.api), clear=True):
            self.assertEqual(UploadSettings.from_environment().job_timeout_seconds, 14400)
        with patch.dict(os.environ, dotenv_values(self.worker), clear=True):
            self.assertEqual(WorkerExecutionProfile.from_environment().mapanything_batch_size, 4)
            monitoring = ResourceMonitorSettings.from_environment()
        self.assertEqual(monitoring.interval_seconds, 5)
        self.assertEqual(monitoring.task_storage_interval_seconds, 15)
        backup = Path(report["backup_directory"])
        self.assertEqual((backup / self.api.name).read_bytes(), self.api_bytes)
        self.assertEqual((backup / self.worker.name).read_bytes(), self.worker_bytes)
        security = self.get_security(backup)
        self.assertTrue(security["protected"])
        self.assertIn("S-1-5-18", security["sids"])
        self.assertIn("S-1-5-32-544", security["sids"])
        self.assertEqual(len(set(security["sids"])), 3)
        for source in (self.api, self.worker):
            self.assertEqual(set(self.get_security(backup / source.name)["sids"]), set(security["sids"]))
        second = self.run_profile(apply=True)
        self.assertEqual(second.returncode, 0, second.stderr)
        repeated = json.loads(second.stdout)
        self.assertFalse(repeated["changed"])
        self.assertFalse(repeated["applied"])
        self.assertIsNone(repeated["backup_directory"])
        self.assertEqual(len(list(backup.parent.iterdir())), 1)

    def test_duplicate_managed_field_is_rejected_before_backup_or_write(self) -> None:
        original = self.api_bytes + b"RE3D_EXECUTION_PROFILE=baseline\r\n"
        self.api.write_bytes(original)
        result = self.run_profile(apply=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("environment-validation", result.stderr)
        self.assertNotIn(self.marker, result.stdout + result.stderr)
        self.assertEqual(self.api.read_bytes(), original)
        self.assertEqual(self.worker.read_bytes(), self.worker_bytes)
        self.assertFalse((self.project / ".restricted-profile-backups").exists())

    def test_running_or_transitioning_service_blocks_all_mutations(self) -> None:
        for status in ("Running", "StartPending", "StopPending"):
            with self.subTest(status=status):
                result = self.run_profile(apply=True, service_status=status)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.api.read_bytes(), self.api_bytes)
                self.assertEqual(self.worker.read_bytes(), self.worker_bytes)
                self.assertNotIn(self.marker, result.stdout + result.stderr)
                self.assertFalse((self.project / ".restricted-profile-backups").exists())

    def test_invalid_worker_prevents_the_valid_api_from_being_updated(self) -> None:
        invalid_worker = self.worker_bytes + b"\nRE3D_EXECUTION_PROFILE=baseline\n"
        self.worker.write_bytes(invalid_worker)
        result = self.run_profile(apply=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.api.read_bytes(), self.api_bytes)
        self.assertEqual(self.worker.read_bytes(), invalid_worker)
        self.assertNotIn(self.marker, result.stdout + result.stderr)
        self.assertFalse((self.project / ".restricted-profile-backups").exists())

    def test_noncanonical_field_is_preserved_and_never_printed(self) -> None:
        extra = f"re3d_execution_profile={self.marker}\r\n".encode()
        self.api.write_bytes(self.api_bytes + extra)
        result = self.run_profile(apply=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(extra, self.api.read_bytes())
        self.assertNotIn(self.marker, result.stdout + result.stderr)

    def test_verification_failure_rolls_back_both_files_to_original_bytes(self) -> None:
        result = self.run_profile(apply=True, fail_verification=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("identity-verification", result.stderr)
        self.assertEqual(self.api.read_bytes(), self.api_bytes)
        self.assertEqual(self.worker.read_bytes(), self.worker_bytes)
        self.assertNotIn(self.marker, result.stdout + result.stderr)
        backups = list((self.project / ".restricted-profile-backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / self.api.name).read_bytes(), self.api_bytes)
        self.assertEqual((backups[0] / self.worker.name).read_bytes(), self.worker_bytes)

    def test_source_bom_is_preserved(self) -> None:
        self.api.write_bytes(b"\xef\xbb\xbf" + self.api_bytes)
        result = self.run_profile(apply=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.api.read_bytes().startswith(b"\xef\xbb\xbf"))


if __name__ == "__main__":
    unittest.main()
