$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

function Assert-Contract {
    param(
        [Parameter(Mandatory = $true)][bool]$Condition,
        [Parameter(Mandatory = $true)][string]$Message
    )
    if (-not $Condition) { throw $Message }
}

$venvPython = "C:\Re3D-platform\.venv\Scripts\python.exe"
$basePython = "C:\Program Files\Re3DPython312\python.exe"
$allowedPythonImages = @($venvPython, $basePython)
$workerArguments = '-m\s+apps\.worker\.main\s+run-real-queued-loop\s*$'

$venvProcess = [pscustomobject]@{
    ExecutablePath = $venvPython
    CommandLine = "`"$venvPython`" -m apps.worker.main run-real-queued-loop"
}
$baseProcess = [pscustomobject]@{
    ExecutablePath = $basePython
    CommandLine = "`"$basePython`" -m apps.worker.main run-real-queued-loop"
}
Assert-Contract `
    -Condition (Test-Re3DProcessImageAndCommand `
        -Process $venvProcess `
        -AllowedExecutablePaths $allowedPythonImages `
        -CommandArgumentsPattern $workerArguments) `
    -Message "The pinned venv process should match."
Assert-Contract `
    -Condition (Test-Re3DProcessImageAndCommand `
        -Process $baseProcess `
        -AllowedExecutablePaths $allowedPythonImages `
        -CommandArgumentsPattern $workerArguments) `
    -Message "The pinned base Python process should match."

$outsideProcess = [pscustomobject]@{
    ExecutablePath = "C:\Untrusted\python.exe"
    CommandLine = 'C:\Untrusted\python.exe -m apps.worker.main run-real-queued-loop'
}
Assert-Contract `
    -Condition (-not (Test-Re3DProcessImageAndCommand `
        -Process $outsideProcess `
        -AllowedExecutablePaths $allowedPythonImages `
        -CommandArgumentsPattern $workerArguments)) `
    -Message "An unpinned Python image must not match."

$mismatchedCommand = [pscustomobject]@{
    ExecutablePath = $basePython
    CommandLine = "`"$venvPython`" -m apps.worker.main run-real-queued-loop"
}
Assert-Contract `
    -Condition (-not (Test-Re3DProcessImageAndCommand `
        -Process $mismatchedCommand `
        -AllowedExecutablePaths $allowedPythonImages `
        -CommandArgumentsPattern $workerArguments)) `
    -Message "ExecutablePath and command argv0 must identify the same image."

foreach ($malformedCommandLine in @(
    "`"$basePython -m apps.worker.main run-real-queued-loop",
    "$basePython`" -m apps.worker.main run-real-queued-loop"
)) {
    $malformedQuoting = [pscustomobject]@{
        ExecutablePath = $basePython
        CommandLine = $malformedCommandLine
    }
    Assert-Contract `
        -Condition (-not (Test-Re3DProcessImageAndCommand `
            -Process $malformedQuoting `
            -AllowedExecutablePaths $allowedPythonImages `
            -CommandArgumentsPattern $workerArguments)) `
        -Message "Unbalanced argv0 quoting must not match."
}

$wrongMode = [pscustomobject]@{
    ExecutablePath = $basePython
    CommandLine = "`"$basePython`" -m apps.worker.main run-test-loop"
}
Assert-Contract `
    -Condition (-not (Test-Re3DProcessImageAndCommand `
        -Process $wrongMode `
        -AllowedExecutablePaths $allowedPythonImages `
        -CommandArgumentsPattern $workerArguments)) `
    -Message "A different Worker mode must not match."

$baseEvidence = [pscustomobject]@{ process_id = 6992 }
$selected = Resolve-Re3DWorkerEvidenceMatch `
    -CandidateProcessIds @(7884, 6992) `
    -EvidenceMatches @($baseEvidence)
Assert-Contract `
    -Condition ($selected.process_id -eq 6992) `
    -Message "Evidence must select the unique base Python PID from both candidates."

$multipleEvidenceFailed = $false
try {
    Resolve-Re3DWorkerEvidenceMatch `
        -CandidateProcessIds @(7884, 6992) `
        -EvidenceMatches @(
            [pscustomobject]@{ process_id = 7884 },
            [pscustomobject]@{ process_id = 6992 }
        ) | Out-Null
} catch {
    $multipleEvidenceFailed = $_.Exception.Message -match "multiple current processes"
}
Assert-Contract `
    -Condition $multipleEvidenceFailed `
    -Message "Evidence for multiple current Worker processes must fail closed."

if ($PSVersionTable.PSVersion.Major -ge 7) {
    $utcTimestamp = "2026-10-13T01:56:27.737662+00:00"
    $utcRecord = ConvertFrom-Re3DWorkerRuntimeJsonLine -Line (
        '{"operation":"worker-runtime-preflight","status":"ready",' +
        '"process_id":6060,"emitted_at_utc":"' + $utcTimestamp + '"}'
    )
    Assert-Contract `
        -Condition (
            $utcRecord.emitted_at_utc -is [string] -and
            $utcRecord.emitted_at_utc -ceq $utcTimestamp
        ) `
        -Message "Worker evidence must preserve the raw UTC timestamp string."
    $parsedUtc = ConvertTo-Re3DUtcEvidenceTime -Value $utcRecord.emitted_at_utc
    Assert-Contract `
        -Condition ($parsedUtc.ToString("o") -ceq "2026-10-13T01:56:27.7376620Z") `
        -Message "The preserved UTC timestamp must retain its instant and precision."

    $wholeSecondUtc = "2026-10-13T01:56:27+00:00"
    $parsedWholeSecond = ConvertTo-Re3DUtcEvidenceTime -Value $wholeSecondUtc
    Assert-Contract `
        -Condition ($parsedWholeSecond.ToString("o") -ceq "2026-10-13T01:56:27.0000000Z") `
        -Message "A canonical whole-second UTC timestamp must be accepted."

    $offsetTimestamp = "2026-10-13T09:56:27.737662+08:00"
    $offsetRecord = ConvertFrom-Re3DWorkerRuntimeJsonLine -Line (
        '{"operation":"worker-runtime-preflight","status":"ready",' +
        '"process_id":6060,"emitted_at_utc":"' + $offsetTimestamp + '"}'
    )
    Assert-Contract `
        -Condition (
            $offsetRecord.emitted_at_utc -is [string] -and
            $offsetRecord.emitted_at_utc -ceq $offsetTimestamp
        ) `
        -Message "Worker evidence must preserve a non-UTC offset for rejection."
    $nonUtcRejected = $false
    try {
        ConvertTo-Re3DUtcEvidenceTime -Value $offsetRecord.emitted_at_utc | Out-Null
    } catch {
        $nonUtcRejected = $_.Exception.Message -match "timestamp is not canonical UTC"
    }
    Assert-Contract `
        -Condition $nonUtcRejected `
        -Message "A non-UTC runtime evidence timestamp must fail closed."

    foreach ($invalidTimestamp in @(
        "2026-10-13 01:56:27+00:00",
        "Tue, 13 Oct 2026 01:56:27 GMT",
        "01:56:27",
        "2026-10-13T01:56:27.737662789+00:00",
        "2026-10-13T01:56:27.737662-00:00",
        "2026-10-13T01:56:27.737662Z"
    )) {
        $invalidRejected = $false
        try {
            ConvertTo-Re3DUtcEvidenceTime -Value $invalidTimestamp | Out-Null
        } catch {
            $invalidRejected = $true
        }
        Assert-Contract `
            -Condition $invalidRejected `
            -Message "A non-canonical runtime evidence timestamp must fail closed."
    }

    foreach ($duplicateLine in @(
        '{"operation":"worker-runtime-preflight","emitted_at_utc":"2026-10-13T01:56:27+00:00","emitted_at_utc":"2026-10-13T01:56:28+00:00"}',
        '{"operation":"worker-runtime-preflight","emitted_at_utc":"2026-10-13T01:56:27+00:00","emitted_at_\u0075tc":"2026-10-13T01:56:28+00:00"}',
        '{"operation":"worker-runtime-preflight","Emitted_At_Utc":"2026-10-13T01:56:27+00:00"}'
    )) {
        $duplicateRejected = $false
        try {
            ConvertFrom-Re3DWorkerRuntimeJsonLine -Line $duplicateLine | Out-Null
        } catch {
            $duplicateRejected = $true
        }
        Assert-Contract `
            -Condition $duplicateRejected `
            -Message "Duplicate or non-canonical runtime timestamp keys must fail closed."
    }

    $nestedAndTop = ConvertFrom-Re3DWorkerRuntimeJsonLine -Line (
        '{"operation":"worker-runtime-preflight","nested":{' +
        '"emitted_at_utc":"2026-10-13T01:56:26+00:00"},' +
        '"emitted_at_utc":"2026-10-13T01:56:27+00:00"}'
    )
    Assert-Contract `
        -Condition ($nestedAndTop.emitted_at_utc -ceq "2026-10-13T01:56:27+00:00") `
        -Message "Only the decoded top-level timestamp may be selected."

    $nestedOnly = ConvertFrom-Re3DWorkerRuntimeJsonLine -Line (
        '{"operation":"worker-runtime-preflight","nested":{' +
        '"emitted_at_utc":"2026-10-13T01:56:27+00:00"}}'
    )
    Assert-Contract `
        -Condition ($null -eq $nestedOnly.emitted_at_utc) `
        -Message "A nested timestamp must not become top-level runtime evidence."

    $missingTimestamp = ConvertFrom-Re3DWorkerRuntimeJsonLine -Line (
        '{"operation":"cleanup-failed-job-storage","status":"ok"}'
    )
    Assert-Contract `
        -Condition ($null -eq $missingTimestamp.emitted_at_utc) `
        -Message "An operation without runtime evidence must not gain a timestamp."
}

Write-Output "Restricted Python process identity contract: PASS"
