[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "scheduled-task-common.ps1")

Assert-Re3DRestrictedTaskAdministrator
$mutex = Enter-Re3DRestrictedProvisioningMutex
try {
    $context = Get-Re3DRestrictedScheduledTaskContext
    $localUser = Get-Re3DRestrictedTaskLocalUser -Context $context
    $accountSid = $localUser.SID.Value

    $administratorSid = [Security.Principal.SecurityIdentifier]::new(
        "S-1-5-32-544"
    )
    $administratorMembers = @(
        Get-LocalGroupMember -SID $administratorSid -ErrorAction Stop
    )
    if ($administratorMembers.SID.Value -contains $accountSid) {
        throw "The fixed re3d-deploy account must not be a local Administrator."
    }
    Assert-Re3DNoBatchLogonDenyAssignments

    $initialMarker = Get-Re3DRestrictedBatchLogonMarker `
        -ExpectedAccountSid $accountSid
    $rightWasPresent = Test-Re3DBatchLogonRight `
        -AccountName $context.run_as_user
    $pendingOwnership = $false
    if ($null -eq $initialMarker -and -not $rightWasPresent) {
        Set-Re3DRestrictedBatchLogonMarker `
            -AccountSid $accountSid `
            -State "pending-add"
        $pendingOwnership = $true
    } elseif (
        $null -ne $initialMarker -and
        [string]$initialMarker.state -ceq "pending-add"
    ) {
        $pendingOwnership = $true
    }

    $createdFolder = $false
    $rightAddedThisRun = $false
    try {
        if (-not (Test-Re3DBatchLogonRight -AccountName $context.run_as_user)) {
            $rightAddedThisRun = [bool](
                Add-Re3DBatchLogonRight `
                    -AccountName $context.run_as_user
            )
        }
        if (-not (Test-Re3DBatchLogonRight -AccountName $context.run_as_user)) {
            throw "SeBatchLogonRight did not persist for the fixed re3d-deploy account."
        }

        $service = New-Re3DTaskSchedulerService
        $rootFolder = $service.GetFolder("\")
        $folder = Get-Re3DRestrictedTaskFolder `
            -Service $service `
            -Context $context `
            -AllowMissing
        if ($null -eq $folder) {
            $folder = $rootFolder.CreateFolder(
                $context.task_folder_name,
                (Get-Re3DRestrictedTaskFolderSddl -Context $context)
            )
            $createdFolder = $true
        }
        Assert-Re3DRestrictedTaskFolderSecurity `
            -Folder $folder `
            -Context $context

        if ($pendingOwnership) {
            Set-Re3DRestrictedBatchLogonMarker `
                -AccountSid $accountSid `
                -State "owned"
        }
    } catch {
        $provisionFailure = $_
        $rollbackFailures = [Collections.Generic.List[string]]::new()
        if ($createdFolder) {
            try {
                $rootFolder.DeleteFolder($context.task_folder_name, 0)
            } catch {
                $rollbackFailures.Add(
                    "task folder rollback: $($_.Exception.Message)"
                )
            }
        }
        $rollbackRight = $pendingOwnership -or $rightAddedThisRun
        $rightRollbackComplete = -not $rollbackRight
        if ($rollbackRight) {
            try {
                $null = Remove-Re3DBatchLogonRight `
                    -AccountName $context.run_as_user
                if (Test-Re3DBatchLogonRight `
                    -AccountName $context.run_as_user
                ) {
                    throw "SeBatchLogonRight remained after rollback."
                }
                $rightRollbackComplete = $true
            } catch {
                $rollbackFailures.Add(
                    "batch-logon rollback: $($_.Exception.Message)"
                )
            }
        }
        if ($pendingOwnership -and $rightRollbackComplete) {
            try {
                Remove-Re3DRestrictedBatchLogonMarker `
                    -ExpectedAccountSid $accountSid
            } catch {
                $rollbackFailures.Add(
                    "batch-logon marker rollback: $($_.Exception.Message)"
                )
            }
        }
        if ($rollbackFailures.Count -ne 0) {
            throw (
                "Restricted task provisioning failed: " +
                $provisionFailure.Exception.Message +
                ". Rollback was incomplete: " +
                ($rollbackFailures -join " | ")
            )
        }
        throw $provisionFailure
    }

    $marker = Get-Re3DRestrictedBatchLogonMarker `
        -ExpectedAccountSid $accountSid
    $rightOwnership = if ($null -ne $marker) {
        "managed-by-re3d"
    } else {
        "pre-existing"
    }
    if ($createdFolder) {
        Write-Host "Restricted Task Scheduler folder provisioned." -ForegroundColor Green
    } else {
        Write-Host "Restricted Task Scheduler folder is already provisioned."
    }
    Write-Host "Folder: $($context.task_folder_path)"
    Write-Host "Allowed identities: SYSTEM, Administrators, $($context.run_as_user)"
    Write-Host "SeBatchLogonRight: present ($rightOwnership)"
} finally {
    Exit-Re3DRestrictedProvisioningMutex -Mutex $mutex
}
