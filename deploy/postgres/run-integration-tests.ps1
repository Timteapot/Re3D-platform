$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$python = if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
    $venvPython
} else {
    (Get-Command python -ErrorAction Stop).Source
}
$installedDocker = (
    Get-Command docker -ErrorAction SilentlyContinue | Select-Object -First 1
).Source
$bundledDocker = (
    "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin\docker.exe"
)
$docker = if ($installedDocker) {
    $installedDocker
} elseif (Test-Path -LiteralPath $bundledDocker -PathType Leaf) {
    $bundledDocker
} else {
    throw "Docker CLI was not found in PATH or the Docker Desktop installation."
}

$containerName = "re3d-platform-pg-test-{0}-{1}" -f (
    $PID,
    [Guid]::NewGuid().ToString("N").Substring(0, 8)
)
$testPassword = [Guid]::NewGuid().ToString("N")
$databaseUrl = $null

try {
    Write-Host "Starting disposable PostgreSQL 18 container $containerName..."
    & $docker run `
        --rm `
        --detach `
        --name $containerName `
        --env "POSTGRES_PASSWORD=$testPassword" `
        --env "POSTGRES_USER=re3d_test" `
        --env "POSTGRES_DB=re3d_platform_test" `
        --publish "127.0.0.1::5432" `
        postgres:18 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker could not start the PostgreSQL test container."
    }

    $mapping = & $docker port $containerName "5432/tcp"
    if ($LASTEXITCODE -ne 0 -or -not $mapping) {
        throw "Docker did not publish the PostgreSQL test port."
    }
    $testPort = [int](($mapping.Trim() -split ":")[-1])

    $ready = $false
    for ($attempt = 1; $attempt -le 30; $attempt++) {
        & $docker exec $containerName `
            pg_isready -U re3d_test -d re3d_platform_test | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $ready = $true
            break
        }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) {
        throw "PostgreSQL test container did not become ready."
    }

    $databaseUrl = (
        "postgresql+psycopg://re3d_test:{0}@127.0.0.1:{1}/re3d_platform_test" `
        -f $testPassword, $testPort
    )
    $env:DATABASE_URL = $databaseUrl
    $env:RE3D_TEST_DATABASE_URL = $databaseUrl

    Push-Location $projectRoot
    try {
        & $python -m alembic -c alembic.ini upgrade head
        if ($LASTEXITCODE -ne 0) {
            throw "Alembic failed against the disposable test database."
        }
        & $python -m unittest `
            tests.integration.test_postgres_job_queue `
            tests.integration.test_postgres_api_worker_flow `
            -v
        if ($LASTEXITCODE -ne 0) {
            throw "PostgreSQL integration tests failed."
        }
        & $python -m alembic -c alembic.ini downgrade 0001_job_queue
        if ($LASTEXITCODE -ne 0) {
            throw "Authentication migration downgrade test failed."
        }
        & $python -m alembic -c alembic.ini upgrade head
        if ($LASTEXITCODE -ne 0) {
            throw "Authentication migration re-upgrade test failed."
        }
    } finally {
        Pop-Location
    }
} finally {
    $env:DATABASE_URL = $null
    $env:RE3D_TEST_DATABASE_URL = $null
    $databaseUrl = $null
    $testPassword = $null
    & $docker stop $containerName 2>$null | Out-Null
    Write-Host "Disposable PostgreSQL test container removed."
}
