. (Join-Path $PSScriptRoot '_config.ps1')
# Refresh the AppFolio session, reusing the EXISTING bridge encryption key.
#
# Use this — not create_appfolio_session.ps1 — when the session has simply
# expired. create_appfolio_session.ps1 mints a NEW key and overwrites
# work\bridge-key.dpapi, which would permanently orphan every payload and PDF
# already encrypted in work\appfolio-bridge.sqlite3 under the old key.
#
# A visible browser opens for you to sign in yourself. Nothing here reads,
# stores, or types your password or MFA code.
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

$keyPath = Join-Path $root 'work\bridge-key.dpapi'
if (!(Test-Path -LiteralPath $keyPath -PathType Leaf)) {
    throw 'No Windows-protected bridge key exists. This is a first-time setup: run create_appfolio_session.ps1 instead.'
}

$pythonPathFile = Join-Path $root 'work\bridge-python-path.txt'
if (!(Test-Path -LiteralPath $pythonPathFile -PathType Leaf)) {
    throw 'Missing the configured Python executable path.'
}
$pythonPath = (Get-Content -LiteralPath $pythonPathFile -Raw).Trim()
if (!(Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'The configured Python executable no longer exists.'
}

# Keep the current session so a failed login is recoverable.
$sessionPath = Join-Path $root 'work\appfolio-session.enc'
if (Test-Path -LiteralPath $sessionPath -PathType Leaf) {
    $backupDir = Join-Path $root 'work\backups'
    if (!(Test-Path -LiteralPath $backupDir)) {
        New-Item -ItemType Directory -Path $backupDir | Out-Null
    }
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
    $backup = Join-Path $backupDir "appfolio-session.enc.$stamp"
    Copy-Item -LiteralPath $sessionPath -Destination $backup
    Write-Output "Backed up the previous session to $backup"
}

# Do not let a scheduled sync overwrite the session mid-login.
$mutex = New-Object Threading.Mutex($false, 'Local\AppFolioBridgeBridgeSync')
$acquired = $false
try {
    $acquired = $mutex.WaitOne(0)
    if (!$acquired) {
        throw 'An AppFolio sync is running right now. Wait for it to finish, then rerun.'
    }

    if (!$env:APPFOLIO_SERVICE_EMAIL) { $env:APPFOLIO_SERVICE_EMAIL = $ServiceEmail }
    if (!$env:APPFOLIO_LOGIN_URL) { $env:APPFOLIO_LOGIN_URL = "$AppFolioBaseUrl/" }

    & (Join-Path $PSScriptRoot 'run_with_bridge_key.ps1') $pythonPath 'scripts\connect_appfolio.py'
    if ($LASTEXITCODE -ne 0) {
        throw "Session capture failed (exit $LASTEXITCODE). The previous session file is unchanged."
    }

    Write-Output ''
    Write-Output 'Verifying the new session against AppFolio...'
    & (Join-Path $PSScriptRoot 'run_with_bridge_key.ps1') $pythonPath 'scripts\verify_appfolio_session.py'
    exit $LASTEXITCODE
} finally {
    Remove-Item Env:APPFOLIO_SERVICE_EMAIL -ErrorAction SilentlyContinue
    Remove-Item Env:APPFOLIO_LOGIN_URL -ErrorAction SilentlyContinue
    if ($acquired) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
