param(
    [Parameter(Mandatory = $true)]
    [string]$TargetRoot,
    [switch]$RuntimeOnly,
    [switch]$Deployment,
    [switch]$AllowExisting
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$anetTokenPattern = "(?i)(^|[^a-z])anet(?:$|[^a-z]|supervisor|node|fabric)"
$ahubTokenPattern = "(?i)(^|[^a-z])ahub(?:$|[^a-z]|server|service)"

function Resolve-FullPath {
    param([string]$Path)
    return [System.IO.Path]::GetFullPath($Path)
}

function Test-AnyPath {
    param([string]$Path)
    return Test-Path -LiteralPath $Path
}

function Get-RootFinding {
    param(
        [string]$Path,
        [bool]$OnlyRuntime
    )
    $root = Resolve-FullPath $Path
    if (-not (Test-AnyPath $root)) {
        return $null
    }
    $markerNames = @("current", "current.json", "versions", "release.json")
    if (-not $OnlyRuntime) {
        $markerNames += @(
            "nodes", "config.json", "identity.json", "card.json",
            "remote-control.json"
        )
    }
    $markers = @(
        $markerNames | Where-Object {
            Test-AnyPath (Join-Path $root $_)
        }
    )
    if ($markers.Count -eq 0) {
        return $null
    }
    $persistent = @(
        @(
            "nodes", "config.json", "identity.json", "card.json",
            "remote-control.json"
        ) | Where-Object { $markers -contains $_ }
    )
    return [ordered]@{
        kind = "anet-root"
        path = $root
        markers = @($markers)
        persistent = ($persistent.Count -gt 0)
    }
}

function Get-AhubFinding {
    param([string]$Path)
    $root = Resolve-FullPath $Path
    if (-not (Test-AnyPath $root)) {
        return $null
    }
    $markers = @(
        "ahub.sqlite3", "control.sqlite3", "config.json" |
            Where-Object { Test-AnyPath (Join-Path $root $_) }
    )
    if ($markers.Count -eq 0) {
        return $null
    }
    return [ordered]@{
        kind = "ahub-root"
        path = $root
        markers = @($markers)
    }
}

function Get-UniquePathList {
    param([string[]]$Paths)
    $seen = @{}
    $result = @()
    foreach ($path in $Paths) {
        $full = Resolve-FullPath $path
        $key = $full.ToLowerInvariant()
        if (-not $seen.ContainsKey($key)) {
            $seen[$key] = $true
            $result += $full
        }
    }
    return $result
}

$target = Resolve-FullPath $TargetRoot
$anetCandidates = Get-UniquePathList @(
    $target,
    (Join-Path $env:LOCALAPPDATA "Anet"),
    (Join-Path $env:ProgramData "Anet")
)
$existingAnet = @(
    $anetCandidates |
        ForEach-Object { Get-RootFinding $_ ([bool]$RuntimeOnly) } |
        Where-Object { $null -ne $_ }
)

$ahubCandidates = Get-UniquePathList @(
    (Join-Path $target "ahub"),
    (Join-Path $target "ahub-data"),
    (Join-Path $env:LOCALAPPDATA "Ahub"),
    (Join-Path $env:LOCALAPPDATA "Anet\Ahub"),
    (Join-Path $env:ProgramData "Ahub"),
    (Join-Path $env:ProgramData "Anet\Ahub"),
    (Join-Path $env:ProgramData "Anet-Ahub")
)
$existingAhub = @(
    $ahubCandidates |
        ForEach-Object { Get-AhubFinding $_ } |
        Where-Object { $null -ne $_ }
)

$services = @()
$tasks = @()
$processes = @()
if (-not $RuntimeOnly) {
    $services = @(
        Get-Service -ErrorAction SilentlyContinue |
            Where-Object {
                ([string]$_.Name -match $anetTokenPattern) -or
                ([string]$_.Name -match $ahubTokenPattern) -or
                ([string]$_.DisplayName -match $anetTokenPattern) -or
                ([string]$_.DisplayName -match $ahubTokenPattern)
            } |
            ForEach-Object {
                [ordered]@{
                    kind = if (
                        ([string]$_.Name -match $ahubTokenPattern) -or
                        ([string]$_.DisplayName -match $ahubTokenPattern)
                    ) { "ahub" } else { "anet" }
                    manager = "windows-service"
                    name = [string]$_.Name
                    state = [string]$_.Status
                }
            }
    )
    $tasks = @(
        Get-ScheduledTask -ErrorAction SilentlyContinue |
            Where-Object {
                $taskName = [string]$_.TaskName
                $taskPath = [string]$_.TaskPath
                $keepAlive = $taskName -match "(?i)^wsl[-_ ]?keepalive$"
                (-not $keepAlive) -and (
                    ($taskPath -eq "\Anet\" -and $taskName -eq "Supervisor") -or
                    ($taskName -match $anetTokenPattern) -or
                    ($taskName -match $ahubTokenPattern)
                )
            } |
            ForEach-Object {
                [ordered]@{
                    kind = if (
                        ([string]$_.TaskName -match $ahubTokenPattern) -or
                        ([string]$_.TaskPath -match $ahubTokenPattern)
                    ) { "ahub" } else { "anet" }
                    manager = "scheduled-task"
                    name = ([string]$_.TaskPath + [string]$_.TaskName)
                    state = [string]$_.State
                }
            }
    )
    $processes = @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $name = [string]$_.Name
                $command = [string]$_.CommandLine
                ($name -match $anetTokenPattern) -or
                ($name -match $ahubTokenPattern) -or
                ($command -match "(?i)anet-fabric|(?:^|\s)-m\s+anet(?:\s|$)|anet\.exe|ahub-serve|anet\s+ahub-serve")
            } |
            ForEach-Object {
                $name = [string]$_.Name
                $command = [string]$_.CommandLine
                [ordered]@{
                    kind = if (
                        ($name -match $ahubTokenPattern) -or
                        ($command -match "(?i)ahub-serve|anet\s+ahub-serve")
                    ) { "ahub" } else { "anet" }
                    manager = "process"
                    pid = [int]$_.ProcessId
                }
            }
    )
}

