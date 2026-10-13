[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$OutputRoot,
    [Parameter(Mandatory = $true)][string]$WinSWPath,
    [string]$ProjectRoot,
    [string]$CaddyPath,
    [string]$MailpitPath,
    [string]$ApiEnvironmentFile = ".env.restricted",
    [string]$WorkerEnvironmentFile = ".env.worker.restricted",
    [string]$WebRoot,
    [string]$WorkerTempRoot,
    [ValidateRange(1, 10000)][int]$ApiLimitConcurrency = 16
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

if (-not (Test-Re3DAdministrator)) {
    throw "Restricted service bundle generation requires an elevated PowerShell session."
}
$resolvedProjectRoot = if ($ProjectRoot) {
    Resolve-Re3DServicePath -Path $ProjectRoot -PathType Container
} else {
    (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
}
$workspaceRoot = Split-Path -Parent $resolvedProjectRoot
$python = Get-Re3DRestrictedPython -ProjectRoot $resolvedProjectRoot
$powerShell = (Get-Command pwsh.exe -ErrorAction Stop).Source
$resolvedWinSW = Resolve-Re3DServicePath -Path $WinSWPath -PathType Leaf
$expectedWinSWSha256 = "05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da"
$actualWinSWSha256 = (Get-FileHash -LiteralPath $resolvedWinSW -Algorithm SHA256).Hash
if ($actualWinSWSha256 -ine $expectedWinSWSha256) {
    throw "WinSW 2.12.0 executable SHA-256 mismatch."
}
$resolvedCaddy = Resolve-Re3DServicePath `
    -Path $(if ($CaddyPath) { $CaddyPath } else { Join-Path $workspaceRoot "tools\caddy\v2.11.4\caddy.exe" }) `
    -PathType Leaf
$resolvedMailpit = Resolve-Re3DServicePath `
    -Path $(if ($MailpitPath) { $MailpitPath } else { Join-Path $workspaceRoot "tools\mailpit\v1.31.3\mailpit.exe" }) `
    -PathType Leaf
$sourceApiEnvironment = Assert-Re3DRestrictedEnvironmentFile `
    -Path $(if ([IO.Path]::IsPathRooted($ApiEnvironmentFile)) { $ApiEnvironmentFile } else { Join-Path $resolvedProjectRoot $ApiEnvironmentFile }) `
    -Python $python `
    -RequireDeploymentOwner
$sourceWorkerEnvironment = Assert-Re3DRestrictedEnvironmentFile `
    -Path $(if ([IO.Path]::IsPathRooted($WorkerEnvironmentFile)) { $WorkerEnvironmentFile } else { Join-Path $resolvedProjectRoot $WorkerEnvironmentFile }) `
    -Python $python `
    -RequireDeploymentOwner
if ($sourceApiEnvironment -ceq $sourceWorkerEnvironment) {
    throw "API and Worker must use separate restricted environment files."
}
Assert-Re3DWorkerSecretBoundary -EnvironmentFile $sourceWorkerEnvironment

$sourceState = Get-Re3DGitSourceState -ProjectRoot $resolvedProjectRoot
if ($sourceState.Dirty) {
    throw "Restricted service bundle generation requires a clean Git worktree."
}
$resolvedWebRoot = if ($WebRoot) {
    Resolve-Re3DServicePath -Path $WebRoot -PathType Container
} else {
    Resolve-Re3DServicePath -Path (Join-Path $resolvedProjectRoot "apps\web\dist") -PathType Container
}
$webDeploymentFile = Resolve-Re3DServicePath -Path (Join-Path $resolvedWebRoot "deployment.json") -PathType Leaf
$webDeployment = Get-Content -LiteralPath $webDeploymentFile -Raw | ConvertFrom-Json
if (
    $webDeployment.schema_version -cne "1.0" -or
    $webDeployment.source_commit -cne $sourceState.Commit -or
    [bool]$webDeployment.source_dirty -or
    [int]$webDeployment.asset_count -lt 1
) {
    throw "Restricted frontend must be a clean build of the current platform commit."
}

$dataRootValue = Get-Re3DEnvironmentValue -EnvironmentFile $sourceWorkerEnvironment -Name "RE3D_DATA_ROOT" -Python $python -Required
$apiDataRootValue = Get-Re3DEnvironmentValue -EnvironmentFile $sourceApiEnvironment -Name "RE3D_DATA_ROOT" -Python $python -Required
if ([IO.Path]::GetFullPath($dataRootValue) -ine [IO.Path]::GetFullPath($apiDataRootValue)) {
    throw "Restricted API and Worker must share RE3D_DATA_ROOT."
}
$dataRoot = Resolve-Re3DServicePath -Path $dataRootValue -PathType Container
$re3dRootValue = Get-Re3DEnvironmentValue -EnvironmentFile $sourceWorkerEnvironment -Name "RE3D_ROOT" -Python $python -Required
$re3dRoot = Resolve-Re3DServicePath -Path $re3dRootValue -PathType Container
$pathsConfiguration = Get-Content -LiteralPath (Join-Path $re3dRoot "configs\paths.local.json") -Raw | ConvertFrom-Json

function Resolve-RestrictedWorkerExecutable {
    param([string]$EnvironmentName, [string]$Fallback, [string]$DisplayName)
    $value = Get-Re3DEnvironmentValue -EnvironmentFile $sourceWorkerEnvironment -Name $EnvironmentName -Python $python
    if ([string]::IsNullOrWhiteSpace($value)) { $value = $Fallback }
    if ([string]::IsNullOrWhiteSpace($value)) { throw "$DisplayName is required." }
    $candidate = if ([IO.Path]::IsPathRooted($value)) { $value } else { Join-Path $re3dRoot $value }
    return Resolve-Re3DServicePath -Path $candidate -PathType Leaf
}
function Get-RestrictedExecutionRoot {
    param([string]$Path)
    $parent = Split-Path -Parent $Path
    if ((Split-Path -Leaf $parent) -ieq "Scripts") { return Split-Path -Parent $parent }
    return $parent
}

$driverPython = Resolve-RestrictedWorkerExecutable "RE3D_DRIVER_PYTHON" $pathsConfiguration.mapanything_python "Re3D driver Python"
$mapPython = Resolve-RestrictedWorkerExecutable "RE3D_MAP_PYTHON" $pathsConfiguration.mapanything_python "MapAnything Python"
$mvsPython = Resolve-RestrictedWorkerExecutable "RE3D_MVS_PYTHON" $pathsConfiguration.mvsanywhere_python "MVSAnywhere Python"
$textureMesh = Resolve-RestrictedWorkerExecutable "RE3D_TEXTUREMESH_EXE" $pathsConfiguration.texturemesh_executable "TextureMesh executable"
$executionPaths = [ordered]@{
    driver_python = $driverPython
    mapanything_python = $mapPython
    mvsanywhere_python = $mvsPython
    texturemesh_executable = $textureMesh
}
$executionRoots = @(
    Get-RestrictedExecutionRoot $driverPython
    Get-RestrictedExecutionRoot $mapPython
    Get-RestrictedExecutionRoot $mvsPython
    Split-Path -Parent $textureMesh
) | Where-Object { -not (Test-Re3DServicePathWithin -Candidate $_ -Root $re3dRoot) } | Select-Object -Unique

$runtimeRoot = [IO.Path]::GetFullPath($OutputRoot)
if (Test-Re3DServicePathWithin -Candidate $runtimeRoot -Root $resolvedProjectRoot) {
    throw "Restricted service runtime files must be outside the Git project root."
}
if (Test-Path -LiteralPath $runtimeRoot) {
    if (@(Get-ChildItem -LiteralPath $runtimeRoot -Force).Count -gt 0) {
        throw "OutputRoot must not contain existing files: $runtimeRoot"
    }
} else {
    New-Item -ItemType Directory -Path $runtimeRoot | Out-Null
}

$secretRoot = Join-Path $runtimeRoot "secrets"
$mailpitStateRoot = Join-Path $runtimeRoot "state\mailpit"
$resolvedWorkerTempRoot = [IO.Path]::GetFullPath($(if ($WorkerTempRoot) {
    $WorkerTempRoot
} else {
    Join-Path $dataRoot "_services\restricted-worker-temp"
}))
if (-not (Test-Re3DServicePathWithin -Candidate $resolvedWorkerTempRoot -Root $dataRoot)) {
    throw "WorkerTempRoot must remain inside RE3D_DATA_ROOT."
}
foreach ($directory in @($secretRoot, $mailpitStateRoot, $resolvedWorkerTempRoot)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}
$apiEnvironment = Join-Path $secretRoot ".env.restricted"
$workerEnvironment = Join-Path $secretRoot ".env.worker.restricted"
Copy-Item -LiteralPath $sourceApiEnvironment -Destination $apiEnvironment
Copy-Item -LiteralPath $sourceWorkerEnvironment -Destination $workerEnvironment
Set-Re3DRestrictedBundleSecretAcl -Path $apiEnvironment
Set-Re3DRestrictedBundleSecretAcl -Path $workerEnvironment

$runScripts = @{
    mailpit = Resolve-Re3DServicePath -Path (Join-Path $resolvedProjectRoot "deploy\restricted\run-mailpit.ps1") -PathType Leaf
    api = Resolve-Re3DServicePath -Path (Join-Path $resolvedProjectRoot "deploy\restricted\run-api.ps1") -PathType Leaf
    worker = Resolve-Re3DServicePath -Path (Join-Path $resolvedProjectRoot "deploy\restricted\run-worker.ps1") -PathType Leaf
    caddy = Resolve-Re3DServicePath -Path (Join-Path $resolvedProjectRoot "deploy\restricted\run-caddy.ps1") -PathType Leaf
}
$serviceRecords = @()
foreach ($definition in Get-Re3DRestrictedWindowsServiceDefinitions) {
    $serviceRoot = Join-Path $runtimeRoot "services\$($definition.Key)"
    $logRoot = Join-Path $runtimeRoot "logs\$($definition.Key)"
    New-Item -ItemType Directory -Path $serviceRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
    $wrapper = Join-Path $serviceRoot "$($definition.Id).exe"
    $config = Join-Path $serviceRoot "$($definition.Id).xml"
    Copy-Item -LiteralPath $resolvedWinSW -Destination $wrapper
    $arguments = switch ($definition.Key) {
        "mailpit" { "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$($runScripts.mailpit)`" -ExecutablePath `"$resolvedMailpit`" -StateRoot `"$mailpitStateRoot`"" }
        "api" { "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$($runScripts.api)`" -EnvironmentFile `"$apiEnvironment`" -LimitConcurrency $ApiLimitConcurrency -RequireVirtualService" }
        "worker" { "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$($runScripts.worker)`" -EnvironmentFile `"$workerEnvironment`" -RequireVirtualService" }
        "caddy" { "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$($runScripts.caddy)`" -CaddyPath `"$resolvedCaddy`" -WebRoot `"$resolvedWebRoot`"" }
    }
    $environment = switch ($definition.Key) {
        "api" { @{ PYTHONUTF8 = "1"; PYTHONUNBUFFERED = "1"; PYTHONDONTWRITEBYTECODE = "1" } }
        "worker" { @{ PYTHONUTF8 = "1"; PYTHONUNBUFFERED = "1"; PYTHONDONTWRITEBYTECODE = "1"; TEMP = $resolvedWorkerTempRoot; TMP = $resolvedWorkerTempRoot } }
        default { @{} }
    }
    Write-Re3DRestrictedWindowsServiceXml -Definition $definition -Path $config -PowerShellPath $powerShell -Arguments $arguments -WorkingDirectory $resolvedProjectRoot -LogDirectory $logRoot -Environment $environment
    $serviceRecords += [ordered]@{
        key = $definition.Key
        id = $definition.Id
        account = "NT SERVICE\$($definition.Id)"
        virtual_identity = "NT SERVICE\$($definition.Id)"
        wrapper_path = $wrapper
        config_path = $config
        config_sha256 = (Get-FileHash -LiteralPath $config -Algorithm SHA256).Hash
        log_directory = $logRoot
    }
}

$manifest = [ordered]@{
    schema_version = "1.0"
    created_at_utc = [DateTime]::UtcNow.ToString("o")
    runtime_root = $runtimeRoot
    source = [ordered]@{
        platform_commit = $sourceState.Commit
        platform_dirty = $false
        web_source_commit = $webDeployment.source_commit
        web_deployment_file = $webDeploymentFile
        web_deployment_sha256 = (Get-FileHash -LiteralPath $webDeploymentFile -Algorithm SHA256).Hash
    }
    winsw = [ordered]@{
        version = $script:Re3DWinSWVersion
        source_path = $resolvedWinSW
        source_sha256 = $actualWinSWSha256
    }
    external = [ordered]@{
        project_root = $resolvedProjectRoot
        platform_python = $python
        powershell_path = $powerShell
        web_root = $resolvedWebRoot
        data_root = $dataRoot
        re3d_root = $re3dRoot
        caddy_path = $resolvedCaddy
        mailpit_path = $resolvedMailpit
        mailpit_sha256 = (Get-FileHash -LiteralPath $resolvedMailpit -Algorithm SHA256).Hash
        mailpit_state_root = $mailpitStateRoot
        worker_temp_root = $resolvedWorkerTempRoot
        api_environment_file = $apiEnvironment
        api_environment_sha256 = (Get-FileHash -LiteralPath $apiEnvironment -Algorithm SHA256).Hash
        worker_environment_file = $workerEnvironment
        worker_environment_sha256 = (Get-FileHash -LiteralPath $workerEnvironment -Algorithm SHA256).Hash
        worker_execution_paths = $executionPaths
        worker_execution_roots = @($executionRoots)
    }
    topology = [ordered]@{
        web_origin = "http://127.0.0.1:8080"
        mailpit_origin = "http://127.0.0.1:8025"
        api_port = 8000
        api_limit_concurrency = $ApiLimitConcurrency
    }
    services = $serviceRecords
}
$manifestPath = Join-Path $runtimeRoot "restricted-service-bundle.json"
[IO.File]::WriteAllText($manifestPath, (($manifest | ConvertTo-Json -Depth 8) + [Environment]::NewLine), [Text.UTF8Encoding]::new($false))
Assert-Re3DRestrictedServiceBundle -BundleRoot $runtimeRoot | Out-Null
Write-Output $runtimeRoot
