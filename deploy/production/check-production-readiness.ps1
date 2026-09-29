param(
    [ValidateSet("api", "worker", "all")]
    [string]$Component = "all",
    [string]$EnvironmentFile = ".env.production"
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$configuredPath = if ([IO.Path]::IsPathRooted($EnvironmentFile)) {
    $EnvironmentFile
} else {
    Join-Path $projectRoot $EnvironmentFile
}

if (-not (Test-Path -LiteralPath $configuredPath -PathType Leaf)) {
    throw "Production environment file was not found: $configuredPath"
}
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    throw "Project virtual environment was not found at $venvPython"
}

Push-Location $projectRoot
try {
    & $venvPython -m dotenv -f $configuredPath run -- `
        $venvPython -m apps.maintenance.main `
        check-production-readiness --component $Component
    if ($LASTEXITCODE -ne 0) {
        throw "Production readiness validation failed for component $Component."
    }
} finally {
    Pop-Location
}
