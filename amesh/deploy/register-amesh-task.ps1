param(
    [Parameter(Mandatory = $true)]
    [string]$AmeshHome,
    [switch]$Admin,
    [string]$Adapter = ""
)

# Register an Amesh\\Serve scheduled task that keeps `amesh serve` running for
# the deployment-owned node home. Default is the current user at logon; with
# -Admin the task runs as SYSTEM at startup. It never creates a node or copies
# an identity.

$ErrorActionPreference = "Stop"
$resolvedHome = (Resolve-Path -LiteralPath $AmeshHome).Path
$scriptDir = $PSScriptRoot
$startScript = Join-Path $scriptDir "start-amesh.ps1"

$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$startScript`" -AmeshHome `"$resolvedHome`""
if ($Adapter) {
    $arguments += " -Adapter `"$Adapter`""
}

$taskName = "Amesh\Serve"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments -WorkingDirectory $scriptDir
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 99 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

if ($Admin) {
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $trigger = New-ScheduledTaskTrigger -AtStartup
} else {
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
}

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

Start-ScheduledTask -TaskName $taskName
$deadline = (Get-Date).AddSeconds(30)
do {
    Start-Sleep -Milliseconds 500
    $task = Get-ScheduledTask -TaskName $taskName
} while ($task.State -ne "Running" -and (Get-Date) -lt $deadline)

if ($task.State -ne "Running") {
    $info = Get-ScheduledTaskInfo -TaskName $taskName
    throw "Amesh\\Serve did not reach Running (last result: $($info.LastTaskResult)). Inspect $resolvedHome\amesh.stderr.log"
}

Write-Output "Amesh\Serve registered and running for home $resolvedHome (Admin=$($Admin.IsPresent))"
