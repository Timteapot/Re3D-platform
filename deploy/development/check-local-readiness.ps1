$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$envPath = Join-Path $projectRoot ".env"
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
    throw ".env does not exist. Run initialize-local-env.ps1 first."
}
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    throw "Project virtual environment was not found at $venvPython"
}

Push-Location $projectRoot
try {
    & $venvPython -m dotenv -f .env run -- `
        $venvPython -m apps.maintenance.main check-local-readiness
    if ($LASTEXITCODE -ne 0) {
        throw "Local readiness validation failed."
    }
} finally {
    Pop-Location
}
