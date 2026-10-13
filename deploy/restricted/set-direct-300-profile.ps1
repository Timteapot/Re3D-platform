#Requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ProjectRoot,
    [string]$BundleRoot = "C:\ProgramData\Re3DRestricted",
    [ValidateSet('direct-300', 'direct-300-restricted-20')]
    [string]$ProfileName = 'direct-300',
    [switch]$Apply
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$stage = "preflight"
$locks = [Collections.Generic.List[IO.FileStream]]::new()
$backupDirectory = $null
$changedTargets = [Collections.Generic.List[object]]::new()

function Get-QuotedValueEnd {
    param([string]$Value, [char]$Quote, [int]$Start = 0)
    for ($index = $Start; $index -lt $Value.Length; $index++) {
        if ($Value[$index] -ne $Quote) { continue }
        $slashes = 0
        for ($before = $index - 1; $before -ge 0 -and $Value[$before] -eq '\'; $before--) {
            $slashes++
        }
        if ($slashes % 2 -eq 0) { return $index }
    }
    return -1
}

function Get-EnvironmentPlan {
    param([byte[]]$Bytes, [Collections.IDictionary]$Settings, [string]$Role)
    $bom = $Bytes.Length -ge 3 -and $Bytes[0] -eq 239 -and $Bytes[1] -eq 187 -and $Bytes[2] -eq 191
    $offset = if ($bom) { 3 } else { 0 }
    $utf8 = [Text.UTF8Encoding]::new($false, $true)
    $text = $utf8.GetString($Bytes, $offset, $Bytes.Length - $offset)
    $builder = [Text.StringBuilder]::new()
    $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    $changes = [Collections.Generic.List[object]]::new()
    $openQuote = $null
    $restricted = $false
    $newline = if ($text.Contains("`r`n")) { "`r`n" } else { "`n" }

    foreach ($lineMatch in [regex]::Matches($text, '[^\r\n]*(?:\r\n|\n|\r|$)')) {
        $line = $lineMatch.Value
        if ($line.Length -eq 0) { continue }
        $body = $line.TrimEnd("`r", "`n")
        $ending = $line.Substring($body.Length)
        if ($null -ne $openQuote) {
            if ((Get-QuotedValueEnd -Value $body -Quote $openQuote) -ge 0) { $openQuote = $null }
            [void]$builder.Append($line)
            continue
        }
        $assignment = [regex]::Match($body, '^(?<prefix>[ \t]*(?:export[ \t]+)?(?<key>[A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*)(?<value>.*)$')
        if (-not $assignment.Success) {
            [void]$builder.Append($line)
            continue
        }
        $key = $assignment.Groups['key'].Value
        $value = $assignment.Groups['value'].Value
        if ($key -ceq 'APP_ENV' -or $Settings.Keys -ccontains $key) {
            if (-not $seen.Add($key)) { throw "Duplicate managed environment key." }
            # Only plain operational values may be printed. Quoted values are
            # accepted, but arbitrary old values never enter reports or errors.
            $parsed = [regex]::Match($value, '^(?:"(?<v>[A-Za-z0-9_.:-]+)"|''(?<v>[A-Za-z0-9_.:-]+)''|(?<v>[A-Za-z0-9_.:-]+))(?<tail>[ \t]*(?:#.*)?)$')
            if (-not $parsed.Success) { throw "Managed key must have one simple operational value." }
            $old = $parsed.Groups['v'].Value
            if ($key -ceq 'APP_ENV') {
                if ($old -cne 'restricted') { throw "Only APP_ENV=restricted is supported." }
                $restricted = $true
                [void]$builder.Append($line)
                continue
            }
            $safeOld = if ($key -ceq 'RE3D_EXECUTION_PROFILE') {
                if ($old -cin @('baseline', 'gpu-large-100', 'gpu-large-150', 'gpu-large-300', 'gpu-experiment-300-20')) { $old } else { '<nonstandard>' }
            } elseif ($key -cin @('RE3D_RESOURCE_MONITOR_ENABLED', 'RE3D_LOW_DISK_GUARD_ENABLED')) {
                if ($old -cin @('true', 'false')) { $old } else { '<nonstandard>' }
            } else {
                if ($old -match '^\d{1,18}$') { $old } else { '<nonstandard>' }
            }
            $next = [string]$Settings[$key]
            $changes.Add([ordered]@{ key = $key; old = $safeOld; new = $next; changed = $old -cne $next })
            if ($old -ceq $next) {
                [void]$builder.Append($line)
            } else {
                [void]$builder.Append($assignment.Groups['prefix'].Value + $next + $parsed.Groups['tail'].Value + $ending)
            }
            continue
        }
        if ($value.Length -gt 0 -and ($value[0] -eq '"' -or $value[0] -eq "'")) {
            if ((Get-QuotedValueEnd -Value $value -Quote $value[0] -Start 1) -lt 0) { $openQuote = $value[0] }
        }
        [void]$builder.Append($line)
    }
    if ($null -ne $openQuote) { throw "Unterminated quoted environment field." }
    if (-not $restricted) { throw "APP_ENV=restricted must be present." }
    foreach ($key in $Settings.Keys) {
        if ($seen.Contains($key)) { continue }
        if ($builder.Length -gt 0 -and $builder[$builder.Length - 1] -notin @("`r", "`n")) {
            [void]$builder.Append($newline)
        }
        [void]$builder.Append($key + '=' + [string]$Settings[$key] + $newline)
        $changes.Add([ordered]@{ key = $key; old = '<absent>'; new = [string]$Settings[$key]; changed = $true })
    }
    $outputBytes = $utf8.GetBytes($builder.ToString())
    if ($bom) { $outputBytes = [byte[]](@(239, 187, 191) + $outputBytes) }
    return [pscustomobject]@{
        role = $Role
        original_bytes = $Bytes
        bytes = $outputBytes
        changes = @($changes)
        changed = @($changes | Where-Object { $_.changed }).Count -gt 0
    }
}

