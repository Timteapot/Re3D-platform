[CmdletBinding()]
param(
    [ValidateRange(10, 300)][int]$ReadyTimeoutSeconds = 120
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "scheduled-task-common.ps1")

Assert-Re3DRestrictedTaskOwnerIdentity
$context = Get-Re3DRestrictedScheduledTaskContext
$service = New-Re3DTaskSchedulerService
$folder = Get-Re3DRestrictedTaskFolder `
    -Service $service `
    -Context $context
Assert-Re3DRestrictedTaskFolderSecurity `
    -Folder $folder `
    -Context $context
$task = Get-Re3DRestrictedRegisteredTask `
    -Folder $folder `
    -Context $context
$null = Assert-Re3DRestrictedScheduledTaskDefinition `
    -Task $task `
    -Context $context

$dispatchObserved = [int]$task.State -in @(2, 4)
$observedRunning = [int]$task.State -eq 4
$startRequestedAt = [DateTime]::UtcNow
if (-not $dispatchObserved) {
    if ([int]$task.State -eq 1) {
        throw "Restricted scheduled task is disabled."
    }
    $null = $task.Run($null)
}

$statusScript = Join-Path $context.project_root `
    "deploy\restricted\get-restricted-scheduled-task-status.ps1"
$deadline = [DateTime]::UtcNow.AddSeconds($ReadyTimeoutSeconds)
while ([DateTime]::UtcNow -lt $deadline) {
    $task = Get-Re3DRestrictedRegisteredTask `
        -Folder $folder `
        -Context $context
    $taskState = [int]$task.State
    if ($taskState -eq 4) {
        $dispatchObserved = $true
        $observedRunning = $true
    } elseif ($taskState -eq 2) {
        $dispatchObserved = $true
    } elseif ($dispatchObserved) {
        $lastTaskResult = Format-Re3DScheduledTaskResult `
            -Value $task.LastTaskResult
        throw (
            "Scheduled task stopped before the stack became ready; " +
            "LastTaskResult=$lastTaskResult."
        )
    } elseif (
        [DateTime]::UtcNow.Subtract($startRequestedAt).TotalSeconds -ge 5
    ) {
        $lastTaskResult = Format-Re3DScheduledTaskResult `
            -Value $task.LastTaskResult
        throw (
            "Scheduled task did not enter Running state; " +
            "state=$(Get-Re3DScheduledTaskStateName -State $taskState); " +
            "LastTaskResult=$lastTaskResult."
        )
    }

    if ($observedRunning) {
        $status = (& $statusScript) | ConvertFrom-Json
        if ([string]$status.status -ceq "ready") {
            $finalStatus = & $statusScript -RequireReady
            Write-Host "Restricted scheduled stack is ready." -ForegroundColor Green
            $finalStatus
            return
        }
        if ([string]$status.status -in @(
            "unsafe-definition",
            "not-installed",
            "orphaned-stack",
            "stopped"
        )) {
            throw (
                "Restricted scheduled stack entered terminal state " +
                "'$($status.status)' before becoming ready."
            )
        }
    }
    Start-Sleep -Seconds 1
}

$lastTaskResult = Format-Re3DScheduledTaskResult `
    -Value $task.LastTaskResult
throw (
    "Restricted scheduled stack did not become ready within " +
    "$ReadyTimeoutSeconds seconds; LastTaskResult=$lastTaskResult."
)
