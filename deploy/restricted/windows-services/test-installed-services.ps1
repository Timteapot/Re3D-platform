[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BundleRoot,
    [ValidateRange(1, 300)][int]$TimeoutSeconds = 30,
    [string]$ReportPath
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")
if (-not (Test-Re3DAdministrator)) { throw "Restricted service acceptance requires elevation." }
$manifest = Assert-Re3DRestrictedServiceBundle -BundleRoot $BundleRoot -CheckExternalBinaryVersions
Assert-Re3DRestrictedServiceSourceState -Manifest $manifest | Out-Null

function Get-AclRightsForSid {
    param([string]$Path, [Security.Principal.SecurityIdentifier]$Sid)
    $rights = [Security.AccessControl.FileSystemRights]0
    foreach ($rule in (Get-Acl -LiteralPath $Path).Access) {
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { continue }
        $ruleSid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier])
        if ($ruleSid.Value -ceq $Sid.Value) { $rights = $rights -bor $rule.FileSystemRights }
    }
    return $rights
}
function Assert-AclRights {
    param([string]$Path, [Security.Principal.SecurityIdentifier]$Sid, [Security.AccessControl.FileSystemRights]$Rights)
    $actual = Get-AclRightsForSid -Path $Path -Sid $Sid
    if (($actual -band $Rights) -ne $Rights) { throw "Service SID is missing $Rights on $Path." }
}
function Assert-RestrictedSecretAcl {
    param([string]$Path, [Security.Principal.SecurityIdentifier]$ServiceSid)
    $acl = Get-Acl -LiteralPath $Path
    $adminSid = "S-1-5-32-544"
    $systemSid = "S-1-5-18"
    if (-not $acl.AreAccessRulesProtected -or $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -cne $adminSid) {
        throw "Restricted service secret ownership or inheritance is unsafe: $Path"
    }
    $allowed = @($adminSid, $systemSid, $ServiceSid.Value)
    foreach ($rule in $acl.Access) {
        $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or $allowed -cnotcontains $sid) {
            throw "Restricted service secret contains an unexpected ACL entry: $sid"
        }
    }
    foreach ($sidText in @($adminSid, $systemSid)) {
        Assert-AclRights -Path $Path -Sid ([Security.Principal.SecurityIdentifier]::new($sidText)) -Rights ([Security.AccessControl.FileSystemRights]::FullControl)
    }
    $serviceRights = Get-AclRightsForSid -Path $Path -Sid $ServiceSid
    $writeRights = (
        [Security.AccessControl.FileSystemRights]::WriteData -bor
        [Security.AccessControl.FileSystemRights]::AppendData -bor
        [Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor
        [Security.AccessControl.FileSystemRights]::WriteAttributes -bor
        [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
        [Security.AccessControl.FileSystemRights]::Delete -bor
        [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [Security.AccessControl.FileSystemRights]::TakeOwnership
    )
    if (($serviceRights -band [Security.AccessControl.FileSystemRights]::Read) -ne [Security.AccessControl.FileSystemRights]::Read -or ($serviceRights -band $writeRights) -ne 0) {
        throw "Restricted service secret permissions are unsafe for $($ServiceSid.Value)."
    }
}
function Wait-RestrictedRequest {
    param([string]$Uri)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        try {
            $response = Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -eq 200) { return $response }
        } catch { Start-Sleep -Milliseconds 500 }
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Restricted endpoint did not become ready: $Uri"
}
function Get-Re3DDescendantProcesses {
    param([Parameter(Mandatory = $true)][int]$RootProcessId)
    $snapshot = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    $frontier = @($RootProcessId)
    $visited = [Collections.Generic.HashSet[int]]::new()
    [void]$visited.Add($RootProcessId)
    $descendants = @()
    while ($frontier.Count -gt 0) {
        $children = @(
            $snapshot | Where-Object {
                $frontier -contains [int]$_.ParentProcessId -and
                -not $visited.Contains([int]$_.ProcessId)
            }
        )
        if ($children.Count -eq 0) { break }
        $descendants += $children
        foreach ($child in $children) { [void]$visited.Add([int]$child.ProcessId) }
        $frontier = @($children | ForEach-Object { [int]$_.ProcessId })
    }
    return $descendants
}
function ConvertTo-Re3DUtcDateTime {
    param($Value)
    if ($Value -is [DateTime]) { return ([DateTime]$Value).ToUniversalTime() }
    if ($Value -is [DateTimeOffset]) { return ([DateTimeOffset]$Value).UtcDateTime }
    try {
        return [Management.ManagementDateTimeConverter]::ToDateTime([string]$Value).ToUniversalTime()
    } catch {
        return [DateTimeOffset]::Parse(
            [string]$Value,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind
        ).UtcDateTime
    }
}
function ConvertTo-Re3DUtcEvidenceTime {
    param([Parameter(Mandatory = $true)][string]$Value)
    $parsed = [DateTimeOffset]::Parse(
        $Value,
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::RoundtripKind
    )
    if ($parsed.Offset -ne [TimeSpan]::Zero) {
        throw "Worker runtime evidence timestamp is not UTC."
    }
    return $parsed.UtcDateTime
}
function Get-Re3DWorkerRuntimeRecords {
    param([Parameter(Mandatory = $true)][string]$LogDirectory)
    $records = [Collections.Generic.List[object]]::new()
    foreach ($file in @(Get-ChildItem -LiteralPath $LogDirectory -File -ErrorAction Stop | Sort-Object LastWriteTimeUtc, Name)) {
        foreach ($line in @(Get-Content -LiteralPath $file.FullName -Tail 1000 -ErrorAction Stop)) {
            if ([string]::IsNullOrWhiteSpace($line)) { continue }
            try {
                $record = $line | ConvertFrom-Json -ErrorAction Stop
                if ($null -ne $record.operation) { $records.Add($record) }
            } catch {
                # WinSW diagnostic lines are expected alongside JSON child output.
            }
        }
    }
    return $records
}
function Wait-Re3DListenerOwnership {
    param(
        [Parameter(Mandatory = $true)]$Service,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string[]]$ExpectedExecutables,
        [string]$CommandArgumentsPattern = ""
    )
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $instance = Get-CimInstance Win32_Service -Filter "Name = '$($Service.id)'" -ErrorAction SilentlyContinue
        if ($instance -and $instance.State -ceq "Running" -and [int]$instance.ProcessId -gt 0) {
            $descendants = @(Get-Re3DDescendantProcesses -RootProcessId ([int]$instance.ProcessId))
            $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
            if ($listeners.Count -gt 0) {
                $valid = $true
                $owners = [Collections.Generic.List[int]]::new()
                foreach ($listener in $listeners) {
                    $owner = @(
                        $descendants | Where-Object {
                            [int]$_.ProcessId -eq [int]$listener.OwningProcess -and
                            (Test-Re3DProcessImageAndCommand `
                                -Process $_ `
                                -AllowedExecutablePaths $ExpectedExecutables `
                                -CommandArgumentsPattern $CommandArgumentsPattern)
                        }
                    )
                    if (
                        $listener.LocalAddress -notin @("127.0.0.1", "::1") -or
                        $owner.Count -ne 1
                    ) {
                        $valid = $false
                        break
                    }
                    $owners.Add([int]$listener.OwningProcess)
                }
                if ($valid) {
                    return [ordered]@{
                        service = $Service.id
                        port = $Port
                        owning_process_ids = @($owners | Select-Object -Unique)
                    }
                }
            }
        }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Restricted port $Port is not owned by the expected $($Service.id) child process."
}
function Wait-Re3DWorkerRuntimeEvidence {
    param([Parameter(Mandatory = $true)]$WorkerService)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $workerPythonImages = @(
        $manifest.external.platform_python
        $manifest.external.platform_python_runtime_image
    ) | Sort-Object -Unique
    $commandArgumentsPattern = (
        '-m\s+apps\.worker\.main\s+run-real-queued-loop\s*$'
    )
    do {
        $serviceInstance = Get-CimInstance Win32_Service -Filter "Name = '$($WorkerService.id)'" -ErrorAction SilentlyContinue
        if ($serviceInstance -and $serviceInstance.State -ceq "Running" -and [int]$serviceInstance.ProcessId -gt 0) {
            $candidates = @(
                Get-Re3DDescendantProcesses -RootProcessId ([int]$serviceInstance.ProcessId) |
                    Where-Object {
                        Test-Re3DProcessImageAndCommand `
                            -Process $_ `
                            -AllowedExecutablePaths $workerPythonImages `
                            -CommandArgumentsPattern $commandArgumentsPattern
                    }
            )
            $records = @(Get-Re3DWorkerRuntimeRecords -LogDirectory $WorkerService.log_directory)
            $evidenceMatches = [Collections.Generic.List[object]]::new()
            foreach ($candidate in $candidates) {
                $processId = [int]$candidate.ProcessId
                $processStartedAtUtc = ConvertTo-Re3DUtcDateTime -Value $candidate.CreationDate
                $freshAfterUtc = $processStartedAtUtc
                $preflight = $null
                $loop = $null
                foreach ($record in $records) {
                    if ([int]$record.process_id -ne $processId -or [string]::IsNullOrWhiteSpace("$($record.emitted_at_utc)")) { continue }
                    try { $emittedAtUtc = ConvertTo-Re3DUtcEvidenceTime -Value $record.emitted_at_utc } catch { continue }
                    if ($emittedAtUtc -lt $freshAfterUtc -or $emittedAtUtc -gt [DateTime]::UtcNow.AddSeconds(5)) { continue }
                    if ($record.operation -ceq "worker-runtime-preflight" -and $record.status -ceq "ready") {
                        $preflight = $emittedAtUtc
                    } elseif ($record.operation -ceq "real-worker-loop" -and $record.status -ceq "started") {
                        $loop = $emittedAtUtc
                    }
                }
                if ($preflight -and $loop -and $preflight -le $loop) {
                    Start-Sleep -Seconds 2
                    $currentService = Get-CimInstance Win32_Service -Filter "Name = '$($WorkerService.id)'" -ErrorAction SilentlyContinue
                    if (-not $currentService -or $currentService.State -cne "Running" -or [int]$currentService.ProcessId -le 0) { continue }
                    $stillRunning = @(
                        Get-Re3DDescendantProcesses -RootProcessId ([int]$currentService.ProcessId) |
                            Where-Object {
                                [int]$_.ProcessId -eq $processId -and
                                (Test-Re3DProcessImageAndCommand `
                                    -Process $_ `
                                    -AllowedExecutablePaths $workerPythonImages `
                                    -CommandArgumentsPattern $commandArgumentsPattern) -and
                                (ConvertTo-Re3DUtcDateTime -Value $_.CreationDate) -eq $processStartedAtUtc
                            }
                    )
                    if ($stillRunning.Count -gt 0) {
                        $evidenceMatches.Add([pscustomobject][ordered]@{
                            process_id = $processId
                            process_started_at_utc = $processStartedAtUtc.ToString("o")
                            preflight_evidence_at_utc = $preflight.ToString("o")
                            queue_loop_evidence_at_utc = $loop.ToString("o")
                        })
                    }
                }
            }
            $evidenceMatch = Resolve-Re3DWorkerEvidenceMatch `
                -CandidateProcessIds @($candidates | ForEach-Object { [int]$_.ProcessId }) `
                -EvidenceMatches $evidenceMatches
            if ($null -ne $evidenceMatch) {
                return $evidenceMatch
            }
        }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Restricted Worker did not provide fresh preflight and stable queue-loop evidence from its current process."
}

$serviceSids = @{}
$serviceResults = @()
foreach ($service in @($manifest.services)) {
    $instance = Get-CimInstance Win32_Service -Filter "Name = '$($service.id)'" -ErrorAction SilentlyContinue
    if (-not $instance) { throw "Restricted Windows service is not installed: $($service.id)" }
    $expectedAccount = "NT SERVICE\$($service.id)"
    $actualPath = "$($instance.PathName)".Trim().Trim('"')
    if (
        $instance.State -cne "Running" -or
        $instance.StartMode -cne "Manual" -or
        $instance.StartName -ine $expectedAccount -or
        [int]$instance.ProcessId -le 0 -or
        [IO.Path]::GetFullPath($actualPath) -ine [IO.Path]::GetFullPath($service.wrapper_path)
    ) { throw "Restricted service state does not match its bundle: $($service.id)" }
    $registry = Get-ItemProperty -LiteralPath "HKLM:\SYSTEM\CurrentControlSet\Services\$($service.id)"
    if ([int]$registry.ServiceSidType -ne 1 -or $null -eq $registry.FailureActions) {
        throw "Restricted service lifecycle policy is incomplete: $($service.id)"
    }
    $serviceSids[$service.key] = Get-Re3DIdentitySid -Identity $service.virtual_identity
    $serviceResults += [ordered]@{ id = $service.id; state = $instance.State; start_mode = $instance.StartMode; account = $instance.StartName; process_id = [int]$instance.ProcessId }
}

Assert-RestrictedSecretAcl -Path $manifest.external.api_environment_file -ServiceSid $serviceSids.api
Assert-RestrictedSecretAcl -Path $manifest.external.worker_environment_file -ServiceSid $serviceSids.worker
Assert-AclRights -Path $manifest.external.data_root -Sid $serviceSids.api -Rights ([Security.AccessControl.FileSystemRights]::Modify)
Assert-AclRights -Path $manifest.external.data_root -Sid $serviceSids.worker -Rights ([Security.AccessControl.FileSystemRights]::Modify)
Assert-AclRights -Path $manifest.external.re3d_root -Sid $serviceSids.worker -Rights ([Security.AccessControl.FileSystemRights]::ReadAndExecute)
Assert-AclRights -Path $manifest.external.worker_temp_root -Sid $serviceSids.worker -Rights ([Security.AccessControl.FileSystemRights]::Modify)
Assert-AclRights -Path $manifest.external.web_root -Sid $serviceSids.caddy -Rights ([Security.AccessControl.FileSystemRights]::ReadAndExecute)
Assert-AclRights -Path $manifest.external.mailpit_state_root -Sid $serviceSids.mailpit -Rights ([Security.AccessControl.FileSystemRights]::Modify)

$servicesByKey = @{}
foreach ($service in @($manifest.services)) { $servicesByKey[$service.key] = $service }
$pythonProcessImages = @(
    $manifest.external.platform_python
    $manifest.external.platform_python_runtime_image
) | Sort-Object -Unique
$apiCommandArgumentsPattern = (
    '-m\s+uvicorn\s+apps\.api\.main:create_app' +
    '\s+--factory\s+--host\s+127\.0\.0\.1\s+--port\s+8000' +
    '\s+--workers\s+1\s+--proxy-headers' +
    '\s+--forwarded-allow-ips\s+(?:"127\.0\.0\.1,::1"|127\.0\.0\.1,::1)' +
    '\s+--limit-concurrency\s+' + [Regex]::Escape("$($manifest.topology.api_limit_concurrency)") +
    '\s+--timeout-graceful-shutdown\s+30' +
    '\s+--no-server-header\s+--no-access-log\s*$'
)
$listenerResults = [ordered]@{
    smtp = Wait-Re3DListenerOwnership -Service $servicesByKey.mailpit -Port 1025 -ExpectedExecutables $manifest.external.mailpit_path
    mailpit_ui = Wait-Re3DListenerOwnership -Service $servicesByKey.mailpit -Port 8025 -ExpectedExecutables $manifest.external.mailpit_path
    api = Wait-Re3DListenerOwnership -Service $servicesByKey.api -Port 8000 -ExpectedExecutables $pythonProcessImages -CommandArgumentsPattern $apiCommandArgumentsPattern
    web = Wait-Re3DListenerOwnership -Service $servicesByKey.caddy -Port 8080 -ExpectedExecutables $manifest.external.caddy_path
}

$readiness = Join-Path $manifest.external.project_root "deploy\restricted\check-restricted-readiness.ps1"
$apiReadinessRaw = (& $readiness `
    -Component api `
    -EnvironmentFile $manifest.external.api_environment_file `
    -VirtualService Re3DRestrictedApi | Out-String)
$apiReadiness = $apiReadinessRaw | ConvertFrom-Json
if (
    $apiReadiness.status -cne "ready" -or
    $apiReadiness.component -cne "api" -or
    $apiReadiness.smtp.connectivity_checked -ne $true
) {
    throw "Installed restricted API readiness did not verify SMTP connectivity."
}

$api = Wait-RestrictedRequest -Uri "http://127.0.0.1:8000/health/live"
$apiPayload = $api.Content | ConvertFrom-Json
if ($apiPayload.status -cne "ok" -or $apiPayload.environment -cne "restricted" -or [bool]$apiPayload.development_routes_enabled) {
    throw "Restricted API health boundary is invalid."
}
$home = Wait-RestrictedRequest -Uri "http://127.0.0.1:8080/"
if ($home.Content -notmatch '<div id="root"></div>') { throw "Restricted Caddy did not serve the React entry point." }
$proxy = Wait-RestrictedRequest -Uri "http://127.0.0.1:8080/health/live"
if (($proxy.Content | ConvertFrom-Json).environment -cne "restricted") { throw "Restricted Caddy did not proxy the API." }
if (-not [string]::IsNullOrWhiteSpace((@($home.Headers["Strict-Transport-Security"]) -join ", "))) {
    throw "Restricted loopback HTTP must not emit HSTS."
}
$mailpit = Wait-RestrictedRequest -Uri "http://127.0.0.1:8025/"
if ($mailpit.StatusCode -ne 200) { throw "Restricted Mailpit UI is unavailable." }

$worker = @($manifest.services) | Where-Object { $_.key -ceq "worker" } | Select-Object -First 1
$workerRuntime = Wait-Re3DWorkerRuntimeEvidence -WorkerService $worker

$report = [ordered]@{
    status = "accepted"
    checked_at_utc = [DateTime]::UtcNow.ToString("o")
    platform_commit = $manifest.source.platform_commit
    start_mode = "Manual"
    services = $serviceResults
    endpoints = [ordered]@{ api = "pass"; web = "pass"; proxy = "pass"; mailpit = "pass"; smtp = "pass"; loopback_scope = "pass" }
    listener_ownership = $listenerResults
    access_boundaries = [ordered]@{ secrets = "pass"; data = "pass"; worker_runtime = "pass"; web = "pass"; mailpit_state = "pass" }
    worker_queue_loop = "pass"
    worker_runtime = $workerRuntime
}
$json = $report | ConvertTo-Json -Depth 8
if ($ReportPath) {
    $resolved = [IO.Path]::GetFullPath($ReportPath)
    if (-not (Test-Path -LiteralPath (Split-Path -Parent $resolved) -PathType Container)) { throw "Report parent does not exist." }
    [IO.File]::WriteAllText($resolved, "$json$([Environment]::NewLine)", [Text.UTF8Encoding]::new($false))
}
Write-Output $json
