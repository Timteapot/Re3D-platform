#Requires -Version 5.1
#Requires -RunAsAdministrator

[CmdletBinding()]
param(
    [string]$CredentialConsumer = "$env:COMPUTERNAME\re3d-deploy",
    [string]$PasswordEnvelopePath = "",
    [string]$BackupRoot = "",
    [string]$ServiceName = "",
    [string]$DataDirectory = "",
    [ValidateRange(2, 30)]
    [int]$RollbackDelayMinutes = 5,
    [ValidateRange(15, 1440)]
    [int]$EnvelopeLifetimeMinutes = 240
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Security -ErrorAction Stop

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$workspaceRoot = Split-Path -Parent $projectRoot

$adminRole = "postgres"
$DatabasePort = 5432
$envelopePurpose = "Re3D PostgreSQL administrator recovery envelope v1"
$securitySections = (
    [Security.AccessControl.AccessControlSections]::Access -bor
    [Security.AccessControl.AccessControlSections]::Owner -bor
    [Security.AccessControl.AccessControlSections]::Group
)
$systemSid = New-Object Security.Principal.SecurityIdentifier("S-1-5-18")
$administratorsSid = New-Object Security.Principal.SecurityIdentifier(
    "S-1-5-32-544"
)
$consumerSid = (
    New-Object Security.Principal.NTAccount($CredentialConsumer)
).Translate([Security.Principal.SecurityIdentifier])

function Test-Re3DPathWithin {
    param(
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Root
    )

    $candidatePath = [IO.Path]::GetFullPath($Candidate)
    $rootPath = [IO.Path]::GetFullPath($Root).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    return $candidatePath.Equals(
        $rootPath,
        [StringComparison]::OrdinalIgnoreCase
    ) -or $candidatePath.StartsWith(
        "$rootPath$([IO.Path]::DirectorySeparatorChar)",
        [StringComparison]::OrdinalIgnoreCase
    )
}

function Get-Re3DSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = [IO.File]::OpenRead($Path)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    $digest = $null
    try {
        $digest = $sha256.ComputeHash($stream)
        return [BitConverter]::ToString($digest).Replace("-", "")
    } finally {
        $sha256.Dispose()
        $stream.Dispose()
        if ($null -ne $digest) {
            [Array]::Clear($digest, 0, $digest.Length)
        }
    }
}

function New-Re3DFileSecurity {
    param([Security.Principal.SecurityIdentifier]$ConsumerSid)

    $security = New-Object Security.AccessControl.FileSecurity
    $security.SetOwner($systemSid)
    $security.SetAccessRuleProtection($true, $false)
    foreach ($sid in @($systemSid, $administratorsSid)) {
        $security.AddAccessRule(
            (New-Object Security.AccessControl.FileSystemAccessRule(
                $sid,
                [Security.AccessControl.FileSystemRights]::FullControl,
                [Security.AccessControl.AccessControlType]::Allow
            ))
        )
    }
    if ($null -ne $ConsumerSid) {
        $security.AddAccessRule(
            (New-Object Security.AccessControl.FileSystemAccessRule(
                $ConsumerSid,
                [Security.AccessControl.FileSystemRights]::Modify,
                [Security.AccessControl.AccessControlType]::Allow
            ))
        )
    }
    return $security
}

