[CmdletBinding()]
param(
    [string]$EnvironmentFile = ".env.worker.restricted",
    [switch]$RequireVirtualService
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$projectRoot = Get-Re3DRestrictedProjectRoot
$expectedServiceIdentity = "NT SERVICE\Re3DRestrictedWorker"
if ($RequireVirtualService) {
    $actualSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $expectedSid = [Security.Principal.NTAccount]::new(
        $expectedServiceIdentity
    ).Translate([Security.Principal.SecurityIdentifier])
    if ($null -eq $actualSid -or $actualSid.Value -cne $expectedSid.Value) {
        throw "Restricted Worker service must run as $expectedServiceIdentity."
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
    -VirtualService $(if ($RequireVirtualService) { "Re3DRestrictedWorker" } else { "" })

Push-Location $projectRoot
$processExitCode = 0
try {
    & $python -m dotenv -f $environmentPath run -- `
        $python -m apps.worker.main run-real-queued-loop
    $processExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $processExitCode

