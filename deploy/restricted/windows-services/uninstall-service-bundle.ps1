[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param([Parameter(Mandatory = $true)][string]$BundleRoot)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")
if (-not (Test-Re3DAdministrator)) { throw "Removing restricted services requires elevation." }
$manifest = Assert-Re3DRestrictedServiceBundle `
    -BundleRoot $BundleRoot `
    -AllowOptionalServiceSecretAcls

function Reset-Re3DRestrictedSecretAcl {
    param([Parameter(Mandatory = $true)][string]$Key)
    if ($Key -ceq "api") {
        Set-Re3DRestrictedBundleSecretAcl -Path $manifest.external.api_environment_file
    } elseif ($Key -ceq "worker") {
        Set-Re3DRestrictedBundleSecretAcl -Path $manifest.external.worker_environment_file
    }
}

foreach ($key in @("caddy", "worker", "api", "mailpit")) {
    $service = @($manifest.services) | Where-Object { $_.key -ceq $key } | Select-Object -First 1
    $controller = Get-Service -Name $service.id -ErrorAction SilentlyContinue
    if (-not $controller) {
        if (
            $key -in @("api", "worker") -and
            $PSCmdlet.ShouldProcess($service.id, "remove stale service SID access from secret file")
        ) {
            Reset-Re3DRestrictedSecretAcl -Key $key
        }
        continue
    }
    if (-not $PSCmdlet.ShouldProcess($service.id, "stop and uninstall service")) {
        $controller.Dispose()
        continue
    }
    try {
        if ($controller.Status -ne [ServiceProcess.ServiceControllerStatus]::Stopped) {
            Stop-Service -Name $service.id -Force
            $controller.WaitForStatus([ServiceProcess.ServiceControllerStatus]::Stopped, [TimeSpan]::FromSeconds(60))
        }
    } finally {
        $controller.Dispose()
    }
    Push-Location (Split-Path -Parent $service.wrapper_path)
    try {
        & $service.wrapper_path uninstall
        if ($LASTEXITCODE -ne 0) { throw "WinSW could not uninstall $($service.id)." }
        Reset-Re3DRestrictedSecretAcl -Key $key
    } finally { Pop-Location }
}
Write-Warning (
    "Services were removed; bundle files, logs, state, data, and non-secret " +
    "ACL entries were retained. Service SID access was removed from secret files."
)