function Set-Re3DProtectedDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Security.Principal.SecurityIdentifier]$ConsumerSid
    )

    [IO.Directory]::CreateDirectory($Path) | Out-Null
    $security = New-Object Security.AccessControl.DirectorySecurity
    $security.SetOwner($systemSid)
    $security.SetAccessRuleProtection($true, $false)
    foreach ($sid in @($systemSid, $administratorsSid)) {
        $security.AddAccessRule(
            (New-Object Security.AccessControl.FileSystemAccessRule(
                $sid,
                [Security.AccessControl.FileSystemRights]::FullControl,
                (
                    [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
                    [Security.AccessControl.InheritanceFlags]::ObjectInherit
                ),
                [Security.AccessControl.PropagationFlags]::None,
                [Security.AccessControl.AccessControlType]::Allow
            ))
        )
    }
    if ($null -ne $ConsumerSid) {
        $security.AddAccessRule(
            (New-Object Security.AccessControl.FileSystemAccessRule(
                $ConsumerSid,
                [Security.AccessControl.FileSystemRights]::Modify,
                (
                    [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
                    [Security.AccessControl.InheritanceFlags]::ObjectInherit
                ),
                [Security.AccessControl.PropagationFlags]::None,
                [Security.AccessControl.AccessControlType]::Allow
            ))
        )
    }
    [IO.Directory]::SetAccessControl($Path, $security)
}

function Assert-Re3DNoReparsePoint {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Root
    )

    $resolvedRoot = (Resolve-Path -LiteralPath $Root -ErrorAction Stop).Path
    $currentPath = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    if (-not (Test-Re3DPathWithin `
        -Candidate $currentPath `
        -Root $resolvedRoot
    )) {
        throw "Recovery path is outside the workspace root: $currentPath"
    }
    while ($true) {
        $item = Get-Item -LiteralPath $currentPath -Force -ErrorAction Stop
        if (
            ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
        ) {
            throw "Recovery path contains a reparse point: $currentPath"
        }
        if ($currentPath.Equals(
            $resolvedRoot,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            break
        }
        $currentPath = [IO.Path]::GetFullPath(
            (Split-Path -Parent $currentPath)
        )
    }
}

function Test-Re3DContainsByteSequence {
    param(
        [Parameter(Mandatory = $true)][byte[]]$Bytes,
        [Parameter(Mandatory = $true)][byte[]]$Sequence
    )

    if ($Sequence.Length -eq 0 -or $Bytes.Length -lt $Sequence.Length) {
        return $false
    }
    for ($offset = 0; $offset -le $Bytes.Length - $Sequence.Length; $offset++) {
        $matches = $true
        for ($index = 0; $index -lt $Sequence.Length; $index++) {
            if ($Bytes[$offset + $index] -ne $Sequence[$index]) {
                $matches = $false
                break
            }
        }
        if ($matches) {
            return $true
        }
    }
    return $false
}

function Write-Re3DProtectedFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][byte[]]$Bytes,
        [Security.Principal.SecurityIdentifier]$ConsumerSid
    )

    if (Test-Path -LiteralPath $Path) {
        throw "Refusing to overwrite protected recovery file: $Path"
    }
    $directory = Split-Path -Parent $Path
    $temporaryPath = Join-Path $directory (
        ".re3d-protected-$([Guid]::NewGuid().ToString('N')).tmp"
    )
    $stream = $null
    try {
        $stream = New-Object IO.FileStream(
            $temporaryPath,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
        $stream.Dispose()
        $stream = $null
        $security = New-Re3DFileSecurity -ConsumerSid $ConsumerSid
        [IO.File]::SetAccessControl($temporaryPath, $security)
        [IO.File]::WriteAllBytes($temporaryPath, $Bytes)
        [IO.File]::Move($temporaryPath, $Path)
    } finally {
        if ($null -ne $stream) {
            $stream.Dispose()
        }
        if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
            [IO.File]::Delete($temporaryPath)
        }
    }
}

