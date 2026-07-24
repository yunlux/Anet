param(
    [Parameter(Mandatory = $true)]
    [string]$NodeHome
)

$ErrorActionPreference = "Stop"
$resolvedHome = (Resolve-Path -LiteralPath $NodeHome).Path
$pidFile = Join-Path $resolvedHome "anet.pid"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Anet project environment is missing: $python"
}
$running = $false
$nodePid = $null

if (Test-Path -LiteralPath $pidFile) {
    $nodePid = [int](Get-Content -LiteralPath $pidFile -Raw)
    $running = $null -ne (Get-Process -Id $nodePid -ErrorAction SilentlyContinue)
}

[ordered]@{
    running = $running
    pid = $nodePid
    home = $resolvedHome
    node = (& $python -m anet --home $resolvedHome status | ConvertFrom-Json)
} | ConvertTo-Json -Depth 8
