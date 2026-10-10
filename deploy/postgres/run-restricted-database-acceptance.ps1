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

$containerName = "re3d-restricted-db-accept-{0}-{1}" -f (
    $PID,
    [Guid]::NewGuid().ToString("N").Substring(0, 8)
)
$adminSecret = [Guid]::NewGuid().ToString("N")
$migratorSecret = [Guid]::NewGuid().ToString("N")
$runtimeSecret = [Guid]::NewGuid().ToString("N")
$adminSecure = ConvertTo-SecureString $adminSecret -AsPlainText -Force
$migratorSecure = ConvertTo-SecureString $migratorSecret -AsPlainText -Force
$runtimeSecure = ConvertTo-SecureString $runtimeSecret -AsPlainText -Force
$previousDatabaseUrl = $env:DATABASE_URL
$containerStarted = $false

try {
    Write-Host (
        "Starting disposable PostgreSQL 18 restricted-contract container..."
    ) -ForegroundColor Cyan
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
        throw "Docker could not start the restricted database acceptance container."
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

    foreach ($iteration in 1..2) {
        & (Join-Path $PSScriptRoot "bootstrap-restricted.ps1") `
            -DatabasePort $databasePort `
            -AdminPassword $adminSecure `
            -MigratorPassword $migratorSecure `
            -RuntimePassword $runtimeSecure
        if ($LASTEXITCODE -ne 0) {
            throw "Restricted bootstrap iteration $iteration failed."
        }
    }

    & (Join-Path $PSScriptRoot "migrate-restricted.ps1") `
        -DatabasePort $databasePort `
        -MigratorPassword $migratorSecure `
        -RuntimePassword $runtimeSecure
    if ($LASTEXITCODE -ne 0) {
        throw "Restricted migration acceptance failed."
    }

    $env:DATABASE_URL = ConvertTo-Re3DDatabaseUrl `
        -DatabaseHost "127.0.0.1" `
        -DatabasePort $databasePort `
        -DatabaseUser "re3d_restricted_runtime" `
        -DatabasePassword $runtimeSecret `
        -DatabaseName "re3d_platform_restricted"
    Push-Location $projectRoot
    try {
        & $python -c (
            "import json; " +
            "from apps.maintenance.readiness import _check_restricted_database; " +
            "from backend.db.runtime import DatabaseSettings; " +
            "print(json.dumps(_check_restricted_database(" +
            "DatabaseSettings.from_environment())))"
        )
        if ($LASTEXITCODE -ne 0) {
            throw "Restricted database readiness acceptance failed."
        }
    } finally {
        Pop-Location
    }

    Write-Host "Restricted database acceptance completed." -ForegroundColor Green
} finally {
    $env:DATABASE_URL = $previousDatabaseUrl
    $adminSecret = $null
    $migratorSecret = $null
    $runtimeSecret = $null
    if ($containerStarted) {
        & $docker stop $containerName 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Disposable restricted database container removed."
        } else {
            Write-Warning "Could not stop disposable container $containerName."
        }
    }
}