function Set-Re3DFileAtomically {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][byte[]]$Bytes,
        [Parameter(Mandatory = $true)][byte[]]$SecurityDescriptor
    )

    $directory = Split-Path -Parent $Path
    $temporaryPath = Join-Path $directory (
        ".re3d-hba-update-$([Guid]::NewGuid().ToString('N')).tmp"
    )
    $replacementBackupPath = Join-Path $directory (
        ".re3d-hba-update-backup-$([Guid]::NewGuid().ToString('N')).tmp"
    )
    $replacementFailure = $null
    try {
        [IO.File]::WriteAllBytes($temporaryPath, $Bytes)
        $security = New-Object Security.AccessControl.FileSecurity
        $security.SetSecurityDescriptorBinaryForm(
            $SecurityDescriptor,
            $securitySections
        )
        [IO.File]::SetAccessControl($temporaryPath, $security)
        [IO.File]::Replace(
            $temporaryPath,
            $Path,
            $replacementBackupPath,
            $true
        )
        [IO.File]::SetAccessControl($Path, $security)
    } catch {
        $replacementFailure = $_
    }

    $cleanupFailures = @()
    foreach ($cleanupPath in @(
        $temporaryPath,
        $replacementBackupPath
    )) {
        try {
            if (Test-Path -LiteralPath $cleanupPath -PathType Leaf) {
                [IO.File]::Delete($cleanupPath)
            }
        } catch {
            $cleanupFailures += (
                "${cleanupPath}: $($_.Exception.Message)"
            )
        }
    }

    if ($null -ne $replacementFailure) {
        if ($cleanupFailures.Count -gt 0) {
            throw (
                "Atomic HBA replacement failed: " +
                "$($replacementFailure.Exception.Message). " +
                "Temporary-file cleanup also failed: " +
                ($cleanupFailures -join "; ")
            )
        }
        throw $replacementFailure
    }
    if ($cleanupFailures.Count -gt 0) {
        throw (
            "Atomic HBA replacement completed, but temporary-file cleanup " +
            "failed: " + ($cleanupFailures -join "; ")
        )
    }
}

function Get-Re3DPostgresService {
    if ([string]::IsNullOrWhiteSpace($ServiceName)) {
        $candidates = @(
            Get-CimInstance Win32_Service |
                Where-Object {
                    $_.Name -like "postgresql*" -and
                    $_.PathName -match '(?i)pg_ctl(?:\.exe)?'
                }
        )
    } else {
        $candidates = @(
            Get-CimInstance Win32_Service |
                Where-Object { $_.Name -ceq $ServiceName }
        )
    }
    if ($candidates.Count -ne 1) {
        $names = ($candidates | Select-Object -ExpandProperty Name) -join ", "
        throw (
            "Expected exactly one PostgreSQL service but found " +
            "$($candidates.Count): $names. Pass -ServiceName explicitly."
        )
    }
    if ($candidates[0].PathName -notmatch '(?i)pg_ctl(?:\.exe)?') {
        throw "Selected service does not use pg_ctl: $($candidates[0].Name)"
    }
    if ($candidates[0].State -ne "Running") {
        throw "PostgreSQL service is not running: $($candidates[0].Name)"
    }
    return $candidates[0]
}

function Get-Re3DPgCtlPath {
    param([Parameter(Mandatory = $true)][string]$PathName)

    if ($PathName -match '^\s*"([^"]*\\pg_ctl(?:\.exe)?)"') {
        return $Matches[1]
    }
    if ($PathName -match '^\s*([^\s]*\\pg_ctl(?:\.exe)?)') {
        return $Matches[1]
    }
    throw "Could not resolve pg_ctl from the PostgreSQL service command."
}

function Get-Re3DDataDirectoryFromService {
    param([Parameter(Mandatory = $true)][string]$PathName)

    if ($PathName -match '(?i)(?:^|\s)-D\s+"([^"]+)"') {
        return $Matches[1]
    }
    if ($PathName -match "(?i)(?:^|\s)-D\s+'([^']+)'") {
        return $Matches[1]
    }
    if ($PathName -match '(?i)(?:^|\s)-D\s+([^\s]+)') {
        return $Matches[1]
    }
    throw "Could not resolve the PostgreSQL data directory from the service command."
}

