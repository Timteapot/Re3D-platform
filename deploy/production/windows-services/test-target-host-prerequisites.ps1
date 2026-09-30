[CmdletBinding()]
param(
    [string]$ProjectRoot = "",
    [string]$DataRoot = "",
    [string]$Re3DRoot = "",
    [string]$WinSWPath = "",
    [string]$CaddyPath = "",
    [string]$PostgreSQLBin = "",
    [string]$NvidiaSmiPath = "",
    [string]$SiteAddress = "",
    [string]$DatabaseHost = "127.0.0.1",
    [ValidateRange(1, 65535)][int]$DatabasePort = 5432,
    [ValidateRange(0, [long]::MaxValue)]
    [long]$MinimumSystemFreeBytes = 21474836480,
    [ValidateRange(0, [long]::MaxValue)]
    [long]$MinimumDataFreeBytes = 53687091200,
    [string]$ReportPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$checks = [Collections.Generic.List[object]]::new()

function Add-Re3DHostCheck {
    param(
        [Parameter(Mandatory = $true)][string]$Id,
        [Parameter(Mandatory = $true)]
        [ValidateSet("pass", "warning", "blocker")]
        [string]$Status,
        [Parameter(Mandatory = $true)][string]$Summary,
        [object]$Details = $null
    )

    $entry = [ordered]@{
        id = $Id
        status = $Status
        summary = $Summary
    }
    if ($null -ne $Details) {
        $entry.details = $Details
    }
    $checks.Add([pscustomobject]$entry)
}

function Get-Re3DCommandPath {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [string]$PreferredDirectory = ""
    )

    if (-not [string]::IsNullOrWhiteSpace($PreferredDirectory)) {
        $candidate = Join-Path $PreferredDirectory $Name
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
        return $null
    }

    $command = Get-Command $Name -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) {
        return $command.Source
    }
    return $null
}

function Invoke-Re3DVersionCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Id,
        [Parameter(Mandatory = $true)][string]$Label,
        [string]$Path,
        [string[]]$Arguments = @("--version"),
        [string]$RequiredPattern = ""
    )

    if ([string]::IsNullOrWhiteSpace($Path) -or -not (
        Test-Path -LiteralPath $Path -PathType Leaf
    )) {
        Add-Re3DHostCheck `
            -Id $Id `
            -Status blocker `
            -Summary "$Label executable was not found."
        return
    }

    try {
        $output = (& $Path @Arguments 2>&1 | Out-String).Trim()
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0) {
            Add-Re3DHostCheck `
                -Id $Id `
                -Status blocker `
                -Summary "$Label version command failed." `
                -Details @{ exit_code = $exitCode }
            return
        }
        if (
            -not [string]::IsNullOrWhiteSpace($RequiredPattern) -and
            $output -notmatch $RequiredPattern
        ) {
            Add-Re3DHostCheck `
                -Id $Id `
                -Status blocker `
                -Summary "$Label does not match the required version." `
                -Details @{ path = $Path; version = $output }
            return
        }
        Add-Re3DHostCheck `
            -Id $Id `
            -Status pass `
            -Summary "$Label is available." `
            -Details @{ path = $Path; version = $output }
    } catch {
        Add-Re3DHostCheck `
            -Id $Id `
            -Status blocker `
            -Summary "$Label could not be executed." `
            -Details @{ error = $_.Exception.Message }
    }
}

function Test-Re3DTcpEndpoint {
    param(
        [Parameter(Mandatory = $true)][string]$HostName,
        [Parameter(Mandatory = $true)][int]$Port,
        [int]$TimeoutMilliseconds = 3000
    )

    $client = [Net.Sockets.TcpClient]::new()
    try {
        $operation = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $operation.AsyncWaitHandle.WaitOne($TimeoutMilliseconds)) {
            return $false
        }
        $client.EndConnect($operation)
        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Test-Re3DPendingRestart {
    $paths = @(
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending",
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired"
    )
    if ($paths | Where-Object { Test-Path -LiteralPath $_ }) {
        return $true
    }
    try {
        $sessionManager = Get-ItemProperty `
            -LiteralPath "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager" `
            -Name PendingFileRenameOperations `
            -ErrorAction Stop
        return $null -ne $sessionManager.PendingFileRenameOperations
    } catch {
        return $false
    }
}

