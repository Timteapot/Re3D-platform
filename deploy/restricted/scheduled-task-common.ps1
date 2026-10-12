. (Join-Path $PSScriptRoot "common.ps1")

function Get-Re3DRestrictedScheduledTaskContext {
    $projectRoot = Get-Re3DRestrictedProjectRoot
    $powerShellPath = Join-Path `
        $env:SystemRoot `
        "System32\WindowsPowerShell\v1.0\powershell.exe"
    $startScript = Join-Path $projectRoot `
        "deploy\restricted\start-restricted-stack.ps1"
    $arguments = (
        '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass ' +
        "-File `"$startScript`" -SkipWebBuild -StayAttached"
    )

    return [pscustomobject][ordered]@{
        task_folder_name = "Re3D"
        task_folder_path = "\Re3D"
        task_name = "RestrictedStack"
        task_full_path = "\Re3D\RestrictedStack"
        run_as_user = "$env:COMPUTERNAME\re3d-deploy"
        project_root = $projectRoot
        powershell_path = $powerShellPath
        start_script = $startScript
        arguments = $arguments
    }
}

function Get-Re3DRestrictedAccountSid {
    param([Parameter(Mandatory = $true)][string]$AccountName)

    if ($AccountName -match '^S-\d-(?:\d+-){1,14}\d+$') {
        try {
            return ([Security.Principal.SecurityIdentifier]::new(
                $AccountName
            )).Value
        } catch {
            throw "Scheduled-task account SID is invalid: $AccountName"
        }
    }

    try {
        $account = [Security.Principal.NTAccount]::new($AccountName)
        return $account.Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
    } catch {
        throw "Could not resolve scheduled-task account '$AccountName' to a Windows SID."
    }
}

function Assert-Re3DRestrictedTaskOwnerIdentity {
    $context = Get-Re3DRestrictedScheduledTaskContext
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $expectedSid = Get-Re3DRestrictedAccountSid `
        -AccountName $context.run_as_user
    if ($identity.User.Value -cne $expectedSid) {
        throw (
            "This passwordless scheduled-task operation must run as " +
            "'$($context.run_as_user)' itself, not as '$($identity.Name)'."
        )
    }
}

function Assert-Re3DRestrictedTaskAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )) {
        throw "This task-folder operation requires an elevated Administrator PowerShell."
    }
}

function New-Re3DTaskSchedulerService {
    try {
        $service = New-Object -ComObject "Schedule.Service"
        $service.Connect()
        return $service
    } catch {
        throw "Could not connect to the Windows Task Scheduler service: $($_.Exception.Message)"
    }
}

function Get-Re3DRestrictedTaskFolder {
    param(
        [Parameter(Mandatory = $true)]$Service,
        [Parameter(Mandatory = $true)]$Context,
        [switch]$AllowMissing
    )

    try {
        return $Service.GetFolder($Context.task_folder_path)
    } catch {
        if (
            $_.Exception.HResult -in @(-2147024894, -2147024893) -and
            $AllowMissing
        ) {
            return $null
        }
        if ($_.Exception.HResult -in @(-2147024894, -2147024893)) {
            throw (
                "Restricted Task Scheduler folder is not provisioned. " +
                "Run provision-restricted-task-folder.ps1 as Administrator."
            )
        }
        throw
    }
}

function Get-Re3DRestrictedRegisteredTask {
    param(
        [Parameter(Mandatory = $true)]$Folder,
        [Parameter(Mandatory = $true)]$Context,
        [switch]$AllowMissing
    )

    try {
        return $Folder.GetTask($Context.task_name)
    } catch {
        if (
            $_.Exception.HResult -in @(-2147024894, -2147024893) -and
            $AllowMissing
        ) {
            return $null
        }
        if ($_.Exception.HResult -in @(-2147024894, -2147024893)) {
            throw "Restricted scheduled task is not installed."
        }
        throw
    }
}

function Get-Re3DRestrictedTaskFolderSddl {
    param([Parameter(Mandatory = $true)]$Context)

    $runAsSid = Get-Re3DRestrictedAccountSid `
        -AccountName $Context.run_as_user
    return (
        "D:P" +
        "(A;OICI;FA;;;SY)" +
        "(A;OICI;FA;;;BA)" +
        "(A;OICI;FA;;;$runAsSid)"
    )
}