function Restart-Re3DPostgresService {
    param([Parameter(Mandatory = $true)][string]$Name)

    Restart-Service -Name $Name -Force -ErrorAction Stop
    $service = Get-Service -Name $Name -ErrorAction Stop
    $service.WaitForStatus(
        [ServiceProcess.ServiceControllerStatus]::Running,
        [TimeSpan]::FromSeconds(60)
    )
    if ($service.Status -ne [ServiceProcess.ServiceControllerStatus]::Running) {
        throw "PostgreSQL service did not return to Running."
    }
}

function New-Re3DRecoveryPassword {
    $bytes = New-Object byte[] 48
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $random.GetBytes($bytes)
        return [Convert]::ToBase64String($bytes)
    } finally {
        $random.Dispose()
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
}

function Invoke-Re3DPsqlInput {
    param(
        [Parameter(Mandatory = $true)][string[]]$InputLines,
        [string]$Password = ""
    )

    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = $psqlPath
    $startInfo.Arguments = (
        "-X --no-password -A -t -q -v ON_ERROR_STOP=1 " +
        "-h 127.0.0.1 -p $DatabasePort -U postgres -d postgres"
    )
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    [void]$startInfo.EnvironmentVariables.Remove("PGPASSWORD")
    [void]$startInfo.EnvironmentVariables.Remove("PGSERVICE")
    [void]$startInfo.EnvironmentVariables.Remove("PGSERVICEFILE")
    $startInfo.EnvironmentVariables["PGPASSFILE"] = $noPasswordFile
    $startInfo.EnvironmentVariables["PGCONNECT_TIMEOUT"] = "5"
    $startInfo.EnvironmentVariables["PGSSLMODE"] = "disable"
    $startInfo.EnvironmentVariables["PGAPPNAME"] = "re3d-admin-recovery"
    if (-not [string]::IsNullOrEmpty($Password)) {
        $startInfo.EnvironmentVariables["PGPASSWORD"] = $Password
    }

    $process = New-Object Diagnostics.Process
    $process.StartInfo = $startInfo
    $standardOutput = $null
    $standardError = $null
    $output = $null
    $errorOutput = $null
    try {
        if (-not $process.Start()) {
            throw "Could not start psql."
        }
        $standardOutput = $process.StandardOutput.ReadToEndAsync()
        $standardError = $process.StandardError.ReadToEndAsync()
        foreach ($line in $InputLines) {
            $process.StandardInput.WriteLine($line)
        }
        $process.StandardInput.Close()
        if (-not $process.WaitForExit(30000)) {
            $process.Kill()
            $process.WaitForExit()
            throw "psql did not finish within 30 seconds and was terminated."
        }
        $output = $standardOutput.Result
        $errorOutput = $standardError.Result
        if ($process.ExitCode -ne 0) {
            throw "psql exited with code $($process.ExitCode)."
        }
        return $output.Trim()
    } finally {
        $output = $null
        $errorOutput = $null
        $standardOutput = $null
        $standardError = $null
        $process.Dispose()
    }
}

function Invoke-Re3DPsqlScalar {
    param(
        [Parameter(Mandatory = $true)][string]$Sql,
        [string]$Password = ""
    )

    return Invoke-Re3DPsqlInput `
        -InputLines @($Sql, "\q") `
        -Password $Password
}

