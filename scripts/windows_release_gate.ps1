param(
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [Parameter(Mandatory = $true)]
    [string]$Wheel,
    [Parameter(Mandatory = $true)]
    [string]$WheelSha256,
    [Parameter(Mandatory = $true)]
    [string]$RollbackWheel,
    [Parameter(Mandatory = $true)]
    [string]$Venv,
    [Parameter(Mandatory = $true)]
    [string[]]$NodeHome,
    [Parameter(Mandatory = $true)]
    [string]$BackupRoot,
    [Parameter(Mandatory = $true)]
    [string]$Report
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Resolve-RequiredPath {
    param([string]$Path, [string]$Label)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "$Label does not exist: $Path"
    }
    return (Resolve-Path -LiteralPath $Path).Path
}

function Invoke-Checked {
    param([string]$FilePath, [string[]]$Arguments)
    $output = & $FilePath @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "command failed: $FilePath $($output | Select-Object -Last 20)"
    }
    return ($output -join "`n").Trim()
}

function Install-Wheel {
    param([string]$Python, [string]$Artifact)
    $uv = Get-Command uv.exe -ErrorAction SilentlyContinue
    if ($uv) {
        Invoke-Checked $uv.Source @(
            "pip", "install", "--python", $Python,
            "--no-deps", "--force-reinstall", $Artifact
        ) | Out-Null
        return
    }
    Invoke-Checked $Python @(
        "-m", "pip", "install", "--disable-pip-version-check",
        "--no-deps", "--force-reinstall", $Artifact
    ) | Out-Null
}

function Get-NodeSnapshot {
    param([string]$Python, [string]$NodePath)
    $status = Invoke-Checked $Python @(
        "-m", "anet", "--home", $NodePath, "status"
    ) | ConvertFrom-Json
    $peers = Invoke-Checked $Python @(
        "-m", "anet", "--home", $NodePath, "peer-list"
    ) | ConvertFrom-Json
    $revocations = Invoke-Checked $Python @(
        "-m", "anet", "--home", $NodePath, "peer-revocations"
    ) | ConvertFrom-Json
    $protected = [ordered]@{}
    foreach ($name in @(
        "identity.json", "card.json", "config.json", "peers.json",
        "relationships.json", "relationship-claims.json",
        "tls-key.pem", "revocations.json"
    )) {
        $path = Join-Path $NodePath $name
        if (Test-Path -LiteralPath $path) {
            $protected[$name] = (
                Get-FileHash -LiteralPath $path -Algorithm SHA256
            ).Hash
        }
    }
    return [ordered]@{
        node_id = $status.node_id
        peers = $peers
        revocations = $revocations
        protected_hashes = $protected
        status_gates = [ordered]@{
            pending = [int]$status.store.pending
            rejections = [int]$status.store.rejections
            untrusted = [int]$status.store.untrusted
        }
    }
}

function Assert-Snapshot {
    param($Before, $After)
    foreach ($name in @(
        "node_id", "peers", "revocations", "protected_hashes"
    )) {
        $old = $Before.$name | ConvertTo-Json -Depth 20 -Compress
        $new = $After.$name | ConvertTo-Json -Depth 20 -Compress
        if ($old -ne $new) {
            throw "protected runtime state changed: $name"
        }
    }
    foreach ($name in @("rejections", "untrusted")) {
        if ([int]$After.status_gates.$name -gt [int]$Before.status_gates.$name) {
            throw "runtime status regression: $name increased"
        }
    }
}

$wheelPath = Resolve-RequiredPath $Wheel "wheel"
$rollbackPath = Resolve-RequiredPath $RollbackWheel "rollback wheel"
$venvPath = Resolve-RequiredPath $Venv "venv"
$python = Resolve-RequiredPath (
    Join-Path $venvPath "Scripts\python.exe"
) "persistent Python"
$expectedHash = $WheelSha256.Trim().ToUpperInvariant()
if ($expectedHash -notmatch "^[0-9A-F]{64}$") {
    throw "WheelSha256 must be a 64-character hexadecimal digest"
}
$actualHash = (
    Get-FileHash -LiteralPath $wheelPath -Algorithm SHA256
).Hash
if ($actualHash -ne $expectedHash) {
    throw "wheel SHA-256 mismatch"
}

