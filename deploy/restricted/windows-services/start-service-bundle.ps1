[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$BundleRoot)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")
if (-not (Test-Re3DAdministrator)) { throw "Starting restricted services requires elevation." }
$manifest = Assert-Re3DRestrictedServiceBundle -BundleRoot $BundleRoot
Assert-Re3DRestrictedServiceSourceState -Manifest $manifest | Out-Null
$portsByService = @{
    mailpit = @(1025, 8025)
    api = @(8000)
    worker = @()
    caddy = @(8080)
}

$started = [Collections.Generic.List[object]]::new()
try {
    foreach ($key in @("mailpit", "api", "worker", "caddy")) {
        $service = @($manifest.services) | Where-Object { $_.key -ceq $key } | Select-Object -First 1
        $controller = Get-Service -Name $service.id -ErrorAction Stop
        if ($controller.Status -ne [ServiceProcess.ServiceControllerStatus]::Running) {
            foreach ($port in $portsByService[$key]) {
                if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
                    throw (
                        "TCP port $port is already in use before $($service.id) starts. " +
                        "Stop the previous standalone or interactive stack first."
                    )
                }
            }
            Start-Service -Name $service.id
            $started.Add($service)
            $controller.WaitForStatus([ServiceProcess.ServiceControllerStatus]::Running, [TimeSpan]::FromSeconds(60))
        }
    }
} catch {
    $failure = $_
    for ($index = $started.Count - 1; $index -ge 0; $index--) {
        try {
            $controller = Get-Service -Name $started[$index].id -ErrorAction SilentlyContinue
            if ($controller -and $controller.Status -ne [ServiceProcess.ServiceControllerStatus]::Stopped) {
                Stop-Service -Name $started[$index].id -Force
                $controller.WaitForStatus([ServiceProcess.ServiceControllerStatus]::Stopped, [TimeSpan]::FromSeconds(60))
            }
        } catch {
            Write-Warning "Could not roll back service $($started[$index].id)."
        }
    }
    throw $failure
}
Write-Output '{"status":"running"}'
