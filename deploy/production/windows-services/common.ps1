$script:Re3DWinSWVersion = "2.12.0"

function Get-Re3DWindowsServiceDefinitions {
    return @(
        [pscustomobject]@{
            Key = "api"
            Id = "Re3DPlatformApi"
            Name = "Re3D Platform API"
            Description = "Serves the loopback-only Re3D Platform production API."
            AccountDomain = "NT SERVICE"
            AccountUser = "Re3DPlatformApi"
            DelayedStart = $true
        },
        [pscustomobject]@{
            Key = "worker"
            Id = "Re3DPlatformWorker"
            Name = "Re3D Platform Worker"
            Description = "Consumes real Re3D reconstruction jobs on the Windows GPU host."
            AccountDomain = "NT SERVICE"
            AccountUser = "Re3DPlatformWorker"
            DelayedStart = $true
        },
        [pscustomobject]@{
            Key = "caddy"
            Id = "Re3DPlatformCaddy"
            Name = "Re3D Platform Caddy"
            Description = "Provides HTTPS, static files, and the same-origin Re3D API proxy."
            AccountDomain = "NT SERVICE"
            AccountUser = "Re3DPlatformCaddy"
            DelayedStart = $false
        }
    )
}

function Get-Re3DServiceDefinition {
    param([Parameter(Mandatory = $true)][string]$Key)

    $definition = Get-Re3DWindowsServiceDefinitions |
        Where-Object { $_.Key -ceq $Key } |
        Select-Object -First 1
    if (-not $definition) {
        throw "Unknown Re3D Windows service key: $Key"
    }
    return $definition
}

function Resolve-Re3DServicePath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [ValidateSet("Leaf", "Container")][string]$PathType
    )

    $resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    if ($PathType -and -not (Test-Path -LiteralPath $resolved -PathType $PathType)) {
        throw "Expected a $PathType path: $resolved"
    }
    if ($resolved.Contains('"') -or $resolved.Contains("`r") -or $resolved.Contains("`n")) {
        throw "Service paths cannot contain quotes or line breaks."
    }
    return $resolved
}

function Test-Re3DServicePathWithin {
    param(
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Root
    )

    $candidatePath = [IO.Path]::GetFullPath($Candidate).TrimEnd('\')
    $rootPath = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    return (
        $candidatePath -ceq $rootPath -or
        $candidatePath.StartsWith(
            "$rootPath\",
            [StringComparison]::OrdinalIgnoreCase
        )
    )
}

function Get-Re3DGitSourceState {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)

    $root = Resolve-Re3DServicePath -Path $ProjectRoot -PathType Container
    $git = (Get-Command git.exe -ErrorAction Stop).Source
    $arguments = @("-c", "safe.directory=$root", "-C", $root)
    $commit = (& $git @arguments rev-parse HEAD 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $commit -notmatch '^[0-9a-f]{40}$') {
        throw "Could not resolve the platform Git commit at $root."
    }
    $changes = @(& $git @arguments status --porcelain --untracked-files=all)
    if ($LASTEXITCODE -ne 0) {
        throw "Could not inspect the platform Git worktree at $root."
    }
    return [pscustomobject]@{
        Commit = $commit
        Dirty = $changes.Count -gt 0
    }
}

function Get-Re3DEnvironmentValue {
    param(
        [Parameter(Mandatory = $true)][string]$EnvironmentFile,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Python,
        [switch]$Required
    )

    $value = & $Python -m dotenv -f $EnvironmentFile get $Name 2>$null
    if ($LASTEXITCODE -ne 0) {
        $value = $null
    }
    $normalized = if ($null -eq $value) { "" } else { "$value".Trim() }
    if ($Required -and [string]::IsNullOrWhiteSpace($normalized)) {
        throw "$Name is required in $EnvironmentFile."
    }
    return $normalized
}

