# SPDX-License-Identifier: MIT
# Selective restore integration: isolated fixtures, no GPU changes or runtime start.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$temp = Join-Path ([IO.Path]::GetTempPath()) ('psvr-direct-restore-' + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($temp) | Out-Null
try {
    $settingsPath = Join-Path $temp 'settings.json'
    $backupPath = Join-Path $temp 'restore.json'
    $settings = [pscustomobject]@{
        driver_psvrpc=[pscustomobject]@{ nativeDirectMode=$true; edidVendorId=55629; edidProductId=27140; renderWidth=960 }
        direct_mode=[pscustomobject]@{ enable=$true; count=5; edidVid4=55629; edidPid4=27140 }
        user_data=[pscustomobject]@{ nested=@('keep',123,$true) }
        power=[pscustomobject]@{ pauseCompositorOnStandby=$false }
    }
    $changes = @()
    foreach ($s in @('driver_psvrpc','direct_mode')) {
        foreach ($p in $settings.$s.PSObject.Properties) {
            if ($p.Name -eq 'renderWidth') { continue }
            $changes += [pscustomobject]@{ section=$s; key=$p.Name; section_existed=($s -eq 'driver_psvrpc'); existed=$false; old_value=$null; written_value=$p.Value }
        }
    }
    $backup=[pscustomobject]@{ schema_version=1; settings_path=$settingsPath; changes=$changes }
    [IO.File]::WriteAllText($settingsPath,($settings | ConvertTo-Json -Depth 12))
    [IO.File]::WriteAllText($backupPath,($backup | ConvertTo-Json -Depth 12))
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'scripts\native_direct_mode.ps1') -Action Restore -SettingsPath $settingsPath -BackupPath $backupPath
    if ($LASTEXITCODE -ne 0) { throw 'Restore failed' }
    $after=Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
    if ($after.PSObject.Properties['direct_mode'] -or $after.driver_psvrpc.PSObject.Properties['nativeDirectMode']) { throw 'Experiment-only overrides/section remained' }
    if ($after.driver_psvrpc.renderWidth -ne 960 -or $after.power.pauseCompositorOnStandby -ne $false -or
        ($after.user_data | ConvertTo-Json -Compress) -cne ($settings.user_data | ConvertTo-Json -Compress)) { throw 'Unrelated settings were modified' }
    # A concurrent/user edit to an owned key must reject restoration without writing.
    $settings.driver_psvrpc.nativeDirectMode=$false
    [IO.File]::WriteAllText($settingsPath,($settings | ConvertTo-Json -Depth 12))
    $before=(Get-FileHash -LiteralPath $settingsPath).Hash
    $ErrorActionPreference = 'Continue' # Expected child-process stderr/nonzero exit.
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'scripts\native_direct_mode.ps1') -Action Restore -SettingsPath $settingsPath -BackupPath $backupPath 2>$null
    $ErrorActionPreference = 'Stop'
    if ($LASTEXITCODE -eq 0 -or (Get-FileHash -LiteralPath $settingsPath).Hash -ne $before) { throw 'Conflict guard did not preserve settings' }
    'PASS selective direct-mode restore, unrelated settings, and conflict rejection'
} finally { [IO.Directory]::Delete($temp,$true) }
