param(
    [ValidateSet("api", "worker", "all")]
    [string]$Component = "all",
    [string]$EnvironmentFile = ".env.restricted"
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$configuredPath = if ([IO.Path]::IsPathRooted($EnvironmentFile)) {
    $EnvironmentFile
} else {
    Join-Path $projectRoot $EnvironmentFile
}

if (-not (Test-Path -LiteralPath $configuredPath -PathType Leaf)) {
    throw "Restricted environment file was not found: $configuredPath"
}
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    throw "Project virtual environment was not found at $venvPython"
}
Assert-Re3DRestrictedSecretFileAcl -Path $configuredPath
if ($Component -in @("worker", "all")) {
    $workerEnvironment = Join-Path $projectRoot ".env.worker.restricted"
    Assert-Re3DRestrictedSecretFileAcl -Path $workerEnvironment
}

Push-Location $projectRoot
try {
    & $venvPython -m dotenv -f $configuredPath run -- `
        $venvPython -m apps.maintenance.main `
        check-restricted-readiness --component $Component
    if ($LASTEXITCODE -ne 0) {
        throw "Restricted readiness validation failed for component $Component."
    }
} finally {
    Pop-Location
}