$report = [ordered]@{
    schema_version = 1
    platform = "windows"
    target_root = $target
    target = @(
        $existingAnet | Where-Object { $_.path.ToLowerInvariant() -eq $target.ToLowerInvariant() }
    ) | Select-Object -First 1
    existing_anet = @($existingAnet)
    existing_ahub = @($existingAhub)
    services = @($services)
    tasks = @($tasks)
    processes = @($processes)
}

if ($Deployment -and -not $AllowExisting) {
    $foreign = @(
        $existingAnet |
            Where-Object { $_.path.ToLowerInvariant() -ne $target.ToLowerInvariant() }
    )
    $activeAnet = @(
        @($services) + @($tasks) + @($processes) |
            Where-Object {
                $_.kind -eq "anet" -and
                (
                    $null -eq $_.PSObject.Properties["state"] -or
                    [string]$_.state -ne "Disabled"
                )
            }
    )
    $targetExists = $null -ne $report.target
    if ($foreign.Count -gt 0 -or ($activeAnet.Count -gt 0 -and -not $targetExists)) {
        $locations = @($foreign | ForEach-Object { $_.path }) -join ", "
        $activeNames = @(
            $activeAnet | ForEach-Object {
                if ($null -ne $_.PSObject.Properties["name"]) {
                    [string]$_.name
                } elseif ($null -ne $_.PSObject.Properties["pid"]) {
                    "process:$($_.pid)"
                } else {
                    "active-anet"
                }
            }
        ) -join ", "
        $details = @()
        if ($locations) { $details += "existing roots: $locations" }
        if ($activeNames) { $details += "active items: $activeNames" }
        [Console]::Error.WriteLine(
            "Anet install preflight conflict: " + ($details -join "; ")
        )
        $report | ConvertTo-Json -Depth 10 -Compress
        exit 17
    }
}

$report | ConvertTo-Json -Depth 10 -Compress
exit 0
