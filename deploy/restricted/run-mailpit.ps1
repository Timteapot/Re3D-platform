[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ExecutablePath,
    [Parameter(Mandatory = $true)][string]$StateRoot
)

$ErrorActionPreference = "Stop"
$expectedSha256 = "ee0b025bc9f61e6856d6032128408ee6fe1627f510c118a4fa0faa7bfdb7cd33"
$resolvedExecutable = (Resolve-Path -LiteralPath $ExecutablePath -ErrorAction Stop).Path
$resolvedStateRoot = [IO.Path]::GetFullPath($StateRoot)
if (-not (Test-Path -LiteralPath $resolvedStateRoot -PathType Container)) {
    throw "Mailpit service state root does not exist: $resolvedStateRoot"
}
$actualSha256 = (Get-FileHash -LiteralPath $resolvedExecutable -Algorithm SHA256).Hash
if ($actualSha256 -ine $expectedSha256) {
    throw "Mailpit executable SHA-256 mismatch."
}

$databasePath = Join-Path $resolvedStateRoot "mailpit.db"
& $resolvedExecutable `
    "--smtp=127.0.0.1:1025" `
    "--listen=127.0.0.1:8025" `
    "--allowed-hosts=127.0.0.1,localhost" `
    "--database=$databasePath" `
    "--max=500" `
    "--disable-version-check"
exit $LASTEXITCODE