$homes = @()
foreach ($candidate in $NodeHome) {
    $nodePath = Resolve-RequiredPath $candidate "node home"
    if (
        -not (Test-Path -LiteralPath (Join-Path $nodePath "identity.json")) -or
        -not (Test-Path -LiteralPath (Join-Path $nodePath "config.json"))
    ) {
        throw "node home is incomplete: $nodePath"
    }
    $homes += $nodePath
}
if (($homes | Select-Object -Unique).Count -ne $homes.Count) {
    throw "duplicate node home"
}

$running = @(Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -match "\-m\s+anet"
})
foreach ($nodePath in $homes) {
    $owners = @(
        $running | Where-Object { $_.CommandLine -like "*$nodePath*" }
    )
    if ($owners.Count -gt 0) {
        throw "stop the owning Anet process before release: $nodePath"
    }
}

$started = (Get-Date).ToUniversalTime().ToString("o")
$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$backupDirectory = Join-Path (
    [System.IO.Path]::GetFullPath($BackupRoot)
) "anet-$Version-$stamp"
$reportValue = [ordered]@{
    schema_version = 1
    target_version = $Version
    platform = "windows"
    started_utc = $started
    outcome = "running"
    wheel_sha256 = $actualHash
    backup_dir = $backupDirectory
    rollback = [ordered]@{ attempted = $false; succeeded = $false }
}
$deploymentStarted = $false
$backupsCreated = $false

try {
    New-Item -ItemType Directory -Path $backupDirectory -Force:$false |
        Out-Null
    foreach ($nodePath in $homes) {
        Copy-Item -LiteralPath $nodePath -Destination (
            Join-Path $backupDirectory (Split-Path $nodePath -Leaf)
        ) -Recurse
    }
    $backupsCreated = $true

    $before = [ordered]@{}
    foreach ($nodePath in $homes) {
        $before[$nodePath] = Get-NodeSnapshot $python $nodePath
    }
    $reportValue.before = $before

    $deploymentStarted = $true
    Install-Wheel $python $wheelPath

    $versionOutput = Invoke-Checked $python @(
        "-c",
        "import anet,importlib.metadata as m; print(m.version('anet-fabric')); print(anet.__version__)"
    )
    $versionLines = @($versionOutput -split "`n")
    if (
        $versionLines.Count -ne 2 -or
        $versionLines[0].Trim() -ne $Version -or
        $versionLines[1].Trim() -ne $Version
    ) {
        throw "installed version mismatch"
    }

    $after = [ordered]@{}
    foreach ($nodePath in $homes) {
        Invoke-Checked $python @(
            "-m", "anet", "--home", $nodePath, "doctor"
        ) | Out-Null
        $after[$nodePath] = Get-NodeSnapshot $python $nodePath
        Assert-Snapshot $before[$nodePath] $after[$nodePath]
    }
    $reportValue.after = $after
    $reportValue.outcome = "deployed"
}
catch {
    $reportValue.outcome = "failed"
    $reportValue.error = "$($_.Exception.GetType().Name): $($_.Exception.Message)"
    if ($deploymentStarted) {
        $reportValue.rollback.attempted = $true
        try {
            Install-Wheel $python $rollbackPath
            if ($backupsCreated) {
                $failedRoot = Join-Path $backupDirectory "failed-upgrade-state"
                New-Item -ItemType Directory -Path $failedRoot -Force:$false |
                    Out-Null
                foreach ($nodePath in $homes) {
                    Move-Item -LiteralPath $nodePath -Destination (
                        Join-Path $failedRoot (Split-Path $nodePath -Leaf)
                    )
                    Copy-Item -LiteralPath (
                        Join-Path $backupDirectory (Split-Path $nodePath -Leaf)
                    ) -Destination $nodePath -Recurse
                }
            }
            $reportValue.rollback.succeeded = $true
        }
        catch {
            $reportValue.rollback.error = $_.Exception.Message
        }
    }
    throw
}
finally {
    $reportValue.finished_utc = (
        Get-Date
    ).ToUniversalTime().ToString("o")
    $reportPath = [System.IO.Path]::GetFullPath($Report)
    New-Item -ItemType Directory -Path (
        Split-Path -Parent $reportPath
    ) -Force | Out-Null
    $reportValue | ConvertTo-Json -Depth 30 |
        Set-Content -LiteralPath $reportPath -Encoding utf8
}

[ordered]@{
    outcome = $reportValue.outcome
    target = $Version
    nodes = $homes.Count
    report = [System.IO.Path]::GetFullPath($Report)
} | ConvertTo-Json -Compress
