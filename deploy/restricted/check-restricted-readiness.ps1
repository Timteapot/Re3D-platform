param(
    [ValidateSet("api", "worker", "all")]
    [string]$Component = "all",
    [string]$EnvironmentFile = "",
    [ValidateSet("", "Re3DRestrictedApi", "Re3DRestrictedWorker")]
    [string]$VirtualService = "",
    [switch]$VirtualServiceSidOptional,
    [switch]$DeferSmtpConnectivityCheck
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$selectedEnvironmentFile = if ([string]::IsNullOrWhiteSpace($EnvironmentFile)) {
    if ($Component -ceq "worker") { ".env.worker.restricted" } else { ".env.restricted" }
} else { $EnvironmentFile }
$configuredPath = if ([IO.Path]::IsPathRooted($selectedEnvironmentFile)) {
    $selectedEnvironmentFile
} else {
    Join-Path $projectRoot $selectedEnvironmentFile
}

if (-not (Test-Path -LiteralPath $configuredPath -PathType Leaf)) {
    throw "Restricted environment file was not found: $configuredPath"
}
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    throw "Project virtual environment was not found at $venvPython"
}
if ($VirtualServiceSidOptional -and -not $VirtualService) {
    throw "VirtualServiceSidOptional requires an explicit restricted virtual service."
}
if ($DeferSmtpConnectivityCheck -and $Component -cne "api") {
    throw "SMTP connectivity may be deferred only for an API component pre-install check."
}
if (
    $VirtualService -and
    (
        $Component -eq "all" -or
        ($Component -eq "api" -and $VirtualService -cne "Re3DRestrictedApi") -or
        ($Component -eq "worker" -and $VirtualService -cne "Re3DRestrictedWorker")
    )
) {
    throw "Restricted readiness component does not match its virtual service identity."
}
if ($VirtualService) {
    Assert-Re3DRestrictedSecretFileAcl `
        -Path $configuredPath `
        -ExpectedVirtualServiceSid $(Get-Re3DRestrictedVirtualServiceSid -ServiceId $VirtualService) `
        -VirtualServiceSidOptional:$VirtualServiceSidOptional
} else {
    Assert-Re3DRestrictedSecretFileAcl -Path $configuredPath
}
if ($Component -eq "all") {
    $workerEnvironment = Join-Path $projectRoot ".env.worker.restricted"
    Assert-Re3DRestrictedSecretFileAcl -Path $workerEnvironment
}

Push-Location $projectRoot
try {
    $readinessArguments = @(
        "-m", "dotenv", "-f", $configuredPath, "run", "--",
        $venvPython, "-m", "apps.maintenance.main",
        "check-restricted-readiness", "--component", $Component
    )
    if ($DeferSmtpConnectivityCheck) {
        $readinessArguments += "--defer-smtp-connectivity-check"
    }
    & $venvPython @readinessArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Restricted readiness validation failed for component $Component."
    }
} finally {
    Pop-Location
}
