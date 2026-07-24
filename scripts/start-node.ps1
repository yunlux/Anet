param(
    [Parameter(Mandatory = $true)]
    [string]$NodeHome
)

$ErrorActionPreference = "Stop"
$resolvedHome = (Resolve-Path -LiteralPath $NodeHome).Path
$pidFile = Join-Path $resolvedHome "anet.pid"
$launcherPidFile = Join-Path $resolvedHome "anet.launcher.pid"
$stdoutLog = Join-Path $resolvedHome "anet.stdout.log"
$stderrLog = Join-Path $resolvedHome "anet.stderr.log"

if (Test-Path -LiteralPath $pidFile) {
    $existingPid = [int](Get-Content -LiteralPath $pidFile -Raw)
    $existing = Get-CimInstance Win32_Process -Filter "ProcessId=$existingPid" -ErrorAction SilentlyContinue
    if ($existing -and $existing.CommandLine -like "*-m anet*" -and $existing.CommandLine -like "*$resolvedHome*") {
        Write-Output "Anet node already running: PID=$existingPid"
        exit 0
    }
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$projectPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (Test-Path -LiteralPath $projectPython) {
    $command = $projectPython
} else {
    $command = Get-Command python.exe -All -ErrorAction Stop |
        Where-Object { $_.Source -notlike "*\\WindowsApps\\*" } |
        Select-Object -First 1 -ExpandProperty Source
}
if (-not $command) {
    throw "No usable Windows python.exe was found."
}
$launcher = Start-Process `
    -FilePath $command `
    -ArgumentList @("-m", "anet", "--home", $resolvedHome, "serve") `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -PassThru

Set-Content -LiteralPath $launcherPidFile -Value $launcher.Id -Encoding ascii
Start-Sleep -Seconds 1
if (-not (Get-Process -Id $launcher.Id -ErrorAction SilentlyContinue)) {
    throw "Anet node exited during startup. Inspect $stderrLog"
}
$nodeConfig = Get-Content -LiteralPath (Join-Path $resolvedHome "config.json") -Raw | ConvertFrom-Json
$listenEnabled = if ($null -eq $nodeConfig.listen_enabled) { $true } else { [bool]$nodeConfig.listen_enabled }
$daemonPid = $null
if ($listenEnabled) {
    $listenPort = [int]$nodeConfig.listen_port
    $listener = Get-NetTCPConnection -State Listen -LocalPort $listenPort -ErrorAction SilentlyContinue |
        Where-Object {
            $candidate = Get-CimInstance Win32_Process -Filter "ProcessId=$($_.OwningProcess)" -ErrorAction SilentlyContinue
            $candidate -and $candidate.CommandLine -like "*-m anet*" -and $candidate.CommandLine -like "*$resolvedHome*"
        } |
        Select-Object -First 1
    if ($listener) {
        $daemonPid = [int]$listener.OwningProcess
    }
} else {
    $processes = @(Get-CimInstance Win32_Process)
    $descendants = @($launcher.Id)
    do {
        $before = $descendants.Count
        $children = $processes | Where-Object { $_.ParentProcessId -in $descendants }
        $descendants += @($children.ProcessId)
        $descendants = @($descendants | Select-Object -Unique)
    } while ($descendants.Count -gt $before)
    $candidates = @($processes |
        Where-Object {
            $_.ProcessId -in $descendants -and
            $_.CommandLine -like "*-m anet*" -and
            $_.CommandLine -like "*$resolvedHome*"
        })
    $daemon = $candidates | Where-Object {
        $candidatePid = $_.ProcessId
        -not ($candidates | Where-Object { $_.ParentProcessId -eq $candidatePid })
    } | Select-Object -First 1
    if ($daemon) {
        $daemonPid = [int]$daemon.ProcessId
    }
}
if (-not $daemonPid) {
    Stop-Process -Id $launcher.Id -ErrorAction SilentlyContinue
    throw "Anet node did not become ready. Inspect $stderrLog"
}
Set-Content -LiteralPath $pidFile -Value $daemonPid -Encoding ascii
Write-Output "Anet node started: PID=$daemonPid LAUNCHER=$($launcher.Id) HOME=$resolvedHome"
