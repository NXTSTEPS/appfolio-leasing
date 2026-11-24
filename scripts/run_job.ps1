# Run one scheduled bridge job (Apps Script edition) and log the result.
#
#   .\scripts\run_job.ps1 -Job leasing        # read leases, sheet sends reminders + summary
#   .\scripts\run_job.ps1 -Job leasing -Test  # same, but every email goes to the test address
#   .\scripts\run_job.ps1 -Job delinquency    # balances over the threshold -> sheet emails them
#   .\scripts\run_job.ps1 -Job moveouts       # new move-outs + 45-day ledger reviews
#   .\scripts\run_job.ps1 -Job lists          # vendors, properties, GL accounts -> sheet
#   .\scripts\run_job.ps1 -Job bills          # write approved bills into AppFolio
#   .\scripts\run_job.ps1 -Job payments       # check posted bills for payment
#   .\scripts\run_job.ps1 -Job health         # problems -> alert email from the sheet
#
# Secrets come from work\secret-*.dpapi through run_with_bridge_key.ps1 and are
# cleared when the job ends. A failed job emails the sheet's "Alert emails"
# (at most once every 6 hours per job). Log: work\logs\<job>.log
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('leasing', 'delinquency', 'moveouts', 'lists', 'bills', 'payments', 'health')]
    [string]$Job,
    [switch]$Test
)

. (Join-Path $PSScriptRoot '_config.ps1')
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$logDir = Join-Path $root 'work\logs'
New-Item -ItemType Directory -Force $logDir | Out-Null
$logPath = Join-Path $logDir "$Job.log"
$pythonPathFile = Join-Path $root 'work\bridge-python-path.txt'
$python = if (Test-Path $pythonPathFile) { (Get-Content $pythonPathFile -Raw).Trim() } else { (Get-Command python -ErrorAction Stop).Source }
$keyRunner = Join-Path $PSScriptRoot 'run_with_bridge_key.ps1'
$modules = $BridgeConfig.modules

function Test-Module([string]$name) {
    $m = $modules.$name
    return ($null -ne $m) -and [bool]$m.enabled
}

function Invoke-Step([string]$label, [string[]]$command) {
    Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format o) start $label"
    $output = @(& $keyRunner @command 2>&1)
    $code = $LASTEXITCODE
    $output | ForEach-Object { Add-Content -LiteralPath $logPath -Value "    $_" }
    Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format o) end $label exit=$code"
    $output | ForEach-Object { Write-Host $_ }
    return @{ Code = $code; Tail = (($output | Select-Object -Last 25) -join "`n") }
}

$mutex = New-Object Threading.Mutex($false, "Local\AppFolioBridgeJob_$Job")
$acquired = $false
try {
    try { $acquired = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $acquired = $true }
    if (!$acquired) {
        Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format o) skipped: previous $Job run still going"
        exit 0
    }

    $steps = @()
    switch ($Job) {
        'leasing' {
            $cmd = @($python, 'scripts\run_leasing.py', '--send-reminders', '--send-summary')
            if ($Test) { $cmd += '--test' }
            $steps += , @('leasing', $cmd)
        }
        'delinquency' { $steps += , @('delinquency', @($python, '-m', 'scripts.send_delinquency_to_n8n')) }
        'moveouts' {
            $steps += , @('detect move-outs', @($python, '-m', 'scripts.sync_moveout_workflows'))
            if (Test-Module 'moveout_reminder') {
                $steps += , @('move-out reminders', @($python, '-m', 'scripts.send_moveout_events_to_n8n'))
            }
            if (Test-Module 'moveout_ledger_review') {
                $steps += , @('45-day ledger reviews', @($python, '-m', 'scripts.send_moveout_ledger_reviews_to_n8n'))
            }
        }
        'lists' { $steps += , @('bill lists', @($python, 'scripts\sync_bill_lists.py')) }
        'bills' {
            $approvers = @($modules.bills.approvers) -join ','
            if (!$approvers) { throw 'modules.bills.approvers is empty in config\company.json' }
            $env:REIMBURSEMENT_APPROVER_ALLOWLIST = $approvers.ToLowerInvariant()
            $cmd = @($python, 'scripts\poll_reimbursement_approvals.py', '--max-items', '10')
            if ([bool]$modules.bills.save_bills) {
                $cmd += '--save'
                $env:ENABLE_APPFOLIO_BILL_SAVE = 'YES'   # second, independent save gate
            }
            $steps += , @('bills', $cmd)
        }
        'payments' { $steps += , @('bill payments', @($python, 'scripts\check_reimbursement_payments.py')) }
        'health' { $steps += , @('health', @($python, 'scripts\check_bridge_health.py', '--alert')) }
    }

    $failed = @()
    foreach ($step in $steps) {
        $result = Invoke-Step $step[0] $step[1]
        if ($result.Code -ne 0) { $failed += "$($step[0]) (exit $($result.Code)):`n$($result.Tail)" }
    }
    if ($failed.Count -gt 0 -and $Job -ne 'health') {
        # health reports its own problems; every other job alerts on failure.
        $env:BRIDGE_JOB_SUMMARY = "Scheduled job '$Job' failed"
        $env:BRIDGE_JOB_DETAIL = ($failed -join "`n`n")
        $env:BRIDGE_JOB_NAME = $Job
        & $keyRunner $python 'scripts\job_alert.py' | Out-Null
    }
    exit $(if ($failed.Count -gt 0) { 1 } else { 0 })
} finally {
    foreach ($name in 'REIMBURSEMENT_APPROVER_ALLOWLIST', 'ENABLE_APPFOLIO_BILL_SAVE', 'BRIDGE_JOB_SUMMARY', 'BRIDGE_JOB_DETAIL', 'BRIDGE_JOB_NAME') {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
    if ($acquired) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