function Set-Re3DPostgresPasswordSecurely {
    param([Parameter(Mandatory = $true)][string]$Password)

    Invoke-Re3DPsqlInput `
        -InputLines @("\password postgres", $Password, $Password, "\q") |
        Out-Null
}

function Register-Re3DHbaRollback {
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$RestoreScript,
        [Parameter(Mandatory = $true)][string]$Manifest
    )

    $powerShell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $arguments = (
        '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass ' +
        "-File `"$RestoreScript`" -ManifestPath `"$Manifest`""
    )
    $action = New-ScheduledTaskAction `
        -Execute $powerShell `
        -Argument $arguments
    $trigger = New-ScheduledTaskTrigger `
        -Once `
        -At (Get-Date).AddMinutes($RollbackDelayMinutes)
    $principal = New-ScheduledTaskPrincipal `
        -UserId "SYSTEM" `
        -LogonType ServiceAccount `
        -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Force | Out-Null
}

function Write-Re3DPasswordEnvelope {
    param(
        [Parameter(Mandatory = $true)][string]$Password,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$RecoveryId
    )

    $payload = [ordered]@{
        schema_version = 1
        purpose = $envelopePurpose
        recovery_id = $RecoveryId
        target_sid = $consumerSid.Value
        machine_name = $env:COMPUTERNAME
        database_host = "127.0.0.1"
        database_port = $DatabasePort
        admin_role = $adminRole
        expires_at_utc = (Get-Date).ToUniversalTime().AddMinutes(
            $EnvelopeLifetimeMinutes
        ).ToString("o")
        password = $Password
    }
    $payloadJson = $payload | ConvertTo-Json -Compress
    $plainBytes = [Text.Encoding]::UTF8.GetBytes($payloadJson)
    $entropy = [Text.Encoding]::UTF8.GetBytes($envelopePurpose)
    $protectedBytes = $null
    try {
        $protectedBytes = [Security.Cryptography.ProtectedData]::Protect(
            $plainBytes,
            $entropy,
            [Security.Cryptography.DataProtectionScope]::LocalMachine
        )
        Write-Re3DProtectedFile `
            -Path $Path `
            -Bytes $protectedBytes `
            -ConsumerSid $consumerSid
    } finally {
        $payload.password = $null
        $payloadJson = $null
        if ($null -ne $plainBytes) {
            [Array]::Clear($plainBytes, 0, $plainBytes.Length)
        }
        if ($null -ne $entropy) {
            [Array]::Clear($entropy, 0, $entropy.Length)
        }
        if ($null -ne $protectedBytes) {
            [Array]::Clear($protectedBytes, 0, $protectedBytes.Length)
        }
    }
}

