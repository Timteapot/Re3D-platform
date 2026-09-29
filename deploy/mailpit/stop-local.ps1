param(
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$containerName = "re3d-platform-mailpit"
$installedDocker = (
    Get-Command docker -ErrorAction SilentlyContinue | Select-Object -First 1
).Source
$bundledDocker = "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin\docker.exe"
$docker = if ($installedDocker) {
    $installedDocker
} elseif (Test-Path -LiteralPath $bundledDocker -PathType Leaf) {
    $bundledDocker
} else {
    throw "Docker CLI was not found in PATH or the Docker Desktop installation."
}

$existing = & $docker ps -a `
    --filter "name=^/$containerName$" `
    --format "{{.Names}}"
if ($LASTEXITCODE -ne 0) {
    throw "Docker is unavailable. Start Docker Desktop and retry."
}
if ($existing -ne $containerName) {
    Write-Host "Mailpit container does not exist."
    exit 0
}

$running = & $docker inspect --format "{{.State.Running}}" $containerName
if ($running -eq "true") {
    & $docker stop $containerName | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not stop the Mailpit container."
    }
}

if ($Remove) {
    & $docker rm $containerName | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not remove the Mailpit container."
    }
    Write-Host "Mailpit container and its captured development messages were removed."
} else {
    Write-Host "Mailpit stopped. Captured messages remain in the stopped container."
}
