[CmdletBinding()]
param(
    [switch]$ForceUnsafeDefinition,
    [ValidateRange(5, 60)][int]$StopTimeoutSeconds = 20
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
    -Context $context `
    -AllowMissing
if ($null -eq $task) {
    Write-Host "Restricted scheduled task is not installed."
    return
}
$definitionValid = $false
try {
    $null = Assert-Re3DRestrictedScheduledTaskDefinition `
        -Task $task `
        -Context $context
    $definitionValid = $true
} catch {
    if (-not $ForceUnsafeDefinition) {
        throw
    }
    Write-Warning (
        "Removing the fixed restricted task despite definition drift: " +
        $_.Exception.Message
    )
}

if ($definitionValid) {
    & (Join-Path $context.project_root `
        "deploy\restricted\stop-restricted-scheduled-task.ps1") `
        -StopTimeoutSeconds $StopTimeoutSeconds
} else {
    if ([int]$task.State -in @(2, 4)) {
        $task.Stop(0)
    }
    $deadline = [DateTime]::UtcNow.AddSeconds($StopTimeoutSeconds)
    do {
        $task = Get-Re3DRestrictedRegisteredTask `
            -Folder $folder `
            -Context $context
        if ([int]$task.State -notin @(2, 4)) {
            break
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    if ([int]$task.State -in @(2, 4)) {
        throw "Unsafe scheduled task did not stop within $StopTimeoutSeconds seconds."
    }
    & (Join-Path $context.project_root `
        "deploy\restricted\stop-restricted-stack.ps1")
}
$folder.DeleteTask($context.task_name, 0)

Write-Host "Restricted scheduled task uninstalled. Data and logs were retained." -ForegroundColor Green
