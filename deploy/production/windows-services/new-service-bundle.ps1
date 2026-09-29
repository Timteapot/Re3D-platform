[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$OutputRoot,
    [Parameter(Mandatory = $true)][string]$WinSWPath,
    [Parameter(Mandatory = $true)][string]$CaddyPath,
    [Parameter(Mandatory = $true)][string]$SiteAddress,
    [string]$ProjectRoot,
    [string]$ApiEnvironmentFile = ".env.production",
    [string]$WorkerEnvironmentFile = ".env.worker.production",
    [string]$WebRoot,
    [string]$BindAddress = "0.0.0.0",
    [ValidateRange(1, 65535)][int]$ApiPort = 8000,
    [ValidateRange(1, 10000)][int]$ApiLimitConcurrency = 100,
    [string]$MaxRequestBody = "27MB"
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")
. (Join-Path $PSScriptRoot "..\common.ps1")

$resolvedProjectRoot = if ($ProjectRoot) {
    Resolve-Re3DServicePath -Path $ProjectRoot -PathType Container
} else {
    Get-Re3DProductionProjectRoot
}
$python = Get-Re3DProductionPython -ProjectRoot $resolvedProjectRoot
$powerShellPath = (Get-Command pwsh.exe -ErrorAction Stop).Source
$resolvedWinSW = Resolve-Re3DServicePath -Path $WinSWPath -PathType Leaf
$resolvedCaddy = Resolve-Re3DServicePath -Path $CaddyPath -PathType Leaf
$resolvedApiEnvironment = Assert-Re3DProductionEnvironmentFile `
    -Path $(if ([IO.Path]::IsPathRooted($ApiEnvironmentFile)) {
        $ApiEnvironmentFile
    } else {
        Join-Path $resolvedProjectRoot $ApiEnvironmentFile
    }) `
    -Python $python
$resolvedWorkerEnvironment = Assert-Re3DProductionEnvironmentFile `
    -Path $(if ([IO.Path]::IsPathRooted($WorkerEnvironmentFile)) {
        $WorkerEnvironmentFile
    } else {
        Join-Path $resolvedProjectRoot $WorkerEnvironmentFile
    }) `
    -Python $python
if ($resolvedApiEnvironment -ceq $resolvedWorkerEnvironment) {
    throw "API and Worker must use separate production environment files."
}
Assert-Re3DWorkerSecretBoundary -EnvironmentFile $resolvedWorkerEnvironment

$resolvedWebRoot = if ($WebRoot) {
    Resolve-Re3DServicePath -Path $WebRoot -PathType Container
} else {
    Resolve-Re3DServicePath `
        -Path (Join-Path $resolvedProjectRoot "apps\web\dist") `
        -PathType Container
}
if (-not (Test-Path -LiteralPath (Join-Path $resolvedWebRoot "index.html") -PathType Leaf)) {
    throw "Production web root does not contain index.html."
}
if ([string]::IsNullOrWhiteSpace($SiteAddress)) {
    throw "SiteAddress is required."
}
$loopbackSitePattern = '^http://(127\.0\.0\.1|\[::1\]|localhost)(:[0-9]{1,5})?$'
$httpsSitePattern = '^(https://)?[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?$'
if ($SiteAddress -notmatch $loopbackSitePattern -and $SiteAddress -notmatch $httpsSitePattern) {
    throw "SiteAddress must be a production HTTPS hostname or a loopback HTTP URL."
}
if ($MaxRequestBody -notmatch '^[1-9][0-9]*(B|KB|MB|GB)$') {
    throw "MaxRequestBody must be a positive size such as 27MB."
}
$parsedBindAddress = $null
if (-not [Net.IPAddress]::TryParse($BindAddress, [ref]$parsedBindAddress)) {
    throw "BindAddress must be an IP address."
}

$runtimeRoot = [IO.Path]::GetFullPath($OutputRoot)
if (Test-Re3DServicePathWithin -Candidate $runtimeRoot -Root $resolvedProjectRoot) {
    throw "Windows service runtime files must be outside the Git project root."
}
if (Test-Path -LiteralPath $runtimeRoot) {
    $existing = @(Get-ChildItem -LiteralPath $runtimeRoot -Force)
    if ($existing.Count -gt 0) {
        throw "OutputRoot must not contain existing files: $runtimeRoot"
    }
} else {
    New-Item -ItemType Directory -Path $runtimeRoot | Out-Null
}

$apiDataRoot = Get-Re3DEnvironmentValue `
    -EnvironmentFile $resolvedApiEnvironment `
    -Name "RE3D_DATA_ROOT" `
    -Python $python `
    -Required
$workerDataRoot = Get-Re3DEnvironmentValue `
    -EnvironmentFile $resolvedWorkerEnvironment `
    -Name "RE3D_DATA_ROOT" `
    -Python $python `
    -Required
if ([IO.Path]::GetFullPath($apiDataRoot) -cne [IO.Path]::GetFullPath($workerDataRoot)) {
    throw "API and Worker must use the same RE3D_DATA_ROOT."
}
$dataRoot = Resolve-Re3DServicePath -Path $workerDataRoot -PathType Container
$re3dRootValue = Get-Re3DEnvironmentValue `
    -EnvironmentFile $resolvedWorkerEnvironment `
    -Name "RE3D_ROOT" `
    -Python $python `
    -Required
$re3dRoot = Resolve-Re3DServicePath -Path $re3dRootValue -PathType Container
$pathsConfigurationPath = Resolve-Re3DServicePath `
    -Path (Join-Path $re3dRoot "configs\paths.local.json") `
    -PathType Leaf
try {
    $pathsConfiguration = Get-Content `
        -LiteralPath $pathsConfigurationPath `
        -Raw | ConvertFrom-Json
} catch {
    throw "Could not read Re3D runtime paths from $pathsConfigurationPath."
}

function Resolve-Re3DWorkerExecutionPath {
    param(
        [Parameter(Mandatory = $true)][string]$Value,
        [Parameter(Mandatory = $true)][string]$Name
    )

    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "$Name is required for the Re3D Worker service."
    }
    $candidate = if ([IO.Path]::IsPathRooted($Value)) {
        $Value
    } else {
        Join-Path $re3dRoot $Value
    }
    return Resolve-Re3DServicePath -Path $candidate -PathType Leaf
}

