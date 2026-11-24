# Install the Windows scheduled tasks for the Apps Script edition.
# Only modules turned on in config\company.json get a task. Safe to re-run.
#
#   .\scripts\install_schedules.ps1             # install / update
#   .\scripts\install_schedules.ps1 -List       # show what is installed
#   .\scripts\install_schedules.ps1 -Remove     # remove every task this product created
#
# Times (change in config\company.json > schedule):
#   AppFolio sync ........ every 60 minutes (always; keeps the local copy fresh).
#                          Reads only the reports the enabled modules use: AppFolio
#                          allows 500 report runs a day per login, shared by every PC.
#   Health check ......... every hour
#   Leasing .............. weekdays 9:00 AM
#   Delinquency .......... weekdays 8:05 AM
#   Move-outs ............ every 15 minutes
#   Bill lists ........... daily 6:30 AM
#   Bills ................ every 5 minutes
#   Bill payments ........ every 15 minutes
param([switch]$List, [switch]$Remove)

. (Join-Path $PSScriptRoot '_config.ps1')
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent

if ($List) {
    Get-ScheduledTask | Where-Object { $_.TaskName -like "$TaskPrefix *" } |
        Select-Object TaskName, State | Format-Table -AutoSize
    exit 0
}
if ($Remove) {
    Get-ScheduledTask | Where-Object { $_.TaskName -like "$TaskPrefix *" } | ForEach-Object {
        Unregister-ScheduledTask -TaskName $_.TaskName -Confirm:$false
        Write-Output "Removed $($_.TaskName)"
    }
    exit 0
}

$pythonPath = (Get-Command python -ErrorAction Stop).Source
New-Item -ItemType Directory -Force (Join-Path $root 'work') | Out-Null
Set-Content -LiteralPath (Join-Path $root 'work\bridge-python-path.txt') -Value $pythonPath -NoNewline
$pythonw = Join-Path (Split-Path $pythonPath -Parent) 'pythonw.exe'
if (!(Test-Path $pythonw)) { throw 'pythonw.exe was not found next to python.exe' }
$launcher = Join-Path $PSScriptRoot 'run_hidden_powershell.pyw'
$schedule = $BridgeConfig.schedule
$modules = $BridgeConfig.modules

function Get-Setting($value, $default) { if ($null -ne $value -and "$value" -ne '') { return $value } else { return $default } }
function Test-Module([string]$name) { $m = $modules.$name; return ($null -ne $m) -and [bool]$m.enabled }

$weekdays = 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'
function New-Settings([int]$limitMinutes) {
    New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -RunOnlyIfNetworkAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 2 `
        -RestartInterval (New-TimeSpan -Minutes 10) -ExecutionTimeLimit (New-TimeSpan -Minutes $limitMinutes)
}
function Every([int]$minutes) {
    New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes $minutes) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
}
function Register-Job([string]$name, [string]$script, [string]$arguments, $trigger, [int]$limit, [string]$description) {
    $argLine = "`"$launcher`" `"$(Join-Path $PSScriptRoot $script)`""
    if ($arguments) { $argLine += " $arguments" }
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument $argLine -WorkingDirectory $root
    Register-ScheduledTask -TaskName "$TaskPrefix $name" -Action $action -Trigger $trigger `
        -Settings (New-Settings $limit) -Description $description -Force | Out-Null
    Write-Output "Installed '$TaskPrefix $name'"
}
function Remove-Job([string]$name) {
    if (Get-ScheduledTask -TaskName "$TaskPrefix $name" -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName "$TaskPrefix $name" -Confirm:$false
        Write-Output "Removed '$TaskPrefix $name' (module turned off)"
    }
}

# Always: the read-only AppFolio sync and the local API (install_schedule.ps1 owns these).
$syncEvery = [int](Get-Setting $schedule.sync_every_minutes 60)
& (Join-Path $PSScriptRoot 'install_schedule.ps1') -EveryMinutes $syncEvery
# AppFolio allows 500 report runs a day (resets at midnight Eastern), shared by every PC on the same login.
Push-Location $root
try {
    $plan = & $pythonPath -c "from app import settings as s; r = s.sync_resources(); print(len(r)); print(', '.join(r))"
} finally { Pop-Location }
if ($LASTEXITCODE -eq 0 -and $plan.Count -ge 2) {
    $runsPerDay = [int]$plan[0] * [math]::Floor(1440 / $syncEvery)
    Write-Output "AppFolio sync: $($plan[1]) every $syncEvery minutes = about $runsPerDay report runs a day (AppFolio allows 500 a day per login)."
    if ($runsPerDay -gt 250) {
        Write-Warning ("That uses more than half of AppFolio's 500 daily report runs. Another PC or install on the same login can run out. " +
            'Raise schedule.sync_every_minutes or trim appfolio.sync_reports in config\company.json.')
    }
}
Register-Job 'Health' 'run_job.ps1' '-Job health' (Every 60) 10 'Checks the AppFolio session and sync; emails the sheet''s alert list on problems.'

if (Test-Module 'leasing') {
    $at = Get-Setting $schedule.leasing_at '9:00 AM'
    Register-Job 'Leasing' 'run_job.ps1' '-Job leasing' (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At $at) 20 `
        'Reads AppFolio Out for Signing leases; the Google Sheet sends tenant reminders and the staff summary.'
} else { Remove-Job 'Leasing' }

if (Test-Module 'delinquency') {
    $at = Get-Setting $schedule.delinquency_at '8:05 AM'
    Register-Job 'Delinquency' 'run_job.ps1' '-Job delinquency' (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At $at) 30 `
        'Balances over the threshold; the Google Sheet emails the report.'
} else { Remove-Job 'Delinquency' }

if ((Test-Module 'moveout_reminder') -or (Test-Module 'moveout_ledger_review')) {
    Register-Job 'Move-outs' 'run_job.ps1' '-Job moveouts' (Every 15) 20 `
        'New move-outs and 45-day ledger reviews; the Google Sheet sends the emails.'
} else { Remove-Job 'Move-outs' }

if (Test-Module 'bills') {
    $listsAt = Get-Setting $schedule.bill_lists_at '6:30 AM'
    Register-Job 'Bill Lists' 'run_job.ps1' '-Job lists' (New-ScheduledTaskTrigger -Daily -At $listsAt) 20 `
        'Sends vendors, properties, GL accounts and cost categories to the Google Sheet.'
    Register-Job 'Bills' 'run_job.ps1' '-Job bills' (Every 5) 20 'Writes bills approved in the Google Sheet into AppFolio.'
    Register-Job 'Bill Payments' 'run_job.ps1' '-Job payments' (Every 15) 20 'Checks posted bills for payment and updates the sheet.'
} else { Remove-Job 'Bill Lists'; Remove-Job 'Bills'; Remove-Job 'Bill Payments' }

Write-Output ''
Write-Output 'Done. See them with: .\scripts\install_schedules.ps1 -List'
