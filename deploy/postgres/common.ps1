function Get-Re3DPostgresToolPath {
    param([Parameter(Mandatory = $true)][string]$Name)

    $command = Get-Command "$Name.exe" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($command) {
        return $command.Source
    }

    $programFiles = [Environment]::GetFolderPath("ProgramFiles")
    $bundled = Join-Path $programFiles "PostgreSQL\18\bin\$Name.exe"
    if (Test-Path -LiteralPath $bundled -PathType Leaf) {
        return $bundled
    }
    throw "PostgreSQL tool was not found: $Name"
}

function Get-Re3DProjectPython {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)

    $venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        return $venvPython
    }
    return (Get-Command python -ErrorAction Stop).Source
}

function ConvertTo-Re3DDatabaseUrl {
    param(
        [Parameter(Mandatory = $true)][string]$DatabaseHost,
        [Parameter(Mandatory = $true)][int]$DatabasePort,
        [Parameter(Mandatory = $true)][string]$DatabaseUser,
        [Parameter(Mandatory = $true)][string]$DatabasePassword,
        [Parameter(Mandatory = $true)][string]$DatabaseName
    )

    $encodedPassword = [Uri]::EscapeDataString($DatabasePassword)
    return "postgresql+psycopg://$DatabaseUser`:$encodedPassword@$DatabaseHost`:$DatabasePort/$DatabaseName"
}

function Test-Re3DPathWithin {
    param(
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Root
    )

    $candidatePath = [IO.Path]::GetFullPath($Candidate)
    $rootPath = [IO.Path]::GetFullPath($Root).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    return $candidatePath.Equals(
        $rootPath,
        [StringComparison]::OrdinalIgnoreCase
    ) -or $candidatePath.StartsWith(
        "$rootPath$([IO.Path]::DirectorySeparatorChar)",
        [StringComparison]::OrdinalIgnoreCase
    )
}
