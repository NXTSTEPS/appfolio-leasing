. (Join-Path $PSScriptRoot '_config.ps1')
# Creates a new encrypted AppFolio session and protects its key with Windows DPAPI.
# Run this only on the Windows account that will operate the bridge.
Set-Location (Split-Path $PSScriptRoot -Parent)
if (!$env:APPFOLIO_SERVICE_EMAIL) { $env:APPFOLIO_SERVICE_EMAIL = Read-Host 'Enter the AppFolio service email' }
if (!$env:APPFOLIO_LOGIN_URL) { $env:APPFOLIO_LOGIN_URL = "$AppFolioBaseUrl/" }
$secureKey = Read-Host 'Create a new bridge encryption key (at least 24 characters)' -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
try {
    $plainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    if ($plainKey.Length -lt 24) { throw 'The key must be at least 24 characters.' }
    $secureKey | ConvertFrom-SecureString | Set-Content 'work\bridge-key.dpapi' -NoNewline
    $env:BRIDGE_DATA_KEY = $plainKey
    python scripts\connect_appfolio.py
} finally {
    if ($bstr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    Remove-Item Env:BRIDGE_DATA_KEY -ErrorAction SilentlyContinue
}
