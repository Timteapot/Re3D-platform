[CmdletBinding()]
param(
    [string]$CaddyPath,
    [string]$SiteAddress,
    [string]$BindAddress = "0.0.0.0",
    [string]$ApiUpstream = "127.0.0.1:8000",
    [string]$MaxRequestBody = "27MB",
    [string]$WebRoot
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$configPath = Join-Path $PSScriptRoot "Caddyfile"
$configuredCaddy = if ($CaddyPath) {
    (Resolve-Path -LiteralPath $CaddyPath -ErrorAction Stop).Path
} else {
    (Get-Command caddy.exe -ErrorAction Stop).Source
}
$configuredWebRoot = if ($WebRoot) {
    (Resolve-Path -LiteralPath $WebRoot -ErrorAction Stop).Path
} else {
    (Resolve-Path -LiteralPath (
        Join-Path $projectRoot "apps\web\dist"
    ) -ErrorAction Stop).Path
}
if (-not (Test-Path -LiteralPath (Join-Path $configuredWebRoot "index.html") -PathType Leaf)) {
    throw "Production web root does not contain index.html."
}
if ([string]::IsNullOrWhiteSpace($SiteAddress)) {
    throw "SiteAddress is required. Use an HTTPS domain in production."
}
if (
    $SiteAddress -match "^http://" -and
    $SiteAddress -notmatch "^http://(127\.0\.0\.1|\[::1\]|localhost)(:[0-9]{1,5})?$"
) {
    throw "Plain HTTP SiteAddress is allowed only for loopback acceptance."
}
if ($ApiUpstream -notmatch "^(127\.0\.0\.1|\[::1\]):[0-9]{1,5}$") {
    throw "ApiUpstream must be a loopback host and port."
}
if ($MaxRequestBody -notmatch "^[1-9][0-9]*(B|KB|MB|GB)$") {
    throw "MaxRequestBody must be a positive size such as 27MB."
}
$parsedBindAddress = $null
if (-not [Net.IPAddress]::TryParse($BindAddress, [ref]$parsedBindAddress)) {
    throw "BindAddress must be an IP address."
}

$previousSiteAddress = $env:RE3D_SITE_ADDRESS
$previousBindAddress = $env:RE3D_BIND_ADDRESS
$previousApiUpstream = $env:RE3D_API_UPSTREAM
$previousMaxRequestBody = $env:RE3D_MAX_REQUEST_BODY
$previousWebRoot = $env:RE3D_WEB_ROOT
$processExitCode = 0
try {
    $env:RE3D_SITE_ADDRESS = $SiteAddress
    $env:RE3D_BIND_ADDRESS = $parsedBindAddress.ToString()
    $env:RE3D_API_UPSTREAM = $ApiUpstream
    $env:RE3D_MAX_REQUEST_BODY = $MaxRequestBody
    $env:RE3D_WEB_ROOT = $configuredWebRoot.Replace("\", "/")

    & $configuredCaddy validate --config $configPath --adapter caddyfile
    if ($LASTEXITCODE -ne 0) {
        throw "Caddy configuration validation failed."
    }
    & $configuredCaddy run --config $configPath --adapter caddyfile
    $processExitCode = $LASTEXITCODE
} finally {
    $env:RE3D_SITE_ADDRESS = $previousSiteAddress
    $env:RE3D_BIND_ADDRESS = $previousBindAddress
    $env:RE3D_API_UPSTREAM = $previousApiUpstream
    $env:RE3D_MAX_REQUEST_BODY = $previousMaxRequestBody
    $env:RE3D_WEB_ROOT = $previousWebRoot
}
exit $processExitCode
