[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BundleRoot,
    [string]$PublicBaseUrl,
    [ValidateRange(1, 300)][int]$TimeoutSeconds = 15,
    [ValidateRange(1, 365)][int]$MinimumCertificateDays = 14,
    [string]$ReportPath
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

function Get-Re3DHeaderValue {
    param(
        [Parameter(Mandatory = $true)]$Response,
        [Parameter(Mandatory = $true)][string]$Name
    )

    return (@($Response.Headers[$Name]) -join ", ")
}

function Invoke-Re3DReadOnlyRequest {
    param(
        [Parameter(Mandatory = $true)][Uri]$Uri,
        [Parameter(Mandatory = $true)][int]$Timeout
    )

    try {
        return Invoke-WebRequest `
            -Uri $Uri `
            -Method Get `
            -TimeoutSec $Timeout `
            -SkipHttpErrorCheck
    } catch {
        throw "Could not reach $Uri. $($_.Exception.Message)"
    }
}

function Get-Re3DPublicBaseUri {
    param(
        [string]$Override,
        [Parameter(Mandatory = $true)][string]$SiteAddress
    )

    $value = if ([string]::IsNullOrWhiteSpace($Override)) {
        if ($SiteAddress -match '^https?://') {
            $SiteAddress
        } else {
            "https://$SiteAddress"
        }
    } else {
        $Override
    }
    $uri = $null
    if (-not [Uri]::TryCreate($value, [UriKind]::Absolute, [ref]$uri)) {
        throw "PublicBaseUrl is not an absolute URL: $value"
    }
    if (
        $uri.Scheme -notin @("http", "https") -or
        $uri.UserInfo -or
        $uri.Query -or
        $uri.Fragment -or
        $uri.AbsolutePath -ne "/"
    ) {
        throw "PublicBaseUrl must be an HTTP(S) origin without credentials, path, query, or fragment."
    }
    $loopback = $uri.IsLoopback -or $uri.Host -ieq "localhost"
    if ($uri.Scheme -eq "http" -and -not $loopback) {
        throw "Plaintext HTTP acceptance is allowed only for a loopback origin."
    }
    return $uri
}

function Get-Re3DTlsCertificateSummary {
    param(
        [Parameter(Mandatory = $true)][Uri]$Uri,
        [Parameter(Mandatory = $true)][int]$Timeout,
        [Parameter(Mandatory = $true)][int]$MinimumDays
    )

    if ($Uri.Scheme -ne "https") {
        return $null
    }
    $client = [Net.Sockets.TcpClient]::new()
    $stream = $null
    try {
        $connected = $client.ConnectAsync($Uri.DnsSafeHost, $Uri.Port).Wait(
            [TimeSpan]::FromSeconds($Timeout)
        )
        if (-not $connected) {
            throw "TLS connection timed out."
        }
        $client.ReceiveTimeout = $Timeout * 1000
        $client.SendTimeout = $Timeout * 1000
        $stream = [Net.Security.SslStream]::new($client.GetStream(), $false)
        $stream.AuthenticateAsClient($Uri.DnsSafeHost)
        $certificate = [Security.Cryptography.X509Certificates.X509Certificate2]::new(
            $stream.RemoteCertificate
        )
        $minimumExpiry = [DateTime]::UtcNow.AddDays($MinimumDays)
        if ($certificate.NotAfter.ToUniversalTime() -lt $minimumExpiry) {
            throw "TLS certificate expires in fewer than $MinimumDays days."
        }
        return [ordered]@{
            subject = $certificate.Subject
            issuer = $certificate.Issuer
            thumbprint = $certificate.Thumbprint
            not_after_utc = $certificate.NotAfter.ToUniversalTime().ToString("o")
        }
    } finally {
        if ($stream) {
            $stream.Dispose()
        }
        $client.Dispose()
    }
}

function Get-Re3DSidValue {
    param([Parameter(Mandatory = $true)]$IdentityReference)

    return $IdentityReference.Translate(
        [Security.Principal.SecurityIdentifier]
    ).Value
}

function Assert-Re3DAclRight {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][Security.Principal.SecurityIdentifier]$Sid,
        [Parameter(Mandatory = $true)][Security.AccessControl.FileSystemRights]$Rights
    )

    $resolved = Resolve-Re3DServicePath -Path $Path
    $granted = [Security.AccessControl.FileSystemRights]0
    foreach ($rule in (Get-Acl -LiteralPath $resolved).Access) {
        if (
            $rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
            (Get-Re3DSidValue -IdentityReference $rule.IdentityReference) -ceq $Sid.Value
        ) {
            $granted = $granted -bor $rule.FileSystemRights
        }
    }
    if (($granted -band $Rights) -ne $Rights) {
        throw "Service SID $($Sid.Value) is missing $Rights on $resolved."
    }
}

