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
    param([Parameter(Mandatory = $true)][string]$Path)

    $resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    $expectedSids = @(
        Get-Re3DRestrictedSecretAclSids |
            ForEach-Object { $_.Value }
    )
    $acl = Get-Acl -LiteralPath $resolved
    if (-not $acl.AreAccessRulesProtected) {
        throw "Restricted secret file still inherits ACL entries: $resolved"
    }
    $ownerSid = $acl.GetOwner(
        [Security.Principal.SecurityIdentifier]
    ).Value
    if ($ownerSid -cne $expectedSids[0]) {
        throw "Restricted secret file has an unexpected owner: $ownerSid"
    }
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
    foreach ($expectedSid in $expectedSids) {
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
}

function Assert-Re3DRestrictedEnvironmentFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Python
    )

    $resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    Assert-Re3DRestrictedSecretFileAcl -Path $resolved
    $environment = (& $Python -m dotenv -f $resolved get APP_ENV).Trim()
    if ($LASTEXITCODE -ne 0 -or $environment -cne "restricted") {
        throw "Restricted process environment must set APP_ENV=restricted."
    }
    return $resolved
}
