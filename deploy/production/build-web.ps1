[CmdletBinding()]
param(
    [switch]$SkipInstall,
    [switch]$AllowDirty
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$webRoot = Join-Path $projectRoot "apps\web"
$distRoot = Join-Path $webRoot "dist"
$npm = (Get-Command npm.cmd -ErrorAction Stop).Source
$git = (Get-Command git.exe -ErrorAction Stop).Source

$gitRepositoryArguments = @("-c", "safe.directory=$projectRoot", "-C", $projectRoot)
$dirty = @(& $git @gitRepositoryArguments status --porcelain --untracked-files=all)
if ($LASTEXITCODE -ne 0) {
    throw "Could not inspect the source revision before the web build."
}
if ($dirty.Count -gt 0 -and -not $AllowDirty) {
    throw "Production web builds require a clean Git worktree."
}
$commit = (& $git @gitRepositoryArguments rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $commit -notmatch "^[0-9a-f]{40}$") {
    throw "Could not resolve the source commit for the web build."
}

Push-Location $webRoot
try {
    if (-not $SkipInstall) {
        Write-Host "Installing locked frontend dependencies..." -ForegroundColor Cyan
        & $npm ci
        if ($LASTEXITCODE -ne 0) {
            throw "npm ci failed with exit code $LASTEXITCODE."
        }
    }
    Write-Host "Building the production frontend..." -ForegroundColor Cyan
    & $npm run build
    if ($LASTEXITCODE -ne 0) {
        throw "Frontend production build failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}

$indexPath = Join-Path $distRoot "index.html"
$assetRoot = Join-Path $distRoot "assets"
if (
    -not (Test-Path -LiteralPath $indexPath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $assetRoot -PathType Container)
) {
    throw "Vite did not create the expected production output."
}
$assets = @(Get-ChildItem -LiteralPath $assetRoot -File)
if ($assets.Count -eq 0) {
    throw "The production frontend contains no versioned assets."
}

$metadata = [ordered]@{
    schema_version = "1.0"
    source_commit = $commit
    source_dirty = $dirty.Count -gt 0
    built_at_utc = [DateTime]::UtcNow.ToString("o")
    asset_count = $assets.Count
}
$metadataPath = Join-Path $distRoot "deployment.json"
[IO.File]::WriteAllText(
    $metadataPath,
    (($metadata | ConvertTo-Json) + [Environment]::NewLine),
    [Text.UTF8Encoding]::new($false)
)

Write-Host "Frontend production output is ready: $distRoot" -ForegroundColor Green
Write-Output $distRoot
