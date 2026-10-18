#requires -Version 7.0
[CmdletBinding()]
param([switch]$Apply)
$ErrorActionPreference = 'Stop'
$retentionProject = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
$retentionRelease = [IO.Path]::GetFullPath((Join-Path $retentionProject 'release'))
$retentionWork = [IO.Path]::GetFullPath((Join-Path $retentionProject '.work'))
$retentionVersion = (Get-Content -LiteralPath (Join-Path $retentionProject 'package.json') -Raw | ConvertFrom-Json).version
$retentionLatest = Get-Content -LiteralPath (Join-Path $retentionRelease 'latest.json') -Raw | ConvertFrom-Json
$retentionKeep = [IO.Path]::GetFullPath((Join-Path $retentionRelease $retentionLatest.directory))
if ([IO.Path]::GetDirectoryName($retentionKeep) -ne $retentionRelease -or -not (Test-Path -LiteralPath (Join-Path $retentionKeep 'LF2S-Station.exe'))) { throw 'Latest release path is invalid' }
$retentionManifest = Get-Content -LiteralPath (Join-Path $retentionKeep 'bundle-manifest.json') -Raw | ConvertFrom-Json
if ($retentionManifest.version -ne $retentionVersion) { throw 'Latest bundle does not match package.json' }
$retentionAssets = Join-Path $retentionRelease 'assets'
$retentionZip = Join-Path $retentionAssets "LF2S-Station-$retentionVersion-win-x64.zip"
if (-not (Test-Path -LiteralPath $retentionZip)) { throw 'Latest distributable ZIP is missing' }
$retentionZipHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $retentionZip).Hash.ToLowerInvariant()
$retentionChecksum = Get-Content -LiteralPath ($retentionZip + '.sha256') -Raw
if (-not $retentionChecksum.StartsWith($retentionZipHash, [StringComparison]::OrdinalIgnoreCase)) { throw 'Latest ZIP SHA-256 sidecar does not match' }
& (Join-Path $retentionProject '.venv/Scripts/python.exe') -B (Join-Path $PSScriptRoot 'retain_evidence.py')
if ($LASTEXITCODE -ne 0) { throw 'Could not preserve historical verification records' }
$retentionCandidates = [Collections.Generic.List[object]]::new()
function Add-RetentionCandidate([string]$Candidate, [string]$ExpectedParent, [bool]$Directory) {
    $absolute = [IO.Path]::GetFullPath($Candidate)
    if ([IO.Path]::GetDirectoryName($absolute) -ne $ExpectedParent -or $absolute -eq $retentionKeep) { throw "Unexpected cleanup path: $absolute" }
    $retentionCandidates.Add([pscustomobject]@{ path = $absolute; parent = $ExpectedParent; directory = $Directory })
}
# Known older development capture and public-download copies. Keep their parent evidence.
$retentionCapture = Join-Path $retentionWork 'before-final-layout'
$retentionCapturePackage = Join-Path $retentionCapture 'resources/app/package.json'
if ((Test-Path -LiteralPath (Join-Path $retentionCapture 'LF2S-Station.exe')) -and (Test-Path -LiteralPath $retentionCapturePackage)) {
    if ((Get-Content -LiteralPath $retentionCapturePackage -Raw | ConvertFrom-Json).name -ne 'lf2s-station') { throw 'Unexpected capture application' }
    Add-RetentionCandidate $retentionCapture $retentionWork $true
}
Get-ChildItem -LiteralPath $retentionWork -Directory -Filter 'published-v*' | Where-Object { $_.Name -match '^published-v\d+\.\d+\.\d+$' } | ForEach-Object {
    $downloadRoot = $_.FullName
    Get-ChildItem -LiteralPath $downloadRoot -File -Filter 'LF2S-Station-*-win-x64.zip' | Where-Object { $_.Name -ne "LF2S-Station-$retentionVersion-win-x64.zip" } | ForEach-Object { Add-RetentionCandidate $_.FullName $downloadRoot $false }
    $downloadExtracted = Join-Path $downloadRoot 'extracted'
    if (Test-Path -LiteralPath $downloadExtracted) {
        Get-ChildItem -LiteralPath $downloadExtracted -Directory -Filter 'LF2S-Station-*-win-x64' | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'bundle-manifest.json') } | ForEach-Object { Add-RetentionCandidate $_.FullName $downloadExtracted $true }
    }
}
Get-ChildItem -LiteralPath $retentionRelease -Directory -Force | Where-Object { $_.Name -match '^LF2S-Station-\d+\.\d+\.\d+-win-x64(?:-\d{8}-\d{6})?$' -and $_.FullName -ne $retentionKeep } | ForEach-Object { Add-RetentionCandidate $_.FullName $retentionRelease $true }
if (Test-Path -LiteralPath $retentionAssets) {
    Get-ChildItem -LiteralPath $retentionAssets -File | Where-Object { $_.Name -match '^LF2S-Station-(\d+\.\d+\.\d+)-win-x64\.(?:zip|zip\.sha256|manifest\.json|verification\.json)$' -and $Matches[1] -ne $retentionVersion } | ForEach-Object { Add-RetentionCandidate $_.FullName $retentionAssets $false }
}
if (Test-Path -LiteralPath $retentionWork) {
    Get-ChildItem -LiteralPath $retentionWork -Directory -Filter 'zip-check-*' | ForEach-Object {
        $bundles = @(Get-ChildItem -LiteralPath $_.FullName -Directory -Filter 'LF2S-Station-*-win-x64')
        if ($bundles.Count -eq 1 -and (Test-Path -LiteralPath (Join-Path $bundles[0].FullName 'bundle-manifest.json'))) { Add-RetentionCandidate $_.FullName $retentionWork $true }
    }
}
$retentionRecord = [ordered]@{ version = $retentionVersion; keep = $retentionKeep; apply = [bool]$Apply; candidates = $retentionCandidates.ToArray(); timestamp = [DateTimeOffset]::Now.ToString('o'); scope = 'Distributed software and temporary extracted software only; source, evidence and dependencies retained' }
$retentionRecord | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $retentionProject 'evidence/retention-plan.json') -Encoding utf8NoBOM
if ($Apply) {
    $publishedPath = Join-Path $retentionRelease ("published-v$($retentionVersion).json")
    $published = Get-Content -LiteralPath $publishedPath -Raw | ConvertFrom-Json
    if (-not $published.published -or $published.tag -ne "v$retentionVersion") { throw 'Verify the published version before deleting previous software' }
    $asset = @($published.assets | Where-Object { $_.name -eq "LF2S-Station-$retentionVersion-win-x64.zip" })
    if ($asset.Count -ne 1 -or (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $retentionAssets $asset[0].name)).Hash.ToLowerInvariant() -ne $asset[0].sha256) { throw 'Published ZIP hash does not match the local latest version' }
    $processes = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath })
    foreach ($item in $retentionCandidates) {
        $path = [IO.Path]::GetFullPath($item.path)
        $root = [IO.Path]::GetFullPath($item.parent)
        $withinWorkspace = $path.StartsWith($retentionRelease + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or $path.StartsWith($retentionWork + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
        if (-not $withinWorkspace -or [IO.Path]::GetDirectoryName($path) -ne $root -or $path -eq $retentionKeep) { throw "Path boundary failed: $path" }
        if (@($processes | Where-Object { $_.ExecutablePath.StartsWith($path + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) }).Count) { throw "An old application is still running: $path" }
        $entry = Get-Item -LiteralPath $path -Force
        if ($item.directory -and -not ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint)) { Remove-Item -LiteralPath $path -Recurse -Force }
        else { Remove-Item -LiteralPath $path -Force }
    }
    $retentionRecord['removed'] = $retentionCandidates.Count
    $retentionRecord | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $retentionProject 'evidence/retention-result.json') -Encoding utf8NoBOM
}
[pscustomobject]@{ version = $retentionVersion; keep = $retentionKeep; zip_sha256 = $retentionZipHash; candidate_count = $retentionCandidates.Count; applied = [bool]$Apply } | ConvertTo-Json
