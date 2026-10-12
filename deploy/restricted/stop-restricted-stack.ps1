[CmdletBinding()]
param(
    [switch]$KeepMailpit,
    [string]$StateRoot = ""
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "process-common.ps1")
if ([string]::IsNullOrWhiteSpace($StateRoot)) {
    $StateRoot = Get-Re3DRestrictedStackStateRoot
}

$failures = [Collections.Generic.List[string]]::new()
foreach ($component in @("caddy", "worker", "api")) {
    try {
        $outcome = Stop-Re3DRestrictedManagedProcess -StateRoot $StateRoot -Component $component
        Write-Host "$component`: $outcome"
    } catch {
        $failures.Add("$component`: $($_.Exception.Message)")
    }
}

if (-not $KeepMailpit) {
    try {
        & (Join-Path $PSScriptRoot "..\mailpit\stop-standalone.ps1")
    } catch {
        $failures.Add("mailpit: $($_.Exception.Message)")
    }
}

if ($failures.Count -gt 0) {
    throw "Restricted stack stop was incomplete: $($failures -join ' | ')"
}
Write-Host "Restricted stack stopped. PostgreSQL remains running as an installed local dependency." -ForegroundColor Green

