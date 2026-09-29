[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BundleRoot,
    [switch]$CheckExternalBinaryVersions
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$manifest = Assert-Re3DServiceBundle `
    -BundleRoot $BundleRoot `
    -CheckExternalBinaryVersions:$CheckExternalBinaryVersions

$summary = [ordered]@{
    status = "valid"
    schema_version = $manifest.schema_version
    winsw_version = $manifest.winsw.version
    service_ids = @($manifest.services | ForEach-Object { $_.id })
    passwordless_accounts = @($manifest.services | ForEach-Object { $_.account })
    runtime_root = $manifest.runtime_root
}
Write-Output ($summary | ConvertTo-Json -Depth 4)
