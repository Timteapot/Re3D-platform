param(
    [ValidateSet("Docker", "Standalone", "Existing")]
    [string]$StartupMode = "Docker"
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    throw "Project virtual environment was not found at $venvPython"
}

if ($StartupMode -eq "Docker") {
    & (Join-Path $PSScriptRoot "start-local.ps1")
} elseif ($StartupMode -eq "Standalone") {
    & (Join-Path $PSScriptRoot "start-standalone.ps1")
}
if ($StartupMode -ne "Existing" -and $LASTEXITCODE -ne 0) {
    throw "Mailpit startup failed for mode $StartupMode."
}

$previousHost = $env:RE3D_TEST_MAILPIT_SMTP_HOST
$previousPort = $env:RE3D_TEST_MAILPIT_SMTP_PORT
$previousUiUrl = $env:RE3D_TEST_MAILPIT_UI_URL

try {
    $env:RE3D_TEST_MAILPIT_SMTP_HOST = "127.0.0.1"
    $env:RE3D_TEST_MAILPIT_SMTP_PORT = "1025"
    $env:RE3D_TEST_MAILPIT_UI_URL = "http://127.0.0.1:8025"

    Push-Location $projectRoot
    try {
        & $venvPython -m unittest `
            tests.integration.test_mailpit_auth_flow `
            -v
        if ($LASTEXITCODE -ne 0) {
            throw "Mailpit authentication acceptance test failed."
        }
    } finally {
        Pop-Location
    }
} finally {
    if ($null -eq $previousHost) {
        Remove-Item Env:RE3D_TEST_MAILPIT_SMTP_HOST -ErrorAction SilentlyContinue
    } else {
        $env:RE3D_TEST_MAILPIT_SMTP_HOST = $previousHost
    }
    if ($null -eq $previousPort) {
        Remove-Item Env:RE3D_TEST_MAILPIT_SMTP_PORT -ErrorAction SilentlyContinue
    } else {
        $env:RE3D_TEST_MAILPIT_SMTP_PORT = $previousPort
    }
    if ($null -eq $previousUiUrl) {
        Remove-Item Env:RE3D_TEST_MAILPIT_UI_URL -ErrorAction SilentlyContinue
    } else {
        $env:RE3D_TEST_MAILPIT_UI_URL = $previousUiUrl
    }
}

Write-Host "Authentication email acceptance passed. Inspect captured messages at http://127.0.0.1:8025"
