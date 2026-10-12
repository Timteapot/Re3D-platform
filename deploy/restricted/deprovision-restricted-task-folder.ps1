[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "scheduled-task-common.ps1")

Assert-Re3DRestrictedTaskAdministrator
$context = Get-Re3DRestrictedScheduledTaskContext
$service = New-Re3DTaskSchedulerService
$rootFolder = $service.GetFolder("\")
$folder = Get-Re3DRestrictedTaskFolder `
    -Service $service `
    -Context $context `
    -AllowMissing
if ($null -eq $folder) {
    Write-Host "Restricted Task Scheduler folder is not provisioned."
    return
}
Assert-Re3DRestrictedTaskFolderSecurity `
    -Folder $folder `
    -Context $context
if ([int]$folder.GetTasks(1).Count -ne 0) {
    throw "Restricted Task Scheduler folder still contains tasks. Uninstall them first."
}
if ([int]$folder.GetFolders(0).Count -ne 0) {
    throw "Restricted Task Scheduler folder contains subfolders and will not be removed."
}

$rootFolder.DeleteFolder($context.task_folder_name, 0)
Write-Host "Empty restricted Task Scheduler folder removed." -ForegroundColor Green
