. (Join-Path $PSScriptRoot "..\..\production\windows-services\common.ps1")
. (Join-Path $PSScriptRoot "..\common.ps1")

function Get-Re3DRestrictedWindowsServiceDefinitions {
    return @(
        [pscustomobject]@{
            Key = "mailpit"; Id = "Re3DRestrictedMailpit"; Name = "Re3D Restricted Mailpit"
            Description = "Provides loopback-only development SMTP for Re3D restricted deployment."
            AccountDomain = "NT SERVICE"; AccountUser = "Re3DRestrictedMailpit"; StartMode = "Manual"
        },
        [pscustomobject]@{
            Key = "api"; Id = "Re3DRestrictedApi"; Name = "Re3D Restricted API"
            Description = "Serves the loopback-only Re3D restricted API."
            AccountDomain = "NT SERVICE"; AccountUser = "Re3DRestrictedApi"; StartMode = "Manual"
        },
        [pscustomobject]@{
            Key = "worker"; Id = "Re3DRestrictedWorker"; Name = "Re3D Restricted Worker"
            Description = "Consumes Re3D restricted reconstruction jobs on the GPU host."
            AccountDomain = "NT SERVICE"; AccountUser = "Re3DRestrictedWorker"; StartMode = "Manual"
        },
        [pscustomobject]@{
            Key = "caddy"; Id = "Re3DRestrictedCaddy"; Name = "Re3D Restricted Caddy"
            Description = "Provides the loopback-only Re3D restricted web entry point."
            AccountDomain = "NT SERVICE"; AccountUser = "Re3DRestrictedCaddy"; StartMode = "Manual"
        }
    )
}

function Get-Re3DRestrictedWindowsServiceDefinition {
    param([Parameter(Mandatory = $true)][string]$Key)

    $definition = Get-Re3DRestrictedWindowsServiceDefinitions |
        Where-Object { $_.Key -ceq $Key } |
        Select-Object -First 1
    if (-not $definition) {
        throw "Unknown restricted Windows service key: $Key"
    }
    return $definition
}

function Write-Re3DRestrictedWindowsServiceXml {
    param(
        [Parameter(Mandatory = $true)]$Definition,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$PowerShellPath,
        [Parameter(Mandatory = $true)][string]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$LogDirectory,
        [hashtable]$Environment = @{}
    )

    $document = [Xml.XmlDocument]::new()
    [void]$document.AppendChild($document.CreateXmlDeclaration("1.0", "utf-8", $null))
    $service = $document.CreateElement("service")
    [void]$document.AppendChild($service)
    Add-Re3DXmlTextElement $document $service "id" $Definition.Id | Out-Null
    Add-Re3DXmlTextElement $document $service "name" $Definition.Name | Out-Null
    Add-Re3DXmlTextElement $document $service "description" $Definition.Description | Out-Null
    Add-Re3DXmlTextElement $document $service "executable" $PowerShellPath | Out-Null
    Add-Re3DXmlTextElement $document $service "arguments" $Arguments | Out-Null
    Add-Re3DXmlTextElement $document $service "workingdirectory" $WorkingDirectory | Out-Null
    Add-Re3DXmlTextElement $document $service "startmode" $Definition.StartMode | Out-Null
    Add-Re3DXmlTextElement $document $service "stoptimeout" "45 sec" | Out-Null
    Add-Re3DXmlTextElement $document $service "stopparentprocessfirst" "true" | Out-Null

    $account = $document.CreateElement("serviceaccount")
    [void]$service.AppendChild($account)
    Add-Re3DXmlTextElement $document $account "domain" $Definition.AccountDomain | Out-Null
    Add-Re3DXmlTextElement $document $account "user" $Definition.AccountUser | Out-Null

    foreach ($entry in $Environment.GetEnumerator() | Sort-Object Key) {
        $element = $document.CreateElement("env")
        $element.SetAttribute("name", "$($entry.Key)")
        $element.SetAttribute("value", "$($entry.Value)")
        [void]$service.AppendChild($element)
    }
    foreach ($delay in @("10 sec", "30 sec", "1 min")) {
        $failure = $document.CreateElement("onfailure")
        $failure.SetAttribute("action", "restart")
        $failure.SetAttribute("delay", $delay)
        [void]$service.AppendChild($failure)
    }
    Add-Re3DXmlTextElement $document $service "resetfailure" "1 hour" | Out-Null
    Add-Re3DXmlTextElement $document $service "logpath" $LogDirectory | Out-Null
    $log = $document.CreateElement("log")
    $log.SetAttribute("mode", "roll-by-size")
    [void]$service.AppendChild($log)
    Add-Re3DXmlTextElement $document $log "sizeThreshold" "10240" | Out-Null
    Add-Re3DXmlTextElement $document $log "keepFiles" "10" | Out-Null

    $settings = [Xml.XmlWriterSettings]::new()
    $settings.Encoding = [Text.UTF8Encoding]::new($false)
    $settings.Indent = $true
    $writer = [Xml.XmlWriter]::Create($Path, $settings)
    try { $document.Save($writer) } finally { $writer.Dispose() }
}

function Set-Re3DRestrictedBundleSecretAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Security.Principal.SecurityIdentifier]$ServiceSid
    )

    $resolved = Resolve-Re3DServicePath -Path $Path -PathType Leaf
    $administrators = [Security.Principal.SecurityIdentifier]::new("S-1-5-32-544")
    $system = [Security.Principal.SecurityIdentifier]::new("S-1-5-18")
    $acl = [Security.AccessControl.FileSecurity]::new()
    $acl.SetOwner($administrators)
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($entry in @(
        @{ Sid = $administrators; Rights = [Security.AccessControl.FileSystemRights]::FullControl },
        @{ Sid = $system; Rights = [Security.AccessControl.FileSystemRights]::FullControl }
    )) {
        [void]$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $entry.Sid, $entry.Rights, [Security.AccessControl.AccessControlType]::Allow
        ))
    }
    if ($ServiceSid) {
        [void]$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $ServiceSid,
            [Security.AccessControl.FileSystemRights]::Read,
            [Security.AccessControl.AccessControlType]::Allow
        ))
    }
    Set-Acl -LiteralPath $resolved -AclObject $acl
}

function Get-Re3DRestrictedPlatformPythonRuntime {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$PlatformPython
    )

    $resolvedProjectRoot = Resolve-Re3DServicePath `
        -Path $ProjectRoot `
        -PathType Container
    $resolvedPlatformPython = Resolve-Re3DServicePath `
        -Path $PlatformPython `
        -PathType Leaf
    $expectedPlatformPython = Resolve-Re3DServicePath `
        -Path (Get-Re3DRestrictedPython -ProjectRoot $resolvedProjectRoot) `
        -PathType Leaf
    if ($resolvedPlatformPython -ine $expectedPlatformPython) {
        throw "Restricted platform Python must be project_root\.venv\Scripts\python.exe."
    }
    $venvScriptsRoot = Split-Path -Parent $resolvedPlatformPython
    $venvRoot = Split-Path -Parent $venvScriptsRoot
    $venvConfiguration = Resolve-Re3DServicePath `
        -Path (Join-Path $venvRoot "pyvenv.cfg") `
        -PathType Leaf
    foreach ($boundary in @(
        $resolvedProjectRoot,
        $venvRoot,
        $venvScriptsRoot,
        $resolvedPlatformPython,
        $venvConfiguration
    )) {
        Assert-Re3DNoBroadWriteAccess -Path $boundary
    }
    $venvConfigurationText = Get-Content -LiteralPath $venvConfiguration -Raw
    $declaredExecutableMatches = [Regex]::Matches(
        $venvConfigurationText,
        '(?im)^\s*executable\s*=\s*(.+?)\s*$'
    )
    $declaredHomeMatches = [Regex]::Matches(
        $venvConfigurationText,
        '(?im)^\s*home\s*=\s*(.+?)\s*$'
    )
    if (
        $declaredExecutableMatches.Count -ne 1 -or
        $declaredHomeMatches.Count -ne 1 -or
        -not [IO.Path]::IsPathRooted(
            $declaredExecutableMatches[0].Groups[1].Value
        ) -or
        -not [IO.Path]::IsPathRooted(
            $declaredHomeMatches[0].Groups[1].Value
        )
    ) {
        throw "Restricted pyvenv.cfg must declare one absolute home and base executable."
    }
    $declaredRuntimeImage = Resolve-Re3DServicePath `
        -Path $declaredExecutableMatches[0].Groups[1].Value `
        -PathType Leaf
    $declaredRuntimeHome = Resolve-Re3DServicePath `
        -Path $declaredHomeMatches[0].Groups[1].Value `
        -PathType Container
    if ((Split-Path -Parent $declaredRuntimeImage) -ine $declaredRuntimeHome) {
        throw "Restricted pyvenv.cfg home does not contain its base executable."
    }
    foreach ($boundary in @(
        $declaredRuntimeHome,
        $declaredRuntimeImage
    )) {
        Assert-Re3DNoBroadWriteAccess -Path $boundary
    }
    $probeCode = @'