function Test-Re3DFreeSpace {
    param(
        [Parameter(Mandatory = $true)][string]$Id,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][long]$MinimumFreeBytes
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        Add-Re3DHostCheck `
            -Id $Id `
            -Status blocker `
            -Summary "$Label directory does not exist." `
            -Details @{ path = $Path }
        return
    }

    try {
        $resolved = (Resolve-Path -LiteralPath $Path).Path
        $root = [IO.Path]::GetPathRoot($resolved)
        $drive = [IO.DriveInfo]::new($root)
        $details = [ordered]@{
            path = $resolved
            volume = $root
            free_bytes = $drive.AvailableFreeSpace
            minimum_free_bytes = $MinimumFreeBytes
        }
        if ($drive.AvailableFreeSpace -lt $MinimumFreeBytes) {
            Add-Re3DHostCheck `
                -Id $Id `
                -Status blocker `
                -Summary "$Label volume does not meet the free-space floor." `
                -Details $details
        } else {
            Add-Re3DHostCheck `
                -Id $Id `
                -Status pass `
                -Summary "$Label volume meets the free-space floor." `
                -Details $details
        }
    } catch {
        Add-Re3DHostCheck `
            -Id $Id `
            -Status blocker `
            -Summary "$Label free space could not be inspected." `
            -Details @{ path = $Path; error = $_.Exception.Message }
    }
}

$resolvedProjectRoot = if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
} else {
    [IO.Path]::GetFullPath($ProjectRoot)
}

