[CmdletBinding()]
param(
    [string]$EnvironmentFile = ".env.production",
    [ValidateRange(1, 65535)][int]$Port = 8000,
    [ValidateRange(1, 10000)][int]$LimitConcurrency = 100
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$projectRoot = Get-Re3DProductionProjectRoot
$python = Get-Re3DProductionPython -ProjectRoot $projectRoot
$configuredPath = if ([IO.Path]::IsPathRooted($EnvironmentFile)) {
    $EnvironmentFile
} else {
    Join-Path $projectRoot $EnvironmentFile
}
$environmentPath = Assert-Re3DProductionEnvironmentFile `
    -Path $configuredPath `
    -Python $python

Push-Location $projectRoot
$processExitCode = 0
try {
    & $python -m dotenv -f $environmentPath run -- `
        $python -m uvicorn apps.api.main:create_app `
        --factory `
        --host 127.0.0.1 `
        --port $Port `
        --workers 1 `
        --proxy-headers `
        --forwarded-allow-ips "127.0.0.1,::1" `
        --limit-concurrency $LimitConcurrency `
        --timeout-graceful-shutdown 30 `
        --no-server-header `
        --no-access-log
    $processExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $processExitCode
