[CmdletBinding()]
param(
    [string]$CaddyPath = "D:\3Dreconstruction\tools\caddy\v2.11.4\caddy.exe",
    [string]$WebRoot
)

$ErrorActionPreference = "Stop"
$expectedSha256 = "5cb9ab71e5756ce72840b8234177a2f40c8b4ab47a806b8e841e2b784e9df62b"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$configPath = Join-Path $PSScriptRoot "Caddyfile"
$resolvedCaddy = (Resolve-Path -LiteralPath $CaddyPath -ErrorAction Stop).Path
$resolvedWebRoot = if ($WebRoot) {
    (Resolve-Path -LiteralPath $WebRoot -ErrorAction Stop).Path
} else {
    (Resolve-Path -LiteralPath (Join-Path $projectRoot "apps\web\dist") -ErrorAction Stop).Path
}

if (-not (Test-Path -LiteralPath (Join-Path $resolvedWebRoot "index.html") -PathType Leaf)) {
    throw "Restricted web root does not contain index.html."
}
$actualSha256 = (Get-FileHash -LiteralPath $resolvedCaddy -Algorithm SHA256).Hash
if ($actualSha256 -ne $expectedSha256) {
    throw "Caddy executable SHA-256 mismatch. Expected $expectedSha256 but found $actualSha256."
}
$reportedVersion = & $resolvedCaddy version
if ($LASTEXITCODE -ne 0 -or $reportedVersion -notmatch "^v2\.11\.4(?:\s|$)") {
    throw "Restricted stack requires the reviewed Caddy v2.11.4 binary."
}

$previousWebRoot = $env:RE3D_RESTRICTED_WEB_ROOT
$processExitCode = 0
try {
    $env:RE3D_RESTRICTED_WEB_ROOT = $resolvedWebRoot.Replace("\", "/")
    & $resolvedCaddy validate --config $configPath --adapter caddyfile
    if ($LASTEXITCODE -ne 0) {
        throw "Restricted Caddy configuration validation failed."
    }
    & $resolvedCaddy run --config $configPath --adapter caddyfile
    $processExitCode = $LASTEXITCODE
} finally {
    $env:RE3D_RESTRICTED_WEB_ROOT = $previousWebRoot
}
exit $processExitCode