$runningOnWindows = [Runtime.InteropServices.RuntimeInformation]::IsOSPlatform(
    [Runtime.InteropServices.OSPlatform]::Windows
)
if (-not $runningOnWindows) {
    Add-Re3DHostCheck `
        -Id "operating-system" `
        -Status blocker `
        -Summary "The Windows service deployment requires Windows."
} else {
    try {
        $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
        $computer = Get-CimInstance Win32_ComputerSystem -ErrorAction Stop
        $serverSku = [int]$os.ProductType -ne 1
        Add-Re3DHostCheck `
            -Id "operating-system" `
            -Status $(if ($serverSku) { "pass" } else { "warning" }) `
            -Summary $(if ($serverSku) {
                "Windows Server was detected."
            } else {
                "A Windows client edition was detected; use Windows Server for the public host."
            }) `
            -Details @{
                caption = "$($os.Caption)"
                version = "$($os.Version)"
                product_type = [int]$os.ProductType
                logical_processors = [int]$computer.NumberOfLogicalProcessors
                total_memory_bytes = [long]$computer.TotalPhysicalMemory
            }
    } catch {
        Add-Re3DHostCheck `
            -Id "operating-system" `
            -Status blocker `
            -Summary "Windows host inventory could not be read." `
            -Details @{ error = $_.Exception.Message }
    }
}

if ([Environment]::Is64BitOperatingSystem -and [Environment]::Is64BitProcess) {
    Add-Re3DHostCheck `
        -Id "process-architecture" `
        -Status pass `
        -Summary "The operating system and PowerShell process are 64-bit."
} else {
    Add-Re3DHostCheck `
        -Id "process-architecture" `
        -Status blocker `
        -Summary "The deployment must run from a 64-bit PowerShell process on 64-bit Windows."
}

try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    $isAdministrator = $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
    Add-Re3DHostCheck `
        -Id "administrator" `
        -Status $(if ($isAdministrator) { "pass" } else { "blocker" }) `
        -Summary $(if ($isAdministrator) {
            "The current PowerShell process is elevated."
        } else {
            "Run the host preflight from an elevated PowerShell process."
        })
} catch {
    Add-Re3DHostCheck `
        -Id "administrator" `
        -Status blocker `
        -Summary "Administrator membership could not be determined."
}

$pwshPath = Get-Re3DCommandPath -Name "pwsh.exe"
Invoke-Re3DVersionCommand `
    -Id "powershell" `
    -Label "PowerShell 7" `
    -Path $pwshPath `
    -Arguments @("-NoLogo", "-NoProfile", "-Command", '$PSVersionTable.PSVersion.ToString()') `
    -RequiredPattern '^7\.'

Invoke-Re3DVersionCommand `
    -Id "git" `
    -Label "Git" `
    -Path (Get-Re3DCommandPath -Name "git.exe")
Invoke-Re3DVersionCommand `
    -Id "node" `
    -Label "Node.js" `
    -Path (Get-Re3DCommandPath -Name "node.exe") `
    -RequiredPattern '^v(?:2[3-9]|22\.(?:1[2-9]|[2-9][0-9]))(?:\.|$)'
Invoke-Re3DVersionCommand `
    -Id "npm" `
    -Label "npm" `
    -Path (Get-Re3DCommandPath -Name "npm.cmd")

$venvPython = Join-Path $resolvedProjectRoot ".venv\Scripts\python.exe"
Invoke-Re3DVersionCommand `
    -Id "project-python" `
    -Label "Project virtual-environment Python" `
    -Path $venvPython `
    -RequiredPattern '^Python 3\.12(?:\.|$)'

foreach ($postgresTool in @("psql.exe", "pg_dump.exe", "pg_restore.exe")) {
    Invoke-Re3DVersionCommand `
        -Id "postgresql-$($postgresTool.Replace('.exe', ''))" `
        -Label $postgresTool.Replace(".exe", "") `
        -Path (Get-Re3DCommandPath `
            -Name $postgresTool `
            -PreferredDirectory $PostgreSQLBin) `
        -RequiredPattern 'PostgreSQL\) 18\.'
}

Invoke-Re3DVersionCommand `
    -Id "winsw" `
    -Label "WinSW" `
    -Path $WinSWPath `
    -Arguments @("version") `
    -RequiredPattern '(?<![0-9])2\.12\.0(?:\.0)?(?![0-9])'
Invoke-Re3DVersionCommand `
    -Id "caddy" `
    -Label "Caddy" `
    -Path $CaddyPath `
    -Arguments @("version") `
    -RequiredPattern '(?<![0-9])2\.11\.4(?![0-9])'

$resolvedNvidiaSmi = if (-not [string]::IsNullOrWhiteSpace($NvidiaSmiPath)) {
    $NvidiaSmiPath
} else {
    Get-Re3DCommandPath -Name "nvidia-smi.exe"
}
if ([string]::IsNullOrWhiteSpace($resolvedNvidiaSmi) -or -not (
    Test-Path -LiteralPath $resolvedNvidiaSmi -PathType Leaf
)) {
    Add-Re3DHostCheck `
        -Id "nvidia-gpu" `
        -Status blocker `
        -Summary "nvidia-smi was not found."
} else {
    try {
        $gpuRows = @(& $resolvedNvidiaSmi `
            --query-gpu=index,name,driver_version,memory.total `
            --format=csv,noheader,nounits 2>&1)
        if ($LASTEXITCODE -ne 0 -or $gpuRows.Count -eq 0) {
            Add-Re3DHostCheck `
                -Id "nvidia-gpu" `
                -Status blocker `
                -Summary "NVIDIA GPU inventory failed."
        } else {
            Add-Re3DHostCheck `
                -Id "nvidia-gpu" `
                -Status pass `
                -Summary "NVIDIA GPU and driver are visible." `
                -Details @{ executable = $resolvedNvidiaSmi; devices = @($gpuRows) }
        }
    } catch {
        Add-Re3DHostCheck `
            -Id "nvidia-gpu" `
            -Status blocker `
            -Summary "NVIDIA GPU inventory could not be executed." `
            -Details @{ error = $_.Exception.Message }
    }
}

$systemRoot = [IO.Path]::GetPathRoot([Environment]::SystemDirectory)
Test-Re3DFreeSpace `
    -Id "system-volume" `
    -Label "System" `
    -Path $systemRoot `
    -MinimumFreeBytes $MinimumSystemFreeBytes

