[CmdletBinding()]
param(
    [string]$DatabaseHost = "127.0.0.1",
    [ValidateRange(1, 65535)][int]$DatabasePort = 5432,
    [Security.SecureString]$MigratorPassword,
    [Security.SecureString]$RuntimePassword
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
. (Join-Path $PSScriptRoot "common.ps1")

$psql = Get-Re3DPostgresToolPath -Name "psql"
$python = Get-Re3DProjectPython -ProjectRoot $projectRoot
$grantScript = Join-Path $PSScriptRoot "restricted\grant-runtime.psql"
$verifyScript = Join-Path $PSScriptRoot "restricted\verify-runtime.psql"

if ($null -eq $MigratorPassword) {
    $MigratorPassword = Read-Host (
        "Password for re3d_restricted_migrator"
    ) -AsSecureString
}
if ($null -eq $RuntimePassword) {
    $RuntimePassword = Read-Host (
        "Password for re3d_restricted_runtime"
    ) -AsSecureString
}

$migratorPointer = [IntPtr]::Zero
$runtimePointer = [IntPtr]::Zero
$previousPgPassword = $env:PGPASSWORD
$previousDatabaseUrl = $env:DATABASE_URL

try {
    $migratorPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR(
        $MigratorPassword
    )
    $runtimePointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR(
        $RuntimePassword
    )
    $plainMigratorPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
        $migratorPointer
    )
    $plainRuntimePassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
        $runtimePointer
    )

    $env:DATABASE_URL = ConvertTo-Re3DDatabaseUrl `
        -DatabaseHost $DatabaseHost `
        -DatabasePort $DatabasePort `
        -DatabaseUser "re3d_restricted_migrator" `
        -DatabasePassword $plainMigratorPassword `
        -DatabaseName "re3d_platform_restricted"
    $env:PGPASSWORD = $plainMigratorPassword

    Write-Host "Applying restricted Alembic migrations..." -ForegroundColor Cyan
    Push-Location $projectRoot
    try {
        & $python -m alembic -c alembic.ini upgrade head
        if ($LASTEXITCODE -ne 0) {
            throw "Restricted Alembic migration failed with exit code $LASTEXITCODE."
        }
        $expectedRevision = (& $python -c (
            "from apps.maintenance.readiness import _expected_migration_revision; " +
            "print(_expected_migration_revision())"
        )).Trim()
        if (
            $LASTEXITCODE -ne 0 -or
            [string]::IsNullOrWhiteSpace($expectedRevision)
        ) {
            throw "Could not resolve the expected Alembic revision."
        }
    } finally {
        Pop-Location
    }

    & $psql `
        -X `
        --no-password `
        -v ON_ERROR_STOP=1 `
        -h $DatabaseHost `
        -p $DatabasePort `
        -U re3d_restricted_migrator `
        -d re3d_platform_restricted `
        -f $grantScript
    if ($LASTEXITCODE -ne 0) {
        throw "Restricted runtime grants failed with exit code $LASTEXITCODE."
    }

    $env:PGPASSWORD = $plainRuntimePassword
    & $psql `
        -X `
        --no-password `
        -v ON_ERROR_STOP=1 `
        -v "expected_revision=$expectedRevision" `
        -h $DatabaseHost `
        -p $DatabasePort `
        -U re3d_restricted_runtime `
        -d re3d_platform_restricted `
        -f $verifyScript
    if ($LASTEXITCODE -ne 0) {
        throw "Restricted runtime verification failed with exit code $LASTEXITCODE."
    }

    Write-Host (
        "Restricted migration and runtime privilege verification completed."
    ) -ForegroundColor Green
} finally {
    $env:PGPASSWORD = $previousPgPassword
    $env:DATABASE_URL = $previousDatabaseUrl
    $plainMigratorPassword = $null
    $plainRuntimePassword = $null
    if ($migratorPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($migratorPointer)
    }
    if ($runtimePointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($runtimePointer)
    }
}
