# SPDX-License-Identifier: MIT
# Read-only live baseline capture; writes new evidence files, never settings or modes.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [string]$SettingsPath='C:\Program Files (x86)\Steam\config\steamvr.vrsettings',
    [string]$ManifestPath=''
)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
if (Get-Process -Name vrserver,vrcompositor,vrmonitor -ErrorAction SilentlyContinue) { throw 'Close SteamVR before the refresh baseline capture' }
$output=[IO.Path]::GetFullPath($OutputDirectory)
$parent=Split-Path -Parent $output
if (-not (Test-Path -LiteralPath $parent -PathType Container)) { throw 'Output parent missing' }
if (Test-Path -LiteralPath $output) { throw 'Use a new output directory; existing evidence is preserved' }
function Read-FileSnapshot([string]$Path) {
    # Single byte snapshot: the same bytes feed both JSON parsing and SHA256,
    # so values and digest can never come from two different concurrent reads.
    $bytes=[IO.File]::ReadAllBytes($Path)
    $stream=New-Object IO.MemoryStream(,$bytes)
    try {
        $reader=New-Object IO.StreamReader($stream,[Text.Encoding]::UTF8,$true)
        try { $text=$reader.ReadToEnd() } finally { $reader.Close() }
    } finally { $stream.Dispose() }
    $sha=[Security.Cryptography.SHA256]::Create()
    try { $hashBytes=$sha.ComputeHash($bytes) } finally { $sha.Dispose() }
    $hash=($hashBytes | ForEach-Object { $_.ToString('X2') }) -join ''
    return [pscustomobject]@{Bytes=$bytes;Text=$text;Sha256=$hash}
}
function Test-FileSnapshotCurrent([byte[]]$SnapshotBytes,[string]$Path) {
    # Re-read and compare against the snapshot to detect an intervening writer.
    $current=[IO.File]::ReadAllBytes($Path)
    if ($current.Length -ne $SnapshotBytes.Length) { return $false }
    for ($i=0; $i -lt $current.Length; $i++) { if ($current[$i] -ne $SnapshotBytes[$i]) { return $false } }
    return $true
}
$manifestPath=if ($ManifestPath) { [IO.Path]::GetFullPath($ManifestPath) } else { Join-Path $root 'configurations\candidates\hdmi_native_90hz_trial.json' }
$manifestSnapshot=Read-FileSnapshot $manifestPath
$manifest=$manifestSnapshot.Text | ConvertFrom-Json
$monitorPath=if ($manifest.kind -eq 'refresh90_trial_manifest') { [string]$manifest.trial_identity.monitor_device_path } else { [string]$manifest.steamvr_overrides.driver_psvrpc.displayMonitorPath }
if ([string]::IsNullOrWhiteSpace($monitorPath)) { throw 'Manifest must provide the exact selected monitor path' }
$settingsSnapshot=Read-FileSnapshot $SettingsPath
$settings=$settingsSnapshot.Text | ConvertFrom-Json
$settingsHash=$settingsSnapshot.Sha256
$begin=[DateTime]::UtcNow.ToString('o')
$raw=& (Join-Path $root 'out\display\psvr_display.exe') --json
if ($LASTEXITCODE -ne 0) { throw 'Display inventory failed' }
$inventoryText=$raw -join [Environment]::NewLine
$inventory=$inventoryText | ConvertFrom-Json
$selected=@($inventory.displays | Where-Object { $_.monitor_device_path -ceq $monitorPath })
if ($selected.Count -ne 1 -or $selected[0].active -ne $true -or $selected[0].edid_ids.valid -ne $true -or
    $selected[0].edid_ids.manufacturer -ne 55629 -or $selected[0].edid_ids.product -ne 27140) { throw 'One exact active Sony SNY6A04 with valid EDID identity is required' }
$section=$settings.PSObject.Properties['driver_psvrpc']
$changes=@(foreach ($name in @('displayRefreshNumerator','displayRefreshDenominator')) {
    $prop=if ($section) { $section.Value.PSObject.Properties[$name] } else { $null }
    [ordered]@{section='driver_psvrpc';key=$name;section_existed=($null -ne $section);existed=($null -ne $prop);old_value=$(if ($prop) {$prop.Value} else {$null})}
})
if (-not (Test-FileSnapshotCurrent $settingsSnapshot.Bytes $SettingsPath)) { throw 'Settings changed during capture; retry before editing the refresh' }
if (-not (Test-FileSnapshotCurrent $manifestSnapshot.Bytes $manifestPath)) { throw 'Manifest changed during capture; retry before editing the refresh' }
$backup=[ordered]@{schema_version=1;kind='refresh_trial_pretrial_backup';created_utc=[DateTime]::UtcNow.ToString('o');settings_path=[IO.Path]::GetFullPath($SettingsPath);before_sha256=$settingsHash;changes=$changes;monitor_device_path=$selected[0].monitor_device_path;gpu_pretrial_mode=$selected[0];native_direct_setting=$settings.driver_psvrpc.nativeDirectMode;inventory_begin_utc=$begin;manifest_sha256=$manifestSnapshot.Sha256;note='Sequential read-only snapshot, not an atomic hardware transaction. Restore only the two refresh keys from their recorded presence/values. No mode or runtime setting changed.'}
[void][IO.Directory]::CreateDirectory($output)
$utf8=New-Object Text.UTF8Encoding($false)
function Write-NewEvidence([string]$Path,[string]$Text) {
    $stream=[IO.File]::Open($Path,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read)
    try { $bytes=$utf8.GetBytes($Text); $stream.Write($bytes,0,$bytes.Length) } finally { $stream.Dispose() }
}
Write-NewEvidence (Join-Path $output 'inventory_before.json') $inventoryText
Write-NewEvidence (Join-Path $output 'runtime_refresh_before.json') ($backup | ConvertTo-Json -Depth 24)
[pscustomobject]@{output_directory=$output;baseline_refresh=$selected[0].refresh;baseline_signal_refresh=$selected[0].target.signal_refresh;refresh_keys=$changes;note='Evidence saved only. Runtime/settings/GPU unchanged.'} | ConvertTo-Json -Depth 8
