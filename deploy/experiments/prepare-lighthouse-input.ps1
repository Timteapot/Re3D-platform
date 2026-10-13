[CmdletBinding()]
param(
    [string]$SourcePath = "C:\Users\57735\Desktop\GoogleDownload\Lighthouse\Lighthouse",
    [string]$DestinationPath = "",
    [ValidateRange(3, 300)][int]$ImageCount = 300
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$dataRoot = (Resolve-Path (Join-Path $projectRoot "..\Re3D-data")).Path
if ((Get-Item -LiteralPath $dataRoot).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw "Re3D-data root must not be a reparse point."
}
$sourceRoot = (Resolve-Path -LiteralPath $SourcePath).Path
if ([string]::IsNullOrWhiteSpace($DestinationPath)) {
    $DestinationPath = Join-Path $dataRoot ("datasets\tanks-and-temples\lighthouse\subset-" + $ImageCount)
}
$destinationRoot = [IO.Path]::GetFullPath($DestinationPath)
$dataPrefix = $dataRoot.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
if (-not $destinationRoot.StartsWith($dataPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Destination must be a dedicated directory inside Re3D-data."
}
if ($destinationRoot.Equals($sourceRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Destination must differ from the source directory."
}
$expectedName = "subset-" + $ImageCount
if ([IO.Path]::GetFileName($destinationRoot) -ne $expectedName) {
    throw "Destination directory must be named $expectedName."
}
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$validator = Join-Path $PSScriptRoot "validate-lighthouse-input.py"
$sourceNotice = Join-Path $dataRoot "datasets\tanks-and-temples\lighthouse\subset-150\SOURCE.md"
foreach ($requiredFile in @($python, $validator, $sourceNotice)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Required file is missing: $requiredFile"
    }
}
if ((Get-Item -LiteralPath $sourceRoot).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw "Source directory must not be a reparse point."
}
# Reject redirected parents before writing any input or metadata.
$candidate = $destinationRoot
while ($candidate -and $candidate.StartsWith($dataPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    if ((Test-Path -LiteralPath $candidate) -and
        ((Get-Item -LiteralPath $candidate).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Destination path includes a reparse point: $candidate"
    }
    $candidate = [IO.Path]::GetDirectoryName($candidate)
}
$sourceImages = @(Get-ChildItem -LiteralPath $sourceRoot -File |
    Where-Object { $_.Extension.ToLowerInvariant() -in @(".jpg", ".jpeg") } |
    Sort-Object Name)
if ($sourceImages.Count -lt $ImageCount) {
    throw "The source contains fewer JPEG files than requested."
}

if (Test-Path -LiteralPath $destinationRoot) {
    Write-Host "Destination already exists; validating without overwriting files."
} else {
    [IO.Directory]::CreateDirectory($destinationRoot) | Out-Null
    $records = @()
    for ($index = 0; $index -lt $ImageCount; $index++) {
        $sourceIndex = [int][Math]::Round($index * ($sourceImages.Count - 1) / ($ImageCount - 1))
        $sourceImage = $sourceImages[$sourceIndex]
        if ($sourceImage.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Source images must not be reparse points."
        }
        $targetImage = Join-Path $destinationRoot $sourceImage.Name
        Copy-Item -LiteralPath $sourceImage.FullName -Destination $targetImage -ErrorAction Stop
        $digest = (Get-FileHash -LiteralPath $sourceImage.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        $records += [ordered]@{
            selection_index = $index + 1
            source_index = $sourceIndex + 1
            filename = $sourceImage.Name
            source_path = $sourceImage.FullName
            size_bytes = $sourceImage.Length
            sha256 = $digest
        }
    }
    $manifest = [ordered]@{
        schema_version = "1.0"
        dataset = "Tanks and Temples - Lighthouse"
        source_path = $sourceRoot
        source_image_count = $sourceImages.Count
        selected_image_count = $ImageCount
        sampling = "uniform over sorted filenames; index=round(i*(N-1)/(K-1)); includes first and last frame"
        resizing = "none; source bytes copied unchanged"
        created_at_utc = [DateTime]::UtcNow.ToString("o")
        files = $records
    }
    $utf8NoBom = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText((Join-Path $destinationRoot "selection-manifest.json"),
        ($manifest | ConvertTo-Json -Depth 8), $utf8NoBom)
    Copy-Item -LiteralPath $sourceNotice -Destination (Join-Path $destinationRoot "SOURCE.md")
    $notice = [IO.File]::ReadAllText((Join-Path $destinationRoot "SOURCE.md"))
    $notice = $notice.Replace("150-image subset", ($ImageCount.ToString() + "-image subset"))
    $notice = $notice.Replace("Selected images: 150", ("Selected images: " + $ImageCount))
    $notice = $notice.Replace('`N = 309`, `K = 150`', ('`N = ' + $sourceImages.Count + '`, `K = ' + $ImageCount + '`'))
    [IO.File]::WriteAllText((Join-Path $destinationRoot "SOURCE.md"), $notice, $utf8NoBom)
}
Push-Location $projectRoot
try {
    & $python $validator --directory $destinationRoot --source $sourceRoot --count $ImageCount
    if ($LASTEXITCODE -ne 0) {
        throw "Image input validation failed with exit code $LASTEXITCODE. Existing files were retained."
    }
} finally {
    Pop-Location
}
