param(
    [string]$ExecutablePath = "D:\3Dreconstruction\tools\mailpit\v1.31.3\mailpit.exe",
    [string]$StateRoot = "D:\3Dreconstruction\Re3D-data\_services\mailpit",
    [switch]$StayAttached
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$expectedSha256 = "ee0b025bc9f61e6856d6032128408ee6fe1627f510c118a4fa0faa7bfdb7cd33"
$smtpAddress = "127.0.0.1:1025"
$uiAddress = "127.0.0.1:8025"
$uiUrl = "http://$uiAddress"
$pidPath = Join-Path $StateRoot "mailpit.pid"
$databasePath = Join-Path $StateRoot "mailpit.db"
$stdoutPath = Join-Path $StateRoot "mailpit.stdout.log"
$stderrPath = Join-Path $StateRoot "mailpit.stderr.log"

function Get-CanonicalPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
}

function Get-ProcessExecutablePath {
    param([Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process)
    try {
        return $Process.MainModule.FileName
    } catch {
        throw "Could not verify executable path for process $($Process.Id). Refusing to reuse or stop it."
    }
}

if (-not (Test-Path -LiteralPath $ExecutablePath -PathType Leaf)) {
    throw "Mailpit executable was not found at $ExecutablePath. Run install-standalone.ps1 first."
}
$actualSha256 = (Get-FileHash -LiteralPath $ExecutablePath -Algorithm SHA256).Hash
if ($actualSha256 -ne $expectedSha256) {
    throw "Mailpit executable SHA-256 mismatch. Expected $expectedSha256 but found $actualSha256."
}

New-Item -ItemType Directory -Path $StateRoot -Force | Out-Null
$canonicalExecutable = Get-CanonicalPath -Path $ExecutablePath

if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
    $savedPidText = (Get-Content -LiteralPath $pidPath -Raw).Trim()
    $savedPid = 0
    if (-not [int]::TryParse($savedPidText, [ref]$savedPid) -or $savedPid -le 0) {
        throw "Mailpit PID file is invalid: $pidPath"
    }
    $existingProcess = Get-Process -Id $savedPid -ErrorAction SilentlyContinue
    if ($null -ne $existingProcess) {
        $existingPath = Get-CanonicalPath -Path (Get-ProcessExecutablePath -Process $existingProcess)
        if ($existingPath -ne $canonicalExecutable) {
            throw "PID $savedPid belongs to another executable. Refusing to alter it."
        }
        try {
            $response = Invoke-WebRequest -Uri $uiUrl -Method Get -TimeoutSec 2 -UseBasicParsing
            if ($response.StatusCode -eq 200) {
                Write-Host "Mailpit is already running."
                Write-Host "SMTP: $smtpAddress"
                Write-Host "Web UI: $uiUrl"
                exit 0
            }
        } catch {
            throw "Mailpit process $savedPid exists but its loopback UI is not ready. Inspect $stderrPath before stopping it."
        }
    }
    Remove-Item -LiteralPath $pidPath
}

$arguments = @(
    "--smtp=$smtpAddress",
    "--listen=$uiAddress",
    "--allowed-hosts=127.0.0.1,localhost",
    "--database=$databasePath",
    "--max=500",
    "--disable-version-check"
)

$process = Start-Process `
    -FilePath $ExecutablePath `
    -ArgumentList $arguments `
    -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath `
    -WindowStyle Hidden `
    -PassThru
Set-Content -LiteralPath $pidPath -Value $process.Id -Encoding ASCII

$ready = $false
for ($attempt = 1; $attempt -le 30; $attempt++) {
    if ($process.HasExited) {
        break
    }
    try {
        $response = Invoke-WebRequest -Uri $uiUrl -Method Get -TimeoutSec 2 -UseBasicParsing
        if ($response.StatusCode -eq 200) {
            $ready = $true
            break
        }
    } catch {
        Start-Sleep -Milliseconds 500
    }
}

if (-not $ready) {
    if (-not $process.HasExited) {
        Stop-Process -Id $process.Id -Force
    }
    if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
        Remove-Item -LiteralPath $pidPath
    }
    throw "Mailpit did not become ready at $uiUrl. Inspect $stderrPath"
}

Write-Host "Mailpit standalone is ready."
Write-Host "SMTP: $smtpAddress"
Write-Host "Web UI: $uiUrl"
Write-Host "State: $StateRoot"

if ($StayAttached) {
    Write-Host "Mailpit will remain attached to this PowerShell process. Stop it with Ctrl+C or stop-standalone.ps1."
    try {
        Wait-Process -Id $process.Id
    } finally {
        $remainingProcess = Get-Process -Id $process.Id -ErrorAction SilentlyContinue
        if ($null -ne $remainingProcess) {
            Stop-Process -Id $process.Id -Force
        }
        if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
            $currentPidText = (Get-Content -LiteralPath $pidPath -Raw).Trim()
            if ($currentPidText -eq $process.Id.ToString()) {
                Remove-Item -LiteralPath $pidPath
            }
        }
    }
}
