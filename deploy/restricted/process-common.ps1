. (Join-Path $PSScriptRoot "common.ps1")

function Get-Re3DRestrictedStackStateRoot {
    return "D:\3Dreconstruction\Re3D-data\_services\restricted-stack"
}

function Get-Re3DRestrictedCanonicalPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return [IO.Path]::GetFullPath($Path).TrimEnd('\')
}

function Get-Re3DRestrictedProcessStatePath {
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$Component
    )
    return Join-Path $StateRoot "$Component.process.json"
}

function Get-Re3DRestrictedManagedProcessStatus {
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$Component
    )

    $statePath = Get-Re3DRestrictedProcessStatePath -StateRoot $StateRoot -Component $Component
    if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
        return [pscustomobject][ordered]@{
            component = $Component
            state = "stopped"
            pid = $null
            identity_valid = $false
        }
    }

    try {
        $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
        if (
            $state.schema_version -cne "1.0" -or
            $state.component -cne $Component -or
            [int]$state.pid -le 0 -or
            [string]::IsNullOrWhiteSpace([string]$state.process_path) -or
            [long]$state.start_time_utc_ticks -le 0
        ) {
            throw "invalid state fields"
        }
    } catch {
        return [pscustomobject][ordered]@{
            component = $Component
            state = "invalid-state"
            pid = $null
            identity_valid = $false
        }
    }

    $process = Get-Process -Id ([int]$state.pid) -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return [pscustomobject][ordered]@{
            component = $Component
            state = "stale"
            pid = [int]$state.pid
            identity_valid = $false
        }
    }

    try {
        $actualPath = Get-Re3DRestrictedCanonicalPath -Path $process.Path
        $expectedPath = Get-Re3DRestrictedCanonicalPath -Path ([string]$state.process_path)
        $actualTicks = $process.StartTime.ToUniversalTime().Ticks
    } catch {
        return [pscustomobject][ordered]@{
            component = $Component
            state = "unverifiable"
            pid = [int]$state.pid
            identity_valid = $false
        }
    }

    $valid = $actualPath -ieq $expectedPath -and $actualTicks -eq ([long]$state.start_time_utc_ticks)
    return [pscustomobject][ordered]@{
        component = $Component
        state = if ($valid) { "running" } else { "identity-mismatch" }
        pid = [int]$state.pid
        identity_valid = $valid
    }
}

function Start-Re3DRestrictedManagedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$Component,
        [Parameter(Mandatory = $true)][string]$ProcessPath,
        [Parameter(Mandatory = $true)][string]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )

    New-Item -ItemType Directory -Path $StateRoot -Force | Out-Null
    $statePath = Get-Re3DRestrictedProcessStatePath -StateRoot $StateRoot -Component $Component
    $status = Get-Re3DRestrictedManagedProcessStatus -StateRoot $StateRoot -Component $Component
    if ($status.state -eq "running") {
        throw "$Component is already running with PID $($status.pid)."
    }
    if ($status.state -in @("invalid-state", "unverifiable", "identity-mismatch")) {
        throw "$Component has unsafe process state '$($status.state)'. Inspect $statePath before retrying."
    }
    if ($status.state -eq "stale") {
        Remove-Item -LiteralPath $statePath
    }

    $stdoutPath = Join-Path $StateRoot "$Component.stdout.log"
    $stderrPath = Join-Path $StateRoot "$Component.stderr.log"
    $process = Start-Process `
        -FilePath $ProcessPath `
        -ArgumentList $Arguments `
        -WorkingDirectory $WorkingDirectory `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -WindowStyle Hidden `
        -PassThru
    $state = [ordered]@{
        schema_version = "1.0"
        component = $Component
        pid = $process.Id
        process_path = Get-Re3DRestrictedCanonicalPath -Path $ProcessPath
        start_time_utc_ticks = $process.StartTime.ToUniversalTime().Ticks
        started_at_utc = [DateTime]::UtcNow.ToString("o")
    }
    $temporaryStatePath = "$statePath.tmp-$PID"
    try {
        [IO.File]::WriteAllText(
            $temporaryStatePath,
            (($state | ConvertTo-Json) + [Environment]::NewLine),
            [Text.UTF8Encoding]::new($false)
        )
        Move-Item -LiteralPath $temporaryStatePath -Destination $statePath
    } catch {
        $taskkill = (Get-Command taskkill.exe -ErrorAction SilentlyContinue).Source
        if ($taskkill) {
            & $taskkill /PID $process.Id /T /F 2>$null | Out-Null
        } elseif (-not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $temporaryStatePath -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryStatePath -Force
        }
        throw
    }
    return $process
}

function Stop-Re3DRestrictedManagedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$Component
    )

    $statePath = Get-Re3DRestrictedProcessStatePath -StateRoot $StateRoot -Component $Component
    $status = Get-Re3DRestrictedManagedProcessStatus -StateRoot $StateRoot -Component $Component
    if ($status.state -eq "stopped") {
        return "already-stopped"
    }
    if ($status.state -eq "stale") {
        Remove-Item -LiteralPath $statePath
        return "stale-state-removed"
    }
    if ($status.state -ne "running" -or -not $status.identity_valid) {
        throw "$Component process identity is '$($status.state)'. No process was stopped."
    }

    $taskkill = (Get-Command taskkill.exe -ErrorAction Stop).Source
    & $taskkill /PID $status.pid /T /F | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not stop $Component process tree rooted at PID $($status.pid)."
    }
    for ($attempt = 1; $attempt -le 20; $attempt++) {
        if (-not (Get-Process -Id $status.pid -ErrorAction SilentlyContinue)) {
            break
        }
        Start-Sleep -Milliseconds 250
    }
    if (Get-Process -Id $status.pid -ErrorAction SilentlyContinue) {
        throw "$Component process tree did not stop within 5 seconds."
    }
    Remove-Item -LiteralPath $statePath
    return "stopped"
}

function Test-Re3DRestrictedTcpPort {
    param(
        [Parameter(Mandatory = $true)][string]$HostName,
        [Parameter(Mandatory = $true)][int]$Port,
        [int]$TimeoutMilliseconds = 500
    )

    $client = [Net.Sockets.TcpClient]::new()
    try {
        $task = $client.ConnectAsync($HostName, $Port)
        if (-not $task.Wait($TimeoutMilliseconds)) {
            return $false
        }
        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Wait-Re3DRestrictedHttpEndpoint {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][Diagnostics.Process]$Process,
        [Parameter(Mandatory = $true)][string]$Name
    )

    for ($attempt = 1; $attempt -le 60; $attempt++) {
        if ($Process.HasExited) {
            throw "$Name exited before becoming ready."
        }
        try {
            $response = Invoke-WebRequest -Uri $Uri -TimeoutSec 2 -UseBasicParsing
            if ($response.StatusCode -eq 200) {
                return $response
            }
        } catch {
            # A listener can exist briefly before it accepts its first request.
        }
        Start-Sleep -Milliseconds 250
    }
    throw "$Name did not become ready at $Uri."
}

function Read-Re3DRestrictedProcessFailure {
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$Component,
        [Parameter(Mandatory = $true)][string]$Cause
    )

    $lines = @()
    foreach ($suffix in @("stdout.log", "stderr.log")) {
        $path = Join-Path $StateRoot "$Component.$suffix"
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            $lines += Get-Content -LiteralPath $path -Tail 20
        }
    }
    throw "$Component failed. Cause: $Cause. Recent output: $($lines -join ' | ')"
}
