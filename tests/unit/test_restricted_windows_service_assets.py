from __future__ import annotations

import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SERVICE_ROOT = ROOT / "deploy" / "restricted" / "windows-services"


def read_script(name: str) -> str:
    return (SERVICE_ROOT / name).read_text(encoding="utf-8")


class RestrictedWindowsServiceAssetTests(unittest.TestCase):
    def test_service_bundle_has_four_passwordless_isolated_identities(self) -> None:
        common = read_script("common.ps1")
        expected = {
            "mailpit": "Re3DRestrictedMailpit",
            "api": "Re3DRestrictedApi",
            "worker": "Re3DRestrictedWorker",
            "caddy": "Re3DRestrictedCaddy",
        }

        for key, service_id in expected.items():
            self.assertIn(f'Key = "{key}"', common)
            self.assertIn(f'Id = "{service_id}"', common)
            self.assertIn('AccountDomain = "NT SERVICE"', common)
            self.assertIn(f'AccountUser = "{service_id}"', common)

        # Restricted services are intentionally operator-started. A reboot or
        # cloud savings-stop resume must not silently consume queued GPU work.
        self.assertIn('StartMode = "Manual"', common)
        self.assertNotIn('StartMode = "Automatic"', common)
        self.assertIn('"startmode" $Definition.StartMode', common)
        self.assertNotIn('AccountDomain = "LocalSystem"', common)
        self.assertNotRegex(
            common,
            re.compile(r'Add-Re3DXmlTextElement[^\r\n]+"password"', re.I),
        )

    def test_bundle_keeps_api_and_worker_secrets_separate(self) -> None:
        common = read_script("common.ps1")
        generator = read_script("new-service-bundle.ps1")
        installer = read_script("install-service-bundle.ps1")
        shared_common = (
            ROOT
            / "deploy"
            / "production"
            / "windows-services"
            / "common.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("Assert-Re3DWorkerSecretBoundary", generator)
        self.assertIn("production\\windows-services\\common.ps1", common)
        for forbidden in (
            "JWT_SECRET",
            "SMTP_PASSWORD",
            "SMTP_USERNAME",
            "REFRESH_COOKIE_NAME",
        ):
            self.assertIn(forbidden, shared_common)
        self.assertIn('".env.restricted"', generator)
        self.assertIn('".env.worker.restricted"', generator)
        self.assertIn("Copy-Item", generator)

        # Each secret file is narrowed to only its corresponding service SID.
        self.assertIn("Set-Re3DRestrictedBundleSecretAcl", installer)
        self.assertIn("$serviceSids.api", installer)
        self.assertIn("$serviceSids.worker", installer)
        self.assertNotRegex(
            installer,
            re.compile(
                r"Set-Re3DRestrictedBundleSecretAcl"
                r"[\s\S]{0,300}\$serviceSids\.mailpit"
            ),
        )

        # Secrets belong in protected files, never in XML or the JSON manifest.
        self.assertIn("JWT_SECRET|SMTP_PASSWORD|DATABASE_URL|<password>", common)
        self.assertIn("restricted-service-bundle.json", generator)

    def test_bundle_is_loopback_only_and_reuses_restricted_entry_points(self) -> None:
        generator = read_script("new-service-bundle.ps1")
        installed_test = read_script("test-installed-services.ps1")
        mailpit_runner = (
            ROOT / "deploy" / "restricted" / "run-mailpit.ps1"
        ).read_text(encoding="utf-8")

        for entry_point in ("run-api.ps1", "run-worker.ps1", "run-caddy.ps1"):
            self.assertIn(entry_point, generator)
        self.assertIn("run-mailpit.ps1", generator)

        expected_mailpit_hash = (
            "ee0b025bc9f61e6856d6032128408ee6fe1627f510c118a4fa0faa7bfdb7cd33"
        )
        self.assertIn(expected_mailpit_hash, mailpit_runner)
        self.assertIn('"--smtp=127.0.0.1:1025"', mailpit_runner)
        self.assertIn('"--listen=127.0.0.1:8025"', mailpit_runner)
        self.assertIn('"--database=$databasePath"', mailpit_runner)
        self.assertNotIn("Start-Process", mailpit_runner)

        for port in (1025, 8025, 8000, 8080):
            self.assertIn(str(port), generator + installed_test)
        self.assertIn("127.0.0.1", generator)
        self.assertIn("127.0.0.1", installed_test)
        self.assertNotIn("0.0.0.0", generator)

    def test_bundle_pins_wrapper_and_keeps_worker_temp_on_data_disk(self) -> None:
        common = read_script("common.ps1")
        generator = read_script("new-service-bundle.ps1")

        expected_winsw_hash = (
            "05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da"
        )
        self.assertIn(expected_winsw_hash, common)
        self.assertIn(expected_winsw_hash, generator)
        self.assertIn("config_sha256", common)
        self.assertIn("config_sha256", generator)
        self.assertIn("source_path = $resolvedWinSW", generator)
        self.assertNotIn(
            "Get-FileHash -LiteralPath $manifest.winsw.source_path",
            common,
        )
        self.assertIn("external.powershell_path", common)
        self.assertIn("requires PowerShell 7", common)
        self.assertIn('Join-Path $dataRoot "_services\\restricted-worker-temp"', generator)
        self.assertIn("WorkerTempRoot must remain inside RE3D_DATA_ROOT", generator)
        self.assertIn("worker_temp_root", common)
        self.assertIn("external.data_root", common)

    def test_source_secret_acl_accepts_only_api_and_worker_service_sids(self) -> None:
        restricted_common = (
            ROOT / "deploy" / "restricted" / "common.ps1"
        ).read_text(encoding="utf-8")
        api_runner = (
            ROOT / "deploy" / "restricted" / "run-api.ps1"
        ).read_text(encoding="utf-8")
        worker_runner = (
            ROOT / "deploy" / "restricted" / "run-worker.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn('"Re3DRestrictedApi", "Re3DRestrictedWorker"', restricted_common)
        self.assertIn('"NT SERVICE\\$VirtualService"', restricted_common)
        self.assertIn(
            "[Security.AccessControl.FileSystemRights]::Read",
            restricted_common,
        )
        self.assertIn("Restricted service SID has an unsafe secret ACL", restricted_common)
        self.assertIn("if ($hasServiceBoundary)", restricted_common)
        self.assertIn("Get-Re3DRestrictedSecretAclSids", restricted_common)
        self.assertIn("Get-Re3DRestrictedDeploymentOwnerSid", restricted_common)
        self.assertIn("[switch]$RequireDeploymentOwner", restricted_common)
        self.assertIn(
            "$ownerSid -cne $fullControlSids[0]",
            restricted_common,
        )
        self.assertIn(
            '"the current Windows user"',
            restricted_common,
        )
        self.assertIn(
            "the fixed local re3d-deploy account",
            restricted_common,
        )
        self.assertNotIn(
            "$fullControlSids = @($ownerSid, $administratorsSid, $systemSid)",
            restricted_common,
        )
        self.assertIn('"NT SERVICE\\Re3DRestrictedApi"', api_runner)
        self.assertIn('-VirtualService $(if ($RequireVirtualService) { "Re3DRestrictedApi" }', api_runner)
        self.assertIn('"NT SERVICE\\Re3DRestrictedWorker"', worker_runner)
        self.assertIn('-VirtualService $(if ($RequireVirtualService) { "Re3DRestrictedWorker" }', worker_runner)

        generator = read_script("new-service-bundle.ps1")
        self.assertEqual(generator.count("-RequireDeploymentOwner"), 2)

    @unittest.skipUnless(
        os.name == "nt" and shutil.which("pwsh"),
        "restricted ACL contract requires Windows PowerShell ACL types",
    )
    def test_secret_acl_identity_matrix_executes(self) -> None:
        result = subprocess.run(
            [
                shutil.which("pwsh") or "pwsh",
                "-NoLogo",
                "-NoProfile",
                "-File",
                str(SERVICE_ROOT / "test-local-acl-contract.ps1"),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn("Restricted secret ACL identity matrix: PASS", result.stdout)

    def test_lifecycle_order_matches_service_dependencies(self) -> None:
        start = read_script("start-service-bundle.ps1")
        stop = read_script("stop-service-bundle.ps1")
        uninstall = read_script("uninstall-service-bundle.ps1")

        self.assertIn('@("mailpit", "api", "worker", "caddy")', start)
        for source in (stop, uninstall):
            self.assertIn('@("caddy", "worker", "api", "mailpit")', source)

        # Lifecycle operations are explicit; generating or installing a bundle
        # must not start it as a side effect.
        generator = read_script("new-service-bundle.ps1")
        installer = read_script("install-service-bundle.ps1")
        self.assertNotIn("Start-Service", generator)
        self.assertNotIn("Start-Service", installer)
        self.assertNotIn("StartServices", installer)
        self.assertIn("$failures.Add", stop)
        self.assertLess(stop.index("foreach ($key"), stop.index("if ($failures.Count -gt 0)"))
        self.assertIn("$started.Add($service)", start)
        self.assertLess(
            start.index("$started.Add($service)"),
            start.index("$controller.WaitForStatus"),
        )
        self.assertIn("Get-NetTCPConnection -State Listen -LocalPort $port", start)
        self.assertIn("Stop the previous standalone or interactive stack first", start)

    def test_installer_enforces_service_sid_acl_boundaries(self) -> None:
        common = read_script("common.ps1")
        installer = read_script("install-service-bundle.ps1")

        self.assertIn("production\\windows-services\\common.ps1", common)
        self.assertIn("Assert-Re3DNoBroadWriteAccess", installer)
        self.assertIn("sc.exe sidtype", installer)
        self.assertIn("Set-Re3DRestrictedBundleSecretAcl", installer)
        self.assertIn('"NT SERVICE\\$($definition.Id)"', installer)
        self.assertNotIn("-Identity $service.virtual_identity", installer)
        self.assertIn("ReadAndExecute", installer)
        self.assertIn("Modify", installer)

        # SYSTEM may administer the bundle and ACLs, but it must not be the
        # process identity used by a WinSW service.
        self.assertNotIn('account = "LocalSystem"', installer)
        self.assertNotIn('account = "SYSTEM"', installer)

        # Composite FileSystemRights values include read bits and therefore
        # cannot be used as a mutation mask when checking a read-only ACE.
        restricted_common = (
            ROOT / "deploy" / "restricted" / "common.ps1"
        ).read_text(encoding="utf-8")
        production_common = (
            ROOT
            / "deploy"
            / "production"
            / "windows-services"
            / "common.ps1"
        ).read_text(encoding="utf-8")
        installed_test = read_script("test-installed-services.ps1")
        for source in (restricted_common, production_common, installed_test):
            self.assertIn("FileSystemRights]::WriteData", source)
            self.assertIn("FileSystemRights]::WriteAttributes", source)
            self.assertNotRegex(
                source,
                re.compile(
                    r"\$writeRights\s*=\s*\([\s\S]{0,300}"
                    r"FileSystemRights\]::(?:Write|Modify|FullControl)\s+-bor"
                ),
            )

    def test_uninstall_retains_runtime_data_and_logs(self) -> None:
        uninstall = read_script("uninstall-service-bundle.ps1")

        self.assertIn("were retained", uninstall)
        self.assertIn("Set-Re3DRestrictedBundleSecretAcl", uninstall)
        self.assertIn('$PSCmdlet.ShouldProcess($service.id, "remove stale service SID access from secret file")', uninstall)
        self.assertIn("Reset-Re3DRestrictedSecretAcl -Key $key", uninstall)
        self.assertLess(
            uninstall.index("Reset-Re3DRestrictedSecretAcl -Key $key", uninstall.index("if (-not $controller)")),
            uninstall.index("continue", uninstall.index("if (-not $controller)")),
        )
        self.assertIn("Service SID access was removed", uninstall)
        self.assertIn("-AllowOptionalServiceSecretAcls", uninstall)
        self.assertNotIn("Remove-Item", uninstall)

    def test_manifest_validates_fixed_service_identities_and_repair_acl_scope(self) -> None:
        common = read_script("common.ps1")
        restricted_common = (
            ROOT / "deploy" / "restricted" / "common.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn('$record.account -ine $expectedIdentity', common)
        self.assertIn('$record.virtual_identity -ine $expectedIdentity', common)
        self.assertIn("Get-Re3DRestrictedVirtualServiceSid", restricted_common)
        self.assertIn("[switch]$AllowOptionalServiceSecretAcls", common)
        self.assertIn("$serviceInstalled = $null -ne", common)
        self.assertIn(
            "-VirtualServiceSidOptional:($AllowOptionalServiceSecretAcls -or -not $serviceInstalled)",
            common,
        )
        self.assertIn("ExpectedVirtualServiceSid", restricted_common)
        self.assertIn("$VirtualServiceSidOptional -and $rights -eq 0", restricted_common)

        readiness = (
            ROOT / "deploy" / "restricted" / "check-restricted-readiness.ps1"
        ).read_text(encoding="utf-8")
        installer = read_script("install-service-bundle.ps1")
        self.assertIn("[string]$VirtualService", readiness)
        self.assertIn("-ExpectedVirtualServiceSid", readiness)
        self.assertIn("-VirtualServiceSidOptional:$VirtualServiceSidOptional", readiness)
        self.assertIn("-VirtualService Re3DRestrictedApi", installer)
        self.assertIn("-VirtualService Re3DRestrictedWorker", installer)
        self.assertGreaterEqual(installer.count("-VirtualServiceSidOptional"), 2)

    def test_preinstall_defers_only_smtp_connectivity_and_runtime_rechecks_it(self) -> None:
        readiness = (
            ROOT / "deploy" / "restricted" / "check-restricted-readiness.ps1"
        ).read_text(encoding="utf-8")
        installer = read_script("install-service-bundle.ps1")
        installed_test = read_script("test-installed-services.ps1")

        self.assertIn("[switch]$DeferSmtpConnectivityCheck", readiness)
        self.assertIn('if ($Component -ceq "worker") { ".env.worker.restricted" }', readiness)
        self.assertIn('if ($Component -eq "all")', readiness)
        self.assertNotIn('$Component -in @("worker", "all")', readiness)
        self.assertEqual(installer.count("-DeferSmtpConnectivityCheck"), 1)
        self.assertIn("smtp.connectivity_checked -ne $false", installer)
        self.assertLess(
            installer.index("-Component api"),
            installer.index("-Component worker"),
        )
        self.assertIn("smtp.connectivity_checked -ne $true", installed_test)
        self.assertIn("-VirtualService Re3DRestrictedApi", installed_test)
        self.assertNotIn("DeferSmtpConnectivityCheck", installed_test)

    def test_worker_acceptance_requires_current_process_evidence(self) -> None:
        installed_test = read_script("test-installed-services.ps1")
        worker_main = (ROOT / "apps" / "worker" / "main.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("Get-Re3DDescendantProcesses", installed_test)
        self.assertIn("run-real-queued-loop", installed_test)
        self.assertIn("external.platform_python", installed_test)
        self.assertIn("ExecutablePath", installed_test)
        self.assertIn("CreationDate", installed_test)
        self.assertIn("process_id", installed_test)
        self.assertIn("emitted_at_utc", installed_test)
        self.assertIn('$record.status -ceq "ready"', installed_test)
        self.assertIn('$record.status -ceq "started"', installed_test)
        self.assertIn("[int]$record.process_id -ne $processId", installed_test)
        self.assertIn("$emittedAtUtc -gt [DateTime]::UtcNow.AddSeconds(5)", installed_test)
        self.assertIn("$preflight -le $loop", installed_test)
        self.assertIn(
            "(ConvertTo-Re3DUtcDateTime -Value $_.CreationDate) -eq $processStartedAtUtc",
            installed_test,
        )
        self.assertIn("fresh preflight and stable queue-loop evidence", installed_test)
        self.assertIn('"process_id": os.getpid()', worker_main)
        self.assertIn('"emitted_at_utc":', worker_main)

    def test_endpoint_acceptance_binds_ports_to_service_process_trees(self) -> None:
        installed_test = read_script("test-installed-services.ps1")

        self.assertIn("Wait-Re3DListenerOwnership", installed_test)
        self.assertIn("OwningProcess", installed_test)
        self.assertIn("ExecutablePath", installed_test)
        self.assertIn("Get-Re3DDescendantProcesses", installed_test)
        self.assertIn("external.platform_python", installed_test)
        for port in (1025, 8025, 8000, 8080):
            self.assertIn(f"-Port {port}", installed_test)
        ownership_index = installed_test.index("$listenerResults =")
        api_probe_index = installed_test.index(
            'Wait-RestrictedRequest -Uri "http://127.0.0.1:8000/health/live"'
        )
        self.assertLess(ownership_index, api_probe_index)

    def test_status_command_is_read_only_and_covers_all_loopback_ports(self) -> None:
        status = read_script("get-service-bundle-status.ps1")

        for port in (1025, 8025, 8000, 8080):
            self.assertIn(str(port), status)
        self.assertIn('"not-installed"', status)
        self.assertIn('"partial"', status)
        self.assertNotIn("Start-Service", status)
        self.assertNotIn("Stop-Service", status)
        self.assertNotIn("Remove-Item", status)


if __name__ == "__main__":
    unittest.main()
