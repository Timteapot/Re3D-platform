[CmdletBinding()]
param(
    [switch]$RequireReady,
    [string]$StateRoot = "D:\3Dreconstruction\Re3D-data\_services\restricted-stack"
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "process-common.ps1")

$components = [ordered]@{}
foreach ($component in @("api", "worker", "caddy")) {
    $components[$component] = Get-Re3DRestrictedManagedProcessStatus `
        -StateRoot $StateRoot `
        -Component $component
}

$mailpitSmtp = Test-Re3DRestrictedTcpPort -HostName "127.0.0.1" -Port 1025
$mailpitWeb = Test-Re3DRestrictedTcpPort -HostName "127.0.0.1" -Port 8025
$apiHealth = $false
$edgeHealth = $false
try {
    $response = Invoke-WebRequest `
        -Uri "http://127.0.0.1:8000/health/live" `
        -TimeoutSec 2 `
        -UseBasicParsing
    $payload = $response.Content | ConvertFrom-Json
    $apiHealth = $response.StatusCode -eq 200 -and $payload.environment -ceq "restricted"
} catch {}
try {
    $response = Invoke-WebRequest `
        -Uri "http://127.0.0.1:8080/health/live" `
        -TimeoutSec 2 `
        -UseBasicParsing
    $payload = $response.Content | ConvertFrom-Json
    $edgeHealth = $response.StatusCode -eq 200 -and $payload.environment -ceq "restricted"
} catch {}

$managedReady = @($components.Values | Where-Object { $_.state -ne "running" }).Count -eq 0
$ready = $managedReady -and $mailpitSmtp -and $mailpitWeb -and $apiHealth -and $edgeHealth
$anyManaged = @($components.Values | Where-Object { $_.state -ne "stopped" }).Count -gt 0
$overall = if ($ready) { "ready" } elseif ($anyManaged -or $mailpitSmtp -or $mailpitWeb) { "degraded" } else { "stopped" }

$report = [ordered]@{
    status = $overall
    environment = "restricted"
    network_scope = "loopback-only"
    components = $components
    endpoints = [ordered]@{
        api_health = $apiHealth
        edge_health = $edgeHealth
        mailpit_smtp = $mailpitSmtp
        mailpit_web = $mailpitWeb
    }
    web_url = "http://127.0.0.1:8080"
    mail_url = "http://127.0.0.1:8025"
}
$report | ConvertTo-Json -Depth 5
if ($RequireReady -and -not $ready) {
    throw "Restricted stack is not ready."
}

