[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BundleRoot
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

if (-not (Test-Re3DAdministrator)) {
    throw "Restricted Windows service installation requires an elevated PowerShell session."
}
$manifest = Assert-Re3DRestrictedServiceBundle -BundleRoot $BundleRoot -CheckExternalBinaryVersions
Assert-Re3DRestrictedServiceSourceState -Manifest $manifest | Out-Null

$readiness = Join-Path $manifest.external.project_root "deploy\restricted\check-restricted-readiness.ps1"
$apiReadinessRaw = (& $readiness `
    -Component api `
    -EnvironmentFile $manifest.external.api_environment_file `
    -VirtualService Re3DRestrictedApi `
    -VirtualServiceSidOptional `
    -DeferSmtpConnectivityCheck | Out-String)
$apiReadiness = $apiReadinessRaw | ConvertFrom-Json
if (
    $apiReadiness.status -cne "ready" -or
    $apiReadiness.component -cne "api" -or
    $apiReadiness.smtp.connectivity_checked -ne $false
) {
    throw "Restricted API pre-install readiness did not defer only SMTP connectivity."
}
$workerReadinessRaw = (& $readiness `
    -Component worker `
    -EnvironmentFile $manifest.external.worker_environment_file `
    -VirtualService Re3DRestrictedWorker `
    -VirtualServiceSidOptional | Out-String)
$workerReadiness = $workerReadinessRaw | ConvertFrom-Json
if (
    $workerReadiness.status -cne "ready" -or
    $workerReadiness.component -cne "worker" -or
    $null -ne $workerReadiness.smtp
) {
    throw "Restricted Worker pre-install readiness validation failed."
}

foreach ($service in @($manifest.services)) {
    if (Get-Service -Name $service.id -ErrorAction SilentlyContinue) {
        throw "Service already exists and will not be overwritten: $($service.id)"
    }
}
foreach ($boundary in @(
    $manifest.external.project_root,
    $manifest.runtime_root,
    $manifest.external.data_root,
    $manifest.external.re3d_root,
    (Split-Path -Parent $manifest.external.caddy_path),
    (Split-Path -Parent $manifest.external.mailpit_path),
    @($manifest.external.worker_execution_roots)
) | ForEach-Object { $_ } | Select-Object -Unique) {
    Assert-Re3DNoBroadWriteAccess -Path $boundary
}

$installed = [Collections.Generic.List[object]]::new()
try {
    foreach ($service in @($manifest.services)) {
        Push-Location (Split-Path -Parent $service.wrapper_path)
        try {
            & $service.wrapper_path install
            if ($LASTEXITCODE -ne 0) { throw "WinSW could not install $($service.id)." }
        } finally { Pop-Location }
        $installed.Add($service)
        & sc.exe sidtype $service.id unrestricted | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not enable the service SID for $($service.id)." }
    }

    $serviceSids = @{}
    foreach ($service in @($manifest.services)) {
        $definition = Get-Re3DRestrictedWindowsServiceDefinition -Key $service.key
        $sid = Get-Re3DIdentitySid -Identity "NT SERVICE\$($definition.Id)"
        $serviceSids[$service.key] = $sid
        Add-Re3DDirectoryAccessRule -Path $manifest.external.project_root -Sid $sid -Rights ([Security.AccessControl.FileSystemRights]::ReadAndExecute)
        Add-Re3DDirectoryAccessRule -Path (Split-Path -Parent $service.wrapper_path) -Sid $sid -Rights ([Security.AccessControl.FileSystemRights]::ReadAndExecute)
        Add-Re3DDirectoryAccessRule -Path $service.log_directory -Sid $sid -Rights ([Security.AccessControl.FileSystemRights]::Modify)
    }
    Set-Re3DRestrictedBundleSecretAcl -Path $manifest.external.api_environment_file -ServiceSid $serviceSids.api
    Set-Re3DRestrictedBundleSecretAcl -Path $manifest.external.worker_environment_file -ServiceSid $serviceSids.worker
    foreach ($key in @("api", "worker")) {
        Add-Re3DDirectoryAccessRule -Path $manifest.external.data_root -Sid $serviceSids[$key] -Rights ([Security.AccessControl.FileSystemRights]::Modify)
    }
    Add-Re3DDirectoryAccessRule -Path $manifest.external.re3d_root -Sid $serviceSids.worker -Rights ([Security.AccessControl.FileSystemRights]::ReadAndExecute)
    foreach ($executionRoot in @($manifest.external.worker_execution_roots)) {
        Add-Re3DDirectoryAccessRule -Path $executionRoot -Sid $serviceSids.worker -Rights ([Security.AccessControl.FileSystemRights]::ReadAndExecute)
    }
    Add-Re3DDirectoryAccessRule -Path $manifest.external.worker_temp_root -Sid $serviceSids.worker -Rights ([Security.AccessControl.FileSystemRights]::Modify)
    Add-Re3DDirectoryAccessRule -Path $manifest.external.web_root -Sid $serviceSids.caddy -Rights ([Security.AccessControl.FileSystemRights]::ReadAndExecute)
    Add-Re3DFileAccessRule -Path $manifest.external.caddy_path -Sid $serviceSids.caddy -Rights ([Security.AccessControl.FileSystemRights]::ReadAndExecute)
    Add-Re3DDirectoryAccessRule -Path $manifest.external.mailpit_state_root -Sid $serviceSids.mailpit -Rights ([Security.AccessControl.FileSystemRights]::Modify)
    Add-Re3DFileAccessRule -Path $manifest.external.mailpit_path -Sid $serviceSids.mailpit -Rights ([Security.AccessControl.FileSystemRights]::ReadAndExecute)
} catch {
    $failure = $_
    $rolledBack = [Collections.Generic.List[string]]::new()
    for ($index = $installed.Count - 1; $index -ge 0; $index--) {
        $service = $installed[$index]
        try {
            Push-Location (Split-Path -Parent $service.wrapper_path)
            & $service.wrapper_path uninstall 2>$null | Out-Null
            if ($LASTEXITCODE -ne 0) {
                throw "WinSW could not roll back $($service.id)."
            }
            $rolledBack.Add($service.key)
        } catch {
            Write-Warning "Could not roll back service $($service.id)."
        } finally { Pop-Location }
    }
    if ($rolledBack -contains "api") {
        Set-Re3DRestrictedBundleSecretAcl -Path $manifest.external.api_environment_file
    }
    if ($rolledBack -contains "worker") {
        Set-Re3DRestrictedBundleSecretAcl -Path $manifest.external.worker_environment_file
    }
    throw $failure
}

[ordered]@{
    status = "installed"
    start_mode = "Manual"
    services = @($manifest.services | ForEach-Object { $_.id })
    accounts = @($manifest.services | ForEach-Object { $_.account })
} | ConvertTo-Json -Depth 4
