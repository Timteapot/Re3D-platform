#Requires -Version 5.1
#Requires -RunAsAdministrator

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ManifestPath,
    [switch]$RequireRollbackTaskRemoval
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$securitySections = (
    [Security.AccessControl.AccessControlSections]::Access -bor
    [Security.AccessControl.AccessControlSections]::Owner -bor
    [Security.AccessControl.AccessControlSections]::Group
)
$systemSid = New-Object Security.Principal.SecurityIdentifier("S-1-5-18")
$administratorsSid = New-Object Security.Principal.SecurityIdentifier(
    "S-1-5-32-544"
)

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

function Assert-Re3DNoReparsePoint {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Root
    )

    $resolvedRoot = (Resolve-Path -LiteralPath $Root -ErrorAction Stop).Path
    $currentPath = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    $normalizedRoot = $resolvedRoot.TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    $withinRoot = $currentPath.Equals(
        $normalizedRoot,
        [StringComparison]::OrdinalIgnoreCase
    ) -or $currentPath.StartsWith(
        "$normalizedRoot$([IO.Path]::DirectorySeparatorChar)",
        [StringComparison]::OrdinalIgnoreCase
    )
    if (-not $withinRoot) {
        throw "Recovery path is outside the approved root: $currentPath"
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

function Assert-Re3DAdminOnlyAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][bool]$Directory
    )

    if ($Directory) {
        $security = [IO.Directory]::GetAccessControl($Path, $securitySections)
    } else {
        $security = [IO.File]::GetAccessControl($Path, $securitySections)
    }
    if (-not $security.AreAccessRulesProtected) {
        throw "Recovery trust file inherits permissions: $Path"
    }
    $owner = $security.GetOwner(
        [Security.Principal.SecurityIdentifier]
    )
    if ($owner.Value -cne $systemSid.Value) {
        throw "Recovery trust file owner is not SYSTEM: $Path"
    }
    $rightsBySid = @{}
    $rightsBySid.Add($systemSid.Value, [int64]0)
    $rightsBySid.Add($administratorsSid.Value, [int64]0)
    $rules = $security.GetAccessRules(
        $true,
        $true,
        [Security.Principal.SecurityIdentifier]
    )
    foreach ($rule in $rules) {
        $ruleSid = $rule.IdentityReference.Value
        if (
            $rule.IsInherited -or
            -not $rightsBySid.ContainsKey($ruleSid) -or
            $rule.AccessControlType -ne
            [Security.AccessControl.AccessControlType]::Allow
        ) {
            throw "Recovery trust ACL contains an unexpected rule: $Path"
        }
        $rightsBySid[$ruleSid] = (
            [int64]$rightsBySid[$ruleSid] -bor
            [int64]$rule.FileSystemRights
        )
    }
    $fullControl = [int64][Security.AccessControl.FileSystemRights]::FullControl
    foreach ($sidValue in @($systemSid.Value, $administratorsSid.Value)) {
        if (($rightsBySid[$sidValue] -band $fullControl) -ne $fullControl) {
            throw "Recovery trust ACL is missing administrative FullControl: $Path"
        }
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

function Get-Re3DServiceDataDirectory {
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

function Set-Re3DFileSecurityDescriptor {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][byte[]]$Descriptor
    )

    $security = New-Object Security.AccessControl.FileSecurity
    $security.SetSecurityDescriptorBinaryForm($Descriptor, $securitySections)
    [IO.File]::SetAccessControl($Path, $security)
}

function Restore-Re3DHbaAtomically {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Target,
        [Parameter(Mandatory = $true)][byte[]]$Descriptor
    )

    $targetDirectory = Split-Path -Parent $Target
    $temporaryPath = Join-Path $targetDirectory (
        ".re3d-hba-restore-$([Guid]::NewGuid().ToString('N')).tmp"
    )
    $sourceBytes = $null
    try {
        $sourceBytes = [IO.File]::ReadAllBytes($Source)
        [IO.File]::WriteAllBytes($temporaryPath, $sourceBytes)
        Set-Re3DFileSecurityDescriptor `
            -Path $temporaryPath `
            -Descriptor $Descriptor
        if (Test-Path -LiteralPath $Target -PathType Leaf) {
            [IO.File]::Replace($temporaryPath, $Target, $null, $true)
        } else {
            [IO.File]::Move($temporaryPath, $Target)
        }
        Set-Re3DFileSecurityDescriptor -Path $Target -Descriptor $Descriptor
    } finally {
        if ($null -ne $sourceBytes) {
            [Array]::Clear($sourceBytes, 0, $sourceBytes.Length)
        }
        if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
            [IO.File]::Delete($temporaryPath)
        }
    }
}

