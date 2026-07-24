param(
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [Parameter(Mandatory = $true)]
    [string]$Wheel,
    [Parameter(Mandatory = $true)]
    [string]$WheelSha256,
    [ValidateSet("core", "mcp", "full")]
    [string]$Feature = "core",
    [string]$Root = (Join-Path $env:LOCALAPPDATA "Anet")
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Invoke-Checked {
    param([string]$FilePath, [string[]]$Arguments)
    $output = & $FilePath @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "command failed: $FilePath $($output | Select-Object -Last 20)"
    }
    return ($output -join "`n").Trim()
}

$wheelPath = (Resolve-Path -LiteralPath $Wheel).Path
$observedHash = (Get-FileHash -LiteralPath $wheelPath -Algorithm SHA256).Hash
if ($observedHash -ne $WheelSha256.Trim().ToUpperInvariant()) {
    throw "wheel SHA256 mismatch"
}

$rootPath = [System.IO.Path]::GetFullPath($Root)
$userPath = [System.IO.Path]::GetFullPath($env:USERPROFILE)
if ($rootPath -eq [System.IO.Path]::GetPathRoot($rootPath) -or $rootPath -eq $userPath) {
    throw "install root is too broad"
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
    if ($release.wheel_sha256 -ne $observedHash) {
        throw "existing version has a different wheel hash"
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
            "full" { "mcp,ahub" }
        }
        $wheelUri = ([System.Uri]$wheelPath).AbsoluteUri
        $requirement = if ($extras) {
            "anet-fabric[$extras] @ $wheelUri"
        } else {
            $wheelPath
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
        @{
            schema_version = 1
            platform = "windows"
            version = $Version
            feature = $Feature
            wheel_sha256 = $observedHash
        } | ConvertTo-Json | Set-Content -LiteralPath $manifest -Encoding utf8
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
    wheel_sha256 = $observedHash
    runtime = $venv
    cli = $cli
}
$currentJson = Join-Path $rootPath "current.json"
$pendingJson = "$currentJson.new"
$current | ConvertTo-Json | Set-Content -LiteralPath $pendingJson -Encoding utf8
Move-Item -LiteralPath $pendingJson -Destination $currentJson -Force
$current["outcome"] = $outcome
$current | ConvertTo-Json -Compress
