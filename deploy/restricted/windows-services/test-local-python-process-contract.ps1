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

Write-Output "Restricted Python process identity contract: PASS"
