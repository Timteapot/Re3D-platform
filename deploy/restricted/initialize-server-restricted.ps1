[CmdletBinding()]
param(
    [ValidateRange(1, 10)]
    [int]$MaximumPasswordAttempts = 3,
    [string]$DatabaseHost = "127.0.0.1",
    [ValidateRange(1, 65535)]
    [int]$DatabasePort = 5432,
    [string]$AdminUser = "postgres"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$postgresRoot = Join-Path $projectRoot "deploy\postgres"
. (Join-Path $postgresRoot "common.ps1")

$apiEnvironment = Join-Path $projectRoot ".env.restricted"
$workerEnvironment = Join-Path $projectRoot ".env.worker.restricted"
foreach ($environmentFile in ($apiEnvironment, $workerEnvironment)) {
    if (Test-Path -LiteralPath $environmentFile) {
        throw (
            "Restricted environment already exists: $environmentFile. " +
            "Refusing to rotate database credentials during initialization."
        )
    }
}

$psql = Get-Re3DPostgresToolPath -Name "psql"

function Test-Re3DPostgresAdministratorPassword {
    param(
        [Parameter(Mandatory = $true)]
        [Security.SecureString]$Password
    )

    $passwordPointer = [IntPtr]::Zero
    $previousPgPassword = $env:PGPASSWORD
    try {
        $passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR(
            $Password
        )
        $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
            $passwordPointer
        )
        $env:PGPASSWORD = $plainPassword
        & $psql `
            -X `
            --no-password `
            -h $DatabaseHost `
            -p $DatabasePort `
            -U $AdminUser `
            -d postgres `
            -tAc "SELECT 1;" | Out-Null
        return ($LASTEXITCODE -eq 0)
    } finally {
        $env:PGPASSWORD = $previousPgPassword
        $plainPassword = $null
        if ($passwordPointer -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
        }
    }
}

function New-Re3DGeneratedPassword {
    $passwordBytes = New-Object byte[] 48
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $random.GetBytes($passwordBytes)
        return [Convert]::ToBase64String($passwordBytes)
    } finally {
        $random.Dispose()
        [Array]::Clear($passwordBytes, 0, $passwordBytes.Length)
    }
}

$administratorPassword = $null
$candidatePassword = $null
$migratorPassword = $null
$runtimePassword = $null
$plainMigratorPassword = $null
$plainRuntimePassword = $null

try {
    Write-Host "Re3D restricted deployment initialization" -ForegroundColor Cyan
    Write-Host (
        "The PostgreSQL administrator password is checked before any " +
        "database change. Input remains hidden."
    ) -ForegroundColor Yellow

    for ($attempt = 1; $attempt -le $MaximumPasswordAttempts; $attempt++) {
        $candidatePassword = Read-Host (
            "Password for PostgreSQL administrator $AdminUser " +
            "(attempt $attempt/$MaximumPasswordAttempts)"
        ) -AsSecureString
        if (
            Test-Re3DPostgresAdministratorPassword `
                -Password $candidatePassword
        ) {
            $administratorPassword = $candidatePassword
            Write-Host (
                "PostgreSQL administrator authentication verified."
            ) -ForegroundColor Green
            break
        }
        $candidatePassword = $null
        Write-Host (
            "Authentication failed; no database changes were made."
        ) -ForegroundColor Red
    }
    if ($null -eq $administratorPassword) {
        throw (
            "PostgreSQL administrator password failed validation " +
            "$MaximumPasswordAttempts times."
        )
    }

    $plainMigratorPassword = New-Re3DGeneratedPassword
    $plainRuntimePassword = New-Re3DGeneratedPassword
    $migratorPassword = ConvertTo-SecureString `
        $plainMigratorPassword `
        -AsPlainText `
        -Force
    $runtimePassword = ConvertTo-SecureString `
        $plainRuntimePassword `
        -AsPlainText `
        -Force

    & (Join-Path $postgresRoot "bootstrap-restricted.ps1") `
        -DatabaseHost $DatabaseHost `
        -DatabasePort $DatabasePort `
        -AdminUser $AdminUser `
        -AdminPassword $administratorPassword `
        -MigratorPassword $migratorPassword `
        -RuntimePassword $runtimePassword
    & (Join-Path $postgresRoot "migrate-restricted.ps1") `
        -DatabaseHost $DatabaseHost `
        -DatabasePort $DatabasePort `
        -MigratorPassword $migratorPassword `
        -RuntimePassword $runtimePassword
    & (Join-Path $PSScriptRoot "initialize-restricted-env.ps1") `
        -RuntimePassword $runtimePassword
    & (Join-Path $PSScriptRoot "protect-restricted-env.ps1")

    & (Join-Path $projectRoot "deploy\mailpit\install-standalone.ps1")
    & (Join-Path $projectRoot "deploy\mailpit\start-standalone.ps1")
    & (Join-Path $PSScriptRoot "install-caddy.ps1")
    & (Join-Path $PSScriptRoot "check-restricted-readiness.ps1") `
        -Component all

    Write-Host "RE3D_RESTRICTED_INITIALIZATION_COMPLETE" -ForegroundColor Green
} finally {
    $administratorPassword = $null
    $candidatePassword = $null
    $migratorPassword = $null
    $runtimePassword = $null
    $plainMigratorPassword = $null
    $plainRuntimePassword = $null
}