function Assert-Re3DWorkerSecretBoundary {
    param([Parameter(Mandatory = $true)][string]$EnvironmentFile)

    $forbidden = @(
        "JWT_SECRET",
        "SMTP_PASSWORD",
        "SMTP_USERNAME",
        "REFRESH_COOKIE_NAME"
    )
    $configuredKeys = @(
        Get-Content -LiteralPath $EnvironmentFile |
            ForEach-Object {
                if ($_ -match '^\s*([A-Z][A-Z0-9_]*)\s*=') {
                    $Matches[1]
                }
            }
    )
    $leaked = @($forbidden | Where-Object { $configuredKeys -ccontains $_ })
    if ($leaked.Count -gt 0) {
        throw "Worker environment contains web-only secret keys: $($leaked -join ', ')"
    }
}

function Add-Re3DXmlTextElement {
    param(
        [Parameter(Mandatory = $true)][Xml.XmlDocument]$Document,
        [Parameter(Mandatory = $true)][Xml.XmlElement]$Parent,
        [Parameter(Mandatory = $true)][string]$Name,
        [AllowEmptyString()][string]$Value
    )

    $element = $Document.CreateElement($Name)
    if ($null -ne $Value) {
        $element.InnerText = $Value
    }
    [void]$Parent.AppendChild($element)
    return $element
}

function Write-Re3DWindowsServiceXml {
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
    [void]$document.AppendChild(
        $document.CreateXmlDeclaration("1.0", "utf-8", $null)
    )
    $service = $document.CreateElement("service")
    [void]$document.AppendChild($service)

    Add-Re3DXmlTextElement $document $service "id" $Definition.Id | Out-Null
    Add-Re3DXmlTextElement $document $service "name" $Definition.Name | Out-Null
    Add-Re3DXmlTextElement $document $service "description" $Definition.Description | Out-Null
    Add-Re3DXmlTextElement $document $service "executable" $PowerShellPath | Out-Null
    Add-Re3DXmlTextElement $document $service "arguments" $Arguments | Out-Null
    Add-Re3DXmlTextElement $document $service "workingdirectory" $WorkingDirectory | Out-Null
    Add-Re3DXmlTextElement $document $service "startmode" "Automatic" | Out-Null
    if ($Definition.DelayedStart) {
        Add-Re3DXmlTextElement $document $service "delayedAutoStart" "true" | Out-Null
    }
    Add-Re3DXmlTextElement $document $service "stoptimeout" "45 sec" | Out-Null
    Add-Re3DXmlTextElement $document $service "stopparentprocessfirst" "true" | Out-Null

    $account = $document.CreateElement("serviceaccount")
    [void]$service.AppendChild($account)
    Add-Re3DXmlTextElement $document $account "domain" $Definition.AccountDomain | Out-Null
    Add-Re3DXmlTextElement $document $account "user" $Definition.AccountUser | Out-Null

    foreach ($entry in $Environment.GetEnumerator() | Sort-Object Key) {
        $environmentElement = $document.CreateElement("env")
        $environmentElement.SetAttribute("name", "$($entry.Key)")
        $environmentElement.SetAttribute("value", "$($entry.Value)")
        [void]$service.AppendChild($environmentElement)
    }

    foreach ($failure in @(
        @{ Delay = "10 sec" },
        @{ Delay = "30 sec" },
        @{ Delay = "1 min" }
    )) {
        $failureElement = $document.CreateElement("onfailure")
        $failureElement.SetAttribute("action", "restart")
        $failureElement.SetAttribute("delay", $failure.Delay)
        [void]$service.AppendChild($failureElement)
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
    $settings.NewLineChars = [Environment]::NewLine
    $writer = [Xml.XmlWriter]::Create($Path, $settings)
    try {
        $document.Save($writer)
    } finally {
        $writer.Dispose()
    }
}

function Get-Re3DServiceBundleManifest {
    param([Parameter(Mandatory = $true)][string]$BundleRoot)

    $root = Resolve-Re3DServicePath -Path $BundleRoot -PathType Container
    $manifestPath = Join-Path $root "service-bundle.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "Windows service bundle manifest is missing: $manifestPath"
    }
    return Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
}

function Assert-Re3DWinSWVersion {
    param([Parameter(Mandatory = $true)][string]$Path)

    $output = (& $Path version 2>&1 | Out-String).Trim()
    if (
        $LASTEXITCODE -ne 0 -or
        $output -notmatch '(?<![0-9])2\.12\.0(?:\.0)?(?![0-9])'
    ) {
        throw "Expected WinSW 2.12.0 at $Path; received '$output'."
    }
    return $output
}

