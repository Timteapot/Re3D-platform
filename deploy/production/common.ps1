function Get-Re3DProductionProjectRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

function Get-Re3DProductionPython {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)

    $venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        return $venvPython
    }
    return (Get-Command python -ErrorAction Stop).Source
}

function Assert-Re3DProductionEnvironmentFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Python
    )

    $resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    $environment = (& $Python -m dotenv -f $resolved get APP_ENV).Trim()
    if ($LASTEXITCODE -ne 0 -or $environment -cne "production") {
        throw "Production process environment must set APP_ENV=production."
    }
    return $resolved
}

function Get-Re3DFreeLoopbackPort {
    $listener = [Net.Sockets.TcpListener]::new(
        [Net.IPAddress]::Loopback,
        0
    )
    try {
        $listener.Start()
        return ([Net.IPEndPoint]$listener.LocalEndpoint).Port
    } finally {
        $listener.Stop()
    }
}
