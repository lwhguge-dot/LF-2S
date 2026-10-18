#requires -Version 7.0
[CmdletBinding()]
param(
    [ValidateSet('package','dev')][string]$Mode = 'package',
    [string]$BaseUrl = 'http://100.116.120.40:8000/v1',
    [switch]$Voice,
    [string]$AsrBaseUrl = 'http://100.116.120.40:8001'
)
$ErrorActionPreference = 'Stop'
$stationRoot = if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'LF2S-Station.exe')) { $PSScriptRoot } else { Split-Path $PSScriptRoot -Parent }
if (-not $env:LF2S_MODEL_API_KEY) {
    $modelSecret = Read-Host '请输入公司模型访问密钥（不保存到文件）' -AsSecureString
    $modelPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($modelSecret)
    try { $env:LF2S_MODEL_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($modelPointer) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($modelPointer) }
}
$env:LF2S_MODEL_BASE_URL = $BaseUrl
$env:LF2S_MODEL_NAME = 'qwen38'
try {
    if ($Voice) {
        if (-not $env:LF2S_ASR_TOKEN) {
            $voiceSecret = Read-Host '请输入独立语音服务凭据（不保存到文件）' -AsSecureString
            $voicePointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($voiceSecret)
            try { $env:LF2S_ASR_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($voicePointer) }
            finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($voicePointer) }
        }
        $env:LF2S_ASR_BASE_URL = $AsrBaseUrl
    }
    if ($Mode -eq 'dev') { & (Join-Path $PSScriptRoot 'Station.ps1') dev }
    else {
        $modelApp = Join-Path $stationRoot 'LF2S-Station.exe'
        if (-not (Test-Path -LiteralPath $modelApp)) {
            $modelRelease = Get-Content -LiteralPath (Join-Path $stationRoot 'release/latest.json') -Raw | ConvertFrom-Json
            $modelApp = [IO.Path]::GetFullPath((Join-Path $stationRoot ('release/' + $modelRelease.directory + '/LF2S-Station.exe')))
            $modelReleaseRoot = [IO.Path]::GetFullPath((Join-Path $stationRoot 'release')) + [IO.Path]::DirectorySeparatorChar
            if (-not $modelApp.StartsWith($modelReleaseRoot, [StringComparison]::OrdinalIgnoreCase)) { throw '运行包路径无效' }
        }
        Start-Process -FilePath $modelApp -WorkingDirectory (Split-Path $modelApp) -Wait
    }
} finally {
    Remove-Item Env:LF2S_MODEL_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:LF2S_MODEL_BASE_URL -ErrorAction SilentlyContinue
    Remove-Item Env:LF2S_MODEL_NAME -ErrorAction SilentlyContinue
    if ($Voice) {
        Remove-Item Env:LF2S_ASR_TOKEN -ErrorAction SilentlyContinue
        Remove-Item Env:LF2S_ASR_BASE_URL -ErrorAction SilentlyContinue
    }
}
