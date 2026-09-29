[CmdletBinding()]
param(
    [string]$DatabaseHost = "127.0.0.1",
    [ValidateRange(1, 65535)][int]$DatabasePort = 5432,
    [string]$AdminUser = "postgres",
    [Security.SecureString]$AdminPassword,
    [Security.SecureString]$MigratorPassword,
    [Security.SecureString]$RuntimePassword
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$psql = Get-Re3DPostgresToolPath -Name "psql"
$bootstrap = Join-Path $PSScriptRoot "production\bootstrap-production.psql"
if (-not (Test-Path -LiteralPath $bootstrap -PathType Leaf)) {
    throw "Production bootstrap SQL was not found: $bootstrap"
}

if ($null -eq $AdminPassword) {
    $AdminPassword = Read-Host "Password for PostgreSQL administrator $AdminUser" -AsSecureString
}
if ($null -eq $MigratorPassword) {
    $MigratorPassword = Read-Host "New password for re3d_migrator" -AsSecureString
}
if ($null -eq $RuntimePassword) {
    $RuntimePassword = Read-Host "New password for re3d_runtime" -AsSecureString
}

$adminPointer = [IntPtr]::Zero
$migratorPointer = [IntPtr]::Zero
$runtimePointer = [IntPtr]::Zero
$previousPgPassword = $env:PGPASSWORD
$previousMigratorPassword = $env:RE3D_MIGRATOR_PASSWORD
$previousRuntimePassword = $env:RE3D_RUNTIME_PASSWORD

try {
    $adminPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR(
        $AdminPassword
    )
    $migratorPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR(
        $MigratorPassword
    )
    $runtimePointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR(
        $RuntimePassword
    )
    $plainAdminPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
        $adminPointer
    )
    $plainMigratorPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
        $migratorPointer
    )
    $plainRuntimePassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
        $runtimePointer
    )

    if ($plainMigratorPassword.Length -lt 16 -or $plainRuntimePassword.Length -lt 16) {
        throw "Production database passwords must contain at least 16 characters."
    }
    if ($plainMigratorPassword -ceq $plainRuntimePassword) {
        throw "Migration and runtime roles must use different passwords."
    }

    $env:PGPASSWORD = $plainAdminPassword
    $env:RE3D_MIGRATOR_PASSWORD = $plainMigratorPassword
    $env:RE3D_RUNTIME_PASSWORD = $plainRuntimePassword

    Write-Host "Creating restricted production PostgreSQL roles and database..." -ForegroundColor Cyan
    & $psql `
        -X `
        --no-password `
        -v ON_ERROR_STOP=1 `
        -h $DatabaseHost `
        -p $DatabasePort `
        -U $AdminUser `
        -d postgres `
        -f $bootstrap
    if ($LASTEXITCODE -ne 0) {
        throw "Production database bootstrap failed with exit code $LASTEXITCODE."
    }
    Write-Host "Production database ownership bootstrap completed." -ForegroundColor Green
} finally {
    $env:PGPASSWORD = $previousPgPassword
    $env:RE3D_MIGRATOR_PASSWORD = $previousMigratorPassword
    $env:RE3D_RUNTIME_PASSWORD = $previousRuntimePassword
    $plainAdminPassword = $null
    $plainMigratorPassword = $null
    $plainRuntimePassword = $null
    if ($adminPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($adminPointer)
    }
    if ($migratorPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($migratorPointer)
    }
    if ($runtimePointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($runtimePointer)
    }
}
