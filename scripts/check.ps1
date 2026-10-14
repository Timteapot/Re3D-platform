#Requires -Version 7.0
<#
.SYNOPSIS
Run the platform's CPU checks without loading operational environment files.
.EXAMPLE
pwsh -NoProfile -File scripts/check.ps1
.EXAMPLE
pwsh -NoProfile -File scripts/check.ps1 -Component backend -Postgres
.EXAMPLE
pwsh -NoProfile -File scripts/check.ps1 -Component frontend -Install
#>
[CmdletBinding()]
param(
    [ValidateSet("all", "backend", "frontend", "postgres")]
    [string]$Component = "all",
    [switch]$Postgres,
    [switch]$Install,
    [string]$PythonPath
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$originalLocation = Get-Location
$savedEnvironment = @{}
$checkExitCode = 0

function Invoke-CheckCommand {
    param([string]$Label, [string]$Executable, [string[]]$Arguments)
    Write-Host "Checking $Label..." -ForegroundColor Cyan
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        $script:checkExitCode = $LASTEXITCODE
        throw "$Label failed with exit code $LASTEXITCODE."
    }
}

function Set-CheckEnvironment {
    param([string]$Name, [AllowNull()][string]$Value)
    if (-not $script:savedEnvironment.ContainsKey($Name)) {
        $script:savedEnvironment[$Name] = [Environment]::GetEnvironmentVariable($Name, "Process")
    }
    [Environment]::SetEnvironmentVariable($Name, $Value, "Process")
}

try {
    if ($Postgres -and $Component -eq "frontend") {
        throw "-Postgres can only be combined with all, backend or postgres."
    }

    # Tests receive no inherited operational settings or external pytest plugins.
    # Values are saved in memory and restored; .env files are never loaded here.
    foreach ($entry in Get-ChildItem Env:) {
        if ($entry.Name -match "^(APP_|API_|DATABASE_URL$|RE3D_|JWT_|ACCESS_TOKEN_|REFRESH_COOKIE_|REFRESH_TOKEN_|AUTH_|SMTP_|TRANSFER_|UPLOAD_|SUCCESS_|VITE_|PYTHONPATH$|PYTHONHOME$|PYTEST_ADDOPTS$|PYTEST_PLUGINS$)") {
            Set-CheckEnvironment -Name $entry.Name -Value $null
        }
    }
    Set-CheckEnvironment -Name "APP_ENV" -Value "test"
    Set-CheckEnvironment -Name "RE3D_PLATFORM_CHECK" -Value "1"
    # npm ci must include development tools and React tests need the test build,
    # even when the caller normally runs with NODE_ENV=production.
    Set-CheckEnvironment -Name "NODE_ENV" -Value "test"
    Set-CheckEnvironment -Name "PYTHONIOENCODING" -Value "utf-8"
    Set-CheckEnvironment -Name "PYTHONDONTWRITEBYTECODE" -Value "1"
    Set-CheckEnvironment -Name "PYTEST_DISABLE_PLUGIN_AUTOLOAD" -Value "1"

    $runBackend = $Component -in @("all", "backend")
    $runFrontend = $Component -in @("all", "frontend")
    $runPostgres = $Postgres -or $Component -eq "postgres"
    $python = $null

    if ($runBackend -or $runPostgres) {
        $venvCandidates = @(
            (Join-Path $projectRoot ".venv/Scripts/python.exe"),
            (Join-Path $projectRoot ".venv/bin/python")
        )
        $venvPython = $venvCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
        $python = if ($PythonPath) {
            (Get-Command $PythonPath -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
        } elseif ($venvPython) {
            $venvPython
        } else {
            (Get-Command python -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
        }
        $pythonVersion = & $python -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
        if ($LASTEXITCODE -ne 0 -or $pythonVersion -notmatch "^3\.12\.\d+$") {
            throw "Python 3.12 is required. Select it with -PythonPath or create .venv first."
        }
        if ($Install -and -not $PythonPath -and -not $venvPython) {
            Invoke-CheckCommand "project virtual environment" $python @("-m", "venv", (Join-Path $projectRoot ".venv"))
            $python = $venvCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
            if (-not $python) { throw "The project virtual environment was not created." }
        }
    }

    if ($runFrontend) {
        $node = (Get-Command node -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
        $npmName = if ($IsWindows) { "npm.cmd" } else { "npm" }
        $npm = (Get-Command $npmName -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
        $nodeVersion = & $node --version
        if ($LASTEXITCODE -ne 0 -or $nodeVersion -notmatch "^v(\d+\.\d+\.\d+)$" -or [Version]$Matches[1] -lt [Version]"22.12.0") {
            throw "Node.js 22.12.0 or newer is required."
        }
    }

    Set-Location -LiteralPath $projectRoot
    if ($Install -and ($runBackend -or $runPostgres)) {
        Invoke-CheckCommand "locked Python dependencies" $python @("-m", "pip", "install", "-r", "requirements-dev.txt")
    }
    if ($runBackend) {
        Invoke-CheckCommand "Python dependency consistency" $python @("-m", "pip", "check")
        Invoke-CheckCommand "CPU backend tests" $python @("-m", "pytest", "tests/unit", "tests/integration/test_api_worker_flow.py", "-q", "-rs")
    }
    if ($runFrontend) {
        Set-Location -LiteralPath (Join-Path $projectRoot "apps/web")
        if ($Install) {
            Invoke-CheckCommand "locked frontend dependencies" $npm @("ci", "--include=dev")
        }
        Invoke-CheckCommand "frontend tests" $npm @("test")
        # The build script already runs tsc -b before Vite.
        Set-CheckEnvironment -Name "NODE_ENV" -Value "production"
        Invoke-CheckCommand "frontend typecheck and build" $npm @("run", "build")
    }
    if ($runPostgres) {
        Set-Location -LiteralPath $projectRoot
        $pwsh = (Get-Command pwsh -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
        Invoke-CheckCommand "disposable PostgreSQL integration" $pwsh @(
            "-NoProfile", "-File", (Join-Path $projectRoot "deploy/postgres/run-integration-tests.ps1"),
            "-PythonPath", $python
        )
    }
    Write-Host "Requested checks passed." -ForegroundColor Green
} catch {
    if ($checkExitCode -eq 0) { $checkExitCode = 1 }
    Write-Error $_ -ErrorAction Continue
} finally {
    foreach ($name in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], "Process")
    }
    Set-Location -LiteralPath $originalLocation.Path
}
exit $checkExitCode
