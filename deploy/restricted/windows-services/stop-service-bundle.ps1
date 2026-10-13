[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$BundleRoot)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")
if (-not (Test-Re3DAdministrator)) { throw "Stopping restricted services requires elevation." }
[IO.Path]::GetFullPath($BundleRoot) | Out-Null

$failures = [Collections.Generic.List[string]]::new()
foreach ($key in @("caddy", "worker", "api", "mailpit")) {
    $definition = Get-Re3DRestrictedWindowsServiceDefinition -Key $key
    try {
        $controller = Get-Service -Name $definition.Id -ErrorAction SilentlyContinue
        if ($controller -and $controller.Status -ne [ServiceProcess.ServiceControllerStatus]::Stopped) {
            Stop-Service -Name $definition.Id -Force
            $controller.WaitForStatus([ServiceProcess.ServiceControllerStatus]::Stopped, [TimeSpan]::FromSeconds(60))
        }
    } catch {
        $failures.Add("$($definition.Id): $($_.Exception.Message)")
    } finally {
        if ($controller) { $controller.Dispose() }
    }
}
if ($failures.Count -gt 0) {
    throw "One or more restricted services could not be stopped: $($failures -join ' | ')"
}
Write-Output '{"status":"stopped"}'
