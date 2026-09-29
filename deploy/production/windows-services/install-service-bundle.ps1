[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BundleRoot,
    [switch]$StartServices
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

if (-not (Test-Re3DAdministrator)) {
    throw "Windows service installation requires an elevated PowerShell session."
}
$manifest = Assert-Re3DServiceBundle `
    -BundleRoot $BundleRoot `
    -CheckExternalBinaryVersions
Assert-Re3DServiceSourceState -Manifest $manifest | Out-Null
$projectRoot = Resolve-Re3DServicePath `
    -Path $manifest.project_root `
    -PathType Container
$readinessScript = Resolve-Re3DServicePath `
    -Path (Join-Path $projectRoot "deploy\production\check-production-readiness.ps1") `
    -PathType Leaf

& $readinessScript `
    -Component api `
    -EnvironmentFile $manifest.external.api_environment_file
if ($LASTEXITCODE -ne 0) {
    throw "API production readiness check failed."
}
& $readinessScript `
    -Component worker `
    -EnvironmentFile $manifest.external.worker_environment_file
if ($LASTEXITCODE -ne 0) {
    throw "Worker production readiness check failed."
}

foreach ($service in @($manifest.services)) {
    if (Get-Service -Name $service.id -ErrorAction SilentlyContinue) {
        throw "Service already exists and will not be overwritten: $($service.id)"
    }
    $actualHash = (Get-FileHash `
        -LiteralPath $service.wrapper_path `
        -Algorithm SHA256
    ).Hash
    if ($actualHash -cne $manifest.winsw.source_sha256) {
        throw "WinSW wrapper hash mismatch for $($service.id)."
    }
}

$aclBoundaries = @(
    $projectRoot,
    $manifest.runtime_root,
    $manifest.external.data_root,
    $manifest.external.re3d_root,
    (Split-Path -Parent $manifest.external.api_environment_file),
    (Split-Path -Parent $manifest.external.worker_environment_file),
    (Split-Path -Parent $manifest.external.caddy_path)
    @($manifest.external.worker_execution_roots)
) | ForEach-Object { $_ } | Select-Object -Unique
foreach ($boundary in $aclBoundaries) {
    Assert-Re3DNoBroadWriteAccess -Path $boundary
}

$previousSiteAddress = $env:RE3D_SITE_ADDRESS
$previousBindAddress = $env:RE3D_BIND_ADDRESS
$previousApiUpstream = $env:RE3D_API_UPSTREAM
$previousMaxRequestBody = $env:RE3D_MAX_REQUEST_BODY
$previousWebRoot = $env:RE3D_WEB_ROOT
try {
    $env:RE3D_SITE_ADDRESS = $manifest.topology.site_address
    $env:RE3D_BIND_ADDRESS = $manifest.topology.bind_address
    $env:RE3D_API_UPSTREAM = "127.0.0.1:$($manifest.topology.api_port)"
    $env:RE3D_MAX_REQUEST_BODY = $manifest.topology.max_request_body
    $env:RE3D_WEB_ROOT = $manifest.external.web_root.Replace("\", "/")
    & $manifest.external.caddy_path validate `
        --config (Join-Path $projectRoot "deploy\production\Caddyfile") `
        --adapter caddyfile
    if ($LASTEXITCODE -ne 0) {
        throw "Caddy production configuration validation failed."
    }
} finally {
    $env:RE3D_SITE_ADDRESS = $previousSiteAddress
    $env:RE3D_BIND_ADDRESS = $previousBindAddress
    $env:RE3D_API_UPSTREAM = $previousApiUpstream
    $env:RE3D_MAX_REQUEST_BODY = $previousMaxRequestBody
    $env:RE3D_WEB_ROOT = $previousWebRoot
}

$installed = [Collections.Generic.List[object]]::new()
try {
    foreach ($service in @($manifest.services)) {
        Push-Location (Split-Path -Parent $service.wrapper_path)
        try {
            & $service.wrapper_path install
            if ($LASTEXITCODE -ne 0) {
                throw "WinSW could not install $($service.id)."
            }
        } finally {
            Pop-Location
        }
        $installed.Add($service)
        & sc.exe sidtype $service.id unrestricted | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Could not enable the service SID for $($service.id)."
        }
    }

    $serviceSids = @{}
    foreach ($service in @($manifest.services)) {
        $serviceSids[$service.key] = Get-Re3DIdentitySid `
            -Identity $service.virtual_identity
        Add-Re3DDirectoryAccessRule `
            -Path $projectRoot `
            -Sid $serviceSids[$service.key] `
            -Rights ([Security.AccessControl.FileSystemRights]::ReadAndExecute)
        Add-Re3DDirectoryAccessRule `
            -Path (Split-Path -Parent $service.wrapper_path) `
            -Sid $serviceSids[$service.key] `
            -Rights ([Security.AccessControl.FileSystemRights]::Modify)
        Add-Re3DDirectoryAccessRule `
            -Path $service.log_directory `
            -Sid $serviceSids[$service.key] `
            -Rights ([Security.AccessControl.FileSystemRights]::Modify)
    }

    Set-Re3DSecretFileAcl `
        -Path $manifest.external.api_environment_file `
        -ServiceSid $serviceSids.api
    Set-Re3DSecretFileAcl `
        -Path $manifest.external.worker_environment_file `
        -ServiceSid $serviceSids.worker
    foreach ($key in @("api", "worker")) {
        Add-Re3DDirectoryAccessRule `
            -Path $manifest.external.data_root `
            -Sid $serviceSids[$key] `
            -Rights ([Security.AccessControl.FileSystemRights]::Modify)
    }
    Add-Re3DDirectoryAccessRule `
        -Path $manifest.external.re3d_root `
        -Sid $serviceSids.worker `
        -Rights ([Security.AccessControl.FileSystemRights]::ReadAndExecute)
    foreach ($executionRoot in @($manifest.external.worker_execution_roots)) {
        Add-Re3DDirectoryAccessRule `
            -Path $executionRoot `
            -Sid $serviceSids.worker `
            -Rights ([Security.AccessControl.FileSystemRights]::ReadAndExecute)
    }
    Add-Re3DDirectoryAccessRule `
        -Path $manifest.external.web_root `
        -Sid $serviceSids.caddy `
        -Rights ([Security.AccessControl.FileSystemRights]::ReadAndExecute)
    Add-Re3DFileAccessRule `
        -Path $manifest.external.caddy_path `
        -Sid $serviceSids.caddy `
        -Rights ([Security.AccessControl.FileSystemRights]::ReadAndExecute)
    Add-Re3DDirectoryAccessRule `
        -Path (Join-Path $manifest.runtime_root "caddy-state") `
        -Sid $serviceSids.caddy `
        -Rights ([Security.AccessControl.FileSystemRights]::Modify)
} catch {
    $installationError = $_
    for ($index = $installed.Count - 1; $index -ge 0; $index--) {
        $service = $installed[$index]
        try {
            Push-Location (Split-Path -Parent $service.wrapper_path)
            & $service.wrapper_path uninstall 2>$null | Out-Null
        } catch {
            Write-Warning "Could not roll back service $($service.id)."
        } finally {
            Pop-Location
        }
    }
    throw $installationError
}

if ($StartServices) {
    foreach ($key in @("api", "worker", "caddy")) {
        $service = @($manifest.services) |
            Where-Object { $_.key -ceq $key } |
            Select-Object -First 1
        Start-Service -Name $service.id
        $controller = Get-Service -Name $service.id
        $controller.WaitForStatus(
            [ServiceProcess.ServiceControllerStatus]::Running,
            [TimeSpan]::FromSeconds(60)
        )
    }
}

$result = [ordered]@{
    status = if ($StartServices) { "running" } else { "installed" }
    services = @($manifest.services | ForEach-Object { $_.id })
    service_accounts = @($manifest.services | ForEach-Object { $_.account })
    service_sids = @($manifest.services | ForEach-Object { $_.virtual_identity })
}
Write-Output ($result | ConvertTo-Json -Depth 4)
