param(
    [Parameter(Mandatory = $true)]
    [string]$Distribution,
    [string]$LinuxUser = "",
    [string]$TaskName = "WSL-KeepAlive",
    [string]$ServiceName = "anet-supervisor.service"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Assert-SafeToken {
    param(
        [string]$Value,
        [string]$Name
    )
    if (-not $Value -or $Value -match '[\r\n"&|;<>$`]') {
        throw "$Name must be a non-empty token without quotes or newlines"
    }
}

Assert-SafeToken $Distribution "-Distribution"
Assert-SafeToken $TaskName "-TaskName"
Assert-SafeToken $ServiceName "-ServiceName"
if ($ServiceName -notmatch '^[A-Za-z0-9_.@-]+$') {
    throw "-ServiceName contains unsupported shell characters"
}
if ($LinuxUser) {
    Assert-SafeToken $LinuxUser "-LinuxUser"
}

$wslCommand = Get-Command wsl.exe -ErrorAction Stop
$distroNames = @(
    & $wslCommand.Source --list --quiet 2>$null |
        ForEach-Object {
            $_.ToString().Replace([string][char]0, "").Trim().TrimStart([char[]]"*").Trim()
        } |
        Where-Object { $_ }
)
if ($Distribution -notin $distroNames) {
    throw "WSL distribution is not registered: $Distribution"
}

$probeArguments = @("--distribution", $Distribution)
if ($LinuxUser) {
    $probeArguments += @("--user", $LinuxUser)
}
$probeArguments += @("--exec", "/bin/true")
& $wslCommand.Source @probeArguments *> $null
if ($LASTEXITCODE -ne 0) {
    throw "WSL distribution or Linux user could not run /bin/true"
}

# systemd services do not by themselves keep a WSL instance alive. The
# long-running shell is the explicit host-side keepalive; systemd owns Anet.
$linuxCommand = (
    'export XDG_RUNTIME_DIR=/run/user/$(id -u) && ' +
    'export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus && ' +
    "systemctl --user start $ServiceName && " +
    'while :; do sleep 3600; done'
)
$wslArguments = "--distribution `"$Distribution`""
if ($LinuxUser) {
    $wslArguments += " --user `"$LinuxUser`""
}
$wslArguments += " --exec /bin/sh -lc `"$linuxCommand`""

$userId = "$env:USERDOMAIN\$env:USERNAME"
$taskPath = "\Anet\"
$action = New-ScheduledTaskAction `
    -Execute $wslCommand.Source `
    -Argument $wslArguments
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$principal = New-ScheduledTaskPrincipal `
    -UserId $userId `
    -LogonType InteractiveToken `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 99 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask `
    -TaskPath $taskPath `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Force | Out-Null
Start-ScheduledTask -TaskPath $taskPath -TaskName $TaskName

[ordered]@{
    ok = $true
    distribution = $Distribution
    linux_user = $LinuxUser
    service = $ServiceName
    task = ($taskPath + $TaskName)
    mode = "windows-user-wsl-keepalive"
} | ConvertTo-Json -Depth 10 -Compress