function Get-Re3DPythonEnvironmentRoot {
    param([Parameter(Mandatory = $true)][string]$PythonPath)

    $parent = Split-Path -Parent $PythonPath
    if ((Split-Path -Leaf $parent) -ieq "Scripts") {
        return Split-Path -Parent $parent
    }
    return $parent
}

$driverPythonValue = Get-Re3DEnvironmentValue `
    -EnvironmentFile $resolvedWorkerEnvironment `
    -Name "RE3D_DRIVER_PYTHON" `
    -Python $python
$mapPythonValue = Get-Re3DEnvironmentValue `
    -EnvironmentFile $resolvedWorkerEnvironment `
    -Name "RE3D_MAP_PYTHON" `
    -Python $python
$mvsPythonValue = Get-Re3DEnvironmentValue `
    -EnvironmentFile $resolvedWorkerEnvironment `
    -Name "RE3D_MVS_PYTHON" `
    -Python $python
$textureMeshValue = Get-Re3DEnvironmentValue `
    -EnvironmentFile $resolvedWorkerEnvironment `
    -Name "RE3D_TEXTUREMESH_EXE" `
    -Python $python
$driverPython = Resolve-Re3DWorkerExecutionPath `
    -Value $(if ($driverPythonValue) {
        $driverPythonValue
    } else {
        $pathsConfiguration.mapanything_python
    }) `
    -Name "Re3D driver Python"
$mapPython = Resolve-Re3DWorkerExecutionPath `
    -Value $(if ($mapPythonValue) {
        $mapPythonValue
    } else {
        $pathsConfiguration.mapanything_python
    }) `
    -Name "MapAnything Python"
$mvsPython = Resolve-Re3DWorkerExecutionPath `
    -Value $(if ($mvsPythonValue) {
        $mvsPythonValue
    } else {
        $pathsConfiguration.mvsanywhere_python
    }) `
    -Name "MVSAnywhere Python"
$textureMesh = Resolve-Re3DWorkerExecutionPath `
    -Value $(if ($textureMeshValue) {
        $textureMeshValue
    } elseif ($pathsConfiguration.texturemesh_executable) {
        $pathsConfiguration.texturemesh_executable
    } else {
        "vendor/openmvs-2.4.0-33d9484-windows/vc18/x64/Release/TextureMesh.exe"
    }) `
    -Name "TextureMesh executable"
$workerExecutionPaths = [ordered]@{
    driver_python = $driverPython
    mapanything_python = $mapPython
    mvsanywhere_python = $mvsPython
    texturemesh_executable = $textureMesh
}
$workerExecutionRoots = @(
    Get-Re3DPythonEnvironmentRoot -PythonPath $driverPython
    Get-Re3DPythonEnvironmentRoot -PythonPath $mapPython
    Get-Re3DPythonEnvironmentRoot -PythonPath $mvsPython
    Split-Path -Parent $textureMesh
) |
    Where-Object {
        -not (Test-Re3DServicePathWithin -Candidate $_ -Root $re3dRoot)
    } |
    Select-Object -Unique

$runApi = Resolve-Re3DServicePath `
    -Path (Join-Path $resolvedProjectRoot "deploy\production\run-api.ps1") `
    -PathType Leaf
$runWorker = Resolve-Re3DServicePath `
    -Path (Join-Path $resolvedProjectRoot "deploy\production\run-worker.ps1") `
    -PathType Leaf
