[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "scheduled-task-common.ps1")
. (Join-Path $PSScriptRoot "batch-logon-right-common.ps1")

Assert-Re3DRestrictedTaskAdministrator
$mutex = Enter-Re3DRestrictedProvisioningMutex
try {
    $context = Get-Re3DRestrictedScheduledTaskContext
    $localUser = Get-Re3DRestrictedTaskLocalUser -Context $context
    $accountSid = $localUser.SID.Value
    $marker = Get-Re3DRestrictedBatchLogonMarker `
        -ExpectedAccountSid $accountSid

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
        if ([int]$folder.GetTasks(1).Count -ne 0) {
            throw "Restricted Task Scheduler folder still contains tasks. Uninstall them first."
        }
        if ([int]$folder.GetFolders(0).Count -ne 0) {
            throw "Restricted Task Scheduler folder contains subfolders and will not be removed."
        }
        $rootFolder.DeleteFolder($context.task_folder_name, 0)
        Write-Host "Empty restricted Task Scheduler folder removed." -ForegroundColor Green
    } else {
        Write-Host "Restricted Task Scheduler folder is not provisioned."
    }

    if ($null -ne $marker) {
        $null = Remove-Re3DBatchLogonRight `
            -AccountName $context.run_as_user
        if (Test-Re3DBatchLogonRight -AccountName $context.run_as_user) {
            throw (
                "Managed SeBatchLogonRight remained; the ownership marker " +
                "was retained for recovery."
            )
        }
        Remove-Re3DRestrictedBatchLogonMarker `
            -ExpectedAccountSid $accountSid
        Write-Host "Managed SeBatchLogonRight removed." -ForegroundColor Green
    } elseif (
        Test-Re3DBatchLogonRight -AccountName $context.run_as_user
    ) {
        Write-Host "Pre-existing SeBatchLogonRight retained."
    } else {
        Write-Host "SeBatchLogonRight was already absent."
    }
} finally {
    Exit-Re3DRestrictedProvisioningMutex -Mutex $mutex
}
