# Run one non-overlapping sync. Each run also writes an encrypted-data manifest.
#
# Two failure modes this guards against, both seen live on 2026-09-05:
#
#   1. sync_appfolio.py hung inside Playwright with no timeout and held the
#      sync mutex for 35 hours. Nothing killed it, so no sync ran for a day
#      and a half and the AppFolio session silently expired.
#   2. Every blocked run exited 0 ("another sync is running"), so Task
#      Scheduler reported success the entire time and the outage was invisible.
#
# So: bound the run with a hard timeout, and make a *persistent* lock-out
# report a failure instead of a success.
param(
    # A healthy sync takes ~2 minutes. This is a backstop, not a target.
    [ValidateRange(2, 240)]
    [int]$TimeoutMinutes = 20,
    # Consecutive skips tolerated before the task reports failure. The sync
    # runs every 15 minutes, so 4 skips is roughly an hour of no syncing.
    [ValidateRange(1, 100)]
    [int]$MaxConsecutiveSkips = 4
)

$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$logPath = Join-Path $root 'work\bridge-sync.log'
$skipStatePath = Join-Path $root 'work\bridge-sync-skips.txt'

function Write-SyncLog {
    param([Parameter(Mandatory = $true)][string]$Message)
    Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format o) $Message"
}

function Get-SkipCount {
    if (!(Test-Path -LiteralPath $skipStatePath -PathType Leaf)) { return 0 }
    $raw = (Get-Content -LiteralPath $skipStatePath -Raw).Trim()
    $parsed = 0
    if ([int]::TryParse($raw, [ref]$parsed) -and $parsed -ge 0) { return $parsed }
    return 0
}

function Set-SkipCount {
    param([Parameter(Mandatory = $true)][int]$Count)
    Set-Content -LiteralPath $skipStatePath -Value $Count -Encoding utf8
}

$pythonPathFile = Join-Path $root 'work\bridge-python-path.txt'
if (!(Test-Path -LiteralPath $pythonPathFile -PathType Leaf)) {
    throw 'Missing the configured Python executable path.'
}
$pythonPath = (Get-Content -LiteralPath $pythonPathFile -Raw).Trim()
if (!(Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'The configured Python executable no longer exists.'
}
$keyRunner = Join-Path $PSScriptRoot 'run_with_bridge_key.ps1'

$mutex = New-Object Threading.Mutex($false, 'Local\AppFolioBridgeBridgeSync')
$acquired = $false
try {
    try {
        $acquired = $mutex.WaitOne(0)
    } catch [Threading.AbandonedMutexException] {
        # The previous owner died without releasing. We now hold it; say so
        # rather than inheriting a lock silently.
        $acquired = $true
        Write-SyncLog 'recovered an abandoned sync mutex from a dead run'
    }

    if (!$acquired) {
        $skips = (Get-SkipCount) + 1
        Set-SkipCount $skips
        if ($skips -ge $MaxConsecutiveSkips) {
            # Do NOT exit 0 here. A sync that has been locked out this long is
            # an outage, and Task Scheduler is the only thing watching.
            Write-SyncLog (
                "ALERT: sync has been locked out $skips consecutive runs; " +
                'a previous sync is probably hung. Check for a stale ' +
                'sync_appfolio.py process holding Local\AppFolioBridgeBridgeSync.'
            )
            Write-Output "AppFolio sync locked out for $skips consecutive runs."
            exit 1
        }
        Write-SyncLog "skipped: another sync is running (consecutive skips: $skips)"
        Write-Output 'Another AppFolio sync is already running.'
        exit 0
    }

    Set-SkipCount 0

    # Run the sync as a child process so it can be bounded and killed as a
    # tree -- a hung Playwright run leaves node/chrome children behind, and
    # those are what actually pin the browser session.
    $started = Get-Date
    # Start-Process joins -ArgumentList with spaces and does NOT quote for you.
    # The repository path contains a space ("Appfolio API"), so unquoted paths
    # are split mid-path and powershell.exe exits -196608 before running
    # anything. Quote every path argument explicitly.
    $proc = Start-Process -FilePath 'powershell.exe' -PassThru -WindowStyle Hidden `
        -WorkingDirectory $root `
        -ArgumentList @(
            '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
            '-File', "`"$keyRunner`"", "`"$pythonPath`"",
            "`"$(Join-Path $root 'scripts\sync_appfolio.py')`""
        )
    if (!$proc.WaitForExit($TimeoutMinutes * 60 * 1000)) {
        Write-SyncLog "ALERT: sync exceeded $TimeoutMinutes minutes; killing process tree $($proc.Id)"
        # /T takes the descendants (python, playwright node, chrome) with it.
        & taskkill.exe /PID $proc.Id /T /F 2>&1 | Out-Null
        try { $proc.WaitForExit(30000) | Out-Null } catch { }
        Write-Output "AppFolio sync timed out after $TimeoutMinutes minutes and was terminated."
        exit 1
    }
    $syncExitCode = $proc.ExitCode
    $elapsed = [int]((Get-Date) - $started).TotalSeconds
    if ($syncExitCode -ne 0) {
        Write-SyncLog "sync failed (exit $syncExitCode) after ${elapsed}s"
        exit $syncExitCode
    }
    Write-SyncLog "sync success in ${elapsed}s"

    $moveOutWebhook = Join-Path $root 'work\n8n-moveout-webhook.dpapi'
    if (Test-Path -LiteralPath $moveOutWebhook) {
        & (Join-Path $PSScriptRoot 'scheduled_moveout_push.ps1')
        exit $LASTEXITCODE
    }
    exit 0
} finally {
    if ($acquired) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}
