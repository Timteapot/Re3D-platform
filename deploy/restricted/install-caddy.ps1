param(
    [string]$InstallRoot = "D:\3Dreconstruction\tools\caddy\v2.11.4",
    [string]$ArchivePath = "D:\3Dreconstruction\tools\caddy\caddy_2.11.4_windows_amd64.zip"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$version = "2.11.4"
$downloadUrl = "https://github.com/caddyserver/caddy/releases/download/v$version/caddy_2.11.4_windows_amd64.zip"
$archiveSha512 = "cd5ccfd86a4b40732cf715890d0dca5bf3f63adefec5a7914de85adf240c60ce7e5d2791631b88ef9758e46b23bb1730e020b9c5d696889740b284ffd4788e35"
$executableSha256 = "5cb9ab71e5756ce72840b8234177a2f40c8b4ab47a806b8e841e2b784e9df62b"
$executablePath = Join-Path $InstallRoot "caddy.exe"

function Assert-FileHash {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Algorithm,
        [Parameter(Mandatory = $true)][string]$Expected
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required file was not found: $Path"
    }
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm $Algorithm).Hash
    if ($actual -ne $Expected) {
        throw "$Algorithm mismatch for $Path. Expected $Expected but found $actual."
    }
}

if (Test-Path -LiteralPath $executablePath -PathType Leaf) {
    Assert-FileHash -Path $executablePath -Algorithm SHA256 -Expected $executableSha256
    $reportedVersion = & $executablePath version
    if ($LASTEXITCODE -ne 0 -or $reportedVersion -notmatch "^v$([regex]::Escape($version))(?:\s|$)") {
        throw "Installed Caddy executable did not report the expected version v$version."
    }
    Write-Host "Caddy v$version is already installed and its SHA-256 is valid."
    Write-Host "Executable: $executablePath"
    exit 0
}

$archiveParent = Split-Path -Parent $ArchivePath
New-Item -ItemType Directory -Path $archiveParent -Force | Out-Null
if (-not (Test-Path -LiteralPath $ArchivePath -PathType Leaf)) {
    Write-Host "Downloading Caddy v$version from the official GitHub release..."
    Invoke-WebRequest -Uri $downloadUrl -OutFile $ArchivePath -UseBasicParsing
}
Assert-FileHash -Path $ArchivePath -Algorithm SHA512 -Expected $archiveSha512

if (Test-Path -LiteralPath $InstallRoot) {
    throw "Install root already exists but does not contain the expected executable: $InstallRoot"
}
New-Item -ItemType Directory -Path $InstallRoot | Out-Null
try {
    Expand-Archive -LiteralPath $ArchivePath -DestinationPath $InstallRoot
    Assert-FileHash -Path $executablePath -Algorithm SHA256 -Expected $executableSha256
    $reportedVersion = & $executablePath version
    if ($LASTEXITCODE -ne 0 -or $reportedVersion -notmatch "^v$([regex]::Escape($version))(?:\s|$)") {
        throw "Installed Caddy executable did not report the expected version v$version."
    }
} catch {
    throw "Caddy extraction or validation failed under $InstallRoot. Remove that incomplete directory after inspection, then retry. $($_.Exception.Message)"
}

Write-Host "Caddy v$version was installed and verified."
Write-Host "Executable: $executablePath"