function Assert-Re3DRestrictedTaskFolderSecurity {
    param(
        [Parameter(Mandatory = $true)]$Folder,
        [Parameter(Mandatory = $true)]$Context
    )

    try {
        $descriptor = [Security.AccessControl.RawSecurityDescriptor]::new(
            [string]$Folder.GetSecurityDescriptor(4)
        )
    } catch {
        throw "Could not parse the restricted task-folder DACL: $($_.Exception.Message)"
    }
    $descriptorFlags = [int]$descriptor.ControlFlags
    $protectedDacl = [int][Security.AccessControl.ControlFlags]::DiscretionaryAclProtected
    if (($descriptorFlags -band $protectedDacl) -eq 0) {
        throw "Restricted Task Scheduler folder DACL must be protected from inheritance."
    }

    $runAsSid = Get-Re3DRestrictedAccountSid `
        -AccountName $Context.run_as_user
    $expectedSids = @{
        "S-1-5-18" = $false
        "S-1-5-32-544" = $false
        $runAsSid = $false
    }
    $objectInherit = [int][Security.AccessControl.AceFlags]::ObjectInherit
    $containerInherit = [int][Security.AccessControl.AceFlags]::ContainerInherit
    $inheritOnly = [int][Security.AccessControl.AceFlags]::InheritOnly
    $fullControlMask = 0x001f01ff
    foreach ($ace in $descriptor.DiscretionaryAcl) {
        if (
            $ace -isnot [Security.AccessControl.CommonAce] -or
            $ace.AceQualifier -ne [Security.AccessControl.AceQualifier]::AccessAllowed
        ) {
            throw "Restricted Task Scheduler folder contains a non-allow or unsupported ACE."
        }
        $sid = $ace.SecurityIdentifier.Value
        if (-not $expectedSids.ContainsKey($sid)) {
            throw "Restricted Task Scheduler folder grants an unexpected identity: $sid"
        }
        if ([int]$ace.AccessMask -ne $fullControlMask) {
            throw "Restricted Task Scheduler folder does not grant reviewed full control."
        }
        $aceFlags = [int]$ace.AceFlags
        if (
            ($aceFlags -band $objectInherit) -eq 0 -or
            ($aceFlags -band $containerInherit) -eq 0 -or
            ($aceFlags -band $inheritOnly) -ne 0
        ) {
            throw "Restricted Task Scheduler folder ACE inheritance is not OI/CI."
        }
        if ($expectedSids[$sid]) {
            throw "Restricted Task Scheduler folder contains a duplicate identity grant."
        }
        $expectedSids[$sid] = $true
    }
    foreach ($entry in $expectedSids.GetEnumerator()) {
        if (-not $entry.Value) {
            throw "Restricted Task Scheduler folder is missing required SID $($entry.Key)."
        }
    }
}

function ConvertFrom-Re3DScheduledTaskDuration {
    param(
        [AllowNull()][object]$Value,
        [Parameter(Mandatory = $true)][string]$SettingName
    )

    if ($null -eq $Value -or [string]::IsNullOrWhiteSpace([string]$Value)) {
        return [TimeSpan]::Zero
    }
    if ($Value -is [TimeSpan]) {
        return [TimeSpan]$Value
    }
    try {
        return [Xml.XmlConvert]::ToTimeSpan([string]$Value)
    } catch {
        throw "Scheduled-task $SettingName is not a valid duration."
    }
}

function Format-Re3DScheduledTaskResult {
    param([Parameter(Mandatory = $true)][object]$Value)

    $signedValue = [long]$Value
    $unsignedValue = $signedValue -band 0xffffffffL
    return "$signedValue (0x$($unsignedValue.ToString('X8')))"
}

function Get-Re3DScheduledTaskStateName {
    param([Parameter(Mandatory = $true)][int]$State)

    switch ($State) {
        0 { return "unknown" }
        1 { return "disabled" }
        2 { return "queued" }
        3 { return "ready" }
        4 { return "running" }
        default { return "invalid-$State" }
    }
}

function Assert-Re3DRestrictedScheduledTaskDefinition {
    param(
        [Parameter(Mandatory = $true)]$Task,
        [Parameter(Mandatory = $true)]$Context
    )

    $definition = $Task.Definition
    if ([int]$definition.Actions.Count -ne 1) {
        throw "Scheduled task must contain exactly one action."
    }
    $action = $definition.Actions.Item(1)
    if ([int]$action.Type -ne 0) {
        throw "Scheduled task must contain one executable action."
    }
    if (-not [string]::Equals(
        [string]$action.Path,
        [string]$Context.powershell_path,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Scheduled-task executable does not match the reviewed Windows PowerShell path."
    }
    if ([string]$action.Arguments -cne [string]$Context.arguments) {
        throw "Scheduled-task arguments do not match the reviewed restricted-stack command."
    }
    if (-not [string]::Equals(
        [string]$action.WorkingDirectory,
        [string]$Context.project_root,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Scheduled-task working directory does not match the platform checkout."
    }

    $expectedSid = Get-Re3DRestrictedAccountSid `
        -AccountName $Context.run_as_user
    $actualSid = Get-Re3DRestrictedAccountSid `
        -AccountName ([string]$definition.Principal.UserId)
    if ($actualSid -cne $expectedSid) {
        throw "Scheduled-task principal does not match the fixed local re3d-deploy account."
    }
    if ([int]$definition.Principal.LogonType -ne 2) {
        throw "Scheduled-task principal must use S4U logon without a stored password."
    }
    if ([int]$definition.Principal.RunLevel -ne 0) {
        throw "Scheduled-task principal must use a limited token."
    }

    $settings = $definition.Settings
    if (-not [bool]$settings.Enabled) {
        throw "Scheduled task must remain enabled for explicit starts."
    }
    if ([bool]$settings.Hidden) {
        throw "Scheduled task must remain visible to operators."
    }
    if ([int]$settings.MultipleInstances -ne 2) {
        throw "Scheduled task must refuse parallel stack instances."
    }
    $executionTimeLimit = ConvertFrom-Re3DScheduledTaskDuration `
        -Value $settings.ExecutionTimeLimit `
        -SettingName "ExecutionTimeLimit"
    if ($executionTimeLimit -ne [TimeSpan]::Zero) {
        throw "Scheduled task must not impose a runtime limit on reconstruction jobs."
    }
    if ([int]$definition.Triggers.Count -ne 0) {
        throw "Scheduled task must remain on-demand and contain no triggers."
    }
    if ([int]$settings.RestartCount -ne 0) {
        throw "Scheduled task must not automatically restart after failure."
    }
    $restartInterval = ConvertFrom-Re3DScheduledTaskDuration `
        -Value $settings.RestartInterval `
        -SettingName "RestartInterval"
    if ($restartInterval -ne [TimeSpan]::Zero) {
        throw "Scheduled task must not define an automatic restart interval."
    }
    if (-not [bool]$settings.AllowDemandStart) {
        throw "Scheduled task must allow an explicit operator start."
    }
    if ([bool]$settings.StartWhenAvailable) {
        throw "Scheduled task must not start implicitly after a missed schedule."
    }

    return $Task
}
