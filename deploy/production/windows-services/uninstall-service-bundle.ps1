[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [Parameter(Mandatory = $true)][string]$BundleRoot
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

if (-not (Test-Re3DAdministrator)) {
    throw "Windows service removal requires an elevated PowerShell session."
}
$manifest = Assert-Re3DServiceBundle -BundleRoot $BundleRoot
$orderedServices = foreach ($key in @("caddy", "worker", "api")) {
    @($manifest.services) |
        Where-Object { $_.key -ceq $key } |
        Select-Object -First 1
}

foreach ($service in $orderedServices) {
    $controller = Get-Service -Name $service.id -ErrorAction SilentlyContinue
    if (-not $controller) {
        continue
    }
    if (-not $PSCmdlet.ShouldProcess($service.id, "stop and uninstall service")) {
        continue
    }
    if ($controller.Status -ne [ServiceProcess.ServiceControllerStatus]::Stopped) {
        Stop-Service -Name $service.id -Force
        $controller.WaitForStatus(
            [ServiceProcess.ServiceControllerStatus]::Stopped,
            [TimeSpan]::FromSeconds(60)
        )
    }
    Push-Location (Split-Path -Parent $service.wrapper_path)
    try {
        & $service.wrapper_path uninstall
        if ($LASTEXITCODE -ne 0) {
            throw "WinSW could not uninstall $($service.id)."
        }
    } finally {
        Pop-Location
    }
}

Write-Warning (
    "Services were removed, but runtime files, logs, data, and ACL entries were " +
    "preserved for audit and recovery."
)
