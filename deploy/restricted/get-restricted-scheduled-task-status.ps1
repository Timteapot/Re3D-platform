[CmdletBinding()]
param(
    [switch]$RequireReady
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "scheduled-task-common.ps1")

$context = Get-Re3DRestrictedScheduledTaskContext
$service = New-Re3DTaskSchedulerService
$folder = Get-Re3DRestrictedTaskFolder `
    -Service $service `
    -Context $context `
    -AllowMissing
$folderProvisioned = $null -ne $folder
$folderSecurityValid = $false
$task = $null
$taskInstalled = $false
$definitionValid = $false
$folderSecurityError = $null
$definitionError = $null
if ($folderProvisioned) {
    try {
        Assert-Re3DRestrictedTaskFolderSecurity `
            -Folder $folder `
            -Context $context
        $folderSecurityValid = $true
    } catch {
        $folderSecurityError = $_.Exception.Message
    }
    if ($folderSecurityValid) {
        try {
            $task = Get-Re3DRestrictedRegisteredTask `
                -Folder $folder `
                -Context $context `
                -AllowMissing
            $taskInstalled = $null -ne $task
            if ($taskInstalled) {
                $null = Assert-Re3DRestrictedScheduledTaskDefinition `
                    -Task $task `
                    -Context $context
                $definitionValid = $true
            }
        } catch {
            $definitionError = $_.Exception.Message
        }
    }
}

$stackStatusScript = Join-Path $context.project_root `
    "deploy\restricted\get-restricted-stack-status.ps1"
$stack = (& $stackStatusScript) | ConvertFrom-Json
$taskState = if ($taskInstalled) {
    Get-Re3DScheduledTaskStateName -State ([int]$task.State)
} else {
    $null
}
$ready = (
    $folderSecurityValid -and
    $taskInstalled -and
    $definitionValid -and
    $taskState -ceq "running" -and
    [string]$stack.status -eq "ready"
)
$overallStatus = if ($ready) {
    "ready"
} elseif (-not $folderProvisioned -or -not $taskInstalled) {
    if ($folderProvisioned -and (
        -not $folderSecurityValid -or
        -not [string]::IsNullOrWhiteSpace($definitionError)
    )) {
        "unsafe-definition"
    } elseif ([string]$stack.status -ne "stopped") {
        "orphaned-stack"
    } else {
        "not-installed"
    }
} elseif (-not $folderSecurityValid -or -not $definitionValid) {
    "unsafe-definition"
} elseif ($taskState -in @("running", "queued")) {
    "starting-or-degraded"
} elseif ([string]$stack.status -ne "stopped") {
    "orphaned-stack"
} else {
    "stopped"
}

$report = [ordered]@{
    status = $overallStatus
    task_folder = [ordered]@{
        path = $context.task_folder_path
        provisioned = $folderProvisioned
        security_valid = $folderSecurityValid
        security_error = $folderSecurityError
    }
    task = [ordered]@{
        path = $context.task_full_path
        installed = $taskInstalled
        definition_valid = $definitionValid
        definition_error = $definitionError
        state = $taskState
        run_as_user = if ($taskInstalled) {
            [string]$task.Definition.Principal.UserId
        } else {
            $null
        }
        logon_type = if ($taskInstalled) {
            [int]$task.Definition.Principal.LogonType
        } else {
            $null
        }
        run_level = if ($taskInstalled) {
            [int]$task.Definition.Principal.RunLevel
        } else {
            $null
        }
        last_task_result = if ($taskInstalled) {
            [long]$task.LastTaskResult
        } else {
            $null
        }
        last_task_result_hex = if ($taskInstalled) {
            $result = [long]$task.LastTaskResult -band 0xffffffffL
            "0x$($result.ToString('X8'))"
        } else {
            $null
        }
    }
    stack = $stack
}
$report | ConvertTo-Json -Depth 8
if ($RequireReady -and -not $ready) {
    throw "Restricted scheduled stack is not ready."
}
