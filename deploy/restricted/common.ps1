function Get-Re3DRestrictedSecretAclSids {
    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().User
    if ($null -eq $currentUser) {
        throw "Could not resolve the current Windows user SID."
    }
    return @(
        $currentUser,
        [Security.Principal.SecurityIdentifier]::new("S-1-5-32-544"),
        [Security.Principal.SecurityIdentifier]::new("S-1-5-18")
    )
}

function Get-Re3DRestrictedProjectRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

function Get-Re3DRestrictedPython {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)

    $venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        throw "Project virtual environment was not found at $venvPython"
    }
    return $venvPython
}

function Get-Re3DRestrictedDeploymentOwnerSid {
    $account = [Security.Principal.NTAccount]::new(
        $env:COMPUTERNAME,
        "re3d-deploy"
    )
    try {
        return $account.Translate(
            [Security.Principal.SecurityIdentifier]
        )
    } catch {
        throw "Could not resolve the fixed local re3d-deploy account."
    }
}

function Get-Re3DRestrictedVirtualServiceSid {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("Re3DRestrictedApi", "Re3DRestrictedWorker")]
        [string]$ServiceId
    )

    $output = (& sc.exe showsid $ServiceId 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0 -or $output -notmatch '(?m)\b(S-1-5-80-(?:\d+-){4}\d+)\b') {
        throw "Could not derive the virtual service SID for $ServiceId."
    }
    return [Security.Principal.SecurityIdentifier]::new($Matches[1])
}

function Set-Re3DRestrictedSecretFileAcl {
    param([Parameter(Mandatory = $true)][string]$Path)

    $resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw "Restricted secret file was not found: $resolved"
    }
    $sids = @(Get-Re3DRestrictedSecretAclSids)
    $acl = [Security.AccessControl.FileSecurity]::new()
    $acl.SetOwner($sids[0])
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sid in $sids) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $sid,
            [Security.AccessControl.FileSystemRights]::FullControl,
            [Security.AccessControl.AccessControlType]::Allow
        )
        [void]$acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $resolved -AclObject $acl
}

function Assert-Re3DRestrictedSecretFileAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$VirtualService = "",
        [Security.Principal.SecurityIdentifier]$ExpectedVirtualServiceSid,
        [switch]$VirtualServiceSidOptional,
        [switch]$RequireDeploymentOwner
    )

    $resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    if ($VirtualService -and $VirtualService -notin @(
        "Re3DRestrictedApi", "Re3DRestrictedWorker"
    )) {
        throw "Unsupported restricted virtual service identity: $VirtualService"
    }
    if ($VirtualService -and $ExpectedVirtualServiceSid) {
        throw "Specify a virtual service name or SID, not both."
    }
    if ($VirtualServiceSidOptional -and -not $ExpectedVirtualServiceSid) {
        throw "An optional virtual service ACL requires an expected service SID."
    }
    if (
        $RequireDeploymentOwner -and
        ($VirtualService -or $ExpectedVirtualServiceSid)
    ) {
        throw "A source deployment owner cannot be combined with a virtual service boundary."
    }
    $acl = Get-Acl -LiteralPath $resolved
    if (-not $acl.AreAccessRulesProtected) {
        throw "Restricted secret file still inherits ACL entries: $resolved"
    }
    $ownerSid = $acl.GetOwner(
        [Security.Principal.SecurityIdentifier]
    ).Value
    $administratorsSid = "S-1-5-32-544"
    $systemSid = "S-1-5-18"
    $hasServiceBoundary = $VirtualService -or $ExpectedVirtualServiceSid
    if ($hasServiceBoundary) {
        if ($ownerSid -cne $administratorsSid) {
            throw "Restricted service secret must be owned by Administrators: $resolved"
        }
        $fullControlSids = @($administratorsSid, $systemSid)
    } else {
        $sourceOwnerSid = if ($RequireDeploymentOwner) {
            Get-Re3DRestrictedDeploymentOwnerSid
        } else {
            @(Get-Re3DRestrictedSecretAclSids)[0]
        }
        $fullControlSids = @(
            $sourceOwnerSid.Value,
            $administratorsSid,
            $systemSid
        ) | Select-Object -Unique
        if ($ownerSid -cne $fullControlSids[0]) {
            $requiredOwner = if ($RequireDeploymentOwner) {
                "the fixed local re3d-deploy account"
            } else {
                "the current Windows user"
            }
            throw "Restricted source secret must be owned by ${requiredOwner}: $resolved"
        }
    }
    $fullControlSids = @($fullControlSids | Select-Object -Unique)
    $serviceSids = if ($ExpectedVirtualServiceSid) {
        @($ExpectedVirtualServiceSid.Value)
    } elseif ($VirtualService) {
        @(
            [Security.Principal.NTAccount]::new(
                "NT SERVICE\$VirtualService"
            ).Translate([Security.Principal.SecurityIdentifier]).Value
        )
    } else { @() }
    $expectedSids = @($fullControlSids + $serviceSids) | Select-Object -Unique
    foreach ($rule in $acl.Access) {
        $sid = $rule.IdentityReference.Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
        if (
            $rule.AccessControlType -ne (
                [Security.AccessControl.AccessControlType]::Allow
            ) -or
            $expectedSids -cnotcontains $sid
        ) {
            throw "Restricted secret file has an unexpected ACL entry: $sid"
        }
    }
    foreach ($expectedSid in $fullControlSids) {
        $rights = [Security.AccessControl.FileSystemRights]0
        foreach ($rule in $acl.Access) {
            $sid = $rule.IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]
            ).Value
            if ($sid -ceq $expectedSid) {
                $rights = $rights -bor $rule.FileSystemRights
            }
        }
        if (
            ($rights -band [Security.AccessControl.FileSystemRights]::FullControl) -ne
            [Security.AccessControl.FileSystemRights]::FullControl
        ) {
            throw "Restricted secret ACL is incomplete for SID $expectedSid"
        }
    }
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
    foreach ($serviceSid in $serviceSids) {
        $rights = [Security.AccessControl.FileSystemRights]0
        foreach ($rule in $acl.Access) {
            $sid = $rule.IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]
            ).Value
            if ($sid -ceq $serviceSid) {
                $rights = $rights -bor $rule.FileSystemRights
            }
        }
        if ($VirtualServiceSidOptional -and $rights -eq 0) { continue }
        if (
            ($rights -band [Security.AccessControl.FileSystemRights]::Read) -ne
                [Security.AccessControl.FileSystemRights]::Read -or
            ($rights -band $writeRights) -ne 0
        ) {
            throw "Restricted service SID has an unsafe secret ACL: $serviceSid"
        }
    }
}

function Assert-Re3DRestrictedEnvironmentFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Python,
        [string]$VirtualService = "",
        [switch]$RequireDeploymentOwner
    )

    $resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    Assert-Re3DRestrictedSecretFileAcl `
        -Path $resolved `
        -VirtualService $VirtualService `
        -RequireDeploymentOwner:$RequireDeploymentOwner
    $environment = (& $Python -m dotenv -f $resolved get APP_ENV).Trim()
    if ($LASTEXITCODE -ne 0 -or $environment -cne "restricted") {
        throw "Restricted process environment must set APP_ENV=restricted."
    }
    return $resolved
}
