[CmdletBinding()]
param(
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
    -Context $context
$null = Assert-Re3DRestrictedScheduledTaskDefinition `
    -Task $task `
    -Context $context

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
    throw "Scheduled task did not stop within $StopTimeoutSeconds seconds."
}

& (Join-Path $context.project_root `
    "deploy\restricted\stop-restricted-stack.ps1")

$statusScript = Join-Path $context.project_root `
    "deploy\restricted\get-restricted-stack-status.ps1"
$statusJson = & $statusScript
$status = $statusJson | ConvertFrom-Json
if ([string]$status.status -cne "stopped") {
    throw "Restricted stack remained '$($status.status)' after scheduled-task stop."
}

Write-Host "Restricted scheduled stack stopped." -ForegroundColor Green
$statusJson