function Assert-Re3DServiceBundle {
    param(
        [Parameter(Mandatory = $true)][string]$BundleRoot,
        [switch]$CheckExternalBinaryVersions
    )

    $root = Resolve-Re3DServicePath -Path $BundleRoot -PathType Container
    $manifest = Get-Re3DServiceBundleManifest -BundleRoot $root
    if ($manifest.schema_version -cne "1.2") {
        throw "Unsupported Windows service bundle schema."
    }
    if ($manifest.winsw.version -cne $script:Re3DWinSWVersion) {
        throw "Windows service bundle must target WinSW $script:Re3DWinSWVersion."
    }
    if ([IO.Path]::GetFullPath($manifest.runtime_root) -cne $root) {
        throw "Windows service bundle runtime_root does not match its directory."
    }
    if ($manifest.source.platform_commit -notmatch '^[0-9a-f]{40}$') {
        throw "Windows service bundle has an invalid platform source commit."
    }
    if ($manifest.source.web_source_commit -cne $manifest.source.platform_commit) {
        throw "Frontend and platform source commits do not match."
    }
    $webDeploymentFile = Resolve-Re3DServicePath `
        -Path $manifest.source.web_deployment_file `
        -PathType Leaf
    if (-not (Test-Re3DServicePathWithin `
        -Candidate $webDeploymentFile `
        -Root $manifest.external.web_root)) {
        throw "Frontend deployment metadata is outside the configured web root."
    }
    $webDeploymentHash = (Get-FileHash `
        -LiteralPath $webDeploymentFile `
        -Algorithm SHA256
    ).Hash
    if ($webDeploymentHash -cne $manifest.source.web_deployment_sha256) {
        throw "Frontend deployment metadata hash does not match the service bundle."
    }
    try {
        $webDeployment = Get-Content `
            -LiteralPath $webDeploymentFile `
            -Raw | ConvertFrom-Json
    } catch {
        throw "Could not read frontend deployment metadata."
    }
    if (
        $webDeployment.schema_version -cne "1.0" -or
        $webDeployment.source_commit -cne $manifest.source.web_source_commit -or
        [bool]$webDeployment.source_dirty -or
        [int]$webDeployment.asset_count -lt 1
    ) {
        throw "Frontend deployment metadata does not match the reviewed clean build."
    }

    $re3dRoot = Resolve-Re3DServicePath `
        -Path $manifest.external.re3d_root `
        -PathType Container
    $requiredWorkerExecutables = @(
        "driver_python",
        "mapanything_python",
        "mvsanywhere_python",
        "texturemesh_executable"
    )
    $workerExecutionPaths = @()
    foreach ($name in $requiredWorkerExecutables) {
        $property = $manifest.external.worker_execution_paths.PSObject.Properties[$name]
        if (-not $property) {
            throw "Windows service bundle is missing Worker executable: $name"
        }
        $workerExecutionPaths += Resolve-Re3DServicePath `
            -Path $property.Value `
            -PathType Leaf
    }
    $workerExecutionRoots = @(
        $manifest.external.worker_execution_roots | ForEach-Object {
            Resolve-Re3DServicePath -Path $_ -PathType Container
        }
    )
    foreach ($executionPath in $workerExecutionPaths) {
        $covered = Test-Re3DServicePathWithin `
            -Candidate $executionPath `
            -Root $re3dRoot
        if (-not $covered) {
            $covered = @($workerExecutionRoots | Where-Object {
                Test-Re3DServicePathWithin `
                    -Candidate $executionPath `
                    -Root $_
            }).Count -gt 0
        }
        if (-not $covered) {
            throw "Worker execution path has no declared permission root: $executionPath"
        }
    }

    $definitions = Get-Re3DWindowsServiceDefinitions
    foreach ($definition in $definitions) {
        $serviceRecord = @($manifest.services) |
            Where-Object { $_.key -ceq $definition.Key } |
            Select-Object -First 1
        if (-not $serviceRecord -or $serviceRecord.id -cne $definition.Id) {
            throw "Windows service bundle is missing $($definition.Key)."
        }
        $wrapperPath = Resolve-Re3DServicePath `
            -Path $serviceRecord.wrapper_path `
            -PathType Leaf
        $configPath = Resolve-Re3DServicePath `
            -Path $serviceRecord.config_path `
            -PathType Leaf
        [xml]$configuration = Get-Content -LiteralPath $configPath -Raw
        $allowedElements = @(
            "id", "name", "description", "executable", "arguments",
            "workingdirectory", "startmode", "delayedAutoStart",
            "stoptimeout", "stopparentprocessfirst", "serviceaccount",
            "env", "onfailure", "resetfailure", "logpath", "log"
        )
        $unknownElements = @(
            $configuration.service.ChildNodes |
                Where-Object {
                    $_.NodeType -eq [Xml.XmlNodeType]::Element -and
                    $allowedElements -cnotcontains $_.LocalName
                }
        )
        if ($unknownElements.Count -gt 0) {
            $unknownNames = @($unknownElements | ForEach-Object { $_.LocalName })
            throw (
                "$configPath contains unsupported WinSW elements: " +
                ($unknownNames -join ", ")
            )
        }
        if ($configuration.service.id -cne $definition.Id) {
            throw "$configPath has an unexpected service id."
        }
        if (
            $configuration.service.startmode -cne "Automatic" -or
            $configuration.service.stoptimeout -cne "45 sec" -or
            $configuration.service.stopparentprocessfirst -cne "true"
        ) {
            throw "$configPath does not contain the required lifecycle settings."
        }
        $hasDelayedStart = $null -ne $configuration.service.delayedAutoStart
        if ($hasDelayedStart -ne $definition.DelayedStart) {
            throw "$configPath has an unexpected delayed-start policy."
        }
        if (
            $configuration.service.serviceaccount.domain -cne $definition.AccountDomain -or
            $configuration.service.serviceaccount.user -cne $definition.AccountUser -or
            $configuration.service.serviceaccount.password
        ) {
            throw "$configPath does not contain the expected passwordless service account."
        }
        $failures = @($configuration.service.onfailure)
        if (
            $failures.Count -ne 3 -or
            @($failures | Where-Object { $_.action -cne "restart" }).Count -gt 0 -or
            $failures[0].delay -cne "10 sec" -or
            $failures[1].delay -cne "30 sec" -or
            $failures[2].delay -cne "1 min" -or
            $configuration.service.resetfailure -cne "1 hour"
        ) {
            throw "$configPath does not contain the required restart policy."
        }
        if (
            $configuration.service.log.mode -cne "roll-by-size" -or
            $configuration.service.log.sizeThreshold -cne "10240" -or
            $configuration.service.log.keepFiles -cne "10"
        ) {
            throw "$configPath does not contain the required log rotation policy."
        }
        $rawConfiguration = Get-Content -LiteralPath $configPath -Raw
        if ($rawConfiguration -match 'JWT_SECRET|SMTP_PASSWORD|DATABASE_URL|<password>') {
            throw "$configPath contains a secret value or secret-bearing key."
        }
        if ($CheckExternalBinaryVersions) {
            Assert-Re3DWinSWVersion -Path $wrapperPath | Out-Null
        }
    }

    if ($CheckExternalBinaryVersions) {
        $caddyOutput = (& $manifest.external.caddy_path version 2>&1 | Out-String).Trim()
        if (
            $LASTEXITCODE -ne 0 -or
            $caddyOutput -notmatch '^v2\.11\.4(?:\s|$)'
        ) {
            throw "Expected Caddy v2.11.4; received '$caddyOutput'."
        }
    }
    return $manifest
}