import json
import os
import sys

print(json.dumps({
    "executable": os.path.abspath(sys.executable),
    "base_executable": os.path.abspath(sys._base_executable),
    "prefix": os.path.abspath(sys.prefix),
    "base_prefix": os.path.abspath(sys.base_prefix),
}))
'@
    $probeOutput = (& $resolvedPlatformPython -I -c $probeCode 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Could not inspect the restricted platform Python runtime: $probeOutput"
    }
    try {
        $probe = $probeOutput | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw "Restricted platform Python returned invalid runtime metadata."
    }
    foreach ($name in @("executable", "base_executable", "prefix", "base_prefix")) {
        if ([string]::IsNullOrWhiteSpace("$($probe.$name)")) {
            throw "Restricted platform Python runtime metadata is missing $name."
        }
    }

    $reportedExecutable = Resolve-Re3DServicePath `
        -Path "$($probe.executable)" `
        -PathType Leaf
    $runtimeImage = Resolve-Re3DServicePath `
        -Path "$($probe.base_executable)" `
        -PathType Leaf
    $venvPrefix = Resolve-Re3DServicePath `
        -Path "$($probe.prefix)" `
        -PathType Container
    $basePrefix = Resolve-Re3DServicePath `
        -Path "$($probe.base_prefix)" `
        -PathType Container
    if (
        $reportedExecutable -ine $resolvedPlatformPython -or
        -not (Test-Re3DServicePathWithin `
            -Candidate $reportedExecutable `
            -Root $venvPrefix
        ) -or
        $venvPrefix -ieq $basePrefix -or
        $runtimeImage -ieq $resolvedPlatformPython -or
        $runtimeImage -ine $declaredRuntimeImage
    ) {
        throw "Restricted platform Python must be an active project virtual environment."
    }

    return [pscustomobject]@{
        PlatformPython = $resolvedPlatformPython
        PlatformPythonSha256 = (
            Get-FileHash -LiteralPath $resolvedPlatformPython -Algorithm SHA256
        ).Hash
        ConfigurationPath = $venvConfiguration
        ConfigurationSha256 = (
            Get-FileHash -LiteralPath $venvConfiguration -Algorithm SHA256
        ).Hash
        RuntimeImage = $runtimeImage
        RuntimeImageSha256 = (
            Get-FileHash -LiteralPath $runtimeImage -Algorithm SHA256
        ).Hash
    }
}

