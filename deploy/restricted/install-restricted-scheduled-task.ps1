[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "scheduled-task-common.ps1")

Assert-Re3DRestrictedTaskOwnerIdentity
$context = Get-Re3DRestrictedScheduledTaskContext
if (-not (Test-Path -LiteralPath $context.start_script -PathType Leaf)) {
    throw "Restricted stack start script was not found: $($context.start_script)"
}
$webEntry = Join-Path $context.project_root "apps\web\dist\index.html"
if (-not (Test-Path -LiteralPath $webEntry -PathType Leaf)) {
    throw "Restricted frontend output is missing. Build it before task installation."
}
$localUser = Get-LocalUser -Name "re3d-deploy" -ErrorAction Stop
if (-not $localUser.Enabled) {
    throw "The fixed local scheduled-task account 're3d-deploy' is disabled."
}

$service = New-Re3DTaskSchedulerService
$folder = Get-Re3DRestrictedTaskFolder `
    -Service $service `
    -Context $context
Assert-Re3DRestrictedTaskFolderSecurity `
    -Folder $folder `
    -Context $context
if (Get-Re3DRestrictedRegisteredTask `
    -Folder $folder `
    -Context $context `
    -AllowMissing
) {
    throw (
        "Scheduled task '$($context.task_full_path)' already exists. " +
        "Refusing to overwrite it; inspect or uninstall it explicitly."
    )
}

$definition = $service.NewTask(0)
$definition.RegistrationInfo.Author = $context.run_as_user
$definition.RegistrationInfo.Description = (
    "On-demand loopback-only Re3D restricted stack. " +
    "No password is stored; start and stop explicitly."
)
$definition.Principal.UserId = $context.run_as_user
$definition.Principal.LogonType = 2
$definition.Principal.RunLevel = 0
$definition.Settings.Enabled = $true
$definition.Settings.Hidden = $false
$definition.Settings.AllowDemandStart = $true
$definition.Settings.StartWhenAvailable = $false
$definition.Settings.DisallowStartIfOnBatteries = $false
$definition.Settings.StopIfGoingOnBatteries = $false
$definition.Settings.MultipleInstances = 2
$definition.Settings.ExecutionTimeLimit = "PT0S"
$definition.Settings.RestartCount = 0
$action = $definition.Actions.Create(0)
$action.Path = $context.powershell_path
$action.Arguments = $context.arguments
$action.WorkingDirectory = $context.project_root

$created = $false
try {
    $task = $folder.RegisterTaskDefinition(
        $context.task_name,
        $definition,
        2,
        $context.run_as_user,
        $null,
        2,
        $null
    )
    $created = $true
    $null = Assert-Re3DRestrictedScheduledTaskDefinition `
        -Task $task `
        -Context $context
} catch {
    if ($created) {
        try {
            $folder.DeleteTask($context.task_name, 0)
        } catch {
            Write-Warning (
                "Could not roll back the new scheduled task: " +
                $_.Exception.Message
            )
        }
    }
    throw
}

Write-Host "Restricted scheduled task installed." -ForegroundColor Green
Write-Host "Task: $($context.task_full_path)"
Write-Host "Principal: $($context.run_as_user) (S4U, Limited)"
Write-Host "Startup trigger: none (explicit start only)"
Write-Host "Automatic restart: disabled"
