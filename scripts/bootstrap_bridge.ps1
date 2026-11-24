. (Join-Path $PSScriptRoot '_config.ps1')
# Bootstrap the bridge project without writing its API key in plaintext.
Set-Location (Split-Path $PSScriptRoot -Parent)
if (!(Test-Path 'work\bridge-key.dpapi')) { throw 'Missing Windows-protected bridge data key.' }
$secureDataKey = Get-Content 'work\bridge-key.dpapi' -Raw | ConvertTo-SecureString
$dataBstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureDataKey)
$randomBytes = New-Object byte[] 32
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
$rng.GetBytes($randomBytes)
$rng.Dispose()
$plainApiKey = 'afib_' + [Convert]::ToBase64String($randomBytes).Replace('+','-').Replace('/','_').TrimEnd('=')
$secureApiKey = ConvertTo-SecureString $plainApiKey -AsPlainText -Force
$secureApiKey | ConvertFrom-SecureString | Set-Content 'work\bridge-api-key.dpapi' -NoNewline
try {
    $env:BRIDGE_DATA_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($dataBstr)
    $env:BRIDGE_BOOTSTRAP_API_KEY = $plainApiKey
    $env:APPFOLIO_SERVICE_EMAIL = $ServiceEmail
    python scripts\init_project.py
} finally {
    if ($dataBstr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($dataBstr) }
    Remove-Item Env:BRIDGE_DATA_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:BRIDGE_BOOTSTRAP_API_KEY -ErrorAction SilentlyContinue
    $plainApiKey = $null
}
