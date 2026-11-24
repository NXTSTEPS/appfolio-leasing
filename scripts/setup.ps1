# Guided setup: AppFolio Bridge (this PC) + Google Sheet (Apps Script).
# Safe to re-run: every step checks whether it is already done.
#
#   powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
#
# Before you start, set up the Google Sheet (apps-script\README.md, part A) so
# you have its connection code (AppFolio Tools > Connect the PC bridge).
# You sign in to AppFolio yourself (with MFA) in a browser window that opens.
param([switch]$SkipSchedules)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
function Step($n, $text) { Write-Host "`n[$n] $text" -ForegroundColor Cyan }
function Ask($prompt, $default) {
    $answer = Read-Host "$prompt$(if ($default) { " [$default]" })"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $default } else { return $answer.Trim() }
}
function AskYes($prompt, [bool]$default) {
    $d = if ($default) { 'Y' } else { 'N' }
    return ((Ask "$prompt (Y/N)" $d) -match '^[Yy]')
}
function Run-WithKey([string[]]$command) {
    & (Join-Path $PSScriptRoot 'run_with_bridge_key.ps1') @command | ForEach-Object { Write-Host $_ }
    return $LASTEXITCODE
}

Step 1 'Checking Python'
$python = Get-Command python -ErrorAction SilentlyContinue
if (!$python) { throw 'Python 3.11+ is required. Install it from https://www.python.org/downloads/ (tick "Add to PATH"), then re-run.' }
$version = & python -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$version -lt [version]'3.11') { throw "Python $version found; 3.11 or newer is required." }
Write-Host "Python $version OK"
New-Item -ItemType Directory -Force (Join-Path $root 'work') | Out-Null
Set-Content -LiteralPath (Join-Path $root 'work\bridge-python-path.txt') -Value $python.Source -NoNewline

Step 2 'Installing dependencies (a few minutes the first time)'
& python -m pip install --quiet -r requirements.txt
& python -m playwright install chromium

Step 3 'Company settings'
$configPath = Join-Path $root 'config\company.json'
if (Test-Path $configPath) {
    Write-Host 'config\company.json already exists - keeping it. (Edit it in Notepad any time.)'
} else {
    $config = Get-Content (Join-Path $root 'config\company.example.json') -Raw | ConvertFrom-Json
    $config.company.name = Ask 'Company name' $config.company.name
    $sub = Ask 'Your AppFolio address, e.g. "acme" for acme.appfolio.com' ''
    $sub = ($sub -replace '^https?://', '' -replace '\.appfolio\.com.*$', '').Trim('/')
    if (!$sub) { throw 'The AppFolio address is required.' }
    $config.appfolio.base_url = "https://$sub.appfolio.com"
    $config.appfolio.tenant_portal_url = "https://$sub.appfolio.com/connect"
    $config.appfolio.service_email = Ask 'AppFolio login email the bridge will use (a dedicated user is best)' ''
    $config.company.domain = ($config.appfolio.service_email -split '@')[-1]
    $config.company.admin_email = Ask 'Your email (setup messages)' $config.appfolio.service_email
    $config.company.timezone = Ask 'Time zone (IANA name)' $config.company.timezone
    $config.windows.task_prefix = "$($config.company.name) AppFolio"
    Write-Host ''
    Write-Host 'Which features do you want? (Who gets each email is set in the Google Sheet.)'
    $config.modules.leasing.enabled = AskYes 'Leasing: unsigned-lease reminders + daily leasing summary' $true
    # Only modules this edition ships are asked about (the leasing-only edition has just leasing).
    if ($config.modules.delinquency) { $config.modules.delinquency.enabled = AskYes 'Delinquency: daily list of balances over a threshold' $true }
    if ($config.modules.moveout_reminder) { $config.modules.moveout_reminder.enabled = AskYes 'Move-out alert: email when a tenant move-out starts' $false }
    if ($config.modules.moveout_ledger_review) { $config.modules.moveout_ledger_review.enabled = AskYes 'Ledger review: email the tenant ledger PDF 45 days after move-out' $false }
    if ($config.modules.bills) { $config.modules.bills.enabled = AskYes 'Bills: approve bills/reimbursements in the sheet, bridge enters them in AppFolio' $false }
    if ($config.modules.bills -and $config.modules.bills.enabled) {
        $approvers = Ask 'Who may approve bills? (Google emails, comma-separated)' $config.company.admin_email
        $config.modules.bills.approvers = @($approvers -split ',' | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ })
    }
    $config | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $configPath -Encoding UTF8
    Write-Host "Saved $configPath"
}
. (Join-Path $PSScriptRoot '_config.ps1')
$modules = $BridgeConfig.modules

