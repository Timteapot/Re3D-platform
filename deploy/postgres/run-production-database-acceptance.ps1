$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
. (Join-Path $PSScriptRoot "common.ps1")

$python = Get-Re3DProjectPython -ProjectRoot $projectRoot
$installedDocker = Get-Command docker -ErrorAction SilentlyContinue |
    Select-Object -First 1
$bundledDocker = Join-Path $env:LOCALAPPDATA (
    "Programs\DockerDesktop\resources\bin\docker.exe"
)
$docker = if ($installedDocker) {
    $installedDocker.Source
} elseif (Test-Path -LiteralPath $bundledDocker -PathType Leaf) {
    $bundledDocker
} else {
    throw "Docker CLI was not found."
}

$containerName = "re3d-prod-db-accept-{0}-{1}" -f (
    $PID,
    [Guid]::NewGuid().ToString("N").Substring(0, 8)
)
$adminSecret = [Guid]::NewGuid().ToString("N")
$migratorSecret = [Guid]::NewGuid().ToString("N")
$runtimeSecret = [Guid]::NewGuid().ToString("N")
$adminSecure = ConvertTo-SecureString $adminSecret -AsPlainText -Force
$migratorSecure = ConvertTo-SecureString $migratorSecret -AsPlainText -Force
$runtimeSecure = ConvertTo-SecureString $runtimeSecret -AsPlainText -Force
$temporaryRoot = [IO.Path]::GetFullPath((Join-Path (
    [IO.Path]::GetTempPath()
) "re3d-prod-db-accept-$([Guid]::NewGuid().ToString('N'))"))
$backupPath = Join-Path $temporaryRoot "re3d-platform.dump"
$previousDatabaseUrl = $env:DATABASE_URL
$containerStarted = $false

try {
    New-Item -ItemType Directory -Path $temporaryRoot | Out-Null
    Write-Host "Starting disposable PostgreSQL 18 production-contract container..." -ForegroundColor Cyan
    & $docker run `
        --rm `
        --detach `
        --name $containerName `
        --env "POSTGRES_PASSWORD=$adminSecret" `
        --env "POSTGRES_USER=postgres" `
        --env "POSTGRES_DB=postgres" `
        --publish "127.0.0.1::5432" `
        postgres:18 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker could not start the production database acceptance container."
    }
    $containerStarted = $true

    $mapping = & $docker port $containerName "5432/tcp"
    if ($LASTEXITCODE -ne 0 -or -not $mapping) {
        throw "Docker did not publish the PostgreSQL port."
    }
    $databasePort = [int](($mapping.Trim() -split ":")[-1])

    $ready = $false
    for ($attempt = 1; $attempt -le 30; $attempt++) {
        & $docker exec $containerName pg_isready -U postgres -d postgres |
            Out-Null
        if ($LASTEXITCODE -eq 0) {
            $ready = $true
            break
        }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) {
        throw "Disposable PostgreSQL did not become ready."
    }

    & (Join-Path $PSScriptRoot "bootstrap-production.ps1") `
        -DatabasePort $databasePort `
        -AdminPassword $adminSecure `
        -MigratorPassword $migratorSecure `
        -RuntimePassword $runtimeSecure

    & (Join-Path $PSScriptRoot "bootstrap-production.ps1") `
        -DatabasePort $databasePort `
        -AdminPassword $adminSecure `
        -MigratorPassword $migratorSecure `
        -RuntimePassword $runtimeSecure

    & (Join-Path $PSScriptRoot "migrate-production.ps1") `
        -DatabasePort $databasePort `
        -MigratorPassword $migratorSecure `
        -RuntimePassword $runtimeSecure

    $env:DATABASE_URL = ConvertTo-Re3DDatabaseUrl `
        -DatabaseHost "127.0.0.1" `
        -DatabasePort $databasePort `
        -DatabaseUser "re3d_runtime" `
        -DatabasePassword $runtimeSecret `
        -DatabaseName "re3d_platform"
    Push-Location $projectRoot
    try {
        & $python -c (
            "import json; " +
            "from apps.maintenance.readiness import _check_production_database; " +
            "from backend.db.runtime import DatabaseSettings; " +
            "print(json.dumps(_check_production_database(DatabaseSettings.from_environment())))"
        )
        if ($LASTEXITCODE -ne 0) {
            throw "Production database readiness check failed."
        }
        $probeEmail = "restore-probe-$([Guid]::NewGuid().ToString('N').Substring(0, 12))@acceptance.invalid"
        $env:RE3D_ACCEPTANCE_PROBE_EMAIL = $probeEmail
        & $python -c (
            "import os, uuid; " +
            "from sqlalchemy import create_engine, text; " +
            "engine=create_engine(os.environ['DATABASE_URL']); " +
            "connection=engine.connect(); transaction=connection.begin(); " +
            "connection.execute(text('INSERT INTO users " +
            "(id, username, email, password_hash, role, is_active, email_verified) " +
            "VALUES (:id, :username, :email, :password_hash, :role, :active, :verified)'), " +
            "{'id': uuid.uuid4(), 'username': 'restore_probe', " +
            "'email': os.environ['RE3D_ACCEPTANCE_PROBE_EMAIL'], " +
            "'password_hash': 'acceptance-only', 'role': 'user', " +
            "'active': True, 'verified': False}); " +
            "transaction.commit(); connection.close(); engine.dispose()"
        )
        if ($LASTEXITCODE -ne 0) {
            throw "Runtime role could not write the backup acceptance row."
        }
    } finally {
        Remove-Item Env:RE3D_ACCEPTANCE_PROBE_EMAIL -ErrorAction SilentlyContinue
        Pop-Location
    }

    & (Join-Path $PSScriptRoot "backup-production.ps1") `
        -BackupPath $backupPath `
        -DatabasePort $databasePort `
        -MigratorPassword $migratorSecure

    & (Join-Path $PSScriptRoot "test-production-restore.ps1") `
        -BackupPath $backupPath `
        -DatabasePort $databasePort `
        -AdminPassword $adminSecure `
        -ExpectedUserEmail $probeEmail

    Write-Host "Production database acceptance completed successfully." -ForegroundColor Green
} finally {
    $env:DATABASE_URL = $previousDatabaseUrl
    $adminSecret = $null
    $migratorSecret = $null
    $runtimeSecret = $null
    if ($containerStarted) {
        & $docker stop $containerName 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Disposable PostgreSQL container removed."
        } else {
            Write-Warning "Could not stop disposable container $containerName."
        }
    }

    $systemTemporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    if (
        (Test-Path -LiteralPath $temporaryRoot) -and
        (Test-Re3DPathWithin -Candidate $temporaryRoot -Root $systemTemporaryRoot)
    ) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
        Write-Host "Disposable backup artifacts removed."
    }
}
