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

    def test_server_initializer_validates_before_mutating_and_keeps_secrets_local(
        self,
    ) -> None:
        initializer = (
            ROOT / "deploy/restricted/initialize-server-restricted.ps1"
        ).read_text(encoding="utf-8")
        validation = initializer.index("Test-Re3DPostgresAdministratorPassword")
        bootstrap = initializer.index('"bootstrap-restricted.ps1"')
        self.assertLess(validation, bootstrap)
        self.assertIn("Read-Host", initializer)
        self.assertIn("-AsSecureString", initializer)
        self.assertIn("RandomNumberGenerator]::Create()", initializer)
        self.assertIn("GetBytes($passwordBytes)", initializer)
        self.assertIn("Refusing to rotate database credentials", initializer)
        self.assertIn('"migrate-restricted.ps1"', initializer)
        self.assertIn('"initialize-restricted-env.ps1"', initializer)
        self.assertIn('"check-restricted-readiness.ps1"', initializer)
        self.assertIn("[Security.SecureString]$AdminPassword", initializer)
        self.assertIn("AppendChar($character)", initializer)
        self.assertIn("MakeReadOnly()", initializer)
        self.assertNotIn("ConvertTo-SecureString", initializer)
        self.assertIn("exit 1", initializer)
        self.assertNotIn("D:\\3Dreconstruction", initializer)

    def test_postgres_admin_recovery_is_scoped_and_fail_safe(self) -> None:
        recover = (
            ROOT / "deploy/postgres/recover-admin-password.ps1"
        ).read_text(encoding="utf-8")
        restore = (
            ROOT / "deploy/postgres/restore-pg-hba.ps1"
        ).read_text(encoding="utf-8")
        consumer = (
            ROOT
            / "deploy/restricted/initialize-server-from-recovery-envelope.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "host postgres postgres 127.0.0.1/32 trust",
            recover,
        )
        for forbidden_rule in (
            "host all all 0.0.0.0/0",
            "host all all ::/0",
            "host all all 127.0.0.1/32 trust",
        ):
            self.assertNotIn(forbidden_rule, recover)
        self.assertNotIn("ALTER ROLE", recover)
        self.assertNotIn('"\\password postgres"', recover)
        self.assertIn("PQchangePassword", recover)
        self.assertIn("PQconnectdbParams", recover)
        self.assertIn("PQconnectionUsedPassword", recover)
        self.assertIn("PQexec", recover)
        self.assertIn("PQresultStatus", recover)
        self.assertIn("PQclear", recover)
        self.assertIn("PQfinish", recover)
        self.assertIn('Join-Path $PostgresBin "libpq.dll"', recover)
        self.assertIn("LoadLibraryExW", recover)
        self.assertIn("[uint32]0x00001100", recover)
        self.assertIn("GetProcAddress", recover)
        self.assertIn("GetDelegateForFunctionPointer", recover)
        self.assertIn("AllocHGlobal", recover)
        self.assertIn("FreeHGlobal", recover)
        self.assertIn("FreeLibrary", recover)
        self.assertIn("Expected PostgreSQL 18 libpq", recover)
        self.assertIn('-RequireAuth "none"', recover)
        self.assertIn('-RequireAuth "scram-sha-256"', recover)
        self.assertIn("password_encryption=scram-sha-256", recover)
        self.assertIn('$PSBoundParameters.ContainsKey("Password")', recover)
        self.assertIn('passfile = $noPasswordFile', recover)
        self.assertIn("-csearch_path=", recover)
        self.assertIn("pg_catalog.pg_hba_file_rules", recover)
        self.assertNotIn('SetEnvironmentVariable(\n            "PATH"', recover)
        self.assertIn("RedirectStandardInput = $true", recover)
        self.assertIn("WaitForExit(30000)", recover)
        self.assertIn('EnvironmentVariables["PGPASSFILE"]', recover)
        self.assertIn('Invoke-Re3DPsqlScalar -Sql "SHOW hba_file;"', recover)
        self.assertIn("pg_hba_file_rules", recover)
        self.assertIn("[IO.File]::Replace", recover)
        self.assertIn(
            "$replacementBackupPath = Join-Path $directory (",
            recover,
        )
        self.assertRegex(
            recover,
            r"(?s)\[IO\.File\]::Replace\(\s*"
            r"\$temporaryPath,\s*\$Path,\s*"
            r"\$replacementBackupPath,\s*\$true\s*\)",
        )
        self.assertRegex(
            recover,
            r"(?s)foreach \(\$cleanupPath in @\(\s*"
            r"\$temporaryPath,\s*\$replacementBackupPath\s*\)\)",
        )
        self.assertIn("Register-Re3DHbaRollback", recover)
        self.assertIn(
            '$restoreScript = Join-Path $runDirectory "restore-pg-hba.ps1"',
            recover,
        )
        self.assertIn("restore_script_sha256", recover)
        self.assertIn("-RestoreScript $restoreScript", recover)
        self.assertNotIn("-RestoreScript $restoreScriptSource", recover)
        self.assertNotIn("common.ps1", recover)
        protected_copy_index = recover.index("-Path $restoreScript `")
        rollback_registration_index = recover.rindex("Register-Re3DHbaRollback `")
        self.assertLess(protected_copy_index, rollback_registration_index)
        libpq_preflight_index = recover.index(
            "$libpqContext = Open-Re3DLibpqContext"
        )
        hba_replacement_index = recover.rindex("Set-Re3DFileAtomically `")
        self.assertLess(libpq_preflight_index, hba_replacement_index)
        self.assertIn("New-ScheduledTaskPrincipal", recover)
        self.assertIn('-UserId "SYSTEM"', recover)
        self.assertIn("-StartWhenAvailable", recover)
        self.assertIn("-RestartCount 3", recover)
        self.assertIn("postmaster.pid", recover)
        self.assertIn("original_sddl", recover)
        self.assertIn("backup_sha256", recover)

        self.assertIn("[IO.File]::Replace", restore)
        self.assertIn(
            "$replacementBackupPath = Join-Path $targetDirectory (",
            restore,
        )
        self.assertRegex(
            restore,
            r"(?s)\[IO\.File\]::Replace\(\s*"
            r"\$temporaryPath,\s*\$Target,\s*"
            r"\$replacementBackupPath,\s*\$true\s*\)",
        )
        self.assertRegex(
            restore,
            r"(?s)foreach \(\$cleanupPath in @\(\s*"
            r"\$temporaryPath,\s*\$replacementBackupPath\s*\)\)",
        )
        self.assertIn("Get-Re3DSha256", restore)
        self.assertIn("Assert-Re3DAdminOnlyAcl", restore)
        self.assertIn("restore_script_sha256", restore)
        self.assertIn("Protected rollback-helper hash", restore)
        self.assertNotIn("common.ps1", restore)
        self.assertIn("Restart-Service -Name $serviceName", restore)
        self.assertIn("Unregister-ScheduledTask", restore)
        self.assertIn("[switch]$RequireRollbackTaskRemoval", restore)
        self.assertIn("-RequireRollbackTaskRemoval", recover)
        self.assertIn("POSTGRESQL_HBA_RESTORED", restore)

        for content in (recover, consumer):
            self.assertIn("Add-Type -AssemblyName System.Security", content)
            self.assertIn("ProtectedData", content)
            self.assertIn("DataProtectionScope]::LocalMachine", content)
            self.assertNotIn("RandomNumberGenerator]::Fill", content)
            self.assertNotIn("Convert]::ToHexString", content)
            self.assertNotIn("ArgumentList", content)
            self.assertNotIn("ConvertTo-SecureString", content)
            self.assertNotIn("Get-Acl", content)
            self.assertNotIn("Set-Acl", content)

        self.assertIn("ProtectedData]::Unprotect", consumer)
        self.assertIn("target_sid", consumer)
        self.assertIn("machine_name", consumer)
        self.assertIn("expires_at_utc", consumer)
        self.assertIn("database_host", consumer)
        self.assertIn('admin_role -cne "postgres"', consumer)
        self.assertIn("AppendChar($character)", consumer)
        self.assertIn("MakeReadOnly()", consumer)
        self.assertIn("[IO.File]::Delete($resolvedEnvelope)", consumer)
        delete_index = consumer.index("[IO.File]::Delete($resolvedEnvelope)")
        initialize_index = consumer.index("& $initializer")
        self.assertLess(delete_index, initialize_index)
        self.assertIn("-AdminPassword $securePassword", consumer)
        self.assertIn("$initializerExitCode = $LASTEXITCODE", consumer)
        self.assertIn("exit $initializerExitCode", consumer)

    def test_restricted_environment_files_receive_protected_acls(self) -> None:
        common = (ROOT / "deploy/restricted/common.ps1").read_text(encoding="utf-8")
        initializer = (
            ROOT / "deploy/restricted/initialize-restricted-env.ps1"
        ).read_text(encoding="utf-8")
        readiness = (
            ROOT / "deploy/restricted/check-restricted-readiness.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("SetAccessRuleProtection($true, $false)", common)
        self.assertIn("[IO.File]::SetAccessControl($resolved, $acl)", common)
        self.assertNotIn("Set-Acl", common)
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

    def test_restricted_caddy_is_pinned_and_http_loopback_only(self) -> None:
        installer = (ROOT / "deploy/restricted/install-caddy.ps1").read_text(
            encoding="utf-8"
        )
        runner = (ROOT / "deploy/restricted/run-caddy.ps1").read_text(
            encoding="utf-8"
        )
        caddyfile = (ROOT / "deploy/restricted/Caddyfile").read_text(
            encoding="utf-8"
        )
        archive_sha512 = (
            "cd5ccfd86a4b40732cf715890d0dca5bf3f63adefec5a7914de85adf240c60ce"
            "7e5d2791631b88ef9758e46b23bb1730e020b9c5d696889740b284ffd4788e35"
        )
        executable_sha256 = (
            "5cb9ab71e5756ce72840b8234177a2f40c8b4ab47a806b8e841e2b784e9df62b"
        )
        self.assertIn(archive_sha512, installer)
        self.assertIn(executable_sha256, installer)
        self.assertIn(executable_sha256, runner)
        self.assertIn("http://127.0.0.1:8080", caddyfile)
        self.assertIn("bind 127.0.0.1", caddyfile)
        self.assertIn("reverse_proxy 127.0.0.1:8000", caddyfile)
        self.assertNotIn("Strict-Transport-Security", caddyfile)
        self.assertIn("admin off", caddyfile)

    def test_restricted_stack_tracks_process_identity_before_stopping(self) -> None:
        process_common = (
            ROOT / "deploy/restricted/process-common.ps1"
        ).read_text(encoding="utf-8")
        start = (
            ROOT / "deploy/restricted/start-restricted-stack.ps1"
        ).read_text(encoding="utf-8")
        stop = (
            ROOT / "deploy/restricted/stop-restricted-stack.ps1"
        ).read_text(encoding="utf-8")
        status = (
            ROOT / "deploy/restricted/get-restricted-stack-status.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("start_time_utc_ticks", process_common)
        self.assertIn("identity-mismatch", process_common)
        self.assertIn("taskkill.exe", process_common)
        self.assertIn("No process was stopped", process_common)
        self.assertIn('127.0.0.1" -Port 8000', start)
        self.assertIn('127.0.0.1" -Port 8080', start)
        self.assertIn('"operation": "worker-runtime-preflight"', start)
        self.assertIn('@("caddy", "worker", "api")', stop)
        self.assertIn('network_scope = "loopback-only"', status)

    def test_restricted_scheduled_task_is_on_demand_and_passwordless(self) -> None:
        restricted = ROOT / "deploy" / "restricted"
        common = (restricted / "scheduled-task-common.ps1").read_text(
            encoding="utf-8"
        )
        install = (restricted / "install-restricted-scheduled-task.ps1").read_text(
            encoding="utf-8"
        )
        start_task = (
            restricted / "start-restricted-scheduled-task.ps1"
        ).read_text(encoding="utf-8")
        stop_task = (
            restricted / "stop-restricted-scheduled-task.ps1"
        ).read_text(encoding="utf-8")
        status_task = (
            restricted / "get-restricted-scheduled-task-status.ps1"
        ).read_text(encoding="utf-8")
        uninstall = (
            restricted / "uninstall-restricted-scheduled-task.ps1"
        ).read_text(encoding="utf-8")
        provision = (
            restricted / "provision-restricted-task-folder.ps1"
        ).read_text(encoding="utf-8")
        deprovision = (
            restricted / "deprovision-restricted-task-folder.ps1"
        ).read_text(encoding="utf-8")
        stack_start = (restricted / "start-restricted-stack.ps1").read_text(
            encoding="utf-8"
        )
        mailpit_start = (ROOT / "deploy/mailpit/start-standalone.ps1").read_text(
            encoding="utf-8"
        )
        mailpit_stop = (ROOT / "deploy/mailpit/stop-standalone.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn("[switch]$StayAttached", stack_start)
        self.assertIn("$consecutiveEndpointFailures -ge 3", stack_start)
        for port in (1025, 8025, 8000, 8080):
            self.assertIn(f'= {port}', stack_start)
        self.assertIn("-SkipWebBuild -StayAttached", common)
        self.assertIn("WindowsPowerShell\\v1.0\\powershell.exe", common)
        self.assertIn('task_folder_path = "\\Re3D"', common)
        self.assertIn('task_name = "RestrictedStack"', common)
        self.assertIn('New-Object -ComObject "Schedule.Service"', common)
        self.assertIn("Principal.LogonType -ne 2", common)
        self.assertIn("Principal.RunLevel -ne 0", common)
        self.assertIn("settings.MultipleInstances -ne 2", common)
        self.assertIn("Get-Re3DRestrictedAccountSid", common)
        self.assertNotIn("Get-ScheduledTask", common)
        self.assertIn("GetSecurityDescriptor(4)", common)
        self.assertIn("DiscretionaryAclProtected", common)
        self.assertIn('"S-1-5-18"', common)
        self.assertIn('"S-1-5-32-544"', common)

        self.assertIn("Assert-Re3DRestrictedTaskOwnerIdentity", install)
        self.assertIn('Get-LocalUser -Name "re3d-deploy"', install)
        self.assertIn("$definition.Principal.LogonType = 2", install)
        self.assertIn("$definition.Principal.RunLevel = 0", install)
        self.assertIn("$definition.Settings.MultipleInstances = 2", install)
        self.assertIn('$definition.Settings.ExecutionTimeLimit = "PT0S"', install)
        self.assertIn("$definition.Triggers", common)
        self.assertIn("$definition.Settings.RestartCount = 0", install)
        self.assertIn("$definition.Settings.StartWhenAvailable = $false", install)
        self.assertIn("$definition.Actions.Create(0)", install)
        self.assertIn("$folder.RegisterTaskDefinition", install)
        self.assertIn("$context.run_as_user", install)
        self.assertNotIn("New-ScheduledTask", install)
        self.assertNotIn("Register-ScheduledTask", install)
        self.assertNotIn("Password", install)
        self.assertIn("Refusing to overwrite it", install)
        self.assertIn("definition.Triggers.Count -ne 0", common)
        self.assertIn("settings.RestartCount -ne 0", common)
        self.assertIn("AllowDemandStart", common)
        self.assertIn("StartWhenAvailable", common)

        self.assertIn("$task.Run($null)", start_task)
        self.assertIn("$dispatchObserved", start_task)
        self.assertIn("$observedRunning", start_task)
        self.assertIn("-RequireReady", start_task)
        self.assertIn("get-restricted-scheduled-task-status.ps1", start_task)
        self.assertIn('status.status -ceq "ready"', start_task)
        self.assertLess(
            stop_task.index("$task.Stop(0)"),
            stop_task.index('"deploy\\restricted\\stop-restricted-stack.ps1"'),
        )
        self.assertIn('status.status -cne "stopped"', stop_task)
        self.assertIn("definition_valid", status_task)
        self.assertIn("security_error", status_task)
        self.assertIn('"orphaned-stack"', status_task)
        self.assertIn("$folder.DeleteTask", uninstall)
        self.assertIn("$ForceUnsafeDefinition", uninstall)
        self.assertIn("Data and logs were retained", uninstall)
        self.assertIn("Assert-Re3DRestrictedTaskAdministrator", provision)
        self.assertIn("Get-Re3DRestrictedTaskFolderSddl", provision)
        self.assertIn("$rootFolder.CreateFolder", provision)
        self.assertIn("$rootFolder.DeleteFolder", provision)
        self.assertIn("$folder.GetTasks(1).Count", deprovision)
        self.assertIn("$folder.GetFolders(0).Count", deprovision)
        self.assertIn("$rootFolder.DeleteFolder", deprovision)
        self.assertNotIn("exit 0", mailpit_start)
        self.assertNotIn("exit 0", mailpit_stop)


if __name__ == "__main__":
    unittest.main()
