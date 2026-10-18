[CmdletBinding()]
param(
    [ValidateSet('setup','dev','check','package','verify-package','archive','verify-archive')]
    [string]$Task = 'dev',
    [string]$Python = '3.12.14'
)
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSEdition -ne 'Core' -or $PSVersionTable.PSVersion.Major -lt 7) {
    throw '请通过 C:\Program Files\PowerShell\7\pwsh.exe 运行'
}
$stationRoot = Split-Path $PSScriptRoot -Parent
$appPython = Join-Path $stationRoot '.venv\Scripts\python.exe'
function Invoke-Checked([string]$Executable, [string[]]$Arguments) {
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Executable 失败：$LASTEXITCODE" }
}
function Assert-Cleanup {
    $owned = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -in @('electron.exe','LF2S-Station.exe','lf2s-service.exe','python.exe') -and
        $_.CommandLine -and $_.CommandLine.Contains($stationRoot, [StringComparison]::OrdinalIgnoreCase)
    } | Select-Object ProcessId, Name)
    @{
        checked_at = [DateTimeOffset]::Now.ToString('o')
        passed = ($owned.Count -eq 0)
        processes = $owned
        scope = 'Only this application workspace; unrelated processes are not terminated'
    } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath 'evidence\cleanup.json' -Encoding utf8NoBOM
    if ($owned.Count) { throw '桌面或后台仍有进程残留，见 evidence/cleanup.json' }
}
Push-Location -LiteralPath $stationRoot
try {
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:PYTHONUTF8 = '1'
    $env:PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD = '1'
    $env:UV_CACHE_DIR = Join-Path $stationRoot '.work\cache\uv'
    New-Item -ItemType Directory -Force -Path 'evidence' | Out-Null
    if ($Task -eq 'setup') {
        if (-not (Test-Path -LiteralPath $appPython)) {
            Invoke-Checked 'uv' @('venv','--python',$Python,'.venv')
        }
        Invoke-Checked 'uv' @('pip','sync','--python',$appPython,'--require-hashes','requirements-dev.lock')
        Invoke-Checked 'npm.cmd' @('ci','--ignore-scripts','--no-audit','--no-fund','--cache','.work\cache\npm')
        # Official archive, pinned SHA-256, bounded network retry; no npm postinstall hooks.
        Invoke-Checked $appPython @('-B','scripts\install_electron.py')
        Invoke-Checked $appPython @('-B','scripts\sync_protocol.py','--check')
        return
    }
    if (-not (Test-Path -LiteralPath $appPython)) { throw '请先运行 Station.ps1 setup' }
    if ($Task -eq 'check') {
        Invoke-Checked $appPython @('-m','ruff','check','service','simulation','scripts','tests')
        Invoke-Checked $appPython @('-B','scripts\sync_protocol.py','--check')
        Invoke-Checked $appPython @('-m','pytest','tests\test_backend.py','tests\test_esp32.py','tests\test_fc_usb.py','tests\test_text_tasks.py','tests\test_demo_intents.py','tests\test_voice.py','tests\test_vision.py','tests\test_reboot_latch.py','tests\test_controller.py','tests\test_gateway.py','-q','--junitxml=evidence\backend-junit.xml')
        Invoke-Checked 'npm.cmd' @('run','check:desktop')
        Invoke-Checked 'npm.cmd' @('run','build')
        Invoke-Checked 'npm.cmd' @('run','verify:desktop')
        Invoke-Checked 'node' @('tests/workbench_desktop.cjs')
        Assert-Cleanup
        return
    }
    if ($Task -eq 'verify-package') {
        Invoke-Checked 'npm.cmd' @('run','verify:package')
        Invoke-Checked 'node' @('tests/workbench_desktop.cjs','--packaged')
        Assert-Cleanup
        return
    }
    if ($Task -eq 'archive') {
        Invoke-Checked $appPython @('-B','scripts\archive.py')
        return
    }
    if ($Task -eq 'verify-archive') {
        Invoke-Checked $appPython @('-B','scripts\archive.py','--verify')
        Assert-Cleanup
        return
    }
    Invoke-Checked 'npm.cmd' @('run','build')
    if ($Task -eq 'dev') { Invoke-Checked 'node_modules\electron\dist\electron.exe' @('.'); return }
    if ($Task -eq 'package') {
        Invoke-Checked $appPython @('-B','scripts\sync_protocol.py','--check')
        Invoke-Checked $appPython @('-m','PyInstaller','--clean','--noconfirm','--distpath','build\backend','--workpath','build\pyinstaller','service\lf2s-service.spec')
        Invoke-Checked $appPython @('-B','scripts\package.py')
    }
} finally { Pop-Location }