function Assert-Re3DSecretAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][Security.Principal.SecurityIdentifier]$ServiceSid
    )

    $resolved = Resolve-Re3DServicePath -Path $Path -PathType Leaf
    $acl = Get-Acl -LiteralPath $resolved
    if (-not $acl.AreAccessRulesProtected) {
        throw "Production secret file still inherits ACL entries: $resolved"
    }
    $allowed = @(
        "S-1-5-32-544",
        "S-1-5-18",
        $ServiceSid.Value
    )
    foreach ($rule in $acl.Access) {
        $sid = Get-Re3DSidValue -IdentityReference $rule.IdentityReference
        if (
            $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            $allowed -cnotcontains $sid
        ) {
            throw "Production secret file has an unexpected ACL entry for $sid."
        }
    }
    Assert-Re3DAclRight `
        -Path $resolved `
        -Sid $ServiceSid `
        -Rights ([Security.AccessControl.FileSystemRights]::Read)
}

if (-not (Test-Re3DAdministrator)) {
    throw "Installed service acceptance requires an elevated PowerShell session."
}
$manifest = Assert-Re3DServiceBundle `
    -BundleRoot $BundleRoot `
    -CheckExternalBinaryVersions
Assert-Re3DServiceSourceState -Manifest $manifest | Out-Null
$baseUri = Get-Re3DPublicBaseUri `
    -Override $PublicBaseUrl `
    -SiteAddress $manifest.topology.site_address

$serviceSids = @{}
$serviceSummaries = @()
foreach ($service in @($manifest.services)) {
    $definition = Get-Re3DServiceDefinition -Key $service.key
    $instance = Get-CimInstance `
        -ClassName Win32_Service `
        -Filter "Name = '$($service.id)'" `
        -ErrorAction SilentlyContinue
    if (-not $instance) {
        throw "Windows service is not installed: $($service.id)"
    }
    $expectedAccount = "NT SERVICE\$($service.id)"
    $actualPath = "$($instance.PathName)".Trim().Trim('"')
    if (
        $instance.State -cne "Running" -or
        $instance.StartMode -cne "Auto" -or
        $instance.StartName -ine $expectedAccount -or
        [int]$instance.ProcessId -le 0 -or
        [IO.Path]::GetFullPath($actualPath) -cne
            [IO.Path]::GetFullPath($service.wrapper_path)
    ) {
        throw "Windows service runtime state does not match its bundle: $($service.id)"
    }
    $registryPath = "HKLM:\SYSTEM\CurrentControlSet\Services\$($service.id)"
    $registry = Get-ItemProperty -LiteralPath $registryPath -ErrorAction Stop
    $delayed = if ($null -eq $registry.DelayedAutostart) {
        0
    } else {
        [int]$registry.DelayedAutostart
    }
    if (
        [int]$registry.ServiceSidType -ne 1 -or
        $delayed -ne [int]$definition.DelayedStart -or
        $null -eq $registry.FailureActions -or
        @($registry.FailureActions).Count -eq 0
    ) {
        throw "Windows service lifecycle registry settings are incomplete: $($service.id)"
    }
    $serviceSids[$service.key] = Get-Re3DIdentitySid -Identity $service.virtual_identity
    $serviceSummaries += [ordered]@{
        id = $service.id
        state = $instance.State
        start_mode = $instance.StartMode
        account = $instance.StartName
        process_id = [int]$instance.ProcessId
        delayed_auto_start = [bool]$delayed
        service_sid_type = "unrestricted"
    }
}

