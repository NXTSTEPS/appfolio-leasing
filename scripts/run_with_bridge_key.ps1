# Runs a bridge command with the key decrypted only in the child process memory.
param(
    [Parameter(Mandatory = $true, ValueFromRemainingArguments = $true)]
    [string[]]$Command
)

$ErrorActionPreference = 'Stop'
# Codex can launch this Windows PowerShell wrapper from PowerShell 7, whose
# inherited PSModulePath points at incompatible PS7 modules.  Restrict module
# discovery to Windows PowerShell's own built-ins before loading DPAPI cmdlets.
if ($PSVersionTable.PSEdition -eq 'Desktop') {
    $windowsPowerShellModules = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\Modules'
    $env:PSModulePath = $windowsPowerShellModules
    Import-Module Microsoft.PowerShell.Security -ErrorAction Stop
}
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$keyPath = Join-Path $root 'work\bridge-key.dpapi'
$vendorResolutionUrlPath = Join-Path $root 'work\n8n-vendor-resolution-webhook.dpapi'
$vendorResolutionTokenPath = Join-Path $root 'work\n8n-vendor-resolution-token.dpapi'
if (!(Test-Path -LiteralPath $keyPath -PathType Leaf)) {
    throw (
        'No Windows-protected bridge key. Run ' +
        'create_appfolio_session.ps1 first.'
    )
}
if (!$Command -or $Command.Count -eq 0) {
    throw 'Specify the command to run with the bridge key.'
}

$secureKey = (Get-Content -LiteralPath $keyPath -Raw).Trim() |
    ConvertTo-SecureString
$bstr = [IntPtr]::Zero
$childExitCode = 1
$loadedSecretNames = @()
try {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    $env:BRIDGE_DATA_KEY = (
        [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    )
    if (
        (Test-Path -LiteralPath $vendorResolutionUrlPath -PathType Leaf) -and
        (Test-Path -LiteralPath $vendorResolutionTokenPath -PathType Leaf)
    ) {
        $secureResolutionUrl = (Get-Content -LiteralPath $vendorResolutionUrlPath -Raw).Trim() |
            ConvertTo-SecureString
        $secureResolutionToken = (Get-Content -LiteralPath $vendorResolutionTokenPath -Raw).Trim() |
            ConvertTo-SecureString
        $urlBstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureResolutionUrl)
        $tokenBstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureResolutionToken)
        try {
            $env:N8N_VENDOR_RESOLUTION_WEBHOOK_URL = (
                [Runtime.InteropServices.Marshal]::PtrToStringBSTR($urlBstr)
            )
            $env:N8N_VENDOR_RESOLUTION_WEBHOOK_TOKEN = (
                [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenBstr)
            )
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($urlBstr)
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenBstr)
        }
    }
    # Generic secrets saved with set_secret.ps1: work\secret-NAME.dpapi -> $env:NAME
    foreach ($secretFile in Get-ChildItem -LiteralPath (Join-Path $root 'work') -Filter 'secret-*.dpapi' -File -ErrorAction SilentlyContinue) {
        $secretName = $secretFile.BaseName.Substring(7)
        if ($secretName -notmatch '^[A-Z][A-Z0-9_]{2,63}$') { continue }
        $secretValue = (Get-Content -LiteralPath $secretFile.FullName -Raw).Trim() | ConvertTo-SecureString
        $secretBstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secretValue)
        try {
            Set-Item -LiteralPath "Env:$secretName" -Value ([Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretBstr))
            $loadedSecretNames += $secretName
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretBstr)
        }
    }
    $executable = $Command[0]
    [string[]]$arguments = if ($Command.Count -gt 1) {
        @($Command[1..($Command.Count - 1)])
    } else {
        @()
    }

    # PowerShell 7 can promote a native program's stderr progress messages to
    # terminating errors when ErrorActionPreference is Stop. Let the child
    # finish its own retry loop, then propagate its real exit code.
    $priorErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $executable @arguments
        $commandSucceeded = $?
        $nativeExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $priorErrorActionPreference
    }
    $childExitCode = if ($null -ne $nativeExitCode) {
        [int]$nativeExitCode
    } elseif ($commandSucceeded) {
        0
    } else {
        1
    }
} finally {
    if ($bstr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    Remove-Item Env:BRIDGE_DATA_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:N8N_VENDOR_RESOLUTION_WEBHOOK_URL -ErrorAction SilentlyContinue
    Remove-Item Env:N8N_VENDOR_RESOLUTION_WEBHOOK_TOKEN -ErrorAction SilentlyContinue
    foreach ($secretName in @($loadedSecretNames)) {
        Remove-Item -LiteralPath "Env:$secretName" -ErrorAction SilentlyContinue
    }
}
exit $childExitCode
