# Save a secret (password, webhook URL, API key) protected by Windows DPAPI.
# The value is typed into a hidden prompt, never shown, never written in plain text,
# and only this Windows user on this PC can decrypt it.
#
#   .\scripts\set_secret.ps1 -Name SMTP_PASSWORD
#   .\scripts\set_secret.ps1 -Name N8N_EMAIL_WEBHOOK_URL
#
# run_with_bridge_key.ps1 loads every saved secret into the child process as an
# environment variable of the same name, then clears it again.
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Z][A-Z0-9_]{2,63}$')]
    [string]$Name
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$work = Join-Path $root 'work'
New-Item -ItemType Directory -Force $work | Out-Null
$secure = Read-Host "Enter the value for $Name (input hidden)" -AsSecureString
if ($secure.Length -eq 0) { throw 'Nothing entered; secret not saved.' }
$secure | ConvertFrom-SecureString | Set-Content -LiteralPath (Join-Path $work "secret-$Name.dpapi") -NoNewline
Write-Output "Saved $Name (Windows-protected) to work\secret-$Name.dpapi"
