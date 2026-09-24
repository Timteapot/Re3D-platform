$ErrorActionPreference = "Stop"

$psql = "C:\Program Files\PostgreSQL\18\bin\psql.exe"
$bootstrap = Join-Path $PSScriptRoot "bootstrap-dev.psql"

if (-not (Test-Path -LiteralPath $psql -PathType Leaf)) {
    throw "PostgreSQL 18 psql.exe was not found at: $psql"
}

Write-Host "Re3D Platform development database bootstrap" -ForegroundColor Cyan
Write-Host "Passwords are entered directly into psql and are not written to this script."
Write-Host ""

& $psql `
    -W `
    -h 127.0.0.1 `
    -p 5432 `
    -U postgres `
    -d postgres `
    -f $bootstrap

$bootstrapExitCode = $LASTEXITCODE
if ($bootstrapExitCode -eq 0) {
    Write-Host ""
    Write-Host "Bootstrap completed successfully." -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "Bootstrap failed with exit code $bootstrapExitCode." -ForegroundColor Red
}

Read-Host "Press Enter to close this window"
exit $bootstrapExitCode
