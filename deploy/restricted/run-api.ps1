[CmdletBinding()]
param(
    [string]$EnvironmentFile = ".env.restricted",
    [ValidateRange(1, 10000)][int]$LimitConcurrency = 16,
    [switch]$RequireVirtualService
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$projectRoot = Get-Re3DRestrictedProjectRoot
$expectedServiceIdentity = "NT SERVICE\Re3DRestrictedApi"
if ($RequireVirtualService) {
    $actualSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $expectedSid = [Security.Principal.NTAccount]::new(
        $expectedServiceIdentity
    ).Translate([Security.Principal.SecurityIdentifier])
    if ($null -eq $actualSid -or $actualSid.Value -cne $expectedSid.Value) {
        throw "Restricted API service must run as $expectedServiceIdentity."
    }
}
$python = Get-Re3DRestrictedPython -ProjectRoot $projectRoot
$configuredPath = if ([IO.Path]::IsPathRooted($EnvironmentFile)) {
    $EnvironmentFile
} else {
    Join-Path $projectRoot $EnvironmentFile
}
$environmentPath = Assert-Re3DRestrictedEnvironmentFile `
    -Path $configuredPath `
    -Python $python `
    -VirtualService $(if ($RequireVirtualService) { "Re3DRestrictedApi" } else { "" })

Push-Location $projectRoot
$processExitCode = 0
try {
    & $python -m dotenv -f $environmentPath run -- `
        $python -m uvicorn apps.api.main:create_app `
        --factory `
        --host 127.0.0.1 `
        --port 8000 `
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