$resolvedRestoreScript = (
    Resolve-Path -LiteralPath $PSCommandPath -ErrorAction Stop
).Path
$recoveryRunDirectory = [IO.Path]::GetFullPath(
    (Split-Path -Parent $resolvedRestoreScript)
)
Assert-Re3DNoReparsePoint `
    -Path $recoveryRunDirectory `
    -Root $recoveryRunDirectory
Assert-Re3DNoReparsePoint `
    -Path $resolvedRestoreScript `
    -Root $recoveryRunDirectory
Assert-Re3DAdminOnlyAcl -Path $recoveryRunDirectory -Directory $true
Assert-Re3DAdminOnlyAcl -Path $resolvedRestoreScript -Directory $false

$resolvedManifest = (
    Resolve-Path -LiteralPath $ManifestPath -ErrorAction Stop
).Path
Assert-Re3DNoReparsePoint `
    -Path $resolvedManifest `
    -Root $recoveryRunDirectory
$manifestDirectory = [IO.Path]::GetFullPath(
    (Split-Path -Parent $resolvedManifest)
)
if (-not $manifestDirectory.Equals(
    $recoveryRunDirectory,
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw "Recovery manifest must be beside the protected rollback helper."
}
Assert-Re3DAdminOnlyAcl -Path $manifestDirectory -Directory $true
Assert-Re3DAdminOnlyAcl -Path $resolvedManifest -Directory $false
$manifestJson = [IO.File]::ReadAllText(
    $resolvedManifest,
    [Text.Encoding]::UTF8
)
$manifest = $manifestJson | ConvertFrom-Json
$manifestJson = $null
foreach ($propertyName in @(
    "schema_version",
    "service_name",
    "data_directory",
    "hba_path",
    "backup_path",
    "backup_sha256",
    "original_sddl",
    "original_security_descriptor_base64",
    "rollback_task",
    "recovery_marker",
    "restore_script_path",
    "restore_script_sha256"
)) {
    if (
        $null -eq $manifest.PSObject.Properties[$propertyName] -or
        [string]::IsNullOrWhiteSpace([string]$manifest.$propertyName)
    ) {
        throw "Recovery manifest is missing required property: $propertyName"
    }
}
if ([int]$manifest.schema_version -ne 1) {
    throw "Unsupported PostgreSQL HBA recovery manifest version."
}
$manifestRestoreScript = [IO.Path]::GetFullPath(
    [string]$manifest.restore_script_path
)
if (-not $manifestRestoreScript.Equals(
    $resolvedRestoreScript,
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw "Recovery manifest refers to a different rollback helper."
}
$expectedRestoreHash = (
    [string]$manifest.restore_script_sha256
).ToUpperInvariant()
if ($expectedRestoreHash -notmatch '^[A-F0-9]{64}$') {
    throw "Recovery manifest contains an invalid rollback-helper digest."
}
$actualRestoreHash = Get-Re3DSha256 -Path $resolvedRestoreScript
if ($actualRestoreHash -cne $expectedRestoreHash) {
    throw "Protected rollback-helper hash does not match the recovery manifest."
}

$resolvedData = (
    Resolve-Path -LiteralPath ([string]$manifest.data_directory) `
        -ErrorAction Stop
).Path
$resolvedHba = [IO.Path]::GetFullPath([string]$manifest.hba_path)
$expectedHba = [IO.Path]::GetFullPath(
    (Join-Path $resolvedData "pg_hba.conf")
)
if (-not $resolvedHba.Equals(
    $expectedHba,
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw "Refusing to restore a file outside the selected PostgreSQL data directory."
}

$resolvedBackup = (
    Resolve-Path -LiteralPath ([string]$manifest.backup_path) `
        -ErrorAction Stop
).Path
Assert-Re3DNoReparsePoint `
    -Path $resolvedBackup `
    -Root $recoveryRunDirectory
if (-not ([IO.Path]::GetFullPath(
    (Split-Path -Parent $resolvedBackup)
)).Equals(
    $manifestDirectory,
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw "Recovery manifest and HBA backup must share one protected directory."
}
Assert-Re3DAdminOnlyAcl -Path $resolvedBackup -Directory $false
$expectedHash = ([string]$manifest.backup_sha256).ToUpperInvariant()
if ($expectedHash -notmatch '^[A-F0-9]{64}$') {
    throw "Recovery manifest contains an invalid SHA-256 digest."
}
$backupHash = Get-Re3DSha256 -Path $resolvedBackup
if ($backupHash -cne $expectedHash) {
    throw (
        "PostgreSQL HBA backup hash mismatch. Expected $expectedHash " +
        "but found $backupHash."
    )
}

$backupBytes = [IO.File]::ReadAllBytes($resolvedBackup)
$genericMarkerBytes = [Text.Encoding]::ASCII.GetBytes(
    "# BEGIN Re3D temporary PostgreSQL recovery"
)
try {
    if (Test-Re3DContainsByteSequence `
        -Bytes $backupBytes `
        -Sequence $genericMarkerBytes
    ) {
        throw "The HBA backup already contains a temporary recovery marker."
    }
} finally {
    [Array]::Clear($backupBytes, 0, $backupBytes.Length)
    [Array]::Clear($genericMarkerBytes, 0, $genericMarkerBytes.Length)
}

try {
    $originalDescriptor = [Convert]::FromBase64String(
        [string]$manifest.original_security_descriptor_base64
    )
} catch {
    throw "Recovery manifest contains an invalid security descriptor."
}
$descriptorCheck = New-Object Security.AccessControl.FileSecurity
$descriptorCheck.SetSecurityDescriptorBinaryForm(
    $originalDescriptor,
    $securitySections
)
$descriptorSddl = $descriptorCheck.GetSecurityDescriptorSddlForm(
    $securitySections
)
if ($descriptorSddl -cne [string]$manifest.original_sddl) {
    throw "Recovery manifest security descriptor and SDDL do not match."
}

$serviceName = [string]$manifest.service_name
$services = @(
    Get-CimInstance Win32_Service |
        Where-Object { $_.Name -ceq $serviceName }
)
if ($services.Count -ne 1) {
    throw "PostgreSQL service was not found: $serviceName"
}
$serviceData = [IO.Path]::GetFullPath(
    (Get-Re3DServiceDataDirectory -PathName $services[0].PathName)
)
if (-not $serviceData.Equals(
    $resolvedData,
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw "PostgreSQL service data directory no longer matches the recovery manifest."
}

try {
    Restore-Re3DHbaAtomically `
        -Source $resolvedBackup `
        -Target $resolvedHba `
        -Descriptor $originalDescriptor

    $restoredHash = Get-Re3DSha256 -Path $resolvedHba
    if ($restoredHash -cne $expectedHash) {
        throw (
            "Restored PostgreSQL HBA hash mismatch. Expected $expectedHash " +
            "but found $restoredHash."
        )
    }
    $restoredAcl = [IO.File]::GetAccessControl(
        $resolvedHba,
        $securitySections
    )
    $restoredSddl = $restoredAcl.GetSecurityDescriptorSddlForm(
        $securitySections
    )
    if ($restoredSddl -cne [string]$manifest.original_sddl) {
        throw "Restored PostgreSQL HBA ACL does not match the original SDDL."
    }

    try {
        Restart-Service -Name $serviceName -Force -ErrorAction Stop
        $service = Get-Service -Name $serviceName -ErrorAction Stop
        $service.WaitForStatus(
            [ServiceProcess.ServiceControllerStatus]::Running,
            [TimeSpan]::FromSeconds(60)
        )
        if (
            $service.Status -ne
            [ServiceProcess.ServiceControllerStatus]::Running
        ) {
            throw "PostgreSQL service status is $($service.Status)."
        }
    } catch {
        throw (
            "pg_hba.conf bytes and owner/group/DACL were restored, but " +
            "PostgreSQL did not return to Running: $($_.Exception.Message)"
        )
    }
} finally {
    if ($null -ne $originalDescriptor) {
        [Array]::Clear($originalDescriptor, 0, $originalDescriptor.Length)
    }
}

$rollbackTaskName = [string]$manifest.rollback_task
if ($rollbackTaskName -notmatch '^Re3D-PgHba-Rollback-[a-f0-9]{32}$') {
    throw "Recovery manifest contains an invalid rollback task name."
}
try {
    $task = Get-ScheduledTask `
        -TaskName $rollbackTaskName `
        -ErrorAction SilentlyContinue
    if ($null -ne $task) {
        Unregister-ScheduledTask `
            -TaskName $rollbackTaskName `
            -Confirm:$false `
            -ErrorAction Stop
    }
} catch {
    $taskRemovalMessage = (
        "pg_hba.conf was restored, but rollback task " +
        "$rollbackTaskName could not be removed: $($_.Exception.Message)"
    )
    if ($RequireRollbackTaskRemoval) {
        throw $taskRemovalMessage
    }
    Write-Warning $taskRemovalMessage
}

Write-Host "POSTGRESQL_HBA_RESTORED" -ForegroundColor Green
Write-Host "Backup retained at: $resolvedBackup"
