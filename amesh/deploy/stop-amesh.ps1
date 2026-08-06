param(
    [Parameter(Mandatory = $true)]
    [string]$AmeshHome
)

$ErrorActionPreference = "Stop"
$resolvedHome = (Resolve-Path -LiteralPath $AmeshHome).Path
$pidFile = Join-Path $resolvedHome "amesh.pid"

if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Output "Amesh serve is not running (no PID file)."
    exit 0
}

$seedPid = [int](Get-Content -LiteralPath $pidFile -Raw)
$seed = Get-CimInstance Win32_Process -Filter "ProcessId=$seedPid" -ErrorAction SilentlyContinue
if ($seed -and ($seed.CommandLine -notlike "*-m amesh*" -or $seed.CommandLine -notlike "*$resolvedHome*")) {
    throw "Refusing to stop PID $seedPid because it is not the configured Amesh serve."
}
if ($seed) {
    $allProcesses = @(Get-CimInstance Win32_Process)
    $depthByPid = @{}
    function Add-AmeshProcessTree([int]$ProcessId, [int]$Depth) {
        if ($depthByPid.ContainsKey($ProcessId) -and $depthByPid[$ProcessId] -ge $Depth) {
            return
        }
        $depthByPid[$ProcessId] = $Depth
        foreach ($child in $allProcesses | Where-Object { $_.ParentProcessId -eq $ProcessId }) {
            Add-AmeshProcessTree -ProcessId ([int]$child.ProcessId) -Depth ($Depth + 1)
        }
    }
    Add-AmeshProcessTree -ProcessId $seedPid -Depth 0
    foreach ($entry in $depthByPid.GetEnumerator() | Sort-Object Value -Descending) {
        Stop-Process -Id ([int]$entry.Key) -Force -ErrorAction SilentlyContinue
    }
}
Remove-Item -LiteralPath $pidFile -Force
Write-Output "Amesh serve stopped: PID=$seedPid"