Step 4 'Connect to AppFolio (you sign in yourself, including MFA)'
if (Test-Path (Join-Path $root 'work\appfolio-session.enc')) {
    Write-Host 'An AppFolio session already exists. To replace it run scripts\refresh_appfolio_session.ps1'
} else {
    Write-Host 'A browser window will open. Sign in, finish MFA, wait for the AppFolio dashboard,'
    Write-Host 'then come back here and press Enter. Choose a passphrase of 24+ characters and keep it in your password manager.'
    & (Join-Path $PSScriptRoot 'create_appfolio_session.ps1')
}

Step 5 'Create the local bridge project'
if (Test-Path (Join-Path $root 'work\bridge-api-key.dpapi')) {
    Write-Host 'Bridge project already initialised.'
} else {
    & (Join-Path $PSScriptRoot 'bootstrap_bridge.ps1')
}

Step 6 'First full read from AppFolio (5-15 minutes)'
$env:BRIDGE_ESTABLISH_BASELINE = '1'
try {
    if ((Run-WithKey @('python', 'scripts\sync_appfolio.py')) -ne 0) { throw 'The first sync failed - see the messages above.' }
} finally { Remove-Item Env:BRIDGE_ESTABLISH_BASELINE -ErrorAction SilentlyContinue }

Step 7 'Connect the Google Sheet'
$connected = (Test-Path (Join-Path $root 'work\secret-APPS_SCRIPT_URL.dpapi')) -and (Test-Path (Join-Path $root 'work\secret-APPS_SCRIPT_TOKEN.dpapi'))
if ($connected) {
    Write-Host 'Already connected - testing again.'
    & (Join-Path $PSScriptRoot 'connect_apps_script.ps1') -TestOnly
} else {
    Write-Host 'In the Google Sheet: AppFolio Tools > Connect the PC bridge, and copy the code (starts with AFB1.).'
    & (Join-Path $PSScriptRoot 'connect_apps_script.ps1')
}
if ($LASTEXITCODE -ne 0) { throw 'The Google Sheet did not answer. Check the web app deployment (apps-script\README.md, step A4) and re-run setup.' }

Step 8 'Baselines (so old move-outs are not emailed as new)'
if ($modules.moveout_reminder.enabled -or $modules.moveout_ledger_review.enabled) {
    if (Test-Path (Join-Path $root 'work\n8n-moveout-cursor.json')) { Write-Host 'Move-out baseline already exists.' }
    else {
        Run-WithKey @('python', '-m', 'scripts.sync_moveout_workflows', '--initialize') | Out-Null
        Run-WithKey @('python', '-m', 'scripts.send_moveout_events_to_n8n', '--initialize') | Out-Null
        Run-WithKey @('python', '-m', 'scripts.send_moveout_ledger_reviews_to_n8n', '--initialize') | Out-Null
        Write-Host 'Existing move-outs recorded; only new ones will be emailed.'
    }
} else { Write-Host 'Move-out features are off - nothing to baseline.' }

Step 9 'Fill the Google Sheet (no emails are sent)'
if ($modules.leasing.enabled) {
    if ((Run-WithKey @('python', 'scripts\run_leasing.py', '--sync-only')) -ne 0) { Write-Warning 'Leasing data could not be sent - see above.' }
}
if ($modules.bills.enabled) {
    if ((Run-WithKey @('python', 'scripts\sync_bill_lists.py')) -ne 0) { Write-Warning 'Bill lists could not be sent - see above.' }
}

if (!$SkipSchedules) {
    Step 10 'Install scheduled tasks'
    & (Join-Path $PSScriptRoot 'install_schedules.ps1')
}

Write-Host "`nSetup complete." -ForegroundColor Green
Write-Host 'Open the Google Sheet: the Lease Tracker now shows the unsigned leases from AppFolio.'
Write-Host 'Test run (emails go only to the Test mode email in the sheet):  .\scripts\run_job.ps1 -Job leasing -Test'
Write-Host 'Go live: in the Settings tab of the sheet clear "Test mode email" and set "Send tenant reminders" to YES.'
