param(
    [string]$ExecutablePath = "D:\3Dreconstruction\tools\mailpit\v1.31.3\mailpit.exe",
    [string]$StateRoot = "D:\3Dreconstruction\Re3D-data\_services\mailpit"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$pidPath = Join-Path $StateRoot "mailpit.pid"

function Get-CanonicalPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
}

if (-not (Test-Path -LiteralPath $pidPath -PathType Leaf)) {
    Write-Host "Mailpit standalone is not running (PID file is absent)."
    exit 0
}

$savedPidText = (Get-Content -LiteralPath $pidPath -Raw).Trim()
$savedPid = 0
if (-not [int]::TryParse($savedPidText, [ref]$savedPid) -or $savedPid -le 0) {
    throw "Mailpit PID file is invalid: $pidPath"
}

$process = Get-Process -Id $savedPid -ErrorAction SilentlyContinue
if ($null -eq $process) {
    Remove-Item -LiteralPath $pidPath
    Write-Host "Removed a stale Mailpit PID file. No process was stopped."
    exit 0
}

try {
    $processPath = $process.MainModule.FileName
} catch {
    throw "Could not verify executable path for process $savedPid. Refusing to stop it."
}

if ((Get-CanonicalPath -Path $processPath) -ne (Get-CanonicalPath -Path $ExecutablePath)) {
    throw "PID $savedPid belongs to another executable. Refusing to stop it."
}

Stop-Process -Id $savedPid
Wait-Process -Id $savedPid -Timeout 10 -ErrorAction SilentlyContinue
if (Get-Process -Id $savedPid -ErrorAction SilentlyContinue) {
    throw "Mailpit process $savedPid did not stop within 10 seconds."
}
Remove-Item -LiteralPath $pidPath

Write-Host "Mailpit standalone stopped. Captured messages remain under $StateRoot"

