param(
    [string]$Image = "axllent/mailpit:v1.31.3"
)

$ErrorActionPreference = "Stop"
$containerName = "re3d-platform-mailpit"
$smtpAddress = "127.0.0.1:1025"
$uiUrl = "http://127.0.0.1:8025"

function Resolve-DockerExecutable {
    $installed = (
        Get-Command docker -ErrorAction SilentlyContinue | Select-Object -First 1
    ).Source
    $bundled = "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin\docker.exe"
    if ($installed) {
        return $installed
    }
    if (Test-Path -LiteralPath $bundled -PathType Leaf) {
        return $bundled
    }
    throw "Docker CLI was not found in PATH or the Docker Desktop installation."
}

$docker = Resolve-DockerExecutable
$existing = & $docker ps -a `
    --filter "name=^/$containerName$" `
    --format "{{.Names}}"
if ($LASTEXITCODE -ne 0) {
    throw "Docker is unavailable. Start Docker Desktop and retry."
}

if ($existing -eq $containerName) {
    $configuredImage = & $docker inspect `
        --format "{{.Config.Image}}" `
        $containerName
    if ($LASTEXITCODE -ne 0 -or $configuredImage -ne $Image) {
        throw "Existing container $containerName does not use expected image $Image. Remove it explicitly before retrying."
    }
    $running = & $docker inspect `
        --format "{{.State.Running}}" `
        $containerName
    if ($LASTEXITCODE -ne 0) {
        throw "Could not inspect the existing Mailpit container."
    }
    if ($running -ne "true") {
        & $docker start $containerName | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Could not start the existing Mailpit container."
        }
    }
} else {
    & $docker run `
        --detach `
        --name $containerName `
        --restart unless-stopped `
        --publish "127.0.0.1:1025:1025" `
        --publish "127.0.0.1:8025:8025" `
        --env "MP_MAX_MESSAGES=500" `
        $Image | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker could not create the Mailpit container. Check whether ports 1025 and 8025 are available."
    }
}

$portMappings = @(& $docker port $containerName)
if ($LASTEXITCODE -ne 0) {
    throw "Could not inspect Mailpit port mappings."
}
$expectedMappings = @(
    "1025/tcp -> 127.0.0.1:1025",
    "8025/tcp -> 127.0.0.1:8025"
)
foreach ($mapping in $expectedMappings) {
    if ($portMappings -notcontains $mapping) {
        throw "Mailpit must bind SMTP and UI ports only to 127.0.0.1. Missing mapping: $mapping"
    }
}

$ready = $false
for ($attempt = 1; $attempt -le 30; $attempt++) {
    try {
        $response = Invoke-WebRequest `
            -Uri $uiUrl `
            -Method Get `
            -TimeoutSec 2 `
            -UseBasicParsing
        if ($response.StatusCode -eq 200) {
            $ready = $true
            break
        }
    } catch {
        Start-Sleep -Milliseconds 500
    }
}
if (-not $ready) {
    throw "Mailpit did not become ready at $uiUrl. Inspect it with: docker logs $containerName"
}

Write-Host "Mailpit is ready."
Write-Host "SMTP: $smtpAddress"
Write-Host "Web UI: $uiUrl"
Write-Host "Captured messages remain inside container $containerName until it is removed."
