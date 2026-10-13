[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$deploymentSid = [Security.Principal.SecurityIdentifier]::new(
    "S-1-5-21-111111111-222222222-333333333-1001"
)
$unexpectedSid = [Security.Principal.SecurityIdentifier]::new(
    "S-1-5-21-111111111-222222222-333333333-1002"
)
$serviceSid = [Security.Principal.SecurityIdentifier]::new(
    "S-1-5-80-111111111-222222222-333333333-444444444-555555555"
)
$administratorsSid = [Security.Principal.SecurityIdentifier]::new("S-1-5-32-544")
$systemSid = [Security.Principal.SecurityIdentifier]::new("S-1-5-18")
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$fixturePath = Join-Path $PSScriptRoot "common.ps1"
$script:MockAcl = $null

function Get-Re3DRestrictedDeploymentOwnerSid { return $deploymentSid }
function Get-Acl {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)
    return $script:MockAcl
}
function New-MockAcl {
    param(
        [Parameter(Mandatory = $true)][Security.Principal.SecurityIdentifier]$Owner,
        [Parameter(Mandatory = $true)][object[]]$Rules
    )
    $accessRules = @()
    foreach ($entry in $Rules) {
        $accessRules += (
            [Security.AccessControl.FileSystemAccessRule]::new(
                $entry.Sid,
                $entry.Rights,
                [Security.AccessControl.AccessControlType]::Allow
            )
        )
    }
    $acl = [pscustomobject]@{
        AreAccessRulesProtected = $true
        OwnerSid = $Owner
        Access = @($accessRules)
    }
    $acl | Add-Member -MemberType ScriptMethod -Name GetOwner -Value {
        param($TargetType)
        return $this.OwnerSid
    }
    return $acl
}
function New-FullControlRule {
    param([Security.Principal.SecurityIdentifier]$Sid)
    return [pscustomobject]@{
        Sid = $Sid
        Rights = [Security.AccessControl.FileSystemRights]::FullControl
    }
}
function Assert-Rejected {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Action,
        [Parameter(Mandatory = $true)][string]$MessagePattern
    )
    try { & $Action } catch {
        if ($_.Exception.Message -notmatch $MessagePattern) { throw }
        return
    }
    throw "Expected ACL contract rejection: $MessagePattern"
}

$adminRule = New-FullControlRule -Sid $administratorsSid
$systemRule = New-FullControlRule -Sid $systemSid
$deploymentRule = New-FullControlRule -Sid $deploymentSid
$currentRule = New-FullControlRule -Sid $currentSid
$unexpectedRule = New-FullControlRule -Sid $unexpectedSid
$serviceReadRule = [pscustomobject]@{
    Sid = $serviceSid
    Rights = [Security.AccessControl.FileSystemRights]::Read
}
$serviceWriteRule = [pscustomobject]@{
    Sid = $serviceSid
    Rights = (
        [Security.AccessControl.FileSystemRights]::Read -bor
        [Security.AccessControl.FileSystemRights]::WriteData
    )
}

$script:MockAcl = New-MockAcl -Owner $deploymentSid -Rules @(
    $deploymentRule, $adminRule, $systemRule
)
Assert-Re3DRestrictedSecretFileAcl -Path $fixturePath -RequireDeploymentOwner

$script:MockAcl = New-MockAcl -Owner $unexpectedSid -Rules @(
    $unexpectedRule, $adminRule, $systemRule
)
Assert-Rejected -MessagePattern "fixed local re3d-deploy" -Action {
    Assert-Re3DRestrictedSecretFileAcl -Path $fixturePath -RequireDeploymentOwner
}

$script:MockAcl = New-MockAcl -Owner $currentSid -Rules @(
    $currentRule, $adminRule, $systemRule
)
Assert-Re3DRestrictedSecretFileAcl -Path $fixturePath

$script:MockAcl = New-MockAcl -Owner $administratorsSid -Rules @(
    $adminRule, $systemRule
)
Assert-Re3DRestrictedSecretFileAcl `
    -Path $fixturePath `
    -ExpectedVirtualServiceSid $serviceSid `
    -VirtualServiceSidOptional
Assert-Rejected -MessagePattern "unsafe secret ACL" -Action {
    Assert-Re3DRestrictedSecretFileAcl `
        -Path $fixturePath `
        -ExpectedVirtualServiceSid $serviceSid
}

$script:MockAcl = New-MockAcl -Owner $administratorsSid -Rules @(
    $adminRule, $systemRule, $serviceReadRule
)
Assert-Re3DRestrictedSecretFileAcl `
    -Path $fixturePath `
    -ExpectedVirtualServiceSid $serviceSid

$script:MockAcl = New-MockAcl -Owner $administratorsSid -Rules @(
    $adminRule, $systemRule, $serviceWriteRule
)
Assert-Rejected -MessagePattern "unsafe secret ACL" -Action {
    Assert-Re3DRestrictedSecretFileAcl `
        -Path $fixturePath `
        -ExpectedVirtualServiceSid $serviceSid
}

$script:MockAcl = New-MockAcl -Owner $administratorsSid -Rules @(
    $adminRule, $systemRule, $serviceReadRule, $unexpectedRule
)
Assert-Rejected -MessagePattern "unexpected ACL entry" -Action {
    Assert-Re3DRestrictedSecretFileAcl `
        -Path $fixturePath `
        -ExpectedVirtualServiceSid $serviceSid
}

Assert-Rejected -MessagePattern "cannot be combined" -Action {
    Assert-Re3DRestrictedSecretFileAcl `
        -Path $fixturePath `
        -ExpectedVirtualServiceSid $serviceSid `
        -RequireDeploymentOwner
}

Write-Output "Restricted secret ACL identity matrix: PASS"
