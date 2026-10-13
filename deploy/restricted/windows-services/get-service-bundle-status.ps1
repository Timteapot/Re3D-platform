[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$BundleRoot)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")
$bundleValid = $true
$bundleError = $null
try {
    Assert-Re3DRestrictedServiceBundle -BundleRoot $BundleRoot | Out-Null
} catch {
    $bundleValid = $false
    $bundleError = $_.Exception.Message
}
$services = @()
foreach ($definition in Get-Re3DRestrictedWindowsServiceDefinitions) {
    $instance = Get-CimInstance Win32_Service -Filter "Name = '$($definition.Id)'" -ErrorAction SilentlyContinue
    $services += [ordered]@{
        key = $definition.Key
        id = $definition.Id
        installed = $null -ne $instance
        state = if ($instance) { $instance.State } else { "NotInstalled" }
        start_mode = if ($instance) { $instance.StartMode } else { $null }
        account = if ($instance) { $instance.StartName } else { $null }
        process_id = if ($instance) { [int]$instance.ProcessId } else { 0 }
    }
}
$ports = [ordered]@{}
foreach ($port in @(1025, 8025, 8000, 8080)) {
    $ports["$port"] = @(
        Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    ).Count -gt 0
}
$states = @($services | ForEach-Object { $_.state })
[ordered]@{
    status = if (@($states | Where-Object { $_ -eq "Running" }).Count -eq 4) {
        "running"
    } elseif (@($states | Where-Object { $_ -eq "Stopped" }).Count -eq 4) {
        "stopped"
    } elseif (@($states | Where-Object { $_ -eq "NotInstalled" }).Count -eq 4) {
        "not-installed"
    } else {
        "partial"
    }
    bundle_valid = $bundleValid
    bundle_error = $bundleError
    services = $services
    listeners = $ports
} | ConvertTo-Json -Depth 6