Assert-Re3DSecretAcl `
    -Path $manifest.external.api_environment_file `
    -ServiceSid $serviceSids.api
Assert-Re3DSecretAcl `
    -Path $manifest.external.worker_environment_file `
    -ServiceSid $serviceSids.worker
foreach ($key in @("api", "worker")) {
    Assert-Re3DAclRight `
        -Path $manifest.external.data_root `
        -Sid $serviceSids[$key] `
        -Rights ([Security.AccessControl.FileSystemRights]::Modify)
}
Assert-Re3DAclRight `
    -Path $manifest.external.re3d_root `
    -Sid $serviceSids.worker `
    -Rights ([Security.AccessControl.FileSystemRights]::ReadAndExecute)
foreach ($executionRoot in @($manifest.external.worker_execution_roots)) {
    Assert-Re3DAclRight `
        -Path $executionRoot `
        -Sid $serviceSids.worker `
        -Rights ([Security.AccessControl.FileSystemRights]::ReadAndExecute)
}
Assert-Re3DAclRight `
    -Path $manifest.external.web_root `
    -Sid $serviceSids.caddy `
    -Rights ([Security.AccessControl.FileSystemRights]::ReadAndExecute)

$directHealthUri = [Uri]"http://127.0.0.1:$($manifest.topology.api_port)/health/live"
$directHealth = Invoke-Re3DReadOnlyRequest `
    -Uri $directHealthUri `
    -Timeout $TimeoutSeconds
$directPayload = $directHealth.Content | ConvertFrom-Json
if (
    $directHealth.StatusCode -ne 200 -or
    $directPayload.status -cne "ok" -or
    $directPayload.environment -cne "production" -or
    [bool]$directPayload.development_routes_enabled
) {
    throw "Direct API health does not report the production boundary."
}
$listeners = @(Get-NetTCPConnection `
    -State Listen `
    -LocalPort $manifest.topology.api_port `
    -ErrorAction Stop)
if (
    $listeners.Count -eq 0 -or
    @($listeners | Where-Object { $_.LocalAddress -ne "127.0.0.1" }).Count -gt 0
) {
    throw "Production API is not restricted to IPv4 loopback."
}

$home = Invoke-Re3DReadOnlyRequest `
    -Uri ([Uri]::new($baseUri, "/")) `
    -Timeout $TimeoutSeconds
if ($home.StatusCode -ne 200 -or $home.Content -notmatch '<div id="root"></div>') {
    throw "Public origin does not serve the reviewed React entry point."
}
$proxiedHealth = Invoke-Re3DReadOnlyRequest `
    -Uri ([Uri]::new($baseUri, "/health/live")) `
    -Timeout $TimeoutSeconds
$proxiedPayload = $proxiedHealth.Content | ConvertFrom-Json
if (
    $proxiedHealth.StatusCode -ne 200 -or
    $proxiedPayload.environment -cne "production"
) {
    throw "Public origin does not proxy the production health endpoint."
}
$unauthorized = Invoke-Re3DReadOnlyRequest `
    -Uri ([Uri]::new($baseUri, "/api/v1/auth/me")) `
    -Timeout $TimeoutSeconds
if (
    $unauthorized.StatusCode -ne 401 -or
    (Get-Re3DHeaderValue -Response $unauthorized -Name "Cache-Control") -notmatch "no-store"
) {
    throw "Public protected API boundary did not preserve 401 and no-store."
}
$contentSecurityPolicy = Get-Re3DHeaderValue -Response $home -Name "Content-Security-Policy"
if (
    $contentSecurityPolicy -notmatch "frame-ancestors 'none'" -or
    $contentSecurityPolicy -notmatch "connect-src 'self' blob:" -or
    (Get-Re3DHeaderValue -Response $home -Name "X-Content-Type-Options") -cne "nosniff" -or
    (Get-Re3DHeaderValue -Response $home -Name "X-Frame-Options") -cne "DENY" -or
    (Get-Re3DHeaderValue -Response $home -Name "Server")
) {
    throw "Public origin security headers do not match the reviewed policy."
}
if (
    $baseUri.Scheme -eq "https" -and
    (Get-Re3DHeaderValue -Response $home -Name "Strict-Transport-Security") -notmatch "max-age=31536000"
) {
    throw "HTTPS origin does not provide the reviewed HSTS policy."
}

$deploymentResponse = Invoke-Re3DReadOnlyRequest `
    -Uri ([Uri]::new($baseUri, "/deployment.json")) `
    -Timeout $TimeoutSeconds
$publicDeployment = $deploymentResponse.Content | ConvertFrom-Json
if (
    $deploymentResponse.StatusCode -ne 200 -or
    $publicDeployment.source_commit -cne $manifest.source.web_source_commit -or
    [bool]$publicDeployment.source_dirty -or
    [int]$publicDeployment.asset_count -ne [int]$manifest.source.web_asset_count
) {
    throw "Public frontend deployment identity does not match the service bundle."
}
$certificate = Get-Re3DTlsCertificateSummary `
    -Uri $baseUri `
    -Timeout $TimeoutSeconds `
    -MinimumDays $MinimumCertificateDays

$logSummaries = @()
foreach ($service in @($manifest.services)) {
    $files = @(Get-ChildItem -LiteralPath $service.log_directory -File -ErrorAction Stop)
    if ($files.Count -eq 0) {
        throw "Windows service has not created a log file: $($service.id)"
    }
    $logSummaries += [ordered]@{
        service_id = $service.id
        file_count = $files.Count
        latest_write_utc = ($files | Sort-Object LastWriteTimeUtc -Descending |
            Select-Object -First 1).LastWriteTimeUtc.ToString("o")
    }
}
$workerService = @($manifest.services) |
    Where-Object { $_.key -ceq "worker" } |
    Select-Object -First 1
$workerLoopEvidence = @(
    Get-ChildItem -LiteralPath $workerService.log_directory -File |
        ForEach-Object { Get-Content -LiteralPath $_.FullName -Tail 200 } |
        Select-String -Pattern '"operation"\s*:\s*"real-worker-loop"'
)
if ($workerLoopEvidence.Count -eq 0) {
    throw "Worker logs do not show entry into the real queue loop."
}
$workerRetentionEvidence = @(
    Get-ChildItem -LiteralPath $workerService.log_directory -File |
        ForEach-Object { Get-Content -LiteralPath $_.FullName -Tail 200 } |
        Select-String -Pattern (
            '"operation"\s*:\s*"cleanup-success-job-storage".*' +
            '"mode"\s*:\s*"dry_run"'
        )
)
if ($workerRetentionEvidence.Count -eq 0) {
    throw "Worker logs do not show the audited successful-task retention dry-run."
}

$report = [ordered]@{
    status = "accepted"
    checked_at_utc = [DateTime]::UtcNow.ToString("o")
    bundle_schema_version = $manifest.schema_version
    platform_commit = $manifest.source.platform_commit
    web_source_commit = $manifest.source.web_source_commit
    public_origin = $baseUri.GetLeftPart([UriPartial]::Authority)
    tls_certificate = $certificate
    services = $serviceSummaries
    endpoints = [ordered]@{
        direct_api_health = "pass"
        public_home = "pass"
        proxied_health = "pass"
        protected_api_boundary = "pass"
        retention_dry_run = "pass"
        security_headers = "pass"
        deployment_identity = "pass"
    }
    access_boundaries = [ordered]@{
        secret_files = "pass"
        shared_data_root = "pass"
        worker_runtime = "pass"
        web_root = "pass"
    }
    logs = $logSummaries
    worker_queue_loop = "pass"
}
$json = $report | ConvertTo-Json -Depth 8
if ($ReportPath) {
    $resolvedReportPath = [IO.Path]::GetFullPath($ReportPath)
    $parent = Split-Path -Parent $resolvedReportPath
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        throw "Report parent directory does not exist: $parent"
    }
    [IO.File]::WriteAllText(
        $resolvedReportPath,
        "$json$([Environment]::NewLine)",
        [Text.UTF8Encoding]::new($false)
    )
}
Write-Output $json
