param(
    [Parameter(Mandatory = $true)]
    [string]$AmeshHome
)

$ErrorActionPreference = "Stop"
$resolvedHome = (Resolve-Path -LiteralPath $AmeshHome).Path
$pidFile = Join-Path $resolvedHome "amesh.pid"

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$python = $env:AMESH_PYTHON
if (-not $python -and (Test-Path -LiteralPath $venvPython)) {
    $python = $venvPython
}
if (-not $python) {
    $python = (Get-Command python.exe -ErrorAction Stop | Select-Object -First 1 -ExpandProperty Source)
}

$running = $false
$servePid = $null
if (Test-Path -LiteralPath $pidFile) {
    $servePid = [int](Get-Content -LiteralPath $pidFile -Raw)
    $running = $null -ne (Get-Process -Id $servePid -ErrorAction SilentlyContinue)
}

$adapters = @()
if ($running) {
    $adapters = (& $python -m amesh.cli --home $resolvedHome adapter list | ConvertFrom-Json).adapters
}

[ordered]@{
    running = $running
    pid = $servePid
    home = $resolvedHome
    adapters = $adapters
} | ConvertTo-Json -Depth 8