$allowedRecoveryRoot = [IO.Path]::GetFullPath(
    (Join-Path $workspaceRoot "Re3D-data\_recovery")
)
$allowedBackupRoot = [IO.Path]::GetFullPath(
    (Join-Path $allowedRecoveryRoot "postgresql")
)
$credentialDirectory = [IO.Path]::GetFullPath(
    (Join-Path $allowedRecoveryRoot "credentials")
)
if ([string]::IsNullOrWhiteSpace($BackupRoot)) {
    $BackupRoot = $allowedBackupRoot
}
$resolvedBackupRoot = [IO.Path]::GetFullPath($BackupRoot)
if (-not (Test-Re3DPathWithin `
    -Candidate $resolvedBackupRoot `
    -Root $allowedBackupRoot
)) {
    throw "Backup root must stay within $allowedBackupRoot"
}
if ([string]::IsNullOrWhiteSpace($PasswordEnvelopePath)) {
    $PasswordEnvelopePath = Join-Path (
        $credentialDirectory
    ) "postgres-admin-password.dpapi"
}
$resolvedEnvelope = [IO.Path]::GetFullPath($PasswordEnvelopePath)
$envelopeParent = [IO.Path]::GetFullPath(
    (Split-Path -Parent $resolvedEnvelope)
)
if (-not $envelopeParent.Equals(
    $credentialDirectory,
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw "Password envelope must be placed directly in $credentialDirectory"
}
if (Test-Path -LiteralPath $resolvedEnvelope) {
    throw "Password envelope already exists: $resolvedEnvelope"
}

[IO.Directory]::CreateDirectory($resolvedBackupRoot) | Out-Null
[IO.Directory]::CreateDirectory($credentialDirectory) | Out-Null
Assert-Re3DNoReparsePoint -Path $resolvedBackupRoot -Root $workspaceRoot
Assert-Re3DNoReparsePoint -Path $credentialDirectory -Root $workspaceRoot
Set-Re3DProtectedDirectory -Path $resolvedBackupRoot
Set-Re3DProtectedDirectory `
    -Path $credentialDirectory `
    -ConsumerSid $consumerSid
Assert-Re3DNoReparsePoint -Path $resolvedBackupRoot -Root $workspaceRoot
Assert-Re3DNoReparsePoint -Path $credentialDirectory -Root $workspaceRoot

$service = Get-Re3DPostgresService
$servicePgCtl = Get-Re3DPgCtlPath -PathName $service.PathName
$resolvedPgCtl = (
    Resolve-Path -LiteralPath $servicePgCtl -ErrorAction Stop
).Path
$serviceDataDirectory = Get-Re3DDataDirectoryFromService `
    -PathName $service.PathName
$resolvedServiceData = (
    Resolve-Path -LiteralPath $serviceDataDirectory -ErrorAction Stop
).Path
if ([string]::IsNullOrWhiteSpace($DataDirectory)) {
    $resolvedData = $resolvedServiceData
} else {
    $resolvedData = (
        Resolve-Path -LiteralPath $DataDirectory -ErrorAction Stop
    ).Path
    if (-not $resolvedData.Equals(
        $resolvedServiceData,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Requested data directory does not match the PostgreSQL service."
    }
}
$resolvedHba = (
    Resolve-Path -LiteralPath (Join-Path $resolvedData "pg_hba.conf") `
        -ErrorAction Stop
).Path
$postmasterPid = Join-Path $resolvedData "postmaster.pid"
$postmasterLines = [IO.File]::ReadAllLines($postmasterPid)
if ($postmasterLines.Length -lt 4) {
    throw "PostgreSQL postmaster.pid does not contain a port line."
}
$postmasterPort = 0
if (-not [int]::TryParse(
    $postmasterLines[3].Trim(),
    [ref]$postmasterPort
)) {
    throw "PostgreSQL postmaster.pid contains an invalid port."
}
if ($postmasterPort -ne $DatabasePort) {
    throw (
        "PostgreSQL service port $postmasterPort does not match requested " +
        "port $DatabasePort."
    )
}
$psqlPath = (
    Resolve-Path -LiteralPath (
        Join-Path (Split-Path -Parent $resolvedPgCtl) "psql.exe"
    ) -ErrorAction Stop
).Path
$restoreScriptSource = (
    Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "restore-pg-hba.ps1") `
        -ErrorAction Stop
).Path

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$runId = [Guid]::NewGuid().ToString("N")
$runDirectory = Join-Path $resolvedBackupRoot "$timestamp-$runId"
[IO.Directory]::CreateDirectory($runDirectory) | Out-Null
Assert-Re3DNoReparsePoint -Path $runDirectory -Root $resolvedBackupRoot
Set-Re3DProtectedDirectory -Path $runDirectory
Assert-Re3DNoReparsePoint -Path $runDirectory -Root $resolvedBackupRoot

$restoreScript = Join-Path $runDirectory "restore-pg-hba.ps1"
$restoreScriptBytes = [IO.File]::ReadAllBytes($restoreScriptSource)
try {
    Write-Re3DProtectedFile `
        -Path $restoreScript `
        -Bytes $restoreScriptBytes
} finally {
    [Array]::Clear($restoreScriptBytes, 0, $restoreScriptBytes.Length)
}
$restoreScriptHash = Get-Re3DSha256 -Path $restoreScript

$backupPath = Join-Path $runDirectory "pg_hba.conf.backup"
$manifestPath = Join-Path $runDirectory "recovery-manifest.json"
$rollbackTaskName = "Re3D-PgHba-Rollback-$runId"
$recoveryMarker = "# BEGIN Re3D temporary PostgreSQL recovery $runId"
$noPasswordFile = Join-Path $runDirectory ".no-pgpass-$runId"

$originalBytes = [IO.File]::ReadAllBytes($resolvedHba)
$existingMarkerBytes = [Text.Encoding]::ASCII.GetBytes(
    "# BEGIN Re3D temporary PostgreSQL recovery"
)
try {
    if (Test-Re3DContainsByteSequence `
        -Bytes $originalBytes `
        -Sequence $existingMarkerBytes
    ) {
        throw "pg_hba.conf already contains a Re3D temporary recovery marker."
    }
} finally {
    [Array]::Clear($existingMarkerBytes, 0, $existingMarkerBytes.Length)
}
$originalAcl = [IO.File]::GetAccessControl($resolvedHba, $securitySections)
$originalSddl = $originalAcl.GetSecurityDescriptorSddlForm($securitySections)
$originalDescriptor = $originalAcl.GetSecurityDescriptorBinaryForm()
Write-Re3DProtectedFile -Path $backupPath -Bytes $originalBytes
$originalHash = Get-Re3DSha256 -Path $backupPath

$manifest = [ordered]@{
    schema_version = 1
    created_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    service_name = $service.Name
    data_directory = $resolvedData
    hba_path = $resolvedHba
    backup_path = $backupPath
    backup_sha256 = $originalHash
    original_sddl = $originalSddl
    original_security_descriptor_base64 = [Convert]::ToBase64String(
        $originalDescriptor
    )
    rollback_task = $rollbackTaskName
    recovery_marker = $recoveryMarker
    restore_script_path = $restoreScript
    restore_script_sha256 = $restoreScriptHash
}
$manifestBytes = [Text.Encoding]::UTF8.GetBytes(
    ($manifest | ConvertTo-Json -Depth 3)
)
try {
    Write-Re3DProtectedFile -Path $manifestPath -Bytes $manifestBytes
} finally {
    [Array]::Clear($manifestBytes, 0, $manifestBytes.Length)
}

$plainPassword = New-Re3DRecoveryPassword
$rollbackRegistered = $false
$hbaModified = $false
$operationFailure = $null
$restoreFailure = $null
$prefixBytes = $null
$modifiedBytes = $null
try {
    Register-Re3DHbaRollback `
        -TaskName $rollbackTaskName `
        -RestoreScript $restoreScript `
        -Manifest $manifestPath
    $rollbackRegistered = $true

    $temporaryRule = (
        "$recoveryMarker`r`n" +
        "host postgres postgres 127.0.0.1/32 trust`r`n" +
        "# END Re3D temporary PostgreSQL recovery $runId`r`n"
    )
    $prefixBytes = [Text.Encoding]::ASCII.GetBytes($temporaryRule)
    if (
        $originalBytes.Length -ge 3 -and
        $originalBytes[0] -eq 0xEF -and
        $originalBytes[1] -eq 0xBB -and
        $originalBytes[2] -eq 0xBF
    ) {
        $modifiedBytes = New-Object byte[] (
            3 + $prefixBytes.Length + $originalBytes.Length - 3
        )
        [Array]::Copy($originalBytes, 0, $modifiedBytes, 0, 3)
        [Array]::Copy($prefixBytes, 0, $modifiedBytes, 3, $prefixBytes.Length)
        [Array]::Copy(
            $originalBytes,
            3,
            $modifiedBytes,
            3 + $prefixBytes.Length,
            $originalBytes.Length - 3
        )
    } else {
        $modifiedBytes = New-Object byte[] (
            $prefixBytes.Length + $originalBytes.Length
        )
        [Array]::Copy($prefixBytes, 0, $modifiedBytes, 0, $prefixBytes.Length)
        [Array]::Copy(
            $originalBytes,
            0,
            $modifiedBytes,
            $prefixBytes.Length,
            $originalBytes.Length
        )
    }
    Set-Re3DFileAtomically `
        -Path $resolvedHba `
        -Bytes $modifiedBytes `
        -SecurityDescriptor $originalDescriptor
    $hbaModified = $true
    Restart-Re3DPostgresService -Name $service.Name

    $trustReady = $false
    for ($attempt = 1; $attempt -le 10; $attempt++) {
        try {
            $probe = Invoke-Re3DPsqlScalar -Sql "SELECT 1;"
            if ($probe -eq "1") {
                $trustReady = $true
                break
            }
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $trustReady) {
        throw "Temporary loopback trust rule did not become active."
    }

    $reportedHba = Invoke-Re3DPsqlScalar -Sql "SHOW hba_file;"
    $reportedHbaPath = [IO.Path]::GetFullPath($reportedHba)
    if (-not $reportedHbaPath.Equals(
        $resolvedHba,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "PostgreSQL is using a different pg_hba.conf file."
    }
    $hbaErrorCount = Invoke-Re3DPsqlScalar -Sql (
        "SELECT count(*) FROM pg_hba_file_rules " +
        "WHERE error IS NOT NULL;"
    )
    if ($hbaErrorCount -ne "0") {
        throw "PostgreSQL reports invalid pg_hba.conf rules."
    }

    Set-Re3DPostgresPasswordSecurely -Password $plainPassword
} catch {
    $operationFailure = $_
} finally {
    if ($null -ne $prefixBytes) {
        [Array]::Clear($prefixBytes, 0, $prefixBytes.Length)
    }
    if ($null -ne $modifiedBytes) {
        [Array]::Clear($modifiedBytes, 0, $modifiedBytes.Length)
    }
    if ($rollbackRegistered -or $hbaModified) {
        try {
            & $restoreScript `
                -ManifestPath $manifestPath `
                -RequireRollbackTaskRemoval
        } catch {
            $restoreFailure = $_
        }
    }
}

if ($null -ne $restoreFailure) {
    $plainPassword = $null
    throw (
        "CRITICAL: pg_hba.conf rollback verification did not complete. " +
        "The SYSTEM rollback task remains registered. Backup: $backupPath. " +
        "Error: $($restoreFailure.Exception.Message)"
    )
}
if ($null -ne $operationFailure) {
    $plainPassword = $null
    throw $operationFailure
}

try {
    $passwordProbe = Invoke-Re3DPsqlScalar `
        -Sql "SELECT 1;" `
        -Password $plainPassword
    if ($passwordProbe -ne "1") {
        throw "New PostgreSQL administrator password could not be verified."
    }
    $reportedHba = Invoke-Re3DPsqlScalar `
        -Sql "SHOW hba_file;" `
        -Password $plainPassword
    if (-not ([IO.Path]::GetFullPath($reportedHba)).Equals(
        $resolvedHba,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "PostgreSQL HBA path changed after restoration."
    }
    $hbaErrorCount = Invoke-Re3DPsqlScalar `
        -Sql (
            "SELECT count(*) FROM pg_hba_file_rules " +
            "WHERE error IS NOT NULL;"
        ) `
        -Password $plainPassword
    if ($hbaErrorCount -ne "0") {
        throw "Restored pg_hba.conf contains an invalid rule."
    }
    $restoredHash = Get-Re3DSha256 -Path $resolvedHba
    if ($restoredHash -cne $originalHash) {
        throw "pg_hba.conf is not byte-identical after recovery."
    }

    Write-Re3DPasswordEnvelope `
        -Password $plainPassword `
        -Path $resolvedEnvelope `
        -RecoveryId $runId
} finally {
    $plainPassword = $null
    if ($null -ne $originalBytes) {
        [Array]::Clear($originalBytes, 0, $originalBytes.Length)
    }
    if ($null -ne $originalDescriptor) {
        [Array]::Clear($originalDescriptor, 0, $originalDescriptor.Length)
    }
}

Write-Host "POSTGRESQL_ADMIN_PASSWORD_RECOVERED" -ForegroundColor Green
Write-Host "pg_hba.conf was restored from a byte-identical backup."
Write-Host "Backup retained at: $backupPath"
Write-Host "One-time DPAPI password envelope: $resolvedEnvelope"
Write-Host "No plaintext password was printed or written to disk."
