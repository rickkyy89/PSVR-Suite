# SPDX-License-Identifier: MIT
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateSet('Enable','Restore','Inspect')][string]$Action,
    [string]$SettingsPath = 'C:\Program Files (x86)\Steam\config\steamvr.vrsettings',
    [string]$DefaultsPath = 'C:\Program Files (x86)\Steam\steamapps\common\SteamVR\resources\settings\default.vrsettings',
    [string]$BackupPath,
    [string]$DisplayTool,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
if (-not $BackupPath) { $BackupPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'logs\native_direct_restore_20261005.json' }
if (-not $DisplayTool) { $DisplayTool = Join-Path (Split-Path -Parent $PSScriptRoot) 'out\display\psvr_display.exe' }
function Set-Key($Object, [string]$Name, $Value) {
    $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value -Force
}
function Write-AtomicJson([string]$Path, $Value) {
    $parent = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) { throw "Missing parent: $parent" }
    $temporary = Join-Path $parent ([IO.Path]::GetRandomFileName())
    try {
        [IO.File]::WriteAllText($temporary, ($Value | ConvertTo-Json -Depth 64), (New-Object Text.UTF8Encoding($false)))
        if (Test-Path -LiteralPath $Path) { [IO.File]::Replace($temporary,$Path,[System.Management.Automation.Language.NullString]::Value) }
        else { [IO.File]::Move($temporary,$Path) }
    } finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary } }
}
$settings = Get-Content -LiteralPath $SettingsPath -Raw | ConvertFrom-Json
$originalHash = (Get-FileHash -LiteralPath $SettingsPath -Algorithm SHA256).Hash
if ($Action -eq 'Inspect') {
    [pscustomobject]@{ driver_native_direct = $settings.driver_psvrpc.nativeDirectMode; direct_mode_overrides = $settings.direct_mode } | ConvertTo-Json -Depth 8
    return
}
if (-not $DryRun -and (Get-Process -Name vrserver,vrcompositor,vrmonitor -ErrorAction SilentlyContinue)) {
    throw 'Close SteamVR normally before changing direct-mode settings'
}
$changes = @()
if ($Action -eq 'Enable') {
    if (Test-Path -LiteralPath $BackupPath) { throw "Backup already exists; restore or select a new backup path: $BackupPath" }
    if (-not $settings.driver_psvrpc.enable -or -not $settings.driver_psvrpc.displayEnabled) { throw 'An explicitly enabled display driver is required' }
    $inventory = & $DisplayTool --json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw 'Display inventory failed' }
    $selected = @($inventory.displays | Where-Object { $_.monitor_device_path -ceq $settings.driver_psvrpc.displayMonitorPath })
    if ($selected.Count -ne 1 -or -not $selected[0].active -or -not $selected[0].edid_ids.valid) { throw 'Exact active monitor with valid EDID IDs required' }
    $vid = [int]$selected[0].edid_ids.manufacturer
    $pidValue = [int]$selected[0].edid_ids.product
    if ($vid -ne 0xd94d -or $pidValue -ne 0x6a04) { throw 'This experimental setup is for the measured Sony SNY6A04 only' }
    $same = @($inventory.displays | Where-Object { $_.edid_ids.valid -and $_.edid_ids.manufacturer -eq $vid -and $_.edid_ids.product -eq $pidValue })
    if ($same.Count -ne 1) { throw 'Ambiguous EDID identity: GPU direct-mode selection is by EDID pair' }
    $defaults = Get-Content -LiteralPath $DefaultsPath -Raw | ConvertFrom-Json
    $count = [int]$defaults.direct_mode.count
    if ($settings.direct_mode -and $settings.direct_mode.PSObject.Properties['count']) { $count = [int]$settings.direct_mode.count }
    if ($count -lt 0 -or $count -gt 64) { throw 'Unexpected direct-mode list count' }
    $found = $false
    for ($i=0; $i -lt $count; $i++) {
        $v = $defaults.direct_mode.PSObject.Properties["edidVid$i"]
        $p = $defaults.direct_mode.PSObject.Properties["edidPid$i"]
        if ($settings.direct_mode -and $settings.direct_mode.PSObject.Properties["edidVid$i"]) { $v = $settings.direct_mode.PSObject.Properties["edidVid$i"] }
        if ($settings.direct_mode -and $settings.direct_mode.PSObject.Properties["edidPid$i"]) { $p = $settings.direct_mode.PSObject.Properties["edidPid$i"] }
        if (-not $v -or -not $p) { throw "Incomplete inherited direct-mode pair $i" }
        if ([int]$v.Value -eq $vid -and [int]$p.Value -eq $pidValue) { $found = $true }
    }
    $plan = @(
        @{section='driver_psvrpc'; key='nativeDirectMode'; value=$true},
        @{section='driver_psvrpc'; key='edidVendorId'; value=$vid},
        @{section='driver_psvrpc'; key='edidProductId'; value=$pidValue},
        @{section='direct_mode'; key='enable'; value=$true}
    )
    if (-not $found) {
        $plan += @{section='direct_mode'; key="edidVid$count"; value=$vid}
        $plan += @{section='direct_mode'; key="edidPid$count"; value=$pidValue}
        $plan += @{section='direct_mode'; key='count'; value=($count+1)}
    }
    foreach ($item in $plan) {
        $section = $settings.PSObject.Properties[$item.section]
        $old = if ($section) { $section.Value.PSObject.Properties[$item.key] } else { $null }
        $changes += [pscustomobject]@{ section=$item.section; key=$item.key; section_existed=($null -ne $section); existed=($null -ne $old); old_value=$(if ($old) { $old.Value } else { $null }); written_value=$item.value }
        if (-not $section) { Set-Key $settings $item.section ([pscustomobject]@{}) }
        Set-Key $settings.($item.section) $item.key $item.value
    }
    $backup = [pscustomobject]@{ schema_version=1; settings_path=$SettingsPath; before_sha256=$originalHash; changes=$changes; created_utc=[DateTime]::UtcNow.ToString('o') }
} else {
    $backup = Get-Content -LiteralPath $BackupPath -Raw | ConvertFrom-Json
    if ($backup.schema_version -ne 1 -or -not [string]::Equals($backup.settings_path,$SettingsPath,[StringComparison]::OrdinalIgnoreCase)) { throw 'Backup target/schema mismatch' }
    foreach ($item in $backup.changes) {
        $section = $settings.PSObject.Properties[$item.section]
        $current = if ($section) { $section.Value.PSObject.Properties[$item.key] } else { $null }
        if (-not $current -or ($current.Value | ConvertTo-Json -Compress) -cne ($item.written_value | ConvertTo-Json -Compress)) { throw "Setting changed since experiment: $($item.section).$($item.key); merge manually" }
        if ($item.existed) { Set-Key $section.Value $item.key $item.old_value }
        else { $section.Value.PSObject.Properties.Remove($item.key) }
    }
    foreach ($item in $backup.changes) {
        if (-not $item.section_existed -and $settings.PSObject.Properties[$item.section] -and @($settings.($item.section).PSObject.Properties).Count -eq 0) {
            $settings.PSObject.Properties.Remove($item.section)
        }
    }
}
if ($DryRun) {
    [pscustomobject]@{ dry_run=$true; action=$Action; backup=$BackupPath; changes=$(if ($Action -eq 'Enable') { $changes } else { $backup.changes }) } | ConvertTo-Json -Depth 8
    return
}
if ((Get-FileHash -LiteralPath $SettingsPath -Algorithm SHA256).Hash -ne $originalHash) { throw 'Settings changed during setup; retry after SteamVR is closed' }
if ($Action -eq 'Enable') { Write-AtomicJson $BackupPath $backup }
Write-AtomicJson $SettingsPath $settings
[pscustomobject]@{ action=$Action; settings=$SettingsPath; backup=$BackupPath; note='Configuration changed; actual GPU direct-mode acquisition must be verified separately' } | ConvertTo-Json
