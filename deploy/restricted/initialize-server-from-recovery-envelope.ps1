#Requires -Version 5.1

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$EnvelopePath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Security -ErrorAction Stop

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$workspaceRoot = Split-Path -Parent $projectRoot
$postgresRoot = Join-Path $projectRoot "deploy\postgres"
$DatabasePort = 5432
. (Join-Path $postgresRoot "common.ps1")

$initializer = (
    Resolve-Path -LiteralPath (
        Join-Path $PSScriptRoot "initialize-server-restricted.ps1"
    ) -ErrorAction Stop
).Path
$apiEnvironment = Join-Path $projectRoot ".env.restricted"
$workerEnvironment = Join-Path $projectRoot ".env.worker.restricted"
foreach ($environmentFile in @($apiEnvironment, $workerEnvironment)) {
    if (Test-Path -LiteralPath $environmentFile) {
        throw (
            "Restricted environment already exists: $environmentFile. " +
            "The one-time recovery envelope was not consumed."
        )
    }
}

$allowedRecoveryRoot = [IO.Path]::GetFullPath(
    (Join-Path $workspaceRoot "Re3D-data\_recovery")
)
$credentialDirectory = [IO.Path]::GetFullPath(
    (Join-Path $allowedRecoveryRoot "credentials")
)
$resolvedEnvelope = (
    Resolve-Path -LiteralPath $EnvelopePath -ErrorAction Stop
).Path
if (-not (Test-Re3DPathWithin `
    -Candidate $resolvedEnvelope `
    -Root $credentialDirectory
)) {
    throw "Recovery envelope must stay within $credentialDirectory"
}
$envelopeParent = [IO.Path]::GetFullPath(
    (Split-Path -Parent $resolvedEnvelope)
)
if (-not $envelopeParent.Equals(
    $credentialDirectory,
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw "Recovery envelope must be directly inside $credentialDirectory"
}
foreach ($pathToCheck in @($credentialDirectory, $resolvedEnvelope)) {
    $item = Get-Item -LiteralPath $pathToCheck -Force -ErrorAction Stop
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Recovery credential path must not be a reparse point: $pathToCheck"
    }
}

$currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$currentSid = $currentIdentity.User
$systemSid = New-Object Security.Principal.SecurityIdentifier("S-1-5-18")
$administratorsSid = New-Object Security.Principal.SecurityIdentifier(
    "S-1-5-32-544"
)
$securitySections = (
    [Security.AccessControl.AccessControlSections]::Access -bor
    [Security.AccessControl.AccessControlSections]::Owner -bor
    [Security.AccessControl.AccessControlSections]::Group
)

function Assert-Re3DRecoveryAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][bool]$Directory
    )

    if ($Directory) {
        $security = [IO.Directory]::GetAccessControl($Path, $securitySections)
    } else {
        $security = [IO.File]::GetAccessControl($Path, $securitySections)
    }
    if (-not $security.AreAccessRulesProtected) {
        throw "Recovery credential ACL inherits permissions: $Path"
    }
    $owner = $security.GetOwner(
        [Security.Principal.SecurityIdentifier]
    )
    if ($owner.Value -cne $systemSid.Value) {
        throw "Recovery credential owner is not SYSTEM: $Path"
    }

    $allowedSids = @{}
    $allowedSids.Add($systemSid.Value, [int64]0)
    $allowedSids.Add($administratorsSid.Value, [int64]0)
    $allowedSids.Add($currentSid.Value, [int64]0)
    $rules = $security.GetAccessRules(
        $true,
        $true,
        [Security.Principal.SecurityIdentifier]
    )
    foreach ($rule in $rules) {
        $ruleSid = $rule.IdentityReference.Value
        if ($rule.IsInherited) {
            throw "Recovery credential ACL contains an inherited rule: $Path"
        }
        if (-not $allowedSids.ContainsKey($ruleSid)) {
            throw "Recovery credential ACL grants an unexpected identity: $Path"
        }
        if (
            $rule.AccessControlType -ne
            [Security.AccessControl.AccessControlType]::Allow
        ) {
            throw "Recovery credential ACL contains a deny rule: $Path"
        }
        if ($Directory) {
            $requiredInheritance = (
                [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
                [Security.AccessControl.InheritanceFlags]::ObjectInherit
            )
            if (
                ($rule.InheritanceFlags -band $requiredInheritance) -ne
                $requiredInheritance
            ) {
                throw "Recovery credential directory rule does not inherit safely: $Path"
            }
        } elseif (
            $rule.InheritanceFlags -ne
            [Security.AccessControl.InheritanceFlags]::None
        ) {
            throw "Recovery credential file rule has unexpected inheritance: $Path"
        }
        $allowedSids[$ruleSid] = (
            [int64]$allowedSids[$ruleSid] -bor
            [int64]$rule.FileSystemRights
        )
    }
    foreach ($sidValue in @($systemSid.Value, $administratorsSid.Value)) {
        $requiredRights = [int64][Security.AccessControl.FileSystemRights]::FullControl
        if (($allowedSids[$sidValue] -band $requiredRights) -ne $requiredRights) {
            throw "Recovery credential ACL does not grant administrative recovery: $Path"
        }
    }
    $consumerRights = [int64][Security.AccessControl.FileSystemRights]::Modify
    if (
        ($allowedSids[$currentSid.Value] -band $consumerRights) -ne
        $consumerRights
    ) {
        throw "Recovery credential ACL does not grant the consumer Modify rights: $Path"
    }
}

