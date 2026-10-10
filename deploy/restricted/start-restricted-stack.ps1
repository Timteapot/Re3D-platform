[CmdletBinding()]
param(
    [switch]$SkipWebBuild,
    [switch]$InstallWebDependencies,
    [string]$CaddyPath = "D:\3Dreconstruction\tools\caddy\v2.11.4\caddy.exe",
    [string]$StateRoot = "D:\3Dreconstruction\Re3D-data\_services\restricted-stack"
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "process-common.ps1")

$projectRoot = Get-Re3DRestrictedProjectRoot
$powerShell = (Get-Process -Id $PID).Path
$startedComponents = [Collections.Generic.List[string]]::new()
$apiProcess = $null
$workerProcess = $null
$caddyProcess = $null

foreach ($component in @("api", "worker", "caddy")) {
    $status = Get-Re3DRestrictedManagedProcessStatus -StateRoot $StateRoot -Component $component
    if ($status.state -ne "stopped" -and $status.state -ne "stale") {
        throw "Cannot start the stack while $component state is '$($status.state)'."
    }
}
if (Test-Re3DRestrictedTcpPort -HostName "127.0.0.1" -Port 8000) {
    throw "TCP port 8000 is already in use."
}
if (Test-Re3DRestrictedTcpPort -HostName "127.0.0.1" -Port 8080) {
    throw "TCP port 8080 is already in use."
}

try {
    & (Join-Path $projectRoot "deploy\mailpit\start-standalone.ps1")
    & (Join-Path $PSScriptRoot "check-restricted-readiness.ps1") -Component all

    & (Join-Path $PSScriptRoot "install-caddy.ps1")
    if (-not $SkipWebBuild) {
        & (Join-Path $projectRoot "deploy\production\build-web.ps1") `
            -SkipInstall:(-not $InstallWebDependencies) `
            -AllowDirty | Out-Null
    }
    $webRoot = (Resolve-Path (Join-Path $projectRoot "apps\web\dist") -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath (Join-Path $webRoot "index.html") -PathType Leaf)) {
        throw "Restricted frontend output is missing. Run without -SkipWebBuild."
    }

    $apiArguments = (
        "-NoProfile -ExecutionPolicy Bypass -File `"{0}`""
    ) -f (Join-Path $PSScriptRoot "run-api.ps1")
    $apiProcess = Start-Re3DRestrictedManagedProcess `
        -StateRoot $StateRoot `
        -Component "api" `
        -ProcessPath $powerShell `
        -Arguments $apiArguments `
        -WorkingDirectory $projectRoot
    $startedComponents.Add("api")
    try {
        $apiHealth = Wait-Re3DRestrictedHttpEndpoint `
            -Uri "http://127.0.0.1:8000/health/live" `
            -Process $apiProcess `
            -Name "Restricted API"
    } catch {
        Read-Re3DRestrictedProcessFailure -StateRoot $StateRoot -Component "api" -Cause $_.Exception.Message
    }
    $apiPayload = $apiHealth.Content | ConvertFrom-Json
    if ($apiPayload.environment -cne "restricted" -or $apiPayload.development_routes_enabled) {
        throw "API did not start with the restricted route boundary."
    }

    $workerArguments = (
        "-NoProfile -ExecutionPolicy Bypass -File `"{0}`""
    ) -f (Join-Path $PSScriptRoot "run-worker.ps1")
    $workerProcess = Start-Re3DRestrictedManagedProcess `
        -StateRoot $StateRoot `
        -Component "worker" `
        -ProcessPath $powerShell `
        -Arguments $workerArguments `
        -WorkingDirectory $projectRoot
    $startedComponents.Add("worker")
    $workerReady = $false
    $workerOut = Join-Path $StateRoot "worker.stdout.log"
    for ($attempt = 1; $attempt -le 60; $attempt++) {
        if ($workerProcess.HasExited) {
            Read-Re3DRestrictedProcessFailure -StateRoot $StateRoot -Component "worker" -Cause "process exited"
        }
        if (Test-Path -LiteralPath $workerOut -PathType Leaf) {
            $workerOutput = Get-Content -LiteralPath $workerOut -Raw
            if (
                $workerOutput -match '"operation": "real-worker-loop"' -and
                $workerOutput -match '"mode": "dry_run"'
            ) {
                $workerReady = $true
                break
            }
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not $workerReady) {
        throw "Restricted Worker did not enter its real queue loop and retention dry-run."
    }

    $caddyArguments = (
        "-NoProfile -ExecutionPolicy Bypass -File `"{0}`" " +
        "-CaddyPath `"{1}`" -WebRoot `"{2}`""
    ) -f (
        (Join-Path $PSScriptRoot "run-caddy.ps1"),
        $CaddyPath,
        $webRoot
    )
    $caddyProcess = Start-Re3DRestrictedManagedProcess `
        -StateRoot $StateRoot `
        -Component "caddy" `
        -ProcessPath $powerShell `
        -Arguments $caddyArguments `
        -WorkingDirectory $projectRoot
    $startedComponents.Add("caddy")
    try {
        $homeResponse = Wait-Re3DRestrictedHttpEndpoint `
            -Uri "http://127.0.0.1:8080/" `
            -Process $caddyProcess `
            -Name "Restricted Caddy"
    } catch {
        Read-Re3DRestrictedProcessFailure -StateRoot $StateRoot -Component "caddy" -Cause $_.Exception.Message
    }
    if ($homeResponse.Content -notmatch '<div id="root"></div>') {
        throw "Caddy did not serve the React entry point."
    }
    $proxiedHealth = Invoke-WebRequest `
        -Uri "http://127.0.0.1:8080/health/live" `
        -TimeoutSec 5 `
        -UseBasicParsing
    $proxiedPayload = $proxiedHealth.Content | ConvertFrom-Json
    if ($proxiedPayload.environment -cne "restricted") {
        throw "Caddy did not proxy the restricted API."
    }
    $strictTransportSecurity = @(
        $homeResponse.Headers["Strict-Transport-Security"]
    ) -join ", "
    if (-not [string]::IsNullOrWhiteSpace($strictTransportSecurity)) {
        throw "Loopback HTTP must not emit Strict-Transport-Security."
    }

    Write-Host "Restricted stack is ready." -ForegroundColor Green
    Write-Host "Web: http://127.0.0.1:8080"
    Write-Host "Mail: http://127.0.0.1:8025"
    Write-Host "State: $StateRoot"
} catch {
    for ($index = $startedComponents.Count - 1; $index -ge 0; $index--) {
        try {
            Stop-Re3DRestrictedManagedProcess `
                -StateRoot $StateRoot `
                -Component $startedComponents[$index] | Out-Null
        } catch {
            Write-Warning "Rollback could not stop $($startedComponents[$index]): $($_.Exception.Message)"
        }
    }
    throw
}