$runCaddy = Resolve-Re3DServicePath `
    -Path (Join-Path $resolvedProjectRoot "deploy\production\run-caddy.ps1") `
    -PathType Leaf

$serviceRecords = @()
foreach ($definition in Get-Re3DWindowsServiceDefinitions) {
    $serviceRoot = Join-Path $runtimeRoot $definition.Key
    $logRoot = Join-Path $runtimeRoot "logs\$($definition.Key)"
    New-Item -ItemType Directory -Path $serviceRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
    $wrapperPath = Join-Path $serviceRoot "$($definition.Id).exe"
    $configurationPath = Join-Path $serviceRoot "$($definition.Id).xml"
    Copy-Item -LiteralPath $resolvedWinSW -Destination $wrapperPath

    $arguments = switch ($definition.Key) {
        "api" {
            '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass ' +
            "-File `"$runApi`" -EnvironmentFile `"$resolvedApiEnvironment`" " +
            "-Port $ApiPort -LimitConcurrency $ApiLimitConcurrency"
        }
        "worker" {
            '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass ' +
            "-File `"$runWorker`" -EnvironmentFile `"$resolvedWorkerEnvironment`""
        }
        "caddy" {
            '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass ' +
            "-File `"$runCaddy`" -CaddyPath `"$resolvedCaddy`" " +
            "-SiteAddress `"$SiteAddress`" -BindAddress `"$($parsedBindAddress.ToString())`" " +
            "-ApiUpstream `"127.0.0.1:$ApiPort`" " +
            "-MaxRequestBody `"$MaxRequestBody`" -WebRoot `"$resolvedWebRoot`""
        }
    }
    $environment = switch ($definition.Key) {
        "api" { @{ PYTHONUTF8 = "1"; PYTHONUNBUFFERED = "1" } }
        "worker" { @{ PYTHONUTF8 = "1"; PYTHONUNBUFFERED = "1" } }
        "caddy" {
            $caddyStateRoot = Join-Path $runtimeRoot "caddy-state"
            $caddyDataRoot = Join-Path $caddyStateRoot "data"
            $caddyConfigRoot = Join-Path $caddyStateRoot "config"
            New-Item -ItemType Directory -Path $caddyDataRoot -Force | Out-Null
            New-Item -ItemType Directory -Path $caddyConfigRoot -Force | Out-Null
            @{
                XDG_DATA_HOME = $caddyDataRoot
                XDG_CONFIG_HOME = $caddyConfigRoot
            }
        }
    }
    Write-Re3DWindowsServiceXml `
        -Definition $definition `
        -Path $configurationPath `
        -PowerShellPath $powerShellPath `
        -Arguments $arguments `
        -WorkingDirectory $resolvedProjectRoot `
        -LogDirectory $logRoot `
        -Environment $environment
    $serviceRecords += [ordered]@{
        key = $definition.Key
        id = $definition.Id
        account = "$($definition.AccountDomain)\$($definition.AccountUser)"
        virtual_identity = "NT SERVICE\$($definition.Id)"
        wrapper_path = $wrapperPath
        config_path = $configurationPath
        log_directory = $logRoot
    }
}

$manifest = [ordered]@{
    schema_version = "1.1"
    created_at_utc = [DateTime]::UtcNow.ToString("o")
    runtime_root = $runtimeRoot
    project_root = $resolvedProjectRoot
    winsw = [ordered]@{
        version = $script:Re3DWinSWVersion
        source_path = $resolvedWinSW
        source_sha256 = (Get-FileHash -LiteralPath $resolvedWinSW -Algorithm SHA256).Hash
    }
    external = [ordered]@{
        powershell_path = $powerShellPath
        caddy_path = $resolvedCaddy
        web_root = $resolvedWebRoot
        api_environment_file = $resolvedApiEnvironment
        worker_environment_file = $resolvedWorkerEnvironment
        data_root = $dataRoot
        re3d_root = $re3dRoot
        worker_execution_paths = $workerExecutionPaths
        worker_execution_roots = @($workerExecutionRoots)
    }
    topology = [ordered]@{
        site_address = $SiteAddress
        bind_address = $parsedBindAddress.ToString()
        api_port = $ApiPort
        api_limit_concurrency = $ApiLimitConcurrency
        max_request_body = $MaxRequestBody
    }
    services = $serviceRecords
}
$manifestPath = Join-Path $runtimeRoot "service-bundle.json"
[IO.File]::WriteAllText(
    $manifestPath,
    (($manifest | ConvertTo-Json -Depth 8) + [Environment]::NewLine),
    [Text.UTF8Encoding]::new($false)
)

Assert-Re3DServiceBundle -BundleRoot $runtimeRoot | Out-Null
Write-Host "Windows service bundle created: $runtimeRoot" -ForegroundColor Green
Write-Output $runtimeRoot