Assert-Re3DRecoveryAcl -Path $credentialDirectory -Directory $true
Assert-Re3DRecoveryAcl -Path $resolvedEnvelope -Directory $false

$envelopePurpose = "Re3D PostgreSQL administrator recovery envelope v1"
$cipherBytes = [IO.File]::ReadAllBytes($resolvedEnvelope)
$entropy = [Text.Encoding]::UTF8.GetBytes($envelopePurpose)
$plainBytes = $null
$payloadJson = $null
$payload = $null
$passwordCharacters = $null
$securePassword = $null
try {
    $plainBytes = [Security.Cryptography.ProtectedData]::Unprotect(
        $cipherBytes,
        $entropy,
        [Security.Cryptography.DataProtectionScope]::LocalMachine
    )
    $payloadJson = [Text.Encoding]::UTF8.GetString($plainBytes)
    $payload = $payloadJson | ConvertFrom-Json
    foreach ($propertyName in @(
        "schema_version",
        "purpose",
        "recovery_id",
        "target_sid",
        "machine_name",
        "database_host",
        "database_port",
        "admin_role",
        "expires_at_utc",
        "password"
    )) {
        if (
            $null -eq $payload.PSObject.Properties[$propertyName] -or
            [string]::IsNullOrWhiteSpace([string]$payload.$propertyName)
        ) {
            throw "Recovery envelope is missing property: $propertyName"
        }
    }
    if ([int]$payload.schema_version -ne 1) {
        throw "Unsupported recovery envelope version."
    }
    if ([string]$payload.purpose -cne $envelopePurpose) {
        throw "Recovery envelope purpose does not match this initializer."
    }
    if ([string]$payload.recovery_id -notmatch '^[a-f0-9]{32}$') {
        throw "Recovery envelope identifier is invalid."
    }
    if ([string]$payload.target_sid -cne $currentSid.Value) {
        throw "Recovery envelope belongs to a different Windows identity."
    }
    if (-not ([string]$payload.machine_name).Equals(
        $env:COMPUTERNAME,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Recovery envelope belongs to a different computer."
    }
    if ([string]$payload.database_host -cne "127.0.0.1") {
        throw "Recovery envelope is not bound to IPv4 loopback."
    }
    if ([int]$payload.database_port -ne $DatabasePort) {
        throw "Recovery envelope belongs to a different PostgreSQL port."
    }
    if ([string]$payload.admin_role -cne "postgres") {
        throw "Recovery envelope belongs to a different PostgreSQL role."
    }
    $expiresAt = [DateTimeOffset]::MinValue
    if (-not [DateTimeOffset]::TryParse(
        [string]$payload.expires_at_utc,
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::RoundtripKind,
        [ref]$expiresAt
    )) {
        throw "Recovery envelope expiration is invalid."
    }
    if ($expiresAt.UtcDateTime -le [DateTime]::UtcNow) {
        throw "Recovery envelope has expired and was not consumed."
    }
    if (([string]$payload.password).Length -lt 24) {
        throw "Recovery envelope password payload is invalid."
    }

    $securePassword = New-Object Security.SecureString
    $passwordCharacters = ([string]$payload.password).ToCharArray()
    foreach ($character in $passwordCharacters) {
        $securePassword.AppendChar($character)
    }
    $securePassword.MakeReadOnly()
} finally {
    if ($null -ne $payload) {
        $payload.password = $null
    }
    $payloadJson = $null
    if ($null -ne $passwordCharacters) {
        [Array]::Clear(
            $passwordCharacters,
            0,
            $passwordCharacters.Length
        )
    }
    if ($null -ne $plainBytes) {
        [Array]::Clear($plainBytes, 0, $plainBytes.Length)
    }
    [Array]::Clear($cipherBytes, 0, $cipherBytes.Length)
    [Array]::Clear($entropy, 0, $entropy.Length)
}

if ($null -eq $securePassword) {
    throw "Recovery envelope did not produce an administrator credential."
}

[IO.File]::Delete($resolvedEnvelope)
if (Test-Path -LiteralPath $resolvedEnvelope) {
    throw "One-time recovery envelope could not be deleted."
}
Write-Host "One-time PostgreSQL recovery credential consumed." -ForegroundColor Green

$initializerExitCode = 0
try {
    $LASTEXITCODE = 0
    & $initializer `
        -DatabaseHost "127.0.0.1" `
        -DatabasePort $DatabasePort `
        -AdminUser "postgres" `
        -AdminPassword $securePassword
    $initializerExitCode = $LASTEXITCODE
} finally {
    $securePassword.Dispose()
    $securePassword = $null
}
if ($initializerExitCode -ne 0) {
    exit $initializerExitCode
}