function Test-Re3DProcessImageAndCommand {
    param(
        [Parameter(Mandatory = $true)]$Process,
        [Parameter(Mandatory = $true)][string[]]$AllowedExecutablePaths,
        [string]$CommandArgumentsPattern = ""
    )
    if ([string]::IsNullOrWhiteSpace("$($Process.ExecutablePath)")) {
        return $false
    }
    $processImage = [IO.Path]::GetFullPath("$($Process.ExecutablePath)")
    $allowedImages = @(
        $AllowedExecutablePaths |
            ForEach-Object { [IO.Path]::GetFullPath($_) } |
            Sort-Object -Unique
    )
    if ($allowedImages -inotcontains $processImage) {
        return $false
    }
    if ([string]::IsNullOrWhiteSpace($CommandArgumentsPattern)) {
        return $true
    }
    $commandPattern = (
        '(?i)^\s*(?:"' + [Regex]::Escape($processImage) +
        '"|' + [Regex]::Escape($processImage) +
        ')\s+' + $CommandArgumentsPattern
    )
    return "$($Process.CommandLine)" -match $commandPattern
}

function Resolve-Re3DWorkerEvidenceMatch {
    param(
        [AllowEmptyCollection()][int[]]$CandidateProcessIds = @(),
        [object[]]$EvidenceMatches = @()
    )
    $candidateIds = @($CandidateProcessIds | Sort-Object -Unique)
    $matches = @($EvidenceMatches)
    foreach ($match in $matches) {
        $processId = [int]$match.process_id
        if ($processId -le 0 -or $candidateIds -notcontains $processId) {
            throw "Restricted Worker runtime evidence references a non-candidate process."
        }
    }
    $uniqueMatches = @($matches | Sort-Object -Property process_id -Unique)
    if ($uniqueMatches.Count -ne $matches.Count -or $uniqueMatches.Count -gt 1) {
        throw "Restricted Worker runtime evidence matched multiple current processes."
    }
    if ($uniqueMatches.Count -eq 1) {
        return $uniqueMatches[0]
    }
    return $null
}

function Get-Re3DRestrictedServiceBundleManifest {
    param([Parameter(Mandatory = $true)][string]$BundleRoot)

    $root = Resolve-Re3DServicePath -Path $BundleRoot -PathType Container
    $path = Join-Path $root "restricted-service-bundle.json"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Restricted service bundle manifest is missing: $path"
    }
    return Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
}

