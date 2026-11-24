# Register user-level polling and API startup tasks.
param(
    [ValidateRange(5, 1440)]
    [int]$EveryMinutes = 60
)

. (Join-Path $PSScriptRoot '_config.ps1')
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$pythonPath = (Get-Command python -ErrorAction Stop).Source
$pythonwPath = Join-Path (Split-Path $pythonPath -Parent) 'pythonw.exe'
$hiddenLauncher = Join-Path $PSScriptRoot 'run_hidden_powershell.pyw'
$syncScript = Join-Path $PSScriptRoot 'scheduled_sync.ps1'
$startScript = Join-Path $PSScriptRoot 'start_bridge.ps1'
if (
    !(Test-Path -LiteralPath $hiddenLauncher -PathType Leaf) -or
    !(Test-Path -LiteralPath $pythonwPath -PathType Leaf)
) {
    throw 'The no-console PowerShell launcher is missing.'
}

$syncAction = New-ScheduledTaskAction `
    -Execute $pythonwPath `
    -Argument "`"$hiddenLauncher`" `"$syncScript`"" `
    -WorkingDirectory $root
$syncTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$syncSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
Register-ScheduledTask `
    -TaskName "$TaskPrefix Bridge Sync" `
    -Action $syncAction `
    -Trigger $syncTrigger `
    -Settings $syncSettings `
    -Description "Read-only AppFolio cache sync every $EveryMinutes minutes." `
    -Force | Out-Null

if (!(Test-Path -LiteralPath (Join-Path $root 'app\server.py'))) {
    # The leasing-only edition has no local API.
    Write-Output "Installed AppFolio sync every $EveryMinutes minutes."
    return
}
$apiAction = New-ScheduledTaskAction `
    -Execute $pythonwPath `
    -Argument "`"$hiddenLauncher`" `"$startScript`"" `
    -WorkingDirectory $root
$currentUser = "$env:USERDOMAIN\$env:USERNAME"
$apiTrigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$apiSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable
Register-ScheduledTask `
    -TaskName "$TaskPrefix Bridge API" `
    -Action $apiAction `
    -Trigger $apiTrigger `
    -Settings $apiSettings `
    -Description 'Start the local AppFolio bridge API when this user signs in.' `
    -Force | Out-Null

Write-Output (
    "Installed AppFolio sync every $EveryMinutes minutes and API startup " +
    "at sign-in for $currentUser."
)
