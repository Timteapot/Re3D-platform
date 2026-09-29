[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")
. (Join-Path $PSScriptRoot "..\common.ps1")

function Assert-Re3DRejected {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Action,
        [Parameter(Mandatory = $true)][string]$ExpectedMessage
    )

    try {
        & $Action
    } catch {
        if ($_.Exception.Message -notmatch $ExpectedMessage) {
            throw
        }
        return
    }
    throw "Expected service bundle generation to reject: $ExpectedMessage"
}

$projectRoot = Get-Re3DProductionProjectRoot
$sourceState = Get-Re3DGitSourceState -ProjectRoot $projectRoot
$temporaryRoot = [IO.Path]::GetFullPath((Join-Path (
    [IO.Path]::GetTempPath()
) "re3d-service-bundle-$([Guid]::NewGuid().ToString('N'))"))
$fixtureRoot = Join-Path $temporaryRoot "fixtures"
$bundleRoot = Join-Path $temporaryRoot "bundle"
$dataRoot = Join-Path $fixtureRoot "data"
$re3dRoot = Join-Path $fixtureRoot "Re3D"
$re3dConfigRoot = Join-Path $re3dRoot "configs"
$webRoot = Join-Path $fixtureRoot "web"
$webAssetRoot = Join-Path $webRoot "assets"
$driverEnvironment = Join-Path $fixtureRoot "driver-environment"
$mapEnvironment = Join-Path $fixtureRoot "map-environment"
$mvsEnvironment = Join-Path $fixtureRoot "mvs-environment"
$textureRoot = Join-Path $fixtureRoot "texture-tool"
$apiEnvironment = Join-Path $fixtureRoot "api.env"
$workerEnvironment = Join-Path $fixtureRoot "worker.env"
$unsafeWorkerEnvironment = Join-Path $fixtureRoot "unsafe-worker.env"
$fakeWinSW = Join-Path $fixtureRoot "WinSW-x64.exe"
$fakeCaddy = Join-Path $fixtureRoot "caddy.exe"
$markerSecret = "acceptance-secret-must-not-enter-service-xml"