function Assert-Re3DRestrictedServiceBundle {
    param(
        [Parameter(Mandatory = $true)][string]$BundleRoot,
        [switch]$CheckExternalBinaryVersions,
        [switch]$AllowOptionalServiceSecretAcls
    )

    $root = Resolve-Re3DServicePath -Path $BundleRoot -PathType Container
    $manifest = Get-Re3DRestrictedServiceBundleManifest -BundleRoot $root
    if ($manifest.schema_version -cne "1.1" -or [IO.Path]::GetFullPath($manifest.runtime_root) -ine $root) {
        throw "Unsupported or misplaced restricted service bundle."
    }
    if ($manifest.winsw.version -cne $script:Re3DWinSWVersion) {
        throw "Restricted service bundle must target WinSW $script:Re3DWinSWVersion."
    }
    $expectedWinSWSha256 = "05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da"
    if ($manifest.winsw.source_sha256 -ine $expectedWinSWSha256) {
        throw "Restricted service bundle requires the reviewed WinSW 2.12.0 binary."
    }
    if ([bool]$manifest.source.platform_dirty -or $manifest.source.platform_commit -notmatch '^[0-9a-f]{40}$') {
        throw "Restricted service bundle must come from a clean platform commit."
    }
    if ($manifest.source.web_source_commit -cne $manifest.source.platform_commit) {
        throw "Restricted frontend and platform source commits do not match."
    }
    foreach ($path in @(
        $manifest.external.project_root,
        $manifest.external.web_root,
        $manifest.external.data_root,
        $manifest.external.re3d_root,
        $manifest.external.worker_temp_root,
        $manifest.external.mailpit_state_root
    )) {
        Resolve-Re3DServicePath -Path $path -PathType Container | Out-Null
    }
    foreach ($path in @(
        $manifest.external.platform_python,
        $manifest.external.platform_python_configuration,
        $manifest.external.platform_python_runtime_image,
        $manifest.external.powershell_path,
        $manifest.external.caddy_path,
        $manifest.external.mailpit_path,
        $manifest.external.api_environment_file,
        $manifest.external.worker_environment_file,
        $manifest.source.web_deployment_file
    )) {
        Resolve-Re3DServicePath -Path $path -PathType Leaf | Out-Null
    }
    $platformPythonRuntime = Get-Re3DRestrictedPlatformPythonRuntime `
        -ProjectRoot $manifest.external.project_root `
        -PlatformPython $manifest.external.platform_python
    $manifestPythonImages = @(
        [IO.Path]::GetFullPath($manifest.external.platform_python)
        [IO.Path]::GetFullPath($manifest.external.platform_python_runtime_image)
    ) | Sort-Object -Unique
    $detectedPythonImages = @(
        $platformPythonRuntime.PlatformPython
        $platformPythonRuntime.RuntimeImage
    ) | Sort-Object -Unique
    if (
        $manifestPythonImages.Count -ne 2 -or
        $detectedPythonImages.Count -ne 2 -or
        (Compare-Object `
            -ReferenceObject $detectedPythonImages `
            -DifferenceObject $manifestPythonImages
        ) -or
        $manifest.external.platform_python_sha256 -notmatch '^[0-9A-Fa-f]{64}$' -or
        $manifest.external.platform_python_configuration_sha256 -notmatch '^[0-9A-Fa-f]{64}$' -or
        $manifest.external.platform_python_runtime_image_sha256 -notmatch '^[0-9A-Fa-f]{64}$' -or
        $platformPythonRuntime.PlatformPythonSha256 -ine $manifest.external.platform_python_sha256 -or
        $platformPythonRuntime.ConfigurationPath -ine (
            [IO.Path]::GetFullPath($manifest.external.platform_python_configuration)
        ) -or
        $platformPythonRuntime.ConfigurationSha256 -ine $manifest.external.platform_python_configuration_sha256 -or
        $platformPythonRuntime.RuntimeImageSha256 -ine $manifest.external.platform_python_runtime_image_sha256
    ) {
        throw "Restricted platform Python runtime drifted after bundle generation."
    }
    foreach ($path in @(
        $manifest.external.api_environment_file,
        $manifest.external.worker_environment_file,
        $manifest.external.mailpit_state_root
    )) {
        if (-not (Test-Re3DServicePathWithin -Candidate $path -Root $root)) {
            throw "Restricted private runtime path is outside the bundle root: $path"
        }
    }
    if (-not (Test-Re3DServicePathWithin `
        -Candidate $manifest.external.worker_temp_root `
        -Root $manifest.external.data_root
    )) {
        throw "Restricted Worker temp root must remain inside RE3D_DATA_ROOT."
    }
    $requiredWorkerExecutables = @(
        "driver_python", "mapanything_python", "mvsanywhere_python",
        "texturemesh_executable"
    )
    $workerExecutionRoots = @($manifest.external.worker_execution_roots)
    foreach ($name in $requiredWorkerExecutables) {
        $property = $manifest.external.worker_execution_paths.PSObject.Properties[$name]
        if (-not $property) { throw "Restricted Worker executable is missing: $name" }
        $executionPath = Resolve-Re3DServicePath -Path $property.Value -PathType Leaf
        $covered = Test-Re3DServicePathWithin -Candidate $executionPath -Root $manifest.external.re3d_root
        if (-not $covered) {
            $covered = @($workerExecutionRoots | Where-Object {
                Test-Re3DServicePathWithin -Candidate $executionPath -Root $_
            }).Count -gt 0
        }
        if (-not $covered) {
            throw "Restricted Worker execution path has no declared ACL root: $executionPath"
        }
    }
    foreach ($secret in @(
        @{ Path = $manifest.external.api_environment_file; Hash = $manifest.external.api_environment_sha256 },
        @{ Path = $manifest.external.worker_environment_file; Hash = $manifest.external.worker_environment_sha256 }
    )) {
        if ((Get-FileHash -LiteralPath $secret.Path -Algorithm SHA256).Hash -ine $secret.Hash) {
            throw "Restricted service environment file hash mismatch."
        }
    }
    foreach ($secretBoundary in @(
        @{ Key = "api"; Id = "Re3DRestrictedApi"; Path = $manifest.external.api_environment_file },
        @{ Key = "worker"; Id = "Re3DRestrictedWorker"; Path = $manifest.external.worker_environment_file }
    )) {
        $expectedServiceSid = Get-Re3DRestrictedVirtualServiceSid `
            -ServiceId $secretBoundary.Id
        $serviceInstalled = $null -ne (
            Get-Service -Name $secretBoundary.Id -ErrorAction SilentlyContinue
        )
        Assert-Re3DRestrictedSecretFileAcl `
            -Path $secretBoundary.Path `
            -ExpectedVirtualServiceSid $expectedServiceSid `
            -VirtualServiceSidOptional:($AllowOptionalServiceSecretAcls -or -not $serviceInstalled)
    }
    $webHash = (Get-FileHash -LiteralPath $manifest.source.web_deployment_file -Algorithm SHA256).Hash
    if ($webHash -ine $manifest.source.web_deployment_sha256) {
        throw "Restricted frontend deployment metadata hash mismatch."
    }
    $webDeployment = Get-Content -LiteralPath $manifest.source.web_deployment_file -Raw | ConvertFrom-Json
    if (
        $webDeployment.schema_version -cne "1.0" -or
        $webDeployment.source_commit -cne $manifest.source.platform_commit -or
        [bool]$webDeployment.source_dirty -or
        [int]$webDeployment.asset_count -lt 1
    ) {
        throw "Restricted frontend deployment metadata is invalid."
    }

    $expectedCaddySha256 = "5cb9ab71e5756ce72840b8234177a2f40c8b4ab47a806b8e841e2b784e9df62b"
    $expectedMailpitSha256 = "ee0b025bc9f61e6856d6032128408ee6fe1627f510c118a4fa0faa7bfdb7cd33"
    if (
        (Get-FileHash -LiteralPath $manifest.external.caddy_path -Algorithm SHA256).Hash -ine $expectedCaddySha256 -or
        (Get-FileHash -LiteralPath $manifest.external.mailpit_path -Algorithm SHA256).Hash -ine $expectedMailpitSha256 -or
        $manifest.external.mailpit_sha256 -ine $expectedMailpitSha256
    ) {
        throw "Restricted Caddy or Mailpit binary hash does not match the reviewed version."
    }

    foreach ($definition in Get-Re3DRestrictedWindowsServiceDefinitions) {
        $record = @($manifest.services) |
            Where-Object { $_.key -ceq $definition.Key } |
            Select-Object -First 1
        $expectedIdentity = "NT SERVICE\$($definition.Id)"
        if (
            -not $record -or
            $record.id -cne $definition.Id -or
            $record.account -ine $expectedIdentity -or
            $record.virtual_identity -ine $expectedIdentity
        ) {
            throw "Restricted service bundle is missing $($definition.Key)."
        }
        $wrapper = Resolve-Re3DServicePath -Path $record.wrapper_path -PathType Leaf
        $config = Resolve-Re3DServicePath -Path $record.config_path -PathType Leaf
        if (
            -not (Test-Re3DServicePathWithin -Candidate $wrapper -Root $root) -or
            -not (Test-Re3DServicePathWithin -Candidate $config -Root $root) -or
            -not (Test-Re3DServicePathWithin -Candidate $record.log_directory -Root $root)
        ) {
            throw "Restricted service files must remain inside the bundle root."
        }
        if ((Get-FileHash -LiteralPath $wrapper -Algorithm SHA256).Hash -ine $manifest.winsw.source_sha256) {
            throw "WinSW wrapper hash mismatch for $($definition.Id)."
        }
        if (
            $record.config_sha256 -notmatch '^[0-9A-Fa-f]{64}$' -or
            (Get-FileHash -LiteralPath $config -Algorithm SHA256).Hash -ine $record.config_sha256
        ) {
            throw "WinSW configuration hash mismatch for $($definition.Id)."
        }
        [xml]$xml = Get-Content -LiteralPath $config -Raw
        $allowedElements = @(
            "id", "name", "description", "executable", "arguments",
            "workingdirectory", "startmode", "stoptimeout",
            "stopparentprocessfirst", "serviceaccount", "env", "onfailure",
            "resetfailure", "logpath", "log"
        )
        $unknownElements = @($xml.service.ChildNodes | Where-Object {
            $_.NodeType -eq [Xml.XmlNodeType]::Element -and
            $allowedElements -cnotcontains $_.LocalName
        })
        if ($unknownElements.Count -gt 0) {
            throw "Restricted WinSW configuration contains unsupported elements: $config"
        }
        if (
            $xml.service.id -cne $definition.Id -or
            $xml.service.startmode -cne "Manual" -or
            $xml.service.serviceaccount.domain -cne "NT SERVICE" -or
            $xml.service.serviceaccount.user -cne $definition.AccountUser -or
            $xml.service.serviceaccount.password -or
            [IO.Path]::GetFullPath($xml.service.executable) -ine [IO.Path]::GetFullPath($manifest.external.powershell_path) -or
            [IO.Path]::GetFullPath($xml.service.workingdirectory) -ine [IO.Path]::GetFullPath($manifest.external.project_root) -or
            [IO.Path]::GetFullPath($xml.service.logpath) -ine [IO.Path]::GetFullPath($record.log_directory) -or
            $xml.service.stoptimeout -cne "45 sec" -or
            $xml.service.stopparentprocessfirst -cne "true"
        ) {
            throw "Restricted WinSW configuration is unsafe: $config"
        }
        $raw = Get-Content -LiteralPath $config -Raw
        if ($raw -match 'JWT_SECRET|SMTP_PASSWORD|DATABASE_URL|<password>') {
            throw "Restricted WinSW XML contains a secret-bearing key."
        }
        $failures = @($xml.service.onfailure)
        if (
            $failures.Count -ne 3 -or
            @($failures | Where-Object { $_.action -cne "restart" }).Count -gt 0 -or
            $failures[0].delay -cne "10 sec" -or
            $failures[1].delay -cne "30 sec" -or
            $failures[2].delay -cne "1 min" -or
            $xml.service.resetfailure -cne "1 hour" -or
            $xml.service.log.mode -cne "roll-by-size" -or
            $xml.service.log.sizeThreshold -cne "10240" -or
            $xml.service.log.keepFiles -cne "10"
        ) {
            throw "Restricted WinSW recovery or log policy is invalid: $config"
        }
        if ($CheckExternalBinaryVersions) {
            Assert-Re3DWinSWVersion -Path $wrapper | Out-Null
        }
    }
    if (@($manifest.services).Count -ne 4) {
        throw "Restricted service bundle must contain exactly four services."
    }
    if ($CheckExternalBinaryVersions) {
        $pwshMajor = (& $manifest.external.powershell_path `
            -NoLogo -NoProfile -NonInteractive `
            -Command '$PSVersionTable.PSVersion.Major' 2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or $pwshMajor -cne "7") {
            throw "Restricted service bundle requires PowerShell 7."
        }
        $caddyVersion = (& $manifest.external.caddy_path version 2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or $caddyVersion -notmatch '^v2\.11\.4(?:\s|$)') {
            throw "Restricted service bundle requires Caddy v2.11.4."
        }
        if ((Get-FileHash -LiteralPath $manifest.external.mailpit_path -Algorithm SHA256).Hash -ine $manifest.external.mailpit_sha256) {
            throw "Restricted service bundle Mailpit hash mismatch."
        }
    }
    return $manifest
}

function Assert-Re3DRestrictedServiceSourceState {
    param([Parameter(Mandatory = $true)]$Manifest)

    $state = Get-Re3DGitSourceState -ProjectRoot $Manifest.external.project_root
    if ($state.Dirty -or $state.Commit -cne $Manifest.source.platform_commit) {
        throw "Restricted platform source changed after the service bundle was generated."
    }
    return $state
}