if ([string]::IsNullOrWhiteSpace($DataRoot)) {
    Add-Re3DHostCheck `
        -Id "data-volume" `
        -Status blocker `
        -Summary "DataRoot must identify the external production data directory."
} else {
    Test-Re3DFreeSpace `
        -Id "data-volume" `
        -Label "Re3D data" `
        -Path $DataRoot `
        -MinimumFreeBytes $MinimumDataFreeBytes
    if (-not (Test-Path -LiteralPath $DataRoot -PathType Container)) {
        Add-Re3DHostCheck `
            -Id "data-volume-isolation" `
            -Status blocker `
            -Summary "Data volume isolation cannot be verified until DataRoot exists."
    } elseif (
        [IO.Path]::GetPathRoot((Resolve-Path -LiteralPath $DataRoot).Path) -ceq $systemRoot
    ) {
        Add-Re3DHostCheck `
            -Id "data-volume-isolation" `
            -Status warning `
            -Summary "The Re3D data directory shares the Windows system volume."
    } else {
        Add-Re3DHostCheck `
            -Id "data-volume-isolation" `
            -Status pass `
            -Summary "The Re3D data directory is outside the Windows system volume."
    }
}

if ([string]::IsNullOrWhiteSpace($Re3DRoot) -or -not (
    Test-Path -LiteralPath $Re3DRoot -PathType Container
)) {
    Add-Re3DHostCheck `
        -Id "re3d-baseline" `
        -Status blocker `
        -Summary "Re3DRoot does not identify an installed Re3D repository."
} else {
    try {
        $baseline = Get-Content `
            -LiteralPath (Join-Path $resolvedProjectRoot "config\pipeline-baseline.json") `
            -Raw | ConvertFrom-Json
        $gitPath = Get-Re3DCommandPath -Name "git.exe"
        $resolvedRe3DRoot = (Resolve-Path -LiteralPath $Re3DRoot).Path
        $actualCommit = (& $gitPath `
            -c "safe.directory=$resolvedRe3DRoot" `
            -C $resolvedRe3DRoot rev-parse HEAD 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or $actualCommit -cne "$($baseline.commit)") {
            Add-Re3DHostCheck `
                -Id "re3d-baseline" `
                -Status blocker `
                -Summary "The installed Re3D commit does not match the frozen baseline." `
                -Details @{
                    root = $resolvedRe3DRoot
                    expected_commit = "$($baseline.commit)"
                    actual_commit = $actualCommit
                }
        } else {
            Add-Re3DHostCheck `
                -Id "re3d-baseline" `
                -Status pass `
                -Summary "The installed Re3D commit matches the frozen baseline." `
                -Details @{
                    root = $resolvedRe3DRoot
                    tag = "$($baseline.tag)"
                    commit = $actualCommit
                }
        }
    } catch {
        Add-Re3DHostCheck `
            -Id "re3d-baseline" `
            -Status blocker `
            -Summary "The Re3D baseline identity could not be verified." `
            -Details @{ error = $_.Exception.Message }
    }
}

$serviceConflicts = @(
    @(
        "Re3DPlatformApi",
        "Re3DPlatformWorker",
        "Re3DPlatformCaddy"
    ) | Where-Object { Get-Service -Name $_ -ErrorAction SilentlyContinue }
)
if ($serviceConflicts.Count -gt 0) {
    Add-Re3DHostCheck `
        -Id "service-name-availability" `
        -Status blocker `
        -Summary "One or more Re3D Platform service names already exist." `
        -Details @{ services = @($serviceConflicts) }
} else {
    Add-Re3DHostCheck `
        -Id "service-name-availability" `
        -Status pass `
        -Summary "The Re3D Platform service names are available."
}

