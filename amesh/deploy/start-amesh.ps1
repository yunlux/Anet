param(
    [Parameter(Mandatory = $true)]
    [string]$AmeshHome,
    [string]$Python = "",
    [string]$Adapter = ""
)

$ErrorActionPreference = "Stop"
$resolvedHome = (Resolve-Path -LiteralPath $AmeshHome).Path
$pidFile = Join-Path $resolvedHome "amesh.pid"
$stdoutLog = Join-Path $resolvedHome "amesh.stdout.log"
$stderrLog = Join-Path $resolvedHome "amesh.stderr.log"

if (Test-Path -LiteralPath $pidFile) {
    $existingPid = [int](Get-Content -LiteralPath $pidFile -Raw)
    $existing = Get-Process -Id $existingPid -ErrorAction SilentlyContinue
    if ($existing -and $existing.ProcessName -like "*python*") {
        Write-Output "Amesh serve already running: PID=$existingPid"
        exit 0
    }
}

if (-not $Python) {
    $Python = $env:AMESH_PYTHON
}
if (-not $Python) {
    $repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    $venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) {
        $Python = $venvPython
    }
}
if (-not $Python) {
    $Python = Get-Command python.exe -All -ErrorAction Stop |
        Where-Object { $_.Source -notlike "*\\WindowsApps\\*" } |
        Select-Object -First 1 -ExpandProperty Source
}
if (-not $Python) {
    throw "No usable Windows python.exe was found."
}

$arguments = @("-m", "amesh.cli", "--home", $resolvedHome, "serve")
if ($Adapter) {
    $arguments += @("--adapter", $Adapter)
}
$launcher = Start-Process `
    -FilePath $Python `
    -ArgumentList $arguments `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -PassThru

Set-Content -LiteralPath $pidFile -Value $launcher.Id -Encoding ascii
Start-Sleep -Seconds 1
if (-not (Get-Process -Id $launcher.Id -ErrorAction SilentlyContinue)) {
    throw "Amesh serve exited during startup. Inspect $stderrLog"
}
Write-Output "Amesh serve started: PID=$($launcher.Id) HOME=$resolvedHome"