function Assert-ServicesStopped {
    $ids = @('Re3DRestrictedMailpit', 'Re3DRestrictedApi', 'Re3DRestrictedWorker', 'Re3DRestrictedCaddy')
    $services = @(Get-Service -ErrorAction Stop | Where-Object { $_.Name -cin $ids })
    if (@($services | Where-Object { $_.Status.ToString() -cne 'Stopped' }).Count -gt 0) {
        throw "Restricted services must be stopped before preparing or applying this profile."
    }
    return @($services | ForEach-Object { [ordered]@{ name = $_.Name; status = $_.Status.ToString() } })
}

function Protect-BackupDirectory {
    param([string]$Path)
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $current = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl.SetOwner($current)
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sid in @($current, [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'), [Security.Principal.SecurityIdentifier]::new('S-1-5-18'))) {
        [void]$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $sid,
            [Security.AccessControl.FileSystemRights]::FullControl,
            ([Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit),
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        ))
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
    if (-not (Get-Acl -LiteralPath $Path).AreAccessRulesProtected) { throw "Backup ACL protection failed." }
}

function Set-LockedFileBytes {
    param([IO.FileStream]$Stream, [byte[]]$Bytes)
    $Stream.Position = 0
    $Stream.Write($Bytes, 0, $Bytes.Length)
    $Stream.SetLength($Bytes.Length)
    $Stream.Flush($true)
}

try {
    if (-not $IsWindows) { throw "This profile tool requires Windows." }
    if (-not [IO.Path]::IsPathFullyQualified($ProjectRoot) -or -not [IO.Path]::IsPathFullyQualified($BundleRoot)) {
        throw "ProjectRoot and BundleRoot must be explicit absolute paths."
    }
    $project = (Resolve-Path -LiteralPath $ProjectRoot -ErrorAction Stop).Path.TrimEnd('\', '/')
    if ($project -eq [IO.Path]::GetPathRoot($project).TrimEnd('\', '/')) { throw "ProjectRoot cannot be a drive root." }
    $bundle = [IO.Path]::GetFullPath($BundleRoot)
    # The ValidateSet restricts selection to reviewed, non-secret assets. The
    # existing direct-300 contract remains the default and is not weakened.
    $profilePath = Join-Path $PSScriptRoot ('profiles\' + $ProfileName + '.json')
    $profile = Get-Content -LiteralPath $profilePath -Raw | ConvertFrom-Json -AsHashtable
    $expected = @{
        api = [ordered]@{
            RE3D_EXECUTION_PROFILE = 'gpu-large-300'
            RE3D_USER_MAX_PENDING_JOBS = '1'
            RE3D_USER_STORAGE_QUOTA_BYTES = '137438953472'
            RE3D_JOB_STORAGE_RESERVATION_BYTES = '68719476736'
            RE3D_MIN_FREE_DISK_BYTES = '80530636800'
            RE3D_JOB_TIMEOUT_SECONDS = '14400'
            UPLOAD_MAX_FILE_BYTES = '26214400'
            UPLOAD_MAX_TOTAL_BYTES = '1073741824'
        }
        worker = [ordered]@{
            RE3D_EXECUTION_PROFILE = 'gpu-large-300'
            RE3D_JOB_STORAGE_RESERVATION_BYTES = '68719476736'
            RE3D_MIN_FREE_DISK_BYTES = '80530636800'
            RE3D_RESOURCE_MONITOR_ENABLED = 'true'
            RE3D_RESOURCE_SAMPLE_INTERVAL_SECONDS = '5'
            RE3D_TASK_STORAGE_SAMPLE_INTERVAL_SECONDS = '15'
        }
    }
    if ($ProfileName -ceq 'direct-300-restricted-20') {
        foreach ($role in @('api', 'worker')) {
            $expected[$role]['RE3D_EXECUTION_PROFILE'] = 'gpu-experiment-300-20'
            $expected[$role]['RE3D_MIN_FREE_DISK_BYTES'] = '21474836480'
            $expected[$role]['RE3D_LOW_DISK_GUARD_ENABLED'] = 'true'
            $expected[$role]['RE3D_LOW_DISK_GUARD_INTERVAL_SECONDS'] = '1'
        }
    }
    if ($profile.schema_version -ne 1 -or $profile.name -cne $ProfileName) { throw "Unexpected profile format." }
    foreach ($role in @('api', 'worker')) {
        if ($profile[$role].Count -ne $expected[$role].Count) { throw "Unexpected profile keys." }
        foreach ($key in $expected[$role].Keys) {
            if ([string]$profile[$role][$key] -cne $expected[$role][$key]) { throw "Profile differs from the reviewed experiment limits." }
        }
    }
    $serviceState = @(Assert-ServicesStopped)
    $requiresBundle = Test-Path -LiteralPath $bundle
    $plans = [Collections.Generic.List[object]]::new()
    foreach ($role in @('api', 'worker')) {
        $filename = if ($role -ceq 'api') { '.env.restricted' } else { '.env.worker.restricted' }
        $target = Join-Path $project $filename
        $item = Get-Item -LiteralPath $target -ErrorAction Stop
        if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Environment target must be a regular file." }
        $stage = 'environment-validation'
        if ($Apply) {
            $stream = [IO.File]::Open($target, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
            $locks.Add($stream)
            $memory = [IO.MemoryStream]::new()
            try { $stream.CopyTo($memory); $bytes = $memory.ToArray() } finally { $memory.Dispose() }
        } else {
            $stream = $null
            $bytes = [IO.File]::ReadAllBytes($target)
        }
        $plan = Get-EnvironmentPlan -Bytes $bytes -Settings $expected[$role] -Role $role
        $plan | Add-Member -NotePropertyName path -NotePropertyValue $target
        $plan | Add-Member -NotePropertyName stream -NotePropertyValue $stream
        $plan | Add-Member -NotePropertyName acl_sddl -NotePropertyValue (Get-Acl -LiteralPath $target).Sddl
        $plans.Add($plan)
    }
    $hasChanges = @($plans | Where-Object { $_.changed }).Count -gt 0
    if ($Apply -and $hasChanges) {
        $stage = 'backup'
        $backupParent = Join-Path $project '.restricted-profile-backups'
        if (Test-Path -LiteralPath $backupParent) {
            if ((Get-Item -LiteralPath $backupParent).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Backup parent cannot be a reparse point." }
        } else {
            [void][IO.Directory]::CreateDirectory($backupParent)
        }
        $backupDirectory = Join-Path $backupParent ($ProfileName + '-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ') + '-' + [Guid]::NewGuid().ToString('N'))
        [void][IO.Directory]::CreateDirectory($backupDirectory)
        Protect-BackupDirectory -Path $backupDirectory
        foreach ($plan in $plans) {
            $backupPath = Join-Path $backupDirectory ([IO.Path]::GetFileName($plan.path))
            [IO.File]::WriteAllBytes($backupPath, $plan.original_bytes)
            # Backup bytes stay inside an already protected directory. Verify
            # the inherited ACL and owner boundary before changing either source.
            $backupAcl = Get-Acl -LiteralPath $backupPath
            $allowed = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-32-544', 'S-1-5-18')
            foreach ($rule in $backupAcl.Access) {
                if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
                    $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -cnotin $allowed) {
                    throw "Backup file has an unexpected ACL entry."
                }
            }
        }
        $null = Assert-ServicesStopped
        $stage = 'apply'
        foreach ($plan in $plans) {
            if (-not $plan.changed) { continue }
            $changedTargets.Add($plan)
            Set-LockedFileBytes -Stream $plan.stream -Bytes $plan.bytes
        }
        $stage = 'identity-verification'
        foreach ($plan in $plans) {
            if ((Get-Acl -LiteralPath $plan.path).Sddl -cne $plan.acl_sddl) { throw "Environment owner or ACL changed unexpectedly." }
        }
    }
    [ordered]@{
        profile = $ProfileName
        mode = if ($Apply) { 'apply' } else { 'dry-run' }
        changed = $hasChanges
        applied = [bool]($Apply -and $hasChanges)
        project_root = $project
        backup_directory = $backupDirectory
        requires_bundle_regeneration = [bool]$requiresBundle
        services = $serviceState
        limits = [ordered]@{ image_count_min = 3; image_count_max = 300; timeout_seconds = 14400; pending_jobs = 1 }
        environments = @($plans | ForEach-Object { [ordered]@{ role = $_.role; changes = $_.changes } })
    } | ConvertTo-Json -Depth 8
} catch {
    $rollbackFailed = $false
    foreach ($plan in $changedTargets) {
        try { Set-LockedFileBytes -Stream $plan.stream -Bytes $plan.original_bytes } catch { $rollbackFailed = $true }
    }
    $recovery = if ($rollbackFailed) { ' Automatic content rollback failed; restore the protected backups before proceeding.' } else { '' }
    throw "Direct-300 profile failed at stage '$stage'; no environment values were printed.$recovery"
} finally {
    foreach ($stream in $locks) { $stream.Dispose() }
}
