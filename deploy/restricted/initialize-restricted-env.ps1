[CmdletBinding()]
param(
    [Security.SecureString]$RuntimePassword,
    [string]$DataRoot = "D:\3Dreconstruction\Re3D-data\restricted"
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$postgresCommon = Join-Path $projectRoot "deploy\postgres\common.ps1"
. $postgresCommon
. (Join-Path $PSScriptRoot "common.ps1")

$apiTemplate = Join-Path $projectRoot ".env.restricted.example"
$workerTemplate = Join-Path $projectRoot ".env.worker.restricted.example"
$apiTarget = Join-Path $projectRoot ".env.restricted"
$workerTarget = Join-Path $projectRoot ".env.worker.restricted"
$resolvedDataRoot = [IO.Path]::GetFullPath($DataRoot)
$dataParent = [IO.Path]::GetFullPath((Join-Path $projectRoot "..\Re3D-data"))

foreach ($template in ($apiTemplate, $workerTemplate)) {
    if (-not (Test-Path -LiteralPath $template -PathType Leaf)) {
        throw "Restricted environment template was not found: $template"
    }
}
foreach ($target in ($apiTarget, $workerTarget)) {
    if (Test-Path -LiteralPath $target) {
        throw "Restricted environment file already exists: $target"
    }
}
if (Test-Re3DPathWithin -Candidate $resolvedDataRoot -Root $projectRoot) {
    throw "Restricted data root must be outside the source checkout."
}
if ($resolvedDataRoot.Equals($dataParent, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Restricted data root must be a dedicated child of $dataParent"
}
if (-not (Test-Re3DPathWithin -Candidate $resolvedDataRoot -Root $dataParent)) {
    throw "Restricted data root must stay within $dataParent"
}

if ($null -eq $RuntimePassword) {
    $RuntimePassword = Read-Host (
        "Password for re3d_restricted_runtime"
    ) -AsSecureString
}

$passwordPointer = [IntPtr]::Zero
$createdTargets = [Collections.Generic.List[string]]::new()
try {
    $passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR(
        $RuntimePassword
    )
    $plainRuntimePassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
        $passwordPointer
    )
    if ($plainRuntimePassword.Length -lt 16) {
        throw "Restricted runtime password must contain at least 16 characters."
    }

    $databaseUrl = ConvertTo-Re3DDatabaseUrl `
        -DatabaseHost "127.0.0.1" `
        -DatabasePort 5432 `
        -DatabaseUser "re3d_restricted_runtime" `
        -DatabasePassword $plainRuntimePassword `
        -DatabaseName "re3d_platform_restricted"

    $secretBytes = New-Object byte[] 48
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $random.GetBytes($secretBytes)
    } finally {
        $random.Dispose()
    }
    $jwtSecret = [BitConverter]::ToString($secretBytes).Replace(
        "-",
        ""
    ).ToLowerInvariant()
    $portableDataRoot = $resolvedDataRoot.Replace("\", "/")

    $apiContent = [IO.File]::ReadAllText($apiTemplate)
    $apiContent = [regex]::Replace(
        $apiContent,
        "(?m)^DATABASE_URL=.*$",
        "DATABASE_URL=$databaseUrl"
    )
    $apiContent = [regex]::Replace(
        $apiContent,
        "(?m)^JWT_SECRET=.*$",
        "JWT_SECRET=$jwtSecret"
    )
    $apiContent = [regex]::Replace(
        $apiContent,
        "(?m)^RE3D_DATA_ROOT=.*$",
        "RE3D_DATA_ROOT=$portableDataRoot"
    )

    $workerContent = [IO.File]::ReadAllText($workerTemplate)
    $workerContent = [regex]::Replace(
        $workerContent,
        "(?m)^DATABASE_URL=.*$",
        "DATABASE_URL=$databaseUrl"
    )
    $workerContent = [regex]::Replace(
        $workerContent,
        "(?m)^RE3D_DATA_ROOT=.*$",
        "RE3D_DATA_ROOT=$portableDataRoot"
    )

    if (
        $apiContent.Contains("replace-with-") -or
        $workerContent.Contains("replace-with-")
    ) {
        throw "Restricted configuration still contains an example placeholder."
    }

    [IO.Directory]::CreateDirectory($resolvedDataRoot) | Out-Null
    $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($apiTarget, $apiContent, $utf8WithoutBom)
    $createdTargets.Add($apiTarget)
    [IO.File]::WriteAllText($workerTarget, $workerContent, $utf8WithoutBom)
    $createdTargets.Add($workerTarget)
    foreach ($target in $createdTargets) {
        Set-Re3DRestrictedSecretFileAcl -Path $target
        Assert-Re3DRestrictedSecretFileAcl -Path $target
    }

    Write-Host "Restricted configuration initialized." -ForegroundColor Green
    Write-Host "API environment: .env.restricted"
    Write-Host "Worker environment: .env.worker.restricted"
    Write-Host "Data root: $resolvedDataRoot"
    Write-Host "No secret values were printed."
} catch {
    foreach ($target in $createdTargets) {
        if (Test-Path -LiteralPath $target -PathType Leaf) {
            Remove-Item -LiteralPath $target -Force
        }
    }
    throw
} finally {
    $plainRuntimePassword = $null
    $databaseUrl = $null
    $jwtSecret = $null
    $apiContent = $null
    $workerContent = $null
    if ($null -ne $secretBytes) {
        [Array]::Clear($secretBytes, 0, $secretBytes.Length)
    }
    if ($passwordPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
    }
}
