param(
    [Parameter(Mandatory = $true)]
    [string]$NodeHome
)

$ErrorActionPreference = "Stop"
$resolvedHome = (Resolve-Path -LiteralPath $NodeHome).Path
$pidFile = Join-Path $resolvedHome "anet.pid"
$launcherPidFile = Join-Path $resolvedHome "anet.launcher.pid"

if (-not (Test-Path -LiteralPath $pidFile) -and -not (Test-Path -LiteralPath $launcherPidFile)) {
    Write-Output "Anet node is not running (no PID file)."
    exit 0
}

$seedPids = @()
foreach ($path in @($pidFile, $launcherPidFile)) {
    if (Test-Path -LiteralPath $path) {
        $seedPids += [int](Get-Content -LiteralPath $path -Raw)
    }
}
$allProcesses = @(Get-CimInstance Win32_Process)
$depthByPid = @{}
function Add-AnetProcessTree([int]$ProcessId, [int]$Depth) {
    if ($depthByPid.ContainsKey($ProcessId) -and $depthByPid[$ProcessId] -ge $Depth) {
        return
    }
    $depthByPid[$ProcessId] = $Depth
    foreach ($child in $allProcesses | Where-Object { $_.ParentProcessId -eq $ProcessId }) {
        Add-AnetProcessTree -ProcessId ([int]$child.ProcessId) -Depth ($Depth + 1)
    }
}
foreach ($seedPid in $seedPids | Select-Object -Unique) {
    $seed = $allProcesses | Where-Object { $_.ProcessId -eq $seedPid } | Select-Object -First 1
    if ($seed -and ($seed.CommandLine -notlike "*-m anet*" -or $seed.CommandLine -notlike "*$resolvedHome*")) {
        throw "Refusing to stop PID $seedPid because it is not the configured Anet node."
    }
    if ($seed) {
        Add-AnetProcessTree -ProcessId $seedPid -Depth 0
    }
}
foreach ($entry in $depthByPid.GetEnumerator() | Sort-Object Value -Descending) {
    Stop-Process -Id ([int]$entry.Key) -Force -ErrorAction SilentlyContinue
}
foreach ($path in @($pidFile, $launcherPidFile)) {
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Force
    }
}
Write-Output "Anet node stopped: PID=$($seedPids -join ',')"
