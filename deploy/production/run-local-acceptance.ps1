[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$CaddyPath,
    [switch]$SkipWebInstall
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
. (Join-Path $PSScriptRoot "common.ps1")
. (Join-Path $projectRoot "deploy\postgres\common.ps1")

function Wait-Re3DHttpEndpoint {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][Diagnostics.Process]$Process,
        [Parameter(Mandatory = $true)][string]$Name
    )

    for ($attempt = 1; $attempt -le 60; $attempt++) {
        if ($Process.HasExited) {
            throw "$Name exited before becoming ready."
        }
        try {
            $response = Invoke-WebRequest `
                -Uri $Uri `
                -TimeoutSec 2 `
                -SkipHttpErrorCheck
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
                return $response
            }
        } catch {
            # The process can be listening before its first request is accepted.
        }
        Start-Sleep -Milliseconds 250
    }
    throw "$Name did not become ready at $Uri."
}

function Stop-Re3DProcessTree {
    param([Diagnostics.Process]$Process)

    if ($null -eq $Process -or $Process.HasExited) {
        return
    }
    $rootProcessId = $Process.Id
    $children = @(
        Get-CimInstance Win32_Process -Filter "ParentProcessId = $rootProcessId" `
            -ErrorAction SilentlyContinue
    )
    foreach ($child in $children) {
        $childProcess = Get-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
        if ($childProcess) {
            Stop-Re3DProcessTree -Process $childProcess
        }
    }
    Stop-Process -Id $rootProcessId -Force -ErrorAction SilentlyContinue
}

function Get-Re3DHeaderValue {
    param(
        [Parameter(Mandatory = $true)]$Response,
        [Parameter(Mandatory = $true)][string]$Name
    )

    return (@($Response.Headers[$Name]) -join ", ")
}

function Read-Re3DProcessFailure {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$StandardOutput,
        [Parameter(Mandatory = $true)][string]$StandardError,
        [string]$Cause
    )

    $lines = @()
    if (Test-Path -LiteralPath $StandardOutput) {
        $lines += Get-Content -LiteralPath $StandardOutput -Tail 20
    }
    if (Test-Path -LiteralPath $StandardError) {
        $lines += Get-Content -LiteralPath $StandardError -Tail 20
    }
    $causeText = if ($Cause) { " Cause: $Cause." } else { "" }
    throw "$Name failed.$causeText Recent output: $($lines -join ' | ')"
}

$caddy = (Resolve-Path -LiteralPath $CaddyPath -ErrorAction Stop).Path
$caddyVersion = (& $caddy version).Trim()
if ($LASTEXITCODE -ne 0 -or $caddyVersion -notmatch "^v2\.11\.4(?:\s|$)") {
    throw "Local production acceptance requires the reviewed Caddy v2.11.4 binary."
}

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

$python = Get-Re3DProductionPython -ProjectRoot $projectRoot
$pwsh = (Get-Process -Id $PID).Path
$containerName = "re3d-edge-accept-{0}-{1}" -f (
    $PID,
    [Guid]::NewGuid().ToString("N").Substring(0, 8)
)
$adminSecret = [Guid]::NewGuid().ToString("N")
$migratorSecret = [Guid]::NewGuid().ToString("N")
$runtimeSecret = [Guid]::NewGuid().ToString("N")
$jwtSecret = (
    [Guid]::NewGuid().ToString("N") +
    [Guid]::NewGuid().ToString("N")
)
$adminSecure = ConvertTo-SecureString $adminSecret -AsPlainText -Force
$migratorSecure = ConvertTo-SecureString $migratorSecret -AsPlainText -Force
$runtimeSecure = ConvertTo-SecureString $runtimeSecret -AsPlainText -Force
$temporaryRoot = [IO.Path]::GetFullPath((Join-Path (
    [IO.Path]::GetTempPath()
) "re3d-edge-accept-$([Guid]::NewGuid().ToString('N'))"))
$dataRoot = Join-Path $temporaryRoot "data"
$apiEnvironmentPath = Join-Path $temporaryRoot "api.env"
$workerEnvironmentPath = Join-Path $temporaryRoot "worker.env"
$apiOut = Join-Path $temporaryRoot "api.stdout.log"
$apiErr = Join-Path $temporaryRoot "api.stderr.log"
$workerOut = Join-Path $temporaryRoot "worker.stdout.log"
$workerErr = Join-Path $temporaryRoot "worker.stderr.log"
$caddyOut = Join-Path $temporaryRoot "caddy.stdout.log"
$caddyErr = Join-Path $temporaryRoot "caddy.stderr.log"
$apiPort = Get-Re3DFreeLoopbackPort
$edgePort = Get-Re3DFreeLoopbackPort
$containerStarted = $false
$apiProcess = $null
$workerProcess = $null
$caddyProcess = $null

