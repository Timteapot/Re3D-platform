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
if ($null -ne $folder) {
    Assert-Re3DRestrictedTaskFolderSecurity `
        -Folder $folder `
        -Context $context
    Write-Host "Restricted Task Scheduler folder is already provisioned."
    Write-Host "Folder: $($context.task_folder_path)"
    return
}

$created = $false
try {
    $folder = $rootFolder.CreateFolder(
        $context.task_folder_name,
        (Get-Re3DRestrictedTaskFolderSddl -Context $context)
    )
    $created = $true
    Assert-Re3DRestrictedTaskFolderSecurity `
        -Folder $folder `
        -Context $context
} catch {
    if ($created) {
        try {
            $rootFolder.DeleteFolder($context.task_folder_name, 0)
        } catch {
            Write-Warning (
                "Could not roll back the new Task Scheduler folder: " +
                $_.Exception.Message
            )
        }
    }
    throw
}

Write-Host "Restricted Task Scheduler folder provisioned." -ForegroundColor Green
Write-Host "Folder: $($context.task_folder_path)"
Write-Host "Allowed identities: SYSTEM, Administrators, $($context.run_as_user)"