try {
    $occupiedPorts = @()
    foreach ($port in @(80, 443, 8000)) {
        $listeners = @(Get-NetTCPConnection `
            -State Listen `
            -LocalPort $port `
            -ErrorAction SilentlyContinue)
        foreach ($listener in $listeners) {
            $occupiedPorts += [ordered]@{
                port = $port
                address = "$($listener.LocalAddress)"
                process_id = [int]$listener.OwningProcess
            }
        }
    }
    if ($occupiedPorts.Count -gt 0) {
        Add-Re3DHostCheck `
            -Id "deployment-ports" `
            -Status blocker `
            -Summary "One or more required fresh-install ports are already listening." `
            -Details @{ listeners = $occupiedPorts }
    } else {
        Add-Re3DHostCheck `
            -Id "deployment-ports" `
            -Status pass `
            -Summary "Ports 80, 443, and 8000 are available for a fresh installation."
    }
} catch {
    Add-Re3DHostCheck `
        -Id "deployment-ports" `
        -Status blocker `
        -Summary "Required TCP port availability could not be inspected." `
        -Details @{ error = $_.Exception.Message }
}

if (Test-Re3DTcpEndpoint -HostName $DatabaseHost -Port $DatabasePort) {
    Add-Re3DHostCheck `
        -Id "database-endpoint" `
        -Status pass `
        -Summary "The PostgreSQL TCP endpoint is reachable." `
        -Details @{ host = $DatabaseHost; port = $DatabasePort }
} else {
    Add-Re3DHostCheck `
        -Id "database-endpoint" `
        -Status blocker `
        -Summary "The PostgreSQL TCP endpoint is not reachable." `
        -Details @{ host = $DatabaseHost; port = $DatabasePort }
}

if ([string]::IsNullOrWhiteSpace($SiteAddress)) {
    Add-Re3DHostCheck `
        -Id "public-dns" `
        -Status blocker `
        -Summary "SiteAddress must contain the final HTTPS public address."
} else {
    try {
        $siteUri = [Uri]$SiteAddress
        if (
            -not $siteUri.IsAbsoluteUri -or
            $siteUri.Scheme -cne "https" -or
            [string]::IsNullOrWhiteSpace($siteUri.DnsSafeHost)
        ) {
            throw "SiteAddress must be an absolute HTTPS URL."
        }
        $dnsAddresses = @(
            Resolve-DnsName `
                -Name $siteUri.DnsSafeHost `
                -Type A_AAAA `
                -DnsOnly `
                -ErrorAction Stop |
                Where-Object IPAddress |
                Select-Object -ExpandProperty IPAddress -Unique
        )
        if ($dnsAddresses.Count -eq 0) {
            throw "No A or AAAA record was returned."
        }
        Add-Re3DHostCheck `
            -Id "public-dns" `
            -Status pass `
            -Summary "The public host name resolves in DNS." `
            -Details @{ host = $siteUri.DnsSafeHost; addresses = $dnsAddresses }
    } catch {
        Add-Re3DHostCheck `
            -Id "public-dns" `
            -Status blocker `
            -Summary "The final HTTPS public address is not ready in DNS." `
            -Details @{ address = $SiteAddress; error = $_.Exception.Message }
    }
}

if (Test-Re3DPendingRestart) {
    Add-Re3DHostCheck `
        -Id "pending-restart" `
        -Status warning `
        -Summary "Windows reports a pending restart; restart before service installation."
} else {
    Add-Re3DHostCheck `
        -Id "pending-restart" `
        -Status pass `
        -Summary "No common pending-restart marker was found."
}

$blockerCount = @($checks | Where-Object status -ceq "blocker").Count
$warningCount = @($checks | Where-Object status -ceq "warning").Count
$sourceCommit = "unknown"
try {
    $gitPath = Get-Re3DCommandPath -Name "git.exe"
    if ($gitPath -and (Test-Path -LiteralPath $resolvedProjectRoot -PathType Container)) {
        $sourceCommit = (& $gitPath `
            -c "safe.directory=$resolvedProjectRoot" `
            -C $resolvedProjectRoot rev-parse HEAD 2>$null | Out-String).Trim()
    }
} catch {
    $sourceCommit = "unknown"
}

$report = [ordered]@{
    schema_version = "1.0"
    generated_at_utc = [DateTime]::UtcNow.ToString("o")
    status = if ($blockerCount -gt 0) {
        "blocked"
    } elseif ($warningCount -gt 0) {
        "ready_with_warnings"
    } else {
        "ready"
    }
    purpose = "fresh_windows_single_host_installation"
    source_commit = $sourceCommit
    blocker_count = $blockerCount
    warning_count = $warningCount
    checks = @($checks)
}
$json = $report | ConvertTo-Json -Depth 8

if (-not [string]::IsNullOrWhiteSpace($ReportPath)) {
    $resolvedReportPath = [IO.Path]::GetFullPath($ReportPath)
    $reportDirectory = Split-Path -Parent $resolvedReportPath
    if (-not (Test-Path -LiteralPath $reportDirectory -PathType Container)) {
        throw "Report directory does not exist: $reportDirectory"
    }
    [IO.File]::WriteAllText(
        $resolvedReportPath,
        "$json$([Environment]::NewLine)",
        [Text.UTF8Encoding]::new($false)
    )
}

Write-Output $json
if ($blockerCount -gt 0) {
    exit 2
}
