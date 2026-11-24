# Dot-source this file to read config/company.json in PowerShell scripts:
#   . (Join-Path $PSScriptRoot '_config.ps1')
# Exposes $BridgeConfig, $TaskPrefix, $AppFolioBaseUrl, $ServiceEmail, $N8nBaseUrl.
$bridgeRoot = Split-Path $PSScriptRoot -Parent
$configPath = if ($env:BRIDGE_CONFIG) { $env:BRIDGE_CONFIG } else { Join-Path $bridgeRoot 'config\company.json' }
if (!(Test-Path -LiteralPath $configPath)) {
    $configPath = Join-Path $bridgeRoot 'config\company.example.json'
}
$BridgeConfig = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json

$TaskPrefix = if ($BridgeConfig.windows.task_prefix) { $BridgeConfig.windows.task_prefix } else { 'AppFolio Bridge' }
$AppFolioBaseUrl = ([string]$BridgeConfig.appfolio.base_url).TrimEnd('/')
$ServiceEmail = [string]$BridgeConfig.appfolio.service_email
$N8nBaseUrl = ([string]$BridgeConfig.n8n.base_url).TrimEnd('/')

if (!$env:APPFOLIO_LOGIN_URL -and $AppFolioBaseUrl) { $env:APPFOLIO_LOGIN_URL = "$AppFolioBaseUrl/" }
if (!$env:APPFOLIO_SERVICE_EMAIL -and $ServiceEmail) { $env:APPFOLIO_SERVICE_EMAIL = $ServiceEmail }
