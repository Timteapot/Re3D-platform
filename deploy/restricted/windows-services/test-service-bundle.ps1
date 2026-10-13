[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BundleRoot,
    [switch]$CheckExternalBinaryVersions
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")
$manifest = Assert-Re3DRestrictedServiceBundle -BundleRoot $BundleRoot -CheckExternalBinaryVersions:$CheckExternalBinaryVersions
[ordered]@{
    status = "valid"
    schema_version = $manifest.schema_version
    start_mode = "Manual"
    services = @($manifest.services | ForEach-Object { $_.id })
    accounts = @($manifest.services | ForEach-Object { $_.account })
    platform_commit = $manifest.source.platform_commit
} | ConvertTo-Json -Depth 4
