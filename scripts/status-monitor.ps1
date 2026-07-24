param(
    [Parameter(Mandatory = $true)]
    [string]$NodeHome
)

$ErrorActionPreference = "Stop"
$resolvedHome = (Resolve-Path -LiteralPath $NodeHome).Path
$pidFile = Join-Path $resolvedHome "anet-monitor.pid"
$outputFile = Join-Path $resolvedHome "anet-monitor.output"
$monitorPid = $null
$running = $false
if (Test-Path -LiteralPath $pidFile) {
    $monitorPid = [int](Get-Content -LiteralPath $pidFile -Raw)
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$monitorPid" -ErrorAction SilentlyContinue
    $running = [bool](
        $process -and
        $process.CommandLine -like "*-m anet*" -and
        $process.CommandLine -like "* monitor *"
    )
}
$monitorOutput = ""
$lastObservation = $null
if (Test-Path -LiteralPath $outputFile) {
    $monitorOutput = (Get-Content -LiteralPath $outputFile -Raw).Trim()
    if ($monitorOutput -and (Test-Path -LiteralPath $monitorOutput)) {
        $lastLine = Get-Content -LiteralPath $monitorOutput | Select-Object -Last 1
        if ($lastLine) {
            $lastObservation = $lastLine | ConvertFrom-Json
        }
    }
}
[ordered]@{
    running = $running
    pid = $monitorPid
    home = $resolvedHome
    output = $monitorOutput
    last = $lastObservation
} | ConvertTo-Json -Depth 10
