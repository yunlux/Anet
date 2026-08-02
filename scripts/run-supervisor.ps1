param(
    [Parameter(Mandatory = $true)]
    [string]$NodeHome,
    [string]$ControlUrl = "",
    [string]$RuntimeRoot = ""
)

$ErrorActionPreference = "Stop"
$resolvedHome = (Resolve-Path -LiteralPath $NodeHome).Path
if (-not $RuntimeRoot) {
    $RuntimeRoot = Split-Path -Parent (Split-Path -Parent $resolvedHome)
}
$currentPath = Join-Path ([System.IO.Path]::GetFullPath($RuntimeRoot)) "current.json"
if (-not (Test-Path -LiteralPath $currentPath -PathType Leaf)) {
    throw "Anet runtime pointer is missing: $currentPath"
}
$current = Get-Content -LiteralPath $currentPath -Raw | ConvertFrom-Json
$python = Join-Path ([string]$current.runtime) "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Anet runtime Python is missing: $python"
}

$logPath = Join-Path $resolvedHome "supervisor.log"
$arguments = @(
    "-m",
    "anet",
    "--home",
    $resolvedHome,
    "supervisor"
)
if ($ControlUrl) {
    $arguments += @("--url", $ControlUrl)
}

& $python @arguments *>> $logPath
exit $LASTEXITCODE
