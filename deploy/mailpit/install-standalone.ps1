param(
    [string]$InstallRoot = "D:\3Dreconstruction\tools\mailpit\v1.31.3",
    [string]$ArchivePath = "D:\3Dreconstruction\tools\mailpit\mailpit-windows-amd64-v1.31.3.zip"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$version = "1.31.3"
$downloadUrl = "https://github.com/axllent/mailpit/releases/download/v$version/mailpit-windows-amd64.zip"
$archiveSha256 = "863e9502d4e0f14a78c0f91c5091797b1c7b7b7e3fc7e5eab62e5770ce44b76e"
$executableSha256 = "ee0b025bc9f61e6856d6032128408ee6fe1627f510c118a4fa0faa7bfdb7cd33"
$executablePath = Join-Path $InstallRoot "mailpit.exe"

function Assert-FileHash {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$ExpectedSha256
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required file was not found: $Path"
    }
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
    if ($actual -ne $ExpectedSha256) {
        throw "SHA-256 mismatch for $Path. Expected $ExpectedSha256 but found $actual."
    }
}

if (Test-Path -LiteralPath $executablePath -PathType Leaf) {
    Assert-FileHash -Path $executablePath -ExpectedSha256 $executableSha256
    $reportedVersion = & $executablePath version --no-release-check
    if ($LASTEXITCODE -ne 0 -or $reportedVersion -notmatch "v$([regex]::Escape($version))") {
        throw "Installed Mailpit executable did not report the expected version v$version."
    }
    Write-Host "Mailpit v$version is already installed and its SHA-256 is valid."
    Write-Host "Executable: $executablePath"
    exit 0
}

$archiveParent = Split-Path -Parent $ArchivePath
New-Item -ItemType Directory -Path $archiveParent -Force | Out-Null
if (-not (Test-Path -LiteralPath $ArchivePath -PathType Leaf)) {
    Write-Host "Downloading Mailpit v$version from the official GitHub release..."
    Invoke-WebRequest -Uri $downloadUrl -OutFile $ArchivePath -UseBasicParsing
}

Assert-FileHash -Path $ArchivePath -ExpectedSha256 $archiveSha256
if (Test-Path -LiteralPath $InstallRoot) {
    throw "Install root already exists but does not contain the expected executable: $InstallRoot"
}

New-Item -ItemType Directory -Path $InstallRoot | Out-Null
try {
    Expand-Archive -LiteralPath $ArchivePath -DestinationPath $InstallRoot
    Assert-FileHash -Path $executablePath -ExpectedSha256 $executableSha256
    $reportedVersion = & $executablePath version --no-release-check
    if ($LASTEXITCODE -ne 0 -or $reportedVersion -notmatch "v$([regex]::Escape($version))") {
        throw "Installed Mailpit executable did not report the expected version v$version."
    }
} catch {
    throw "Mailpit extraction or validation failed under $InstallRoot. Remove that incomplete directory after inspection, then retry. $($_.Exception.Message)"
}

Write-Host "Mailpit v$version was installed and verified."
Write-Host "Executable: $executablePath"

