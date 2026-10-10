$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
. (Join-Path $PSScriptRoot "common.ps1")

$targets = @(
    (Join-Path $projectRoot ".env.restricted"),
    (Join-Path $projectRoot ".env.worker.restricted")
)
foreach ($target in $targets) {
    Set-Re3DRestrictedSecretFileAcl -Path $target
    Assert-Re3DRestrictedSecretFileAcl -Path $target
}

Write-Host "Restricted environment ACLs are protected." -ForegroundColor Green
