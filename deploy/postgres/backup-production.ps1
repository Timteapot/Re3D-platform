[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BackupPath,
    [string]$DatabaseHost = "127.0.0.1",
    [ValidateRange(1, 65535)][int]$DatabasePort = 5432,
    [Security.SecureString]$MigratorPassword
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
. (Join-Path $PSScriptRoot "common.ps1")

$pgDump = Get-Re3DPostgresToolPath -Name "pg_dump"
$resolvedBackupPath = [IO.Path]::GetFullPath($BackupPath)
if (Test-Re3DPathWithin -Candidate $resolvedBackupPath -Root $projectRoot) {
    throw "Production backups must be stored outside the source checkout."
}
if (Test-Path -LiteralPath $resolvedBackupPath) {
    throw "Backup target already exists: $resolvedBackupPath"
}
$checksumPath = "$resolvedBackupPath.sha256"
if (Test-Path -LiteralPath $checksumPath) {
    throw "Backup checksum target already exists: $checksumPath"
}
$backupDirectory = Split-Path -Parent $resolvedBackupPath
if (-not (Test-Path -LiteralPath $backupDirectory -PathType Container)) {
    New-Item -ItemType Directory -Path $backupDirectory | Out-Null
}

if ($null -eq $MigratorPassword) {
    $MigratorPassword = Read-Host "Password for re3d_migrator" -AsSecureString
}

$passwordPointer = [IntPtr]::Zero
$previousPgPassword = $env:PGPASSWORD
$partialBackup = "$resolvedBackupPath.partial-$PID"
$partialChecksum = "$checksumPath.partial-$PID"

try {
    $passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR(
        $MigratorPassword
    )
    $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
        $passwordPointer
    )
    $env:PGPASSWORD = $plainPassword

    Write-Host "Creating a PostgreSQL custom-format production backup..." -ForegroundColor Cyan
    & $pgDump `
        --format=custom `
        --compress=9 `
        --no-password `
        --host=$DatabaseHost `
        --port=$DatabasePort `
        --username=re3d_migrator `
        --file=$partialBackup `
        re3d_platform
    if ($LASTEXITCODE -ne 0) {
        throw "pg_dump failed with exit code $LASTEXITCODE."
    }

    $hash = (Get-FileHash -LiteralPath $partialBackup -Algorithm SHA256).Hash.ToLowerInvariant()
    $fileName = Split-Path -Leaf $resolvedBackupPath
    [IO.File]::WriteAllText(
        $partialChecksum,
        "$hash  $fileName$([Environment]::NewLine)",
        [Text.UTF8Encoding]::new($false)
    )
    Move-Item -LiteralPath $partialBackup -Destination $resolvedBackupPath
    Move-Item -LiteralPath $partialChecksum -Destination $checksumPath

    Write-Host "Backup created: $resolvedBackupPath" -ForegroundColor Green
    Write-Host "SHA-256: $hash"
} finally {
    $env:PGPASSWORD = $previousPgPassword
    $plainPassword = $null
    if ($passwordPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
    }
    Remove-Item -LiteralPath $partialBackup -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $partialChecksum -Force -ErrorAction SilentlyContinue
}
