param(
    [Parameter(Mandatory = $true)]
    [string]$NodeHome,
    [Parameter(Mandatory = $true)]
    [string]$Destination,
    [double]$Interval = 60.0,
    [double]$Jitter = 0.35,
    [double]$Timeout = 20.0,
    [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"
$resolvedHome = (Resolve-Path -LiteralPath $NodeHome).Path
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Anet project environment is missing: $python"
}
if (-not $OutputPath) {
    $OutputPath = Join-Path $resolvedHome "monitor.jsonl"
}
$resolvedOutput = [System.IO.Path]::GetFullPath($OutputPath)
$pidFile = Join-Path $resolvedHome "anet-monitor.pid"
$launcherPidFile = Join-Path $resolvedHome "anet-monitor.launcher.pid"
$outputFile = Join-Path $resolvedHome "anet-monitor.output"
$stdoutLog = Join-Path $resolvedHome "anet-monitor.stdout.log"
$stderrLog = Join-Path $resolvedHome "anet-monitor.stderr.log"

if (Test-Path -LiteralPath $pidFile) {
    $existingPid = [int](Get-Content -LiteralPath $pidFile -Raw)
    $existing = Get-CimInstance Win32_Process -Filter "ProcessId=$existingPid" -ErrorAction SilentlyContinue
    if ($existing -and $existing.CommandLine -like "*-m anet*" -and $existing.CommandLine -like "* monitor *") {
        Write-Output "Anet monitor already running: PID=$existingPid"
        exit 0
    }
}

$arguments = @(
    "-m", "anet",
    "--home", $resolvedHome,
    "monitor", $Destination,
    "--out", $resolvedOutput,
    "--interval", $Interval.ToString([Globalization.CultureInfo]::InvariantCulture),
    "--jitter", $Jitter.ToString([Globalization.CultureInfo]::InvariantCulture),
    "--timeout", $Timeout.ToString([Globalization.CultureInfo]::InvariantCulture)
)
$launcher = Start-Process `
    -FilePath $python `
    -ArgumentList $arguments `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -PassThru

Set-Content -LiteralPath $launcherPidFile -Value $launcher.Id -Encoding ascii
Set-Content -LiteralPath $outputFile -Value $resolvedOutput -Encoding utf8
Start-Sleep -Seconds 1
if (-not (Get-Process -Id $launcher.Id -ErrorAction SilentlyContinue)) {
    throw "Anet monitor exited during startup. Inspect $stderrLog"
}

$processes = @(Get-CimInstance Win32_Process)
$descendants = @($launcher.Id)
do {
    $before = $descendants.Count
    $children = $processes | Where-Object { $_.ParentProcessId -in $descendants }
    $descendants += @($children.ProcessId)
    $descendants = @($descendants | Select-Object -Unique)
} while ($descendants.Count -gt $before)
$candidates = @($processes | Where-Object {
    $_.ProcessId -in $descendants -and
    $_.CommandLine -like "*-m anet*" -and
    $_.CommandLine -like "* monitor *" -and
    $_.CommandLine -like "*$resolvedHome*"
})
$daemon = $candidates | Where-Object {
    $candidatePid = $_.ProcessId
    -not ($candidates | Where-Object { $_.ParentProcessId -eq $candidatePid })
} | Select-Object -First 1
if (-not $daemon) {
    Stop-Process -Id $launcher.Id -Force -ErrorAction SilentlyContinue
    throw "Anet monitor did not become ready. Inspect $stderrLog"
}
Set-Content -LiteralPath $pidFile -Value ([int]$daemon.ProcessId) -Encoding ascii
Write-Output "Anet monitor started: PID=$($daemon.ProcessId) DESTINATION=$Destination OUTPUT=$resolvedOutput"
