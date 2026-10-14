#Requires -Version 7.0
[CmdletBinding()]
param([string]$PythonPath)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$originalLocation = Get-Location
$savedDatabaseUrl = [Environment]::GetEnvironmentVariable("DATABASE_URL", "Process")
$savedTestDatabaseUrl = [Environment]::GetEnvironmentVariable("RE3D_TEST_DATABASE_URL", "Process")
$integrationExitCode = 0
$containerStarted = $false
$docker = $null
$databaseUrl = $null
$testPassword = $null
$containerName = "re3d-platform-pg-test-{0}-{1}" -f $PID, [Guid]::NewGuid().ToString("N").Substring(0, 8)

function Invoke-IntegrationCommand {
    param([string]$Label, [string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        $script:integrationExitCode = $LASTEXITCODE
        throw "$Label failed with exit code $LASTEXITCODE."
    }
}

try {
    $venvCandidates = @(
        (Join-Path $projectRoot ".venv/Scripts/python.exe"),
        (Join-Path $projectRoot ".venv/bin/python")
    )
    $venvPython = $venvCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    $python = if ($PythonPath) {
        (Get-Command $PythonPath -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    } elseif ($venvPython) {
        $venvPython
    } else {
        (Get-Command python -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    }
    $installedDocker = Get-Command docker -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    $bundledDocker = $null
    if ($IsWindows) {
        $bundledDocker = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "Programs/DockerDesktop/resources/bin/docker.exe"
    }
    $docker = if ($installedDocker) {
        $installedDocker.Source
    } elseif ($bundledDocker -and (Test-Path -LiteralPath $bundledDocker -PathType Leaf)) {
        $bundledDocker
    } else {
        throw "Docker CLI was not found in PATH or the Docker Desktop installation."
    }
    $dockerOs = & $docker info --format "{{.OSType}}" 2>$null
    if ($LASTEXITCODE -ne 0 -or $dockerOs -ne "linux") {
        throw "A running Docker engine using Linux containers is required for postgres:18."
    }

    $testPassword = [Guid]::NewGuid().ToString("N")
    Write-Host "Starting disposable PostgreSQL 18 container $containerName..."
    Invoke-IntegrationCommand "Docker PostgreSQL startup" $docker @(
        "run", "--rm", "--detach", "--name", $containerName,
        "--env", "POSTGRES_PASSWORD=$testPassword", "--env", "POSTGRES_USER=re3d_test",
        "--env", "POSTGRES_DB=re3d_platform_test", "--publish", "127.0.0.1::5432", "postgres:18"
    ) | Out-Null
    $containerStarted = $true

    $mapping = & $docker port $containerName "5432/tcp"
    if ($LASTEXITCODE -ne 0 -or -not $mapping) {
        throw "Docker did not publish the PostgreSQL test port."
    }
    $testPort = [int](($mapping.Trim() -split ":")[-1])
    $ready = $false
    for ($attempt = 1; $attempt -le 30; $attempt++) {
        & $docker exec $containerName pg_isready -h 127.0.0.1 -U re3d_test -d re3d_platform_test | Out-Null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw "PostgreSQL test container did not become ready." }

    $databaseUrl = "postgresql+psycopg://re3d_test:{0}@127.0.0.1:{1}/re3d_platform_test" -f $testPassword, $testPort
    $env:DATABASE_URL = $databaseUrl
    $env:RE3D_TEST_DATABASE_URL = $databaseUrl
    Set-Location -LiteralPath $projectRoot
    Invoke-IntegrationCommand "Alembic upgrade" $python @("-m", "alembic", "-c", "alembic.ini", "upgrade", "head")
    Invoke-IntegrationCommand "PostgreSQL integration tests" $python @(
        "-m", "unittest", "tests.integration.test_postgres_job_queue",
        "tests.integration.test_postgres_api_worker_flow", "tests.integration.test_postgres_transfer_limits", "-v"
    )
    Invoke-IntegrationCommand "Alembic downgrade" $python @("-m", "alembic", "-c", "alembic.ini", "downgrade", "0001_job_queue")
    Invoke-IntegrationCommand "Alembic re-upgrade" $python @("-m", "alembic", "-c", "alembic.ini", "upgrade", "head")
} catch {
    if ($integrationExitCode -eq 0) { $integrationExitCode = 1 }
    Write-Error $_ -ErrorAction Continue
} finally {
    [Environment]::SetEnvironmentVariable("DATABASE_URL", $savedDatabaseUrl, "Process")
    [Environment]::SetEnvironmentVariable("RE3D_TEST_DATABASE_URL", $savedTestDatabaseUrl, "Process")
    Set-Location -LiteralPath $originalLocation.Path
    $databaseUrl = $null
    $testPassword = $null
    if ($containerStarted) {
        try {
            & $docker stop $containerName 2>$null | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "Docker stop failed." }
            Write-Host "Disposable PostgreSQL test container removed."
        } catch {
            # Preserve an earlier migration/test error; still fail a successful run
            # if its disposable container could not be cleaned up.
            if ($integrationExitCode -eq 0) { $integrationExitCode = 1 }
            Write-Warning "Could not remove the disposable PostgreSQL container $containerName."
        }
    }
}
exit $integrationExitCode