function Assert-Re3DServiceSourceState {
    param([Parameter(Mandatory = $true)]$Manifest)

    if ([bool]$Manifest.source.platform_dirty) {
        throw "Windows service bundle was generated from a dirty platform worktree."
    }
    $state = Get-Re3DGitSourceState -ProjectRoot $Manifest.project_root
    if ($state.Dirty) {
        throw "Platform worktree changed after the Windows service bundle was generated."
    }
    if ($state.Commit -cne $Manifest.source.platform_commit) {
        throw "Platform commit changed after the Windows service bundle was generated."
    }
    return $state
}

function Test-Re3DAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

function Get-Re3DIdentitySid {
    param([Parameter(Mandatory = $true)][string]$Identity)

    return [Security.Principal.NTAccount]::new($Identity).Translate(
        [Security.Principal.SecurityIdentifier]
    )
}

function Assert-Re3DNoBroadWriteAccess {
    param([Parameter(Mandatory = $true)][string]$Path)

    $resolved = Resolve-Re3DServicePath -Path $Path
    $broadSids = @(
        "S-1-1-0",       # Everyone
        "S-1-5-11",      # Authenticated Users
        "S-1-5-32-545"   # Built-in Users
    )
    $writeRights = (
        [Security.AccessControl.FileSystemRights]::Write -bor
        [Security.AccessControl.FileSystemRights]::Modify -bor
        [Security.AccessControl.FileSystemRights]::FullControl -bor
        [Security.AccessControl.FileSystemRights]::Delete -bor
        [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
        [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [Security.AccessControl.FileSystemRights]::TakeOwnership
    )
    $acl = Get-Acl -LiteralPath $resolved
    foreach ($rule in $acl.Access) {
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) {
            continue
        }
        try {
            $sid = $rule.IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]
            ).Value
        } catch {
            continue
        }
        if (
            $broadSids -contains $sid -and
            ($rule.FileSystemRights -band $writeRights) -ne 0
        ) {
            throw (
                "Broad identity $sid has write access to production path " +
                "$resolved. Harden the parent ACL before installing services."
            )
        }
    }
}

