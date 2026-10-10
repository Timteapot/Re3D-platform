$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$templatePath = Join-Path $projectRoot ".env.example"
$targetPath = Join-Path $projectRoot ".env"
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (Test-Path -LiteralPath $targetPath) {
    throw ".env already exists. This script will not overwrite an existing secret file."
}
if (-not (Test-Path -LiteralPath $templatePath -PathType Leaf)) {
    throw ".env.example was not found."
}
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    throw "Project virtual environment was not found at $venvPython"
}

Write-Host "Create Re3D Platform local development configuration" -ForegroundColor Cyan
Write-Host "The database password is read as a secure value and is not printed."
$securePassword = Read-Host "Password for PostgreSQL role re3d_app" -AsSecureString
$passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR(
    $securePassword
)
$created = $false

try {
    $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
        $passwordPointer
    )
    if ([string]::IsNullOrWhiteSpace($plainPassword)) {
        throw "Database password must not be empty."
    }
    $encodedPassword = [Uri]::EscapeDataString($plainPassword)
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

    $content = [IO.File]::ReadAllText($templatePath)
    $databaseUrl = (
        "postgresql+psycopg://re3d_app:{0}@127.0.0.1:5432/re3d_platform_dev" `
        -f $encodedPassword
    )
    $content = [regex]::Replace(
        $content,
        "(?m)^DATABASE_URL=.*$",
        "DATABASE_URL=$databaseUrl"
    )
    $content = [regex]::Replace(
        $content,
        "(?m)^JWT_SECRET=.*$",
        "JWT_SECRET=$jwtSecret"
    )
    $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($targetPath, $content, $utf8WithoutBom)
    $created = $true

    Push-Location $projectRoot
    try {
        & $venvPython -m dotenv -f .env run -- `
            $venvPython -m apps.maintenance.main check-local-readiness
        if ($LASTEXITCODE -ne 0) {
            throw "Local readiness validation failed."
        }
    } finally {
        Pop-Location
    }

    Write-Host ".env created and validated successfully." -ForegroundColor Green
    Write-Host "No secret values were printed."
} catch {
    if ($created -and (Test-Path -LiteralPath $targetPath -PathType Leaf)) {
        Remove-Item -LiteralPath $targetPath -Force
    }
    throw
} finally {
    $plainPassword = $null
    $encodedPassword = $null
    $databaseUrl = $null
    $jwtSecret = $null
    if ($null -ne $secretBytes) {
        [Array]::Clear($secretBytes, 0, $secretBytes.Length)
    }
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
}