try {
    foreach ($directory in @(
        $fixtureRoot,
        $dataRoot,
        $re3dConfigRoot,
        $webRoot,
        $webAssetRoot,
        $driverEnvironment,
        $mapEnvironment,
        $mvsEnvironment,
        $textureRoot
    )) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
    $driverPython = Join-Path $driverEnvironment "python.exe"
    $mapPython = Join-Path $mapEnvironment "python.exe"
    $mvsPython = Join-Path $mvsEnvironment "python.exe"
    $textureMesh = Join-Path $textureRoot "TextureMesh.exe"
    foreach ($executable in @(
        $driverPython,
        $mapPython,
        $mvsPython,
        $textureMesh
    )) {
        [IO.File]::WriteAllBytes($executable, [byte[]](1, 2, 3))
    }
    $pathsConfiguration = [ordered]@{
        mapanything_python = $mapPython.Replace("\", "/")
        mvsanywhere_python = $mvsPython.Replace("\", "/")
        texturemesh_executable = $textureMesh.Replace("\", "/")
    }
    [IO.File]::WriteAllText(
        (Join-Path $re3dConfigRoot "paths.local.json"),
        (($pathsConfiguration | ConvertTo-Json) + [Environment]::NewLine),
        [Text.UTF8Encoding]::new($false)
    )
    [IO.File]::WriteAllText(
        (Join-Path $webRoot "index.html"),
        '<div id="root"></div>',
        [Text.UTF8Encoding]::new($false)
    )
    [IO.File]::WriteAllText(
        (Join-Path $webAssetRoot "fixture.js"),
        'export {};',
        [Text.UTF8Encoding]::new($false)
    )
    $deploymentMetadata = [ordered]@{
        schema_version = "1.0"
        source_commit = $sourceState.Commit
        source_dirty = $false
        built_at_utc = [DateTime]::UtcNow.ToString("o")
        asset_count = 1
    }
    [IO.File]::WriteAllText(
        (Join-Path $webRoot "deployment.json"),
        (($deploymentMetadata | ConvertTo-Json) + [Environment]::NewLine),
        [Text.UTF8Encoding]::new($false)
    )
    $normalizedDataRoot = $dataRoot.Replace("\", "/")
    $normalizedRe3DRoot = $re3dRoot.Replace("\", "/")
    $normalizedDriverPython = $driverPython.Replace("\", "/")
    $apiLines = @(
        "APP_ENV=production",
        "RE3D_DATA_ROOT=$normalizedDataRoot",
        "JWT_SECRET=$markerSecret"
    ) -join [Environment]::NewLine
    $workerLines = @(
        "APP_ENV=production",
        "RE3D_DATA_ROOT=$normalizedDataRoot",
        "RE3D_ROOT=$normalizedRe3DRoot",
        "RE3D_DRIVER_PYTHON=$normalizedDriverPython"
    ) -join [Environment]::NewLine
    [IO.File]::WriteAllText(
        $apiEnvironment,
        "$apiLines$([Environment]::NewLine)",
        [Text.UTF8Encoding]::new($false)
    )
    [IO.File]::WriteAllText(
        $workerEnvironment,
        "$workerLines$([Environment]::NewLine)",
        [Text.UTF8Encoding]::new($false)
    )
    [IO.File]::WriteAllText(
        $unsafeWorkerEnvironment,
        "$workerLines$([Environment]::NewLine)JWT_SECRET=$markerSecret$([Environment]::NewLine)",
        [Text.UTF8Encoding]::new($false)
    )
    Copy-Item -LiteralPath (Get-Process -Id $PID).Path -Destination $fakeWinSW
    Copy-Item -LiteralPath (Get-Process -Id $PID).Path -Destination $fakeCaddy

    & (Join-Path $PSScriptRoot "new-service-bundle.ps1") `
        -OutputRoot $bundleRoot `
        -WinSWPath $fakeWinSW `
        -CaddyPath $fakeCaddy `
        -SiteAddress "https://acceptance.example" `
        -ProjectRoot $projectRoot `
        -ApiEnvironmentFile $apiEnvironment `
        -WorkerEnvironmentFile $workerEnvironment `
        -WebRoot $webRoot | Out-Null

    $manifest = Assert-Re3DServiceBundle -BundleRoot $bundleRoot
    if (
        $manifest.source.platform_commit -cne $sourceState.Commit -or
        $manifest.source.web_source_commit -cne $sourceState.Commit -or
        [bool]$manifest.source.platform_dirty -ne $sourceState.Dirty
    ) {
        throw "Service bundle did not preserve the platform and web source identity."
    }
    $bundleText = Get-ChildItem -LiteralPath $bundleRoot -Filter *.xml -Recurse |
        Get-Content -Raw
    if (($bundleText -join [Environment]::NewLine).Contains($markerSecret)) {
        throw "A production secret was copied into a WinSW configuration."
    }
    $apiConfig = [xml](Get-Content -LiteralPath (
        $manifest.services |
            Where-Object { $_.key -ceq "api" } |
            Select-Object -ExpandProperty config_path
    ) -Raw)
    $workerConfig = [xml](Get-Content -LiteralPath (
        $manifest.services |
            Where-Object { $_.key -ceq "worker" } |
            Select-Object -ExpandProperty config_path
    ) -Raw)
    $caddyConfig = [xml](Get-Content -LiteralPath (
        $manifest.services |
            Where-Object { $_.key -ceq "caddy" } |
            Select-Object -ExpandProperty config_path
    ) -Raw)
    if (
        $apiConfig.service.serviceaccount.user -cne "Re3DPlatformApi" -or
        $workerConfig.service.serviceaccount.user -cne "Re3DPlatformWorker" -or
        $caddyConfig.service.serviceaccount.user -cne "Re3DPlatformCaddy" -or
        $apiConfig.service.serviceaccount.domain -cne "NT SERVICE" -or
        $workerConfig.service.serviceaccount.domain -cne "NT SERVICE" -or
        $caddyConfig.service.serviceaccount.domain -cne "NT SERVICE"
    ) {
        throw "The generated service accounts do not match the isolation model."
    }
    if (
        @($caddyConfig.service.env | Where-Object { $_.name -eq "XDG_DATA_HOME" }).Count -ne 1 -or
        @($caddyConfig.service.env | Where-Object { $_.name -eq "XDG_CONFIG_HOME" }).Count -ne 1
    ) {
        throw "Caddy does not have deterministic service state directories."
    }
    $recordedExecutionPaths = @(
        $manifest.external.worker_execution_paths.PSObject.Properties |
            ForEach-Object {
            [IO.Path]::GetFullPath($_.Value)
        }
    )
    foreach ($expectedExecutionPath in @(
        $driverPython,
        $mapPython,
        $mvsPython,
        $textureMesh
    )) {
        if ($recordedExecutionPaths -cnotcontains [IO.Path]::GetFullPath($expectedExecutionPath)) {
            throw "Worker execution path was not recorded: $expectedExecutionPath"
        }
    }
    $recordedExecutionRoots = @(
        $manifest.external.worker_execution_roots | ForEach-Object {
            [IO.Path]::GetFullPath($_)
        }
    )
    foreach ($expectedExecutionRoot in @(
        $driverEnvironment,
        $mapEnvironment,
        $mvsEnvironment,
        $textureRoot
    )) {
        if ($recordedExecutionRoots -cnotcontains [IO.Path]::GetFullPath($expectedExecutionRoot)) {
            throw "Worker execution root was not recorded: $expectedExecutionRoot"
        }
    }

    $deploymentPath = Join-Path $webRoot "deployment.json"
    $originalDeploymentText = Get-Content -LiteralPath $deploymentPath -Raw
    try {
        [IO.File]::WriteAllText(
            $deploymentPath,
            "$originalDeploymentText ",
            [Text.UTF8Encoding]::new($false)
        )
        Assert-Re3DRejected `
            -ExpectedMessage "deployment metadata hash does not match" `
            -Action {
                Assert-Re3DServiceBundle -BundleRoot $bundleRoot | Out-Null
            }
    } finally {
        [IO.File]::WriteAllText(
            $deploymentPath,
            $originalDeploymentText,
            [Text.UTF8Encoding]::new($false)
        )
    }
    $manifestPath = Join-Path $bundleRoot "service-bundle.json"
    $originalManifestText = Get-Content -LiteralPath $manifestPath -Raw
    try {
        $dirtyManifest = $originalManifestText | ConvertFrom-Json
        $dirtyManifest.source.platform_dirty = $true
        [IO.File]::WriteAllText(
            $manifestPath,
            (($dirtyManifest | ConvertTo-Json -Depth 8) + [Environment]::NewLine),
            [Text.UTF8Encoding]::new($false)
        )
        Assert-Re3DRejected `
            -ExpectedMessage "generated from a dirty platform worktree" `
            -Action {
                $rejectedManifest = Get-Re3DServiceBundleManifest `
                    -BundleRoot $bundleRoot
                Assert-Re3DServiceSourceState `
                    -Manifest $rejectedManifest | Out-Null
            }
    } finally {
        [IO.File]::WriteAllText(
            $manifestPath,
            $originalManifestText,
            [Text.UTF8Encoding]::new($false)
        )
    }
    Assert-Re3DServiceBundle -BundleRoot $bundleRoot | Out-Null

    Assert-Re3DRejected `
        -ExpectedMessage "Worker environment contains web-only secret keys" `
        -Action {
            & (Join-Path $PSScriptRoot "new-service-bundle.ps1") `
                -OutputRoot (Join-Path $temporaryRoot "unsafe-secret") `
                -WinSWPath $fakeWinSW `
                -CaddyPath $fakeCaddy `
                -SiteAddress "https://acceptance.example" `
                -ProjectRoot $projectRoot `
                -ApiEnvironmentFile $apiEnvironment `
                -WorkerEnvironmentFile $unsafeWorkerEnvironment `
                -WebRoot $webRoot | Out-Null
        }
    Assert-Re3DRejected `
        -ExpectedMessage "must be outside the Git project root" `
        -Action {
            & (Join-Path $PSScriptRoot "new-service-bundle.ps1") `
                -OutputRoot (Join-Path $projectRoot ".service-bundle-rejected") `
                -WinSWPath $fakeWinSW `
                -CaddyPath $fakeCaddy `
                -SiteAddress "https://acceptance.example" `
                -ProjectRoot $projectRoot `
                -ApiEnvironmentFile $apiEnvironment `
                -WorkerEnvironmentFile $workerEnvironment `
                -WebRoot $webRoot | Out-Null
        }
    Assert-Re3DRejected `
        -ExpectedMessage "production HTTPS hostname or a loopback HTTP URL" `
        -Action {
            & (Join-Path $PSScriptRoot "new-service-bundle.ps1") `
                -OutputRoot (Join-Path $temporaryRoot "unsafe-http") `
                -WinSWPath $fakeWinSW `
                -CaddyPath $fakeCaddy `
                -SiteAddress "http://0.0.0.0:8080" `
                -ProjectRoot $projectRoot `
                -ApiEnvironmentFile $apiEnvironment `
                -WorkerEnvironmentFile $workerEnvironment `
                -WebRoot $webRoot | Out-Null
        }

    Write-Host "Windows service bundle acceptance completed successfully." -ForegroundColor Green
} finally {
    $systemTemporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    if (
        (Test-Path -LiteralPath $temporaryRoot) -and
        $temporaryRoot.StartsWith(
            $systemTemporaryRoot,
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
}
