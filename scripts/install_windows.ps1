param(
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [string]$Wheel,
    [string]$WheelSha256,
    [string]$SourceUrl,
    [string]$SourceRef,
    [ValidateSet("core", "mcp", "full")]
    [string]$Feature = "core",
    [string]$Root = (Join-Path $env:LOCALAPPDATA "Anet")
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Get-OptionalProperty {
    param([object]$Object, [string]$Name)
    if ($null -eq $Object) {
        return ""
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return ""
    }
    return [string]$property.Value
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

function Add-GitSourceRef {
    param([string]$Source, [string]$Reference)
    if (-not $Reference) {
        return $Source
    }
    $parts = $Source.Split("#", 2)
    $result = "$($parts[0])@$Reference"
    if ($parts.Count -eq 2) {
        $result += "#$($parts[1])"
    }
    return $result
}

$hasWheel = -not [string]::IsNullOrWhiteSpace($Wheel)
$hasSource = -not [string]::IsNullOrWhiteSpace($SourceUrl)
$SourceRef = Normalize-RepositoryRef $SourceRef
if ($hasWheel -eq $hasSource) {
    throw "provide exactly one of -Wheel or -SourceUrl"
}
if ($SourceRef -and -not $hasSource) {
    throw "-SourceRef requires -SourceUrl"
}

function Invoke-Checked {
    param([string]$FilePath, [string[]]$Arguments)
    $output = & $FilePath @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "command failed: $FilePath $($output | Select-Object -Last 20)"
    }
    return ($output -join "`n").Trim()
}

$wheelPath = ""
$observedHash = ""
if ($hasWheel) {
    $wheelPath = (Resolve-Path -LiteralPath $Wheel).Path
    $observedHash = (Get-FileHash -LiteralPath $wheelPath -Algorithm SHA256).Hash
    if ($observedHash -ne $WheelSha256.Trim().ToUpperInvariant()) {
        throw "wheel SHA256 mismatch"
    }
}

$rootPath = [System.IO.Path]::GetFullPath($Root)
$userPath = [System.IO.Path]::GetFullPath($env:USERPROFILE)
if ($rootPath -eq [System.IO.Path]::GetPathRoot($rootPath) -or $rootPath -eq $userPath) {
    throw "install root is too broad"
}

$preflight = $null
$preflightScript = ""
if ($PSScriptRoot) {
    $preflightScript = Join-Path $PSScriptRoot "windows_install_preflight.ps1"
}
if ($preflightScript -and (Test-Path -LiteralPath $preflightScript -PathType Leaf)) {
    $preflightJson = & powershell.exe -NoProfile -ExecutionPolicy Bypass `
        -File $preflightScript -TargetRoot $rootPath -RuntimeOnly
    if ($LASTEXITCODE -ne 0) {
        throw "Windows install preflight failed with exit code $LASTEXITCODE"
    }
    $preflight = $preflightJson | ConvertFrom-Json
}

$versions = Join-Path $rootPath "versions"
$releaseName = if ($Feature -eq "core") { $Version } else { "$Version-$Feature" }
$destination = Join-Path $versions $releaseName
$venv = Join-Path $destination "venv"
$python = Join-Path $venv "Scripts\python.exe"
$cli = Join-Path $venv "Scripts\anet.exe"
$manifest = Join-Path $destination "release.json"
New-Item -ItemType Directory -Path $versions -Force | Out-Null
$outcome = "installed"

if (Test-Path -LiteralPath $destination) {
    if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) {
        throw "existing version directory has no release manifest"
    }
    $release = Get-Content -LiteralPath $manifest -Raw | ConvertFrom-Json
    if ($hasWheel) {
        if ($release.wheel_sha256 -ne $observedHash) {
            throw "existing version has a different wheel hash"
        }
    } elseif (
        (Get-OptionalProperty $release "source_url") -ne $SourceUrl -or
        (Get-OptionalProperty $release "source_ref") -ne $SourceRef
    ) {
        throw "existing version has a different repository source"
    }
    $installedFeature = if ($release.PSObject.Properties["feature"]) {
        [string]$release.feature
    } else {
        "core"
    }
    if ($installedFeature -ne $Feature) {
        throw "existing version has a different feature set"
    }
    $outcome = "reused"
} else {
    try {
        New-Item -ItemType Directory -Path $destination | Out-Null
        $uv = Get-Command uv.exe -ErrorAction SilentlyContinue
        $extras = switch ($Feature) {
            "core" { "" }
            "mcp" { "mcp" }
            "full" { "mcp,ahub,qr" }
        }
        if ($hasSource) {
            $gitSource = if ($SourceUrl.StartsWith("git+")) {
                $SourceUrl
            } else {
                "git+$SourceUrl"
            }
            $gitSource = Add-GitSourceRef $gitSource $SourceRef
            $requirement = if ($extras) {
                "anet-fabric[$extras] @ $gitSource"
            } else {
                $gitSource
            }
        } else {
            $wheelUri = ([System.Uri]$wheelPath).AbsoluteUri
            $requirement = if ($extras) {
                "anet-fabric[$extras] @ $wheelUri"
            } else {
                $wheelPath
            }
        }
        if ($uv) {
            Invoke-Checked $uv.Source @(
                "venv", "--python", "python", $venv
            ) | Out-Null
            Invoke-Checked $uv.Source @(
                "pip", "install", "--python", $python, $requirement
            ) | Out-Null
        } else {
            $systemPython = (Get-Command python.exe -ErrorAction Stop).Source
            Invoke-Checked $systemPython @("-m", "venv", $venv) | Out-Null
            Invoke-Checked $python @(
                "-m", "pip", "install", "--disable-pip-version-check", $requirement
            ) | Out-Null
        }
        $observedVersion = Invoke-Checked $python @(
            "-c", "import importlib.metadata as m; print(m.version('anet-fabric'))"
        )
        if ($observedVersion -ne $Version) {
            throw "runtime version mismatch"
        }
        $release = @{
            schema_version = 1
            platform = "windows"
            version = $Version
            feature = $Feature
        }
        if ($hasSource) {
            $release.source_url = $SourceUrl
            $release.source_ref = $SourceRef
        } else {
            $release.wheel_sha256 = $observedHash
        }
        $release | ConvertTo-Json | Set-Content -LiteralPath $manifest -Encoding utf8
    } catch {
        if (Test-Path -LiteralPath $destination) {
            Remove-Item -LiteralPath $destination -Recurse -Force
        }
        throw
    }
}

$observedVersion = Invoke-Checked $python @(
    "-c", "import importlib.metadata as m; print(m.version('anet-fabric'))"
)
if ($observedVersion -ne $Version) {
    throw "installed runtime version mismatch"
}
if ((Invoke-Checked $cli @("--version")) -ne "Anet $Version") {
    throw "Anet CLI version mismatch"
}
if ($Feature -in @("mcp", "full")) {
    Invoke-Checked $python @("-c", "import mcp") | Out-Null
}
if ($Feature -eq "full") {
    Invoke-Checked $python @("-c", "import uvicorn, websockets") | Out-Null
}

$current = @{
    schema_version = 1
    platform = "windows"
    version = $Version
    feature = $Feature
    runtime = $venv
    cli = $cli
}
if ($hasSource) {
    $current.source_url = $SourceUrl
    $current.source_ref = $SourceRef
} else {
    $current.wheel_sha256 = $observedHash
}
$currentJson = Join-Path $rootPath "current.json"
$pendingJson = "$currentJson.new"
$current | ConvertTo-Json | Set-Content -LiteralPath $pendingJson -Encoding utf8
Move-Item -LiteralPath $pendingJson -Destination $currentJson -Force
$current["outcome"] = $outcome
$current["preflight"] = $preflight
$current | ConvertTo-Json -Compress
