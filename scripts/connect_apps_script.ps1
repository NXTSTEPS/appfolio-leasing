# Connect this PC to the Google Sheet's Apps Script.
#
# In the Google Sheet: AppFolio Tools > Connect the PC bridge. Copy the code
# (it starts with AFB1.), run this script and paste it at the hidden prompt.
#
#   .\scripts\connect_apps_script.ps1
#   .\scripts\connect_apps_script.ps1 -TestOnly          # just ping again
#   .\scripts\connect_apps_script.ps1 -SendTestEmail     # ping + one test email
#
# The URL, token and bill-approval key are saved with Windows DPAPI under
# work\secret-*.dpapi (only this Windows user on this PC can read them).
param([switch]$TestOnly, [switch]$SendTestEmail)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$work = Join-Path $root 'work'
New-Item -ItemType Directory -Force $work | Out-Null
$pythonPathFile = Join-Path $work 'bridge-python-path.txt'
$python = if (Test-Path $pythonPathFile) { (Get-Content $pythonPathFile -Raw).Trim() } else { (Get-Command python -ErrorAction Stop).Source }

function Read-Secret([string]$name) {
    $path = Join-Path $work "secret-$name.dpapi"
    if (!(Test-Path $path)) { return $null }
    $secure = (Get-Content -LiteralPath $path -Raw).Trim() | ConvertTo-SecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

if (!$TestOnly) {
    $secureCode = Read-Host 'Paste the connection code from the Google Sheet (input hidden)' -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureCode)
    try { $env:APPS_SCRIPT_CONNECTION_CODE = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    try {
        $decoded = & $python (Join-Path $PSScriptRoot 'apps_script_check.py') --decode
        if ($LASTEXITCODE -ne 0) { throw 'The connection code was not accepted. Copy it again from AppFolio Tools > Connect the PC bridge.' }
    } finally {
        Remove-Item Env:APPS_SCRIPT_CONNECTION_CODE -ErrorAction SilentlyContinue
    }
    $values = $decoded | ConvertFrom-Json
    foreach ($property in $values.PSObject.Properties) {
        $secure = ConvertTo-SecureString $property.Value -AsPlainText -Force
        $secure | ConvertFrom-SecureString | Set-Content -LiteralPath (Join-Path $work "secret-$($property.Name).dpapi") -NoNewline
        Write-Output "Saved $($property.Name) (Windows-protected)."
    }
    $decoded = $null; $values = $null
}

$env:APPS_SCRIPT_URL = Read-Secret 'APPS_SCRIPT_URL'
$env:APPS_SCRIPT_TOKEN = Read-Secret 'APPS_SCRIPT_TOKEN'
try {
    if (!$env:APPS_SCRIPT_URL -or !$env:APPS_SCRIPT_TOKEN) { throw 'No saved connection. Run this script without -TestOnly first.' }
    $checkArgs = @((Join-Path $PSScriptRoot 'apps_script_check.py'))
    if ($SendTestEmail) { $checkArgs += '--send-test-email' }
    & $python @checkArgs
    exit $LASTEXITCODE
} finally {
    Remove-Item Env:APPS_SCRIPT_URL -ErrorAction SilentlyContinue
    Remove-Item Env:APPS_SCRIPT_TOKEN -ErrorAction SilentlyContinue
}