function Add-Re3DDirectoryAccessRule {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][Security.Principal.SecurityIdentifier]$Sid,
        [Parameter(Mandatory = $true)][Security.AccessControl.FileSystemRights]$Rights
    )

    $resolved = Resolve-Re3DServicePath -Path $Path -PathType Container
    $acl = Get-Acl -LiteralPath $resolved
    $rule = [Security.AccessControl.FileSystemAccessRule]::new(
        $Sid,
        $Rights,
        [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [Security.AccessControl.InheritanceFlags]::ObjectInherit,
        [Security.AccessControl.PropagationFlags]::None,
        [Security.AccessControl.AccessControlType]::Allow
    )
    [void]$acl.AddAccessRule($rule)
    Set-Acl -LiteralPath $resolved -AclObject $acl
}

function Add-Re3DFileAccessRule {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][Security.Principal.SecurityIdentifier]$Sid,
        [Parameter(Mandatory = $true)][Security.AccessControl.FileSystemRights]$Rights
    )

    $resolved = Resolve-Re3DServicePath -Path $Path -PathType Leaf
    $acl = Get-Acl -LiteralPath $resolved
    $rule = [Security.AccessControl.FileSystemAccessRule]::new(
        $Sid,
        $Rights,
        [Security.AccessControl.AccessControlType]::Allow
    )
    [void]$acl.AddAccessRule($rule)
    Set-Acl -LiteralPath $resolved -AclObject $acl
}

function Set-Re3DSecretFileAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][Security.Principal.SecurityIdentifier]$ServiceSid
    )

    $resolved = Resolve-Re3DServicePath -Path $Path -PathType Leaf
    $acl = [Security.AccessControl.FileSecurity]::new()
    $administrators = [Security.Principal.SecurityIdentifier]::new(
        "S-1-5-32-544"
    )
    $system = [Security.Principal.SecurityIdentifier]::new("S-1-5-18")
    $acl.SetOwner($administrators)
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($entry in @(
        @{ Sid = $administrators; Rights = [Security.AccessControl.FileSystemRights]::FullControl },
        @{ Sid = $system; Rights = [Security.AccessControl.FileSystemRights]::FullControl },
        @{ Sid = $ServiceSid; Rights = [Security.AccessControl.FileSystemRights]::Read }
    )) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $entry.Sid,
            $entry.Rights,
            [Security.AccessControl.AccessControlType]::Allow
        )
        [void]$acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $resolved -AclObject $acl
}
