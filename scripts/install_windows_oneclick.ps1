param(
    [Parameter(Mandatory = $true)]
    [string]$ControlUrl,
    [string]$ControlKeyId = "",
    [string]$ControlPublicKey = "",
    [ValidateSet("core", "mcp", "full")]
    [string]$Feature = "mcp",
    [string]$Version = "",
    [string]$Wheel = "",
    [string]$WheelSha256 = "",
    [string]$Root = "",
    [string]$NodeHome = "",
    [string]$Label = "windows-node",
    [int]$Port = 0,
    [string]$ListenHost = "",
    [string[]]$Advertise = @(),
    [string[]]$LocatorContext = @(),
    [string]$RuntimeInstallerUrl = "",
    [string]$PreflightScriptUrl = "",
    [string]$SupervisorScriptUrl = "",
    [string]$GitHubBranch = "main",
    [string]$HelperRepository = "https://github.com/yunlux/Anet",
    [switch]$Admin,
    [switch]$AllowExisting
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Read-ControlPage {
    param([string]$Url)
    $response = Invoke-RestMethod -Uri $Url -Method Get -Headers @{
        Accept = "application/json"
        "User-Agent" = "Anet-Windows-OneClick/0.12.1"
    }
    if (-not $response) {
        throw "control page returned no JSON object"
    }
    return $response
}

function Resolve-ControlReference {
    param(
        [string]$BaseUrl,
        [string]$Reference
    )
    $absolute = $null
    if ([System.Uri]::TryCreate(
            $Reference,
            [System.UriKind]::Absolute,
            [ref]$absolute
        )) {
        return $absolute.AbsoluteUri
    }
    $base = [System.Uri]$BaseUrl
    return ([System.Uri]::new($base, $Reference)).AbsoluteUri
}

function Get-GitHubScriptUrl {
    param(
        [string]$RepositoryUrl,
        [string]$Branch,
        [string]$ScriptName
    )
    $repository = [System.Uri]$RepositoryUrl
    if (
        $repository.Scheme -ne "https" -or
        $repository.Host -notin @("github.com", "www.github.com")
    ) {
        throw "automatic helper download requires a GitHub repository URL; pass an explicit helper URL"
    }
    $repositoryPath = $repository.AbsolutePath.Trim("/") -replace "\.git$", ""
    $parts = @(
        $repositoryPath.Split(
            "/",
            [System.StringSplitOptions]::RemoveEmptyEntries
        )
    )
    if ($parts.Count -lt 2) {
        throw "GitHub repository URL must contain an owner and repository"
    }
    return "https://raw.githubusercontent.com/$($parts[0])/$($parts[1])/$Branch/scripts/$ScriptName"
}

function Normalize-RepositoryRef {
    param([string]$Value)
    $reference = if ($null -eq $Value) { "" } else { $Value.Trim() }
    if (-not $reference) {
        return ""
    }
    if (
        $reference -notmatch '^[A-Za-z0-9][A-Za-z0-9._/-]*$' -or
        $reference.Contains("..") -or
        $reference.Contains("//") -or
        $reference.Contains("@{") -or
        $reference.EndsWith(".") -or
        $reference.EndsWith("/")
    ) {
        throw "repository ref contains an invalid Git reference"
    }
    return $reference
}

function Get-OptionalProperty {
    param(
        [object]$Object,
        [string]$Name
    )
    if ($null -eq $Object) {
        return ""
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return ""
    }
    return [string]$property.Value
}

function Test-IsJsonObject {
    param([object]$Value)
    if ($null -eq $Value) {
        return $false
    }
    return $Value -is [pscustomobject]
}

function Assert-DeploymentReceipt {
    param([System.Collections.IDictionary]$Receipt)
    if (
        $Receipt["kind"] -ne "anet.deployment.receipt" -or
        $Receipt["schema_version"] -ne 1 -or
        $Receipt["ok"] -ne $true -or
        $Receipt["platform"] -ne "windows" -or
        $Receipt["outcome"] -notin @("created", "reused")
    ) {
        throw "Windows deployment receipt header is invalid"
    }
    $runtime = $Receipt["runtime"]
    if (
        -not $runtime["version"] -or
        $runtime["feature"] -notin @("core", "mcp", "full") -or
        -not $runtime["runtime"] -or
        -not $runtime["cli"]
    ) {
        throw "Windows deployment receipt runtime is incomplete"
    }
    $node = $Receipt["node"]
    if (
        -not $node["home"] -or
        $node["node_id"] -notmatch '^an1[a-z2-7]{17,125}$' -or
        -not $node["listen_host"] -or
        [int]$node["port"] -lt 1 -or
        [int]$node["port"] -gt 65535
    ) {
        throw "Windows deployment receipt node is incomplete"
    }
    $control = $Receipt["control"]
    if (-not $control["url"] -or $control["verified"] -ne $true) {
        throw "Windows deployment receipt control verification is incomplete"
    }
    $supervisor = $Receipt["supervisor"]
    if (
        -not $supervisor["kind"] -or
        -not $supervisor["name"] -or
        $supervisor["state"] -notin @("active", "running") -or
        $supervisor["autostart"] -ne $true
    ) {
        throw "Windows deployment receipt supervisor is incomplete"
    }
    if ($null -eq $Receipt["preflight"]) {
        throw "Windows deployment receipt preflight is missing"
    }
}

function Merge-JsonObjects {
    param(
        [object]$Base,
        [object]$Patch
    )
    $values = [ordered]@{}
    if ($null -ne $Base) {
        foreach ($property in $Base.PSObject.Properties) {
            $values[$property.Name] = $property.Value
        }
    }
    if ($null -ne $Patch) {
        foreach ($property in $Patch.PSObject.Properties) {
            if (
                $values.Contains($property.Name) -and
                (Test-IsJsonObject $values[$property.Name]) -and
                (Test-IsJsonObject $property.Value)
            ) {
                $values[$property.Name] = Merge-JsonObjects `
                    $values[$property.Name] $property.Value
            } else {
                $values[$property.Name] = $property.Value
            }
        }
    }
    return [pscustomobject]$values
}

function Resolve-WheelSha256 {
    param(
        [string]$Explicit = "",
        [string]$Declared = "",
        [bool]$Require = $false
    )
    $explicitValue = $Explicit.Trim()
    $declaredValue = $Declared.Trim()
    if ($explicitValue -and $explicitValue -notmatch '^[0-9A-Fa-f]{64}$') {
        throw "wheel SHA256 must contain 64 hex characters"
    }
    if ($declaredValue -and $declaredValue -notmatch '^[0-9A-Fa-f]{64}$') {
        throw "software.sha256 must contain 64 hex characters"
    }
    if (
        $explicitValue -and
        $declaredValue -and
        $explicitValue.ToLowerInvariant() -ne $declaredValue.ToLowerInvariant()
    ) {
        throw "-WheelSha256 does not match software.sha256"
    }
    $value = if ($explicitValue) { $explicitValue } else { $declaredValue }
    if (-not $value -and $Require) {
        throw "pinned control page requires software.sha256 or -WheelSha256 for wheel installation"
    }
    return $value
}

function Test-IsLoopbackHost {
    param([string]$HostValue)
    $value = $HostValue.Trim().TrimStart("[").TrimEnd("]").ToLowerInvariant()
    if ($value -eq "localhost") {
        return $true
    }
    $address = $null
    if ([System.Net.IPAddress]::TryParse($value, [ref]$address)) {
        return [System.Net.IPAddress]::IsLoopback($address)
    }
    return $false
}

function Get-EffectivePlatformConfig {
    param(
        [object]$Platforms,
        [object]$CommonConfig,
        [string]$PlatformName
    )
    if ($null -eq $Platforms) {
        return $null
    }
    if (-not (Test-IsJsonObject $Platforms)) {
        throw "control page platforms must be an object"
    }
    $overlayProperty = $Platforms.PSObject.Properties[$PlatformName]
    if ($null -eq $overlayProperty) {
        return $null
    }
    $overlay = $overlayProperty.Value
    if ($null -eq $overlay) {
        return $CommonConfig
    }
    if (-not (Test-IsJsonObject $overlay)) {
        throw "control page platforms.$PlatformName must be an object"
    }
    $config = $null
    if ($null -ne $overlay -and $overlay.PSObject.Properties["config"]) {
        $config = $overlay.config
    } elseif ($null -ne $overlay -and
        $overlay.PSObject.Properties["default_config"]) {
        $config = $overlay.default_config
    }
    if ($null -eq $config) {
        return $CommonConfig
    }
    return Merge-JsonObjects $CommonConfig $config
}

function Get-EffectivePlatformSoftware {
    param(
        [object]$Platforms,
        [object]$CommonSoftware,
        [string]$PlatformName
    )
    if ($null -ne $CommonSoftware -and -not (Test-IsJsonObject $CommonSoftware)) {
        throw "control page software must be an object"
    }
    if ($null -eq $Platforms) {
        return $(if ($null -eq $CommonSoftware) { [pscustomobject]@{} } else { $CommonSoftware })
    }
    if (-not (Test-IsJsonObject $Platforms)) {
        throw "control page platforms must be an object"
    }
    $overlayProperty = $Platforms.PSObject.Properties[$PlatformName]
    if ($null -eq $overlayProperty) {
        return $(if ($null -eq $CommonSoftware) { [pscustomobject]@{} } else { $CommonSoftware })
    }
    $overlay = $overlayProperty.Value
    if ($null -eq $overlay) {
        return $(if ($null -eq $CommonSoftware) { [pscustomobject]@{} } else { $CommonSoftware })
    }
    if (-not (Test-IsJsonObject $overlay)) {
        throw "control page platforms.$PlatformName must be an object"
    }
    if (-not $overlay.PSObject.Properties["software"]) {
        return $(if ($null -eq $CommonSoftware) { [pscustomobject]@{} } else { $CommonSoftware })
    }
    $software = $overlay.software
    if ($null -eq $software) {
        return $(if ($null -eq $CommonSoftware) { [pscustomobject]@{} } else { $CommonSoftware })
    }
    if (-not (Test-IsJsonObject $software)) {
        throw "control page platforms.$PlatformName.software must be an object"
    }
    return Merge-JsonObjects $CommonSoftware $software
}

function Test-HasHostScope {
    param([object]$Config)
    if ($null -eq $Config) {
        return $false
    }
    if ($Config.PSObject.Properties["listen_enabled"] -and
        -not [bool]$Config.listen_enabled) {
        return $false
    }
    $contexts = @()
    if ($Config.PSObject.Properties["locator_contexts"]) {
        $contexts = @($Config.locator_contexts)
    }
    $advertise = @()
    if ($Config.PSObject.Properties["advertise"]) {
        $advertise = @($Config.advertise)
    }
    return (
        @($contexts | Where-Object { [string]$_ -like "host:*" }).Count -gt 0 -or
        @($advertise | Where-Object {
            [string]$_ -match "(?i)[?&]scope=host(?:&|$)"
        }).Count -gt 0
    )
}

function Assert-CrossPlatformPorts {
    param(
        [object]$Platforms,
        [object]$CommonConfig,
        [string]$PlatformName,
        [int]$ListenPort,
        [string[]]$Advertise,
        [string[]]$Contexts
    )
    if ($PlatformName -notin @("windows", "wsl") -or $null -eq $Platforms) {
        return
    }
    $other = if ($PlatformName -eq "windows") { "wsl" } else { "windows" }
    $currentConfig = Get-EffectivePlatformConfig `
        $Platforms $CommonConfig $PlatformName
    $otherConfig = Get-EffectivePlatformConfig `
        $Platforms $CommonConfig $other
    if ($null -eq $currentConfig -or $null -eq $otherConfig) {
        return
    }
    $currentConfig | Add-Member -NotePropertyName locator_contexts `
        -NotePropertyValue @($Contexts) -Force
    $currentConfig | Add-Member -NotePropertyName advertise `
        -NotePropertyValue @($Advertise) -Force
    if ($currentConfig.PSObject.Properties["listen_enabled"] -and
        -not [bool]$currentConfig.listen_enabled) {
        return
    }
    if ($otherConfig.PSObject.Properties["listen_enabled"] -and
        -not [bool]$otherConfig.listen_enabled) {
        return
    }
    $currentHasHostScope = Test-HasHostScope $currentConfig
    $otherHasHostScope = Test-HasHostScope $otherConfig
    if ($currentHasHostScope -ne $otherHasHostScope) {
        throw "Windows and WSL host scope must be declared on both enabled overlays"
    }
    if (-not $currentHasHostScope) {
        return
    }
    $otherPort = 0
    if ($otherConfig.PSObject.Properties["listen_port"]) {
        try {
            $otherPort = [int]$otherConfig.listen_port
        } catch {
            throw "platforms.$other.config.listen_port is invalid"
        }
    }
    if ($ListenPort -lt 1 -or $ListenPort -gt 65535 -or
        $otherPort -lt 1 -or $otherPort -gt 65535) {
        throw "Windows/WSL host-scoped deployments require explicit listener ports from 1 to 65535"
    }
    if ($ListenPort -eq $otherPort) {
        throw "Windows and WSL host-scoped deployments must use distinct listener ports"
    }
}

function Assert-CrossPlatformLocators {
    param(
        [object]$Platforms,
        [object]$CommonConfig,
        [string]$PlatformName,
        [string]$ListenHost,
        [string[]]$Advertise,
        [string[]]$Contexts
    )
    if ($PlatformName -notin @("windows", "wsl") -or $null -eq $Platforms) {
        return
    }
    $other = if ($PlatformName -eq "windows") { "wsl" } else { "windows" }
    if ($null -eq $Platforms.PSObject.Properties[$other]) {
        return
    }
    $currentConfig = Get-EffectivePlatformConfig `
        $Platforms $CommonConfig $PlatformName
    $otherConfig = Get-EffectivePlatformConfig `
        $Platforms $CommonConfig $other
    if ($currentConfig -and
        $currentConfig.PSObject.Properties["listen_enabled"] -and
        -not [bool]$currentConfig.listen_enabled) {
        return
    }
    if ($otherConfig -and
        $otherConfig.PSObject.Properties["listen_enabled"] -and
        -not [bool]$otherConfig.listen_enabled) {
        return
    }
    $hasHostContext = @(
        $Contexts | Where-Object { [string]$_ -like "host:*" }
    ).Count -gt 0
    $hostLocators = @(
        $Advertise |
            Where-Object { [string]$_ -match "(?i)[?&]scope=host(?:&|$)" }
    )
    if (-not $hasHostContext -and $hostLocators.Count -eq 0) {
        return
    }
    if (Test-IsLoopbackHost $ListenHost) {
        throw "Windows/WSL host-scoped deployment must not listen on loopback; use a mirrored host IP/hostname or another non-loopback interface"
    }
    foreach ($address in $hostLocators) {
        if ([string]$address -match "(?i)^(?:tls|tcp\+tls)://(?:127\.0\.0\.1|localhost|\[::1\])(?::|/)") {
            throw "Windows/WSL host-scoped locators must not advertise 127.0.0.1, localhost, or ::1; use the shared non-loopback host address"
        }
    }
    if ($hostLocators.Count -eq 0 -and $ListenHost.Trim() -in @("0.0.0.0", "::")) {
        throw "a wildcard Windows/WSL listener needs an explicit host-scoped -Advertise address reachable from both runtimes"
    }
}

function Get-FreeLoopbackPort {
    $listener = [System.Net.Sockets.TcpListener]::new(
        [System.Net.IPAddress]::Loopback,
        0
    )
    try {
        $listener.Start()
        return ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    }
    finally {
        $listener.Stop()
    }
}

function Invoke-Anet {
    param(
        [string]$Python,
        [string[]]$Arguments
    )
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Anet command failed with exit code $LASTEXITCODE"
    }
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

function Get-ControlTrustedKeys {
    param(
        [string]$KeyId,
        [string]$PublicKey
    )
    $cleanId = $KeyId.Trim()
    $encoded = $PublicKey.Trim()
    if (-not $cleanId -and -not $encoded) {
        return [ordered]@{}
    }
    if (-not $cleanId -or -not $encoded) {
        throw "-ControlKeyId and -ControlPublicKey must be provided together"
    }
    if ($cleanId -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$') {
        throw "-ControlKeyId is invalid"
    }
    try {
        $standard = $encoded.Replace('-', '+').Replace('_', '/')
        while (($standard.Length % 4) -ne 0) { $standard += '=' }
        $bytes = [Convert]::FromBase64String($standard)
    } catch {
        throw "-ControlPublicKey is not valid base64url"
    }
    if ($bytes.Length -ne 32) {
        throw "-ControlPublicKey must contain 32 bytes"
    }
    return [ordered]@{ $cleanId = $encoded }
}

function Enter-InstallMutex {
    param(
        [string]$Scope,
        [string]$RootPath
    )
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($RootPath.ToLowerInvariant())
        $hex = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "")
    } finally {
        $sha.Dispose()
    }
    $mutex = [Threading.Mutex]::new($false, "Local\Anet-$Scope-$($hex.Substring(0, 32))")
    $owned = $false
    try {
        $owned = $mutex.WaitOne(0)
    } catch [Threading.AbandonedMutexException] {
        $owned = $true
    }
    if (-not $owned) {
        $mutex.Dispose()
        throw "another Anet installer already owns the $Scope install lock for $RootPath"
    }
    return $mutex
}

function Stop-ManagedSupervisorTask {
    param(
        [string]$TaskPath,
        [string]$TaskName
    )
    $task = Get-ScheduledTask `
        -TaskPath $TaskPath `
        -TaskName $TaskName `
        -ErrorAction SilentlyContinue
    if ($null -eq $task -or [string]$task.State -ne "Running") {
        return
    }
    Stop-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ([DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 200
        $task = Get-ScheduledTask `
            -TaskPath $TaskPath `
            -TaskName $TaskName `
            -ErrorAction SilentlyContinue
        if ($null -eq $task -or [string]$task.State -ne "Running") {
            return
        }
    }
    throw "managed Anet supervisor task did not stop within 30 seconds"
}

function Wait-ManagedSupervisorTask {
    param(
        [string]$TaskPath,
        [string]$TaskName
    )
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ([DateTime]::UtcNow -lt $deadline) {
        $task = Get-ScheduledTask `
            -TaskPath $TaskPath `
            -TaskName $TaskName `
            -ErrorAction SilentlyContinue
        if ($null -ne $task -and [string]$task.State -eq "Running") {
            return
        }
        Start-Sleep -Milliseconds 200
    }
    $lastResult = "unknown"
    try {
        $lastResult = [string](Get-ScheduledTaskInfo `
            -TaskPath $TaskPath `
            -TaskName $TaskName
        ).LastTaskResult
    } catch {
        $lastResult = "unavailable"
    }
    throw "managed Anet supervisor task did not start within 30 seconds (last task result: $lastResult)"
}

$requestedListenHost = $ListenHost
if ($Admin -and -not (Test-IsAdministrator)) {
    throw "-Admin requires an elevated PowerShell window (Run as administrator)"
}
if (-not $Root) {
    if ($Admin) {
        $Root = Join-Path $env:ProgramData "Anet"
    } else {
        $Root = Join-Path $env:LOCALAPPDATA "Anet"
    }
}

$rootPath = [System.IO.Path]::GetFullPath($Root)
$installMutex = Enter-InstallMutex "deployment" $rootPath
$page = Read-ControlPage $ControlUrl
$trustedKeys = Get-ControlTrustedKeys $ControlKeyId $ControlPublicKey
$commonSoftware = if ($page.PSObject.Properties["software"]) {
    $page.software
} else {
    $null
}
$software = $commonSoftware

$platformConfig = $null
$commonConfig = $null
if ($page.PSObject.Properties["config"]) {
    $commonConfig = $page.config
    $platformConfig = $commonConfig
} elseif ($page.PSObject.Properties["default_config"]) {
    $commonConfig = $page.default_config
    $platformConfig = $commonConfig
}
if ($page.PSObject.Properties["platforms"]) {
    $platforms = $page.platforms
    if ($platforms -and $platforms.PSObject.Properties["windows"]) {
        $platform = $platforms.windows
        $platformConfig = Get-EffectivePlatformConfig `
            $platforms $commonConfig "windows"
        $software = Get-EffectivePlatformSoftware `
            $platforms $commonSoftware "windows"
    }
}
if (-not $software -or $software -isnot [psobject]) {
    throw "control page must contain a software object for one-click installation"
}
if ($platformConfig) {
    if (-not $requestedListenHost -and $platformConfig.PSObject.Properties["listen_host"]) {
        $ListenHost = [string]$platformConfig.listen_host
    }
    if ($Port -eq 0 -and $platformConfig.PSObject.Properties["listen_port"]) {
        $Port = [int]$platformConfig.listen_port
    }
    if ($Advertise.Count -eq 0 -and $platformConfig.PSObject.Properties["advertise"]) {
        $Advertise = @($platformConfig.advertise | ForEach-Object { [string]$_ })
    }
    if ($LocatorContext.Count -eq 0 -and $platformConfig.PSObject.Properties["locator_contexts"]) {
        $LocatorContext = @($platformConfig.locator_contexts | ForEach-Object { [string]$_ })
    }
}
if (-not $ListenHost) {
    $ListenHost = "127.0.0.1"
}
if ($Port -lt 0 -or $Port -gt 65535) {
    throw "-Port must be between 0 and 65535"
}
$platformsForValidation = $null
if ($page.PSObject.Properties["platforms"]) {
    $platformsForValidation = $page.platforms
}
Assert-CrossPlatformLocators `
    $platformsForValidation `
    $commonConfig `
    "windows" `
    $ListenHost `
    $Advertise `
    $LocatorContext
Assert-CrossPlatformPorts `
    $platformsForValidation `
    $commonConfig `
    "windows" `
    $Port `
    $Advertise `
    $LocatorContext

$helperRoot = ""
$preflight = $null
$preflightScript = ""
$sourceRef = Get-OptionalProperty $software "repo_ref"
if (-not $sourceRef) {
    $sourceRef = Get-OptionalProperty $page "repo_ref"
}
if (-not $sourceRef) {
    $sourceRef = Get-OptionalProperty $page "anet_repo_ref"
}
$sourceRef = Normalize-RepositoryRef $sourceRef
$helperBranch = Normalize-RepositoryRef $GitHubBranch
if (-not $helperBranch) {
    $helperBranch = "main"
}
$helperRepository = $HelperRepository.Trim()
if ($PSScriptRoot) {
    $localPreflight = Join-Path $PSScriptRoot "windows_install_preflight.ps1"
    if (Test-Path -LiteralPath $localPreflight -PathType Leaf) {
        $preflightScript = $localPreflight
    }
}
if (-not $preflightScript) {
    if (-not $PreflightScriptUrl) {
        $PreflightScriptUrl = Get-GitHubScriptUrl `
            $helperRepository $helperBranch "windows_install_preflight.ps1"
    }
    $helperRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
        "anet-bootstrap-" + [Guid]::NewGuid().ToString("N")
    )
    New-Item -ItemType Directory -Path $helperRoot -Force | Out-Null
    $preflightScript = Join-Path $helperRoot "windows_install_preflight.ps1"
    Invoke-WebRequest -Uri $PreflightScriptUrl -OutFile $preflightScript -UseBasicParsing
}
$preflightArguments = @(
    "-File", $preflightScript,
    "-TargetRoot", $rootPath,
    "-Deployment"
)
if ($NodeHome) {
    $preflightArguments += @("-NodeHome", $NodeHome)
}
if ($AllowExisting) {
    $preflightArguments += "-AllowExisting"
}
$preflightOutput = & powershell.exe -NoProfile -ExecutionPolicy Bypass @preflightArguments
if ($LASTEXITCODE -ne 0) {
    throw "Windows install preflight found an existing deployment: $($preflightOutput -join "`n")"
}
$preflight = $preflightOutput | ConvertFrom-Json

if (-not $Version) {
    $Version = Get-OptionalProperty $software "version"
}
if (-not $Version) {
    throw "control page software.version is required"
}
$sourceUrl = ""
$declaredWheelSha256 = Get-OptionalProperty $software "sha256"
$resolvedWheelSha256 = ""
if (-not $Wheel) {
    $wheelUrl = Get-OptionalProperty $software "wheel_url"
    if ($wheelUrl) {
        $resolvedWheelSha256 = Resolve-WheelSha256 `
            -Explicit $WheelSha256 `
            -Declared $declaredWheelSha256 `
            -Require ($trustedKeys.Count -gt 0)
        $wheelUrl = Resolve-ControlReference $ControlUrl $wheelUrl
        $tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
            "anet-install-" + [Guid]::NewGuid().ToString("N")
        )
        New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
        $Wheel = Join-Path $tempRoot ("anet-fabric-" + $Version + ".whl")
        Invoke-WebRequest -Uri $wheelUrl -OutFile $Wheel -UseBasicParsing
    } else {
        $sourceUrl = Get-OptionalProperty $software "repo_url"
        if (-not $sourceUrl) {
            $sourceUrl = Get-OptionalProperty $page "repo_url"
        }
        if (-not $sourceUrl) {
            $sourceUrl = Get-OptionalProperty $page "anet_repo"
        }
        if (-not $sourceUrl) {
            throw "control page software.wheel_url or software.repo_url is required for one-click installation"
        }
        $sourceUrl = Resolve-ControlReference $ControlUrl $sourceUrl
    }
}

$installer = ""
$localInstaller = ""
$localLauncher = ""
if ($PSScriptRoot) {
    $localInstaller = Join-Path $PSScriptRoot "install_windows.ps1"
    $localLauncher = Join-Path $PSScriptRoot "run-supervisor.ps1"
}
if ($localInstaller -and (Test-Path -LiteralPath $localInstaller -PathType Leaf)) {
    $installer = $localInstaller
} else {
    if (-not $RuntimeInstallerUrl) {
        $RuntimeInstallerUrl = Get-GitHubScriptUrl `
            $helperRepository $helperBranch "install_windows.ps1"
    }
    if (-not $helperRoot) {
        $helperRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
            "anet-bootstrap-" + [Guid]::NewGuid().ToString("N")
        )
        New-Item -ItemType Directory -Path $helperRoot -Force | Out-Null
    }
    $installer = Join-Path $helperRoot "install_windows.ps1"
    Invoke-WebRequest -Uri $RuntimeInstallerUrl -OutFile $installer -UseBasicParsing
}
$runtimeArguments = @(
    "-Version", $Version,
    "-Feature", $Feature,
    "-Root", $rootPath
)
if ($sourceUrl) {
    $runtimeArguments += @(
        "-SourceUrl", $sourceUrl,
        "-SourceRef", $sourceRef
    )
} else {
    $wheelPath = (Resolve-Path -LiteralPath $Wheel).Path
    $resolvedWheelSha256 = Resolve-WheelSha256 `
        -Explicit $WheelSha256 `
        -Declared $declaredWheelSha256 `
        -Require ($trustedKeys.Count -gt 0)
    if ($resolvedWheelSha256) {
        $WheelSha256 = $resolvedWheelSha256
    }
    if (-not $WheelSha256) {
        $WheelSha256 = (Get-FileHash -LiteralPath $wheelPath -Algorithm SHA256).Hash
    }
    $runtimeArguments += @(
        "-Wheel", $wheelPath,
        "-WheelSha256", $WheelSha256
    )
}
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer @runtimeArguments
if ($LASTEXITCODE -ne 0) {
    throw "runtime installation failed with exit code $LASTEXITCODE"
}

$currentPath = Join-Path $rootPath "current.json"
$current = Get-Content -LiteralPath $currentPath -Raw | ConvertFrom-Json
$python = Join-Path ([string]$current.runtime) "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "installed Anet runtime Python is missing: $python"
}

if (-not $NodeHome) {
    $NodeHome = Join-Path $rootPath "nodes\default"
}
$nodePath = [System.IO.Path]::GetFullPath($NodeHome)
$configPath = Join-Path $nodePath "config.json"
$nodeCreated = -not (Test-Path -LiteralPath $configPath -PathType Leaf)
if ($nodeCreated) {
    if ($Port -eq 0 -and $ListenHost -ne "127.0.0.1") {
        throw "-Port is required when -ListenHost is not 127.0.0.1"
    }
    $port = if ($Port -gt 0) { $Port } else { Get-FreeLoopbackPort }
    Invoke-Anet $python @(
        "-m", "anet", "--home", $nodePath, "init",
        "--label", $Label,
        "--host", $ListenHost,
        "--port", $port
    )
} else {
    $existingConfig = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    $port = [int]$existingConfig.listen_port
    if ($Port -gt 0 -and $Port -ne $port) {
        throw "existing node listens on port $port; requested -Port $Port"
    }
    if (
        $requestedListenHost -and
        $requestedListenHost -ne [string]$existingConfig.listen_host
    ) {
        throw "existing node listens on $($existingConfig.listen_host); requested -ListenHost $requestedListenHost"
    }
    $ListenHost = [string]$existingConfig.listen_host
}

if ($LocatorContext.Count -gt 0 -or $Advertise.Count -gt 0) {
    $locatorArguments = @(
        "-m", "anet", "--home", $nodePath, "locator-config"
    )
    foreach ($context in $LocatorContext) {
        $locatorArguments += @("--add-context", $context)
    }
    foreach ($address in $Advertise) {
        $locatorArguments += @("--advertise", $address)
    }
    Invoke-Anet $python $locatorArguments
}

$settings = [ordered]@{
    version = 1
    url = $ControlUrl
    interval = if ($page.poll_seconds) {
        [Math]::Max(5, [Math]::Min([double]$page.poll_seconds, 86400))
    } else {
        300
    }
}
if ($trustedKeys.Count -gt 0) {
    $settings["trusted_keys"] = $trustedKeys
}
New-Item -ItemType Directory -Path $nodePath -Force | Out-Null
$settings | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (
    Join-Path $nodePath "remote-control.json"
) -Encoding utf8

$controlVerifyArguments = @(
    "-m", "anet", "--home", $nodePath,
    "control-verify", "--url", $ControlUrl
)
if ($trustedKeys.Count -gt 0) {
    $controlVerifyArguments += @(
        "--control-key-id", $ControlKeyId,
        "--control-public-key", $ControlPublicKey
    )
}
$controlVerifyOutput = & $python @controlVerifyArguments
if ($LASTEXITCODE -ne 0) {
    throw "remote control page verification failed with exit code $LASTEXITCODE"
}

$statusOutput = & $python "-m" "anet" "--home" $nodePath "status"
if ($LASTEXITCODE -ne 0) {
    throw "Anet status failed with exit code $LASTEXITCODE"
}
$nodeStatus = ($statusOutput -join "`n") | ConvertFrom-Json
$nodeId = Get-OptionalProperty $nodeStatus "node_id"
if ($nodeId -notmatch '^an1[a-z2-7]{17,125}$') {
    throw "Anet status did not return a complete Node ID"
}

$launcher = Join-Path $rootPath "run-supervisor.ps1"
if ($localLauncher -and (Test-Path -LiteralPath $localLauncher -PathType Leaf)) {
    $launcherSource = $localLauncher
} else {
    if (-not $SupervisorScriptUrl) {
        $SupervisorScriptUrl = Get-GitHubScriptUrl `
            $helperRepository $helperBranch "run-supervisor.ps1"
    }
    if (-not $helperRoot) {
        $helperRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
            "anet-bootstrap-" + [Guid]::NewGuid().ToString("N")
        )
        New-Item -ItemType Directory -Path $helperRoot -Force | Out-Null
    }
    $launcherSource = Join-Path $helperRoot "run-supervisor.ps1"
    Invoke-WebRequest -Uri $SupervisorScriptUrl -OutFile $launcherSource -UseBasicParsing
}
Copy-Item -LiteralPath $launcherSource -Destination $launcher -Force

$taskPath = "\Anet\"
$taskName = "Supervisor"
$taskArguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$launcher`" -NodeHome `"$nodePath`" -RuntimeRoot `"$rootPath`""
$action = New-ScheduledTaskAction -Execute "PowerShell.exe" -Argument $taskArguments
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 99 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
if ($Admin) {
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal `
        -UserId "SYSTEM" `
        -LogonType ServiceAccount `
        -RunLevel Highest
    $mode = "windows-machine-scheduled-task"
} else {
    $userId = "$env:USERDOMAIN\$env:USERNAME"
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
    $principal = New-ScheduledTaskPrincipal `
        -UserId $userId `
        -LogonType InteractiveToken `
        -RunLevel Limited
    $mode = "windows-user-scheduled-task"
}
Stop-ManagedSupervisorTask $taskPath $taskName
Register-ScheduledTask `
    -TaskPath $taskPath `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Force | Out-Null
Start-ScheduledTask -TaskPath $taskPath -TaskName $taskName
Wait-ManagedSupervisorTask $taskPath $taskName

$currentConfig = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$runtimeReceipt = [ordered]@{
    platform = "windows"
    version = [string]$current.version
    feature = [string]$current.feature
    runtime = [string]$current.runtime
    cli = [string]$current.cli
}
$nodeReceipt = [ordered]@{
    home = $nodePath
    node_id = $nodeId
    listen_host = $ListenHost
    port = $port
    advertise = @($currentConfig.advertise)
    locator_contexts = @($currentConfig.locator_contexts)
}
$controlReceipt = [ordered]@{
    url = $ControlUrl
    key_id = $ControlKeyId
    verified = $true
}
$supervisorReceipt = [ordered]@{
    kind = $mode
    name = ($taskPath + $taskName)
    state = "running"
    autostart = $true
}
$receipt = [ordered]@{
    kind = "anet.deployment.receipt"
    schema_version = 1
    ok = $true
    outcome = if ($nodeCreated) { "created" } else { "reused" }
    platform = "windows"
    runtime = $runtimeReceipt
    node = $nodeReceipt
    control = $controlReceipt
    supervisor = $supervisorReceipt
    preflight = $preflight
}
Assert-DeploymentReceipt $receipt
$result = $receipt | ConvertTo-Json -Depth 10 -Compress
$installMutex.ReleaseMutex()
$installMutex.Dispose()
$result
