[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BackupPath,
    [string]$DatabaseHost = "127.0.0.1",
    [ValidateRange(1, 65535)][int]$DatabasePort = 5432,
    [string]$AdminUser = "postgres",
    [Security.SecureString]$AdminPassword,
    [string]$ExpectedUserEmail
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
. (Join-Path $PSScriptRoot "common.ps1")

$createdb = Get-Re3DPostgresToolPath -Name "createdb"
$dropdb = Get-Re3DPostgresToolPath -Name "dropdb"
$pgRestore = Get-Re3DPostgresToolPath -Name "pg_restore"
$psql = Get-Re3DPostgresToolPath -Name "psql"
$python = Get-Re3DProjectPython -ProjectRoot $projectRoot
$verifyScript = Join-Path $PSScriptRoot "production\verify-restore.psql"

$resolvedBackupPath = [IO.Path]::GetFullPath($BackupPath)
if (-not (Test-Path -LiteralPath $resolvedBackupPath -PathType Leaf)) {
    throw "Backup file does not exist: $resolvedBackupPath"
}
if (Test-Re3DPathWithin -Candidate $resolvedBackupPath -Root $projectRoot) {
    throw "Production backups must not be stored inside the source checkout."
}
$checksumPath = "$resolvedBackupPath.sha256"
if (-not (Test-Path -LiteralPath $checksumPath -PathType Leaf)) {
    throw "Backup checksum file does not exist: $checksumPath"
}
$expectedHash = ((Get-Content -LiteralPath $checksumPath -Raw).Trim() -split "\s+")[0].ToLowerInvariant()
$actualHash = (Get-FileHash -LiteralPath $resolvedBackupPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($expectedHash -notmatch "^[0-9a-f]{64}$" -or $actualHash -cne $expectedHash) {
    throw "Backup SHA-256 verification failed."
}

if ($null -eq $AdminPassword) {
    $AdminPassword = Read-Host "Password for PostgreSQL administrator $AdminUser" -AsSecureString
}

$passwordPointer = [IntPtr]::Zero
$previousPgPassword = $env:PGPASSWORD
$restoreDatabase = "re3d_restore_test_$([Guid]::NewGuid().ToString('N').Substring(0, 12))"
$created = $false

try {
    if ($restoreDatabase -notmatch "^re3d_restore_test_[0-9a-f]{12}$") {
        throw "Generated restore-test database name is unsafe."
    }
    $passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR(
        $AdminPassword
    )
    $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
        $passwordPointer
    )
    $env:PGPASSWORD = $plainPassword

    Write-Host "Creating disposable restore database $restoreDatabase..." -ForegroundColor Cyan
    & $createdb `
        --no-password `
        --host=$DatabaseHost `
        --port=$DatabasePort `
        --username=$AdminUser `
        --maintenance-db=postgres `
        --owner=$AdminUser `
        $restoreDatabase
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the disposable restore database."
    }
    $created = $true

    & $pgRestore `
        --exit-on-error `
        --no-owner `
        --no-privileges `
        --no-password `
        --host=$DatabaseHost `
        --port=$DatabasePort `
        --username=$AdminUser `
        --dbname=$restoreDatabase `
        $resolvedBackupPath
    if ($LASTEXITCODE -ne 0) {
        throw "pg_restore failed with exit code $LASTEXITCODE."
    }

    Push-Location $projectRoot
    try {
        $expectedRevision = (& $python -c (
            "from apps.maintenance.readiness import _expected_migration_revision; " +
            "print(_expected_migration_revision())"
        )).Trim()
    } finally {
        Pop-Location
    }
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($expectedRevision)) {
        throw "Could not resolve the expected Alembic revision."
    }

    $verificationArguments = @(
        "-X",
        "--no-password",
        "-v", "ON_ERROR_STOP=1",
        "-v", "expected_revision=$expectedRevision",
        "-h", $DatabaseHost,
        "-p", $DatabasePort,
        "-U", $AdminUser,
        "-d", $restoreDatabase,
        "-f", $verifyScript
    )
    if (-not [string]::IsNullOrWhiteSpace($ExpectedUserEmail)) {
        $verificationArguments += @(
            "-v", "expected_probe_email=$ExpectedUserEmail"
        )
    }
    & $psql @verificationArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Restored database verification failed with exit code $LASTEXITCODE."
    }

    Write-Host "Backup restore drill completed successfully." -ForegroundColor Green
} finally {
    if ($created) {
        if ($restoreDatabase -notmatch "^re3d_restore_test_[0-9a-f]{12}$") {
            throw "Refusing to drop an unexpected database name."
        }
        & $dropdb `
            --if-exists `
            --force `
            --no-password `
            --host=$DatabaseHost `
            --port=$DatabasePort `
            --username=$AdminUser `
            --maintenance-db=postgres `
            $restoreDatabase
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Could not remove disposable database $restoreDatabase."
        } else {
            Write-Host "Disposable restore database removed."
        }
    }
    $env:PGPASSWORD = $previousPgPassword
    $plainPassword = $null
    if ($passwordPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
    }
}