try {
    New-Item -ItemType Directory -Path $dataRoot -Force | Out-Null

    & (Join-Path $PSScriptRoot "build-web.ps1") `
        -SkipInstall:$SkipWebInstall `
        -AllowDirty | Out-Null
    $webRoot = (Resolve-Path (Join-Path $projectRoot "apps\web\dist")).Path

    Write-Host "Starting disposable PostgreSQL 18 for edge acceptance..." -ForegroundColor Cyan
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
        throw "Docker could not start the edge acceptance database."
    }
    $containerStarted = $true
    $mapping = & $docker port $containerName "5432/tcp"
    if ($LASTEXITCODE -ne 0 -or -not $mapping) {
        throw "Docker did not publish the PostgreSQL port."
    }
    $databasePort = [int](($mapping.Trim() -split ":")[-1])
    $databaseReady = $false
    for ($attempt = 1; $attempt -le 30; $attempt++) {
        & $docker exec $containerName pg_isready -U postgres -d postgres |
            Out-Null
        if ($LASTEXITCODE -eq 0) {
            $databaseReady = $true
            break
        }
        Start-Sleep -Seconds 1
    }
    if (-not $databaseReady) {
        throw "Disposable PostgreSQL did not become ready."
    }

    & (Join-Path $projectRoot "deploy\postgres\bootstrap-production.ps1") `
        -DatabasePort $databasePort `
        -AdminPassword $adminSecure `
        -MigratorPassword $migratorSecure `
        -RuntimePassword $runtimeSecure
    & (Join-Path $projectRoot "deploy\postgres\migrate-production.ps1") `
        -DatabasePort $databasePort `
        -MigratorPassword $migratorSecure `
        -RuntimePassword $runtimeSecure

    $databaseUrl = ConvertTo-Re3DDatabaseUrl `
        -DatabaseHost "127.0.0.1" `
        -DatabasePort $databasePort `
        -DatabaseUser "re3d_runtime" `
        -DatabasePassword $runtimeSecret `
        -DatabaseName "re3d_platform"
    $normalizedDataRoot = $dataRoot.Replace("\", "/")
    $re3dRoot = (Resolve-Path (Join-Path $projectRoot "..\Re3D")).Path.Replace("\", "/")
    $apiEnvironment = @(
        "APP_ENV=production",
        "APP_PUBLIC_BASE_URL=https://acceptance.local",
        "DATABASE_URL=$databaseUrl",
        "JWT_SECRET=$jwtSecret",
        "REFRESH_COOKIE_SECURE=true",
        "AUTH_TRUSTED_PROXY_CIDRS=127.0.0.1/32,::1/128",
        "RE3D_DATA_ROOT=$normalizedDataRoot",
        "UPLOAD_MAX_FILE_BYTES=26214400",
        "UPLOAD_MAX_TOTAL_BYTES=1073741824",
        "UPLOAD_MAX_PIXELS=50000000",
        "SMTP_HOST=127.0.0.1",
        "SMTP_PORT=1",
        "SMTP_FROM=no-reply@acceptance.local",
        "SMTP_STARTTLS=true"
    ) -join [Environment]::NewLine
    $workerEnvironment = @(
        "APP_ENV=production",
        "DATABASE_URL=$databaseUrl",
        "RE3D_ROOT=$re3dRoot",
        "RE3D_DATA_ROOT=$normalizedDataRoot",
        "RE3D_GPU_RESOURCE=gpu:0",
        "RE3D_LEASE_SECONDS=60",
        "RE3D_HEARTBEAT_SECONDS=20",
        "RE3D_WORKER_POLL_SECONDS=1",
        "RE3D_WORKER_ID=acceptance-worker"
    ) -join [Environment]::NewLine
    [IO.File]::WriteAllText(
        $apiEnvironmentPath,
        "$apiEnvironment$([Environment]::NewLine)",
        [Text.UTF8Encoding]::new($false)
    )
    [IO.File]::WriteAllText(
        $workerEnvironmentPath,
        "$workerEnvironment$([Environment]::NewLine)",
        [Text.UTF8Encoding]::new($false)
    )

    $apiArguments = (
        "-NoProfile -ExecutionPolicy Bypass -File `"{0}`" " +
        "-EnvironmentFile `"{1}`" -Port {2}"
    ) -f (
        (Join-Path $PSScriptRoot "run-api.ps1"),
        $apiEnvironmentPath,
        $apiPort
    )
    $apiProcess = Start-Process `
        -FilePath $pwsh `
        -ArgumentList $apiArguments `
        -WindowStyle Hidden `
        -RedirectStandardOutput $apiOut `
        -RedirectStandardError $apiErr `
        -PassThru
    try {
        $directHealth = Wait-Re3DHttpEndpoint `
            -Uri "http://127.0.0.1:$apiPort/health/live" `
            -Process $apiProcess `
            -Name "Production API"
    } catch {
        Read-Re3DProcessFailure `
            -Name "Production API" `
            -StandardOutput $apiOut `
            -StandardError $apiErr `
            -Cause $_.Exception.Message
    }
    $directHealthPayload = $directHealth.Content | ConvertFrom-Json
    if (
        $directHealthPayload.environment -cne "production" -or
        $directHealthPayload.development_routes_enabled
    ) {
        throw "API did not start with the production route boundary."
    }

    $workerArguments = (
        "-NoProfile -ExecutionPolicy Bypass -File `"{0}`" " +
        "-EnvironmentFile `"{1}`""
    ) -f (
        (Join-Path $PSScriptRoot "run-worker.ps1"),
        $workerEnvironmentPath
    )
    $workerProcess = Start-Process `
        -FilePath $pwsh `
        -ArgumentList $workerArguments `
        -WindowStyle Hidden `
        -RedirectStandardOutput $workerOut `
        -RedirectStandardError $workerErr `
        -PassThru
    $workerReady = $false
    for ($attempt = 1; $attempt -le 40; $attempt++) {
        if ($workerProcess.HasExited) {
            Read-Re3DProcessFailure -Name "Production Worker" -StandardOutput $workerOut -StandardError $workerErr
        }
        if (
            (Test-Path -LiteralPath $workerOut) -and
            (Get-Content -LiteralPath $workerOut -Raw) -match '"operation": "real-worker-loop"'
        ) {
            $workerReady = $true
            break
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not $workerReady) {
        throw "Production Worker did not enter the real queue loop."
    }

    $caddyArguments = (
        "-NoProfile -ExecutionPolicy Bypass -File `"{0}`" " +
        "-CaddyPath `"{1}`" -SiteAddress `"http://127.0.0.1:{2}`" " +
        "-BindAddress `"127.0.0.1`" " +
        "-ApiUpstream `"127.0.0.1:{3}`" -MaxRequestBody `"1KB`" " +
        "-WebRoot `"{4}`""
    ) -f (
        (Join-Path $PSScriptRoot "run-caddy.ps1"),
        $caddy,
        $edgePort,
        $apiPort,
        $webRoot
    )
    $caddyProcess = Start-Process `
        -FilePath $pwsh `
        -ArgumentList $caddyArguments `
        -WindowStyle Hidden `
        -RedirectStandardOutput $caddyOut `
        -RedirectStandardError $caddyErr `
        -PassThru
    try {
        $homeResponse = Wait-Re3DHttpEndpoint `
            -Uri "http://127.0.0.1:$edgePort/" `
            -Process $caddyProcess `
            -Name "Caddy edge"
    } catch {
        Read-Re3DProcessFailure `
            -Name "Caddy edge" `
            -StandardOutput $caddyOut `
            -StandardError $caddyErr `
            -Cause $_.Exception.Message
    }

    if (
        $homeResponse.StatusCode -ne 200 -or
        $homeResponse.Content -notmatch '<div id="root"></div>'
    ) {
        throw "Caddy did not serve the production React entry point."
    }
    $spa = Invoke-WebRequest `
        -Uri "http://127.0.0.1:$edgePort/workspace/jobs/acceptance-route" `
        -SkipHttpErrorCheck
    if ($spa.StatusCode -ne 200 -or $spa.Content -notmatch '<div id="root"></div>') {
        throw "Caddy SPA fallback did not return index.html."
    }
    $proxiedHealth = Invoke-WebRequest `
        -Uri "http://127.0.0.1:$edgePort/health/live" `
        -SkipHttpErrorCheck
    $proxiedHealthPayload = $proxiedHealth.Content | ConvertFrom-Json
    if ($proxiedHealthPayload.environment -cne "production") {
        throw "Caddy did not proxy the production API health endpoint."
    }
    $unauthorized = Invoke-WebRequest `
        -Uri "http://127.0.0.1:$edgePort/api/v1/auth/me" `
        -SkipHttpErrorCheck
    if ($unauthorized.StatusCode -ne 401) {
        throw "Protected API route did not preserve its 401 response through Caddy."
    }
    $oversizedResponse = Invoke-WebRequest `
        -Uri "http://127.0.0.1:$edgePort/api/v1/auth/login" `
        -Method Post `
        -ContentType "application/json" `
        -Body ('x' * 2048) `
        -SkipHttpErrorCheck
    if ($oversizedResponse.StatusCode -ne 413) {
        throw "Caddy did not reject a request larger than its configured limit."
    }
    $apiCacheControl = Get-Re3DHeaderValue `
        -Response $unauthorized `
        -Name "Cache-Control"
    if ($apiCacheControl -notmatch "no-store") {
        throw "Proxied API responses must disable shared caching; received '$apiCacheControl'."
    }
    $contentSecurityPolicy = Get-Re3DHeaderValue `
        -Response $homeResponse `
        -Name "Content-Security-Policy"
    $contentTypeOptions = Get-Re3DHeaderValue `
        -Response $homeResponse `
        -Name "X-Content-Type-Options"
    $frameOptions = Get-Re3DHeaderValue `
        -Response $homeResponse `
        -Name "X-Frame-Options"
    if (
        $contentSecurityPolicy -notmatch "frame-ancestors 'none'" -or
        $contentTypeOptions -cne "nosniff" -or
        $frameOptions -cne "DENY"
    ) {
        throw "Expected edge security headers are missing."
    }
    if (Get-Re3DHeaderValue -Response $homeResponse -Name "Server") {
        throw "The edge response exposes a Server header."
    }

    $firstAsset = Get-ChildItem (Join-Path $webRoot "assets") -File |
        Select-Object -First 1
    $asset = Invoke-WebRequest `
        -Uri "http://127.0.0.1:$edgePort/assets/$($firstAsset.Name)" `
        -SkipHttpErrorCheck
    if (
        $asset.StatusCode -ne 200 -or
        (Get-Re3DHeaderValue -Response $asset -Name "Cache-Control") -notmatch "immutable"
    ) {
        throw "Versioned frontend assets do not have immutable caching."
    }

    $apiListeners = @(Get-NetTCPConnection -State Listen -LocalPort $apiPort)
    $nonLoopbackApiListeners = @(
        $apiListeners | Where-Object { $_.LocalAddress -ne "127.0.0.1" }
    )
    if ($apiListeners.Count -eq 0 -or $nonLoopbackApiListeners.Count -gt 0) {
        throw "Production API must listen only on IPv4 loopback."
    }
    $edgeListeners = @(Get-NetTCPConnection -State Listen -LocalPort $edgePort)
    $nonLoopbackEdgeListeners = @(
        $edgeListeners | Where-Object { $_.LocalAddress -ne "127.0.0.1" }
    )
    if ($edgeListeners.Count -eq 0 -or $nonLoopbackEdgeListeners.Count -gt 0) {
        throw "Local acceptance edge must listen only on IPv4 loopback."
    }

    Write-Host "Local production topology acceptance completed successfully." -ForegroundColor Green
    Write-Host "Validated URL: http://127.0.0.1:$edgePort"
} finally {
    Stop-Re3DProcessTree -Process $caddyProcess
    Stop-Re3DProcessTree -Process $workerProcess
    Stop-Re3DProcessTree -Process $apiProcess
    if ($containerStarted) {
        & $docker stop $containerName 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Disposable PostgreSQL container removed."
        } else {
            Write-Warning "Could not stop disposable container $containerName."
        }
    }

    $adminSecret = $null
    $migratorSecret = $null
    $runtimeSecret = $null
    $jwtSecret = $null
    $databaseUrl = $null
    $systemTemporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    if (
        (Test-Path -LiteralPath $temporaryRoot) -and
        (Test-Re3DPathWithin -Candidate $temporaryRoot -Root $systemTemporaryRoot)
    ) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
        Write-Host "Temporary production acceptance files removed."
    }
}
