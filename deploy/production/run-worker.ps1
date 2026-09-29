[CmdletBinding()]
param(
    [string]$EnvironmentFile = ".env.worker.production"
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
        $python -m apps.worker.main run-real-queued-loop
    $processExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $processExitCode
