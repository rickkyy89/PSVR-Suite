# SPDX-License-Identifier: MIT
<#
.SYNOPSIS
    Offline regression tests for B9-06 (re-read plus merge-only commit in set_trial_refresh.ps1).
.DESCRIPTION
    Everything runs against throwaway fixtures under %TEMP%\opencode. The
    production script is NEVER invoked (that path needs the live vrsettings
    file, the psvr_display.exe inventory, real displays and display-mode
    syscalls), the live settings path is never read or written, and no
    SteamVR process, GUI, hardware or real settings file is touched. The
    production snapshot/merge helpers are imported from the real script via
    AST (renamed, not copy-pasted) so the behavioral cases exercise
    production code; static cases pin the re-read/fingerprint/merge pattern
    in place.
    Usage: powershell -ExecutionPolicy Bypass -File tests\test_set_trial_refresh_merge.ps1
    Exit 0 when all cases pass, 1 otherwise.
#>
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$scriptPath = Join-Path $root 'scripts\set_trial_refresh.ps1'
$liveSettingsDefault = 'C:\Program Files (x86)\Steam\config\steamvr.vrsettings'

$fx = Join-Path $env:TEMP ('opencode\psvr_refresh_merge_' + [Guid]::NewGuid().ToString('N').Substring(0, 8))

$script:Failed = 0
function Invoke-TestCase {
    param([string]$Name, [scriptblock]$Body)
    try { & $Body; Write-Host "PASS $Name" }
    catch { $script:Failed++; Write-Host "FAIL $Name :: $($_.Exception.Message)" }
}
function Assert-Equal {
    param($Expected, $Actual, [string]$What = 'value')
    if ($Expected -ne $Actual) { throw "$What - expected '$Expected', got '$Actual'" }
}
function Assert-Match {
    param([string]$Actual, [string]$Pattern, [string]$What = 'output')
    if ($Actual -notmatch $Pattern) { throw "$What does not match /$Pattern/" }
}
function Assert-NotMatch {
    param([string]$Actual, [string]$Pattern, [string]$What = 'output')
    if ($Actual -match $Pattern) { throw "$What must not match /$Pattern/" }
}
function Write-FixtureSettings {
    param([string]$Path, [int]$Numerator = 90, [int]$Denominator = 1, [string]$Foreign = 'keep-me')
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    $json = [ordered]@{
        driver_psvrpc = [ordered]@{
            displayRefreshNumerator   = $Numerator
            displayRefreshDenominator = $Denominator
            displayMonitorPath        = 'DISPLAY\SNY6A04\5&test&0&UID123'
            foreignOwnedElsewhere     = $Foreign
        }
        steamvr   = [ordered]@{ foreignSteamKey = 'steam-keep' }
        dashboard = [ordered]@{ foreignDashKey = 'dash-keep' }
    } | ConvertTo-Json -Depth 10
    # No BOM, like the real vrsettings writer path in this repo.
    [IO.File]::WriteAllText($Path, $json, (New-Object Text.UTF8Encoding($false)))
}
function Import-ProductionFunction {
    param($Ast, [string]$Name, [string]$Alias)
    $def = @($Ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $Name }, $true))
    if ($def.Count -ne 1) { throw "production function $Name not found exactly once (found $($def.Count))" }
    $renamed = $def[0].Extent.Text -replace ('function\s+' + $Name), ("function global:$Alias")
    Invoke-Expression $renamed
}

try {
    New-Item -ItemType Directory -Force -Path $fx | Out-Null
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$null, [ref]$errors)
    if ($errors.Count -ne 0) { throw "parse errors: $($errors[0].Message)" }
    $text = Get-Content -LiteralPath $scriptPath -Raw
    Import-ProductionFunction $ast 'Read-FileSnapshot' 'B906_ReadFileSnapshot'
    Import-ProductionFunction $ast 'Merge-TrialRefreshSettings' 'B906_MergeTrialRefresh'

    Invoke-TestCase 'ast/script parses with unchanged params and backup-journal format' {
        $params = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.ParameterAst] }, $true) |
                  ForEach-Object { $_.Name.VariablePath.UserPath }
        foreach ($p in 'Hz', 'TestOnly', 'PersistForTrial', 'RestoreRecordingMirrorSettings', 'DisableDashboard', 'RestoreDashboardFrom') {
            if ($params -notcontains $p) { throw "param $p not found (got: $($params -join ','))" }
        }
        Assert-Match $text ([regex]::Escape($liveSettingsDefault)) 'live settings default kept'
        foreach ($k in 'refresh_settings_before_', '\.psvr-refresh\.tmp', 'settings_backup', 'driver_refresh', 'applied=') {
            Assert-Match $text $k "backup/journal key $k kept"
        }
        Assert-Match $text ([regex]::Escape('Cannot disable and restore dashboard together')) 'dashboard conflict message kept'
    }
    Invoke-TestCase 'ast/re-read plus fingerprint refuse plus merge-only commit, no stale whole-doc write' {
        Assert-Match $text 'function Read-FileSnapshot' 'snapshot helper present'
        Assert-Match $text 'function Merge-TrialRefreshSettings' 'merge helper present'
        Assert-Match $text ([regex]::Escape('$settingsSnapshot=Read-FileSnapshot $settingsPath')) 'initial snapshot'
        Assert-Match $text ([regex]::Escape('$recentSnapshot=Read-FileSnapshot $settingsPath')) 'recent re-read before commit'
        Assert-Match $text ([regex]::Escape('$recentSnapshot.Sha256 -ne $settingsSnapshot.Sha256')) 'fingerprint comparison'
        Assert-Match $text 'commit refused' 'refuse message present'
        Assert-Match $text ([regex]::Escape('settings backup=$backup')) 'refuse message names the existing backup'
        Assert-Match $text ([regex]::Escape('$recent=$recentSnapshot.Text | ConvertFrom-Json')) 'commit rebased on recent text'
        Assert-Match $text ([regex]::Escape('Merge-TrialRefreshSettings -TargetSettings $recent')) 'commit merges onto recent'
        Assert-Match $text ([regex]::Escape('($recent | ConvertTo-Json -Depth 30)')) 'commit serializes the merged recent doc'
        Assert-NotMatch $text ([regex]::Escape('($settings | ConvertTo-Json -Depth 30)')) 'stale whole-doc write gone'
        Assert-NotMatch $text ([regex]::Escape('$settings.driver_psvrpc.displayRefreshNumerator')) 'stale trial-key mutation gone'
    }
    Invoke-TestCase 'snap/snapshot digest matches file digest and values come from the same bytes' {
        $s = Join-Path $fx 'coherent\steamvr.vrsettings'
        Write-FixtureSettings -Path $s -Numerator 90
        $snap = & 'B906_ReadFileSnapshot' $s
        Assert-Equal (Get-FileHash -LiteralPath $s -Algorithm SHA256).Hash $snap.Sha256 'snapshot digest equals file digest'
        $parsed = $snap.Text | ConvertFrom-Json
        Assert-Equal 90 $parsed.driver_psvrpc.displayRefreshNumerator 'parsed trial value from snapshot bytes'
        Assert-Equal 'keep-me' $parsed.driver_psvrpc.foreignOwnedElsewhere 'parsed foreign value from snapshot bytes'
    }
    Invoke-TestCase 'race/foreign-key concurrent write changes the fingerprint so the commit is refused' {
        # Reviewer scenario: after the initial snapshot a concurrent writer
        # changes a foreign key. The old code serialized the stale $settings
        # object and silently overwrote it (recoverable only from the backup).
        # The new code must refuse: the recent fingerprint differs.
        $s = Join-Path $fx 'race\steamvr.vrsettings'
        Write-FixtureSettings -Path $s -Numerator 90 -Foreign 'keep-me'
        $initial = & 'B906_ReadFileSnapshot' $s
        Write-FixtureSettings -Path $s -Numerator 90 -Foreign 'changed-by-someone-else'
        $recent = & 'B906_ReadFileSnapshot' $s
        if ($recent.Sha256 -eq $initial.Sha256) { throw 'post-race fingerprint must differ; production would commit over the foreign key' }
        Assert-Equal 'changed-by-someone-else' ($recent.Text | ConvertFrom-Json).driver_psvrpc.foreignOwnedElsewhere 'recent doc carries the foreign write'
        Assert-Equal 'keep-me' ($initial.Text | ConvertFrom-Json).driver_psvrpc.foreignOwnedElsewhere 'stale snapshot still carries the old foreign value'
    }
    Invoke-TestCase 'race/same-length foreign-key write is also refused' {
        $s = Join-Path $fx 'racelen\steamvr.vrsettings'
        Write-FixtureSettings -Path $s -Numerator 90 -Foreign 'aaa'
        $initial = & 'B906_ReadFileSnapshot' $s
        Write-FixtureSettings -Path $s -Numerator 90 -Foreign 'bbb'
        $recent = & 'B906_ReadFileSnapshot' $s
        if ($recent.Sha256 -eq $initial.Sha256) { throw 'same-length foreign change must alter the fingerprint' }
    }
    Invoke-TestCase 'still/unchanged file keeps the fingerprint so the commit proceeds' {
        $s = Join-Path $fx 'still\steamvr.vrsettings'
        Write-FixtureSettings -Path $s -Numerator 90
        $initial = & 'B906_ReadFileSnapshot' $s
        $recent = & 'B906_ReadFileSnapshot' $s
        Assert-Equal $initial.Sha256 $recent.Sha256 'quiet file must keep the expected fingerprint'
    }
    Invoke-TestCase 'merge/trial keys applied onto recent doc, foreign keys preserved through JSON round trip' {
        $s = Join-Path $fx 'merge\steamvr.vrsettings'
        Write-FixtureSettings -Path $s -Numerator 90 -Foreign 'keep-me'
        $recent = (& 'B906_ReadFileSnapshot' $s).Text | ConvertFrom-Json
        [void](& 'B906_MergeTrialRefresh' -TargetSettings $recent -Hz 120)
        $roundTripped = ($recent | ConvertTo-Json -Depth 30 | ConvertFrom-Json)
        Assert-Equal 120 $roundTripped.driver_psvrpc.displayRefreshNumerator 'trial numerator applied'
        Assert-Equal 1 $roundTripped.driver_psvrpc.displayRefreshDenominator 'trial denominator applied'
        Assert-Equal 'DISPLAY\SNY6A04\5&test&0&UID123' $roundTripped.driver_psvrpc.displayMonitorPath 'unrelated driver key preserved'
        Assert-Equal 'keep-me' $roundTripped.driver_psvrpc.foreignOwnedElsewhere 'foreign key preserved, not reverted to stale'
        Assert-Equal 'steam-keep' $roundTripped.steamvr.foreignSteamKey 'steamvr section preserved'
        Assert-Equal 'dash-keep' $roundTripped.dashboard.foreignDashKey 'dashboard section preserved'
    }
    Invoke-TestCase 'merge/dashboard disable, restore-value and restore-clear paths' {
        $s = Join-Path $fx 'dash\steamvr.vrsettings'
        Write-FixtureSettings -Path $s
        $doc = (& 'B906_ReadFileSnapshot' $s).Text | ConvertFrom-Json
        [void](& 'B906_MergeTrialRefresh' -TargetSettings $doc -Hz 90 -DisableDashboard)
        Assert-Equal $false $doc.dashboard.enableDashboard 'disable sets false'
        Assert-Equal 'dash-keep' $doc.dashboard.foreignDashKey 'disable keeps sibling dashboard keys'
        $doc2 = (& 'B906_ReadFileSnapshot' $s).Text | ConvertFrom-Json
        [void](& 'B906_MergeTrialRefresh' -TargetSettings $doc2 -Hz 90 -HasRestoreDashboardValue $true -RestoreDashboardValue $true)
        Assert-Equal $true $doc2.dashboard.enableDashboard 'restore sets the previous value'
        $doc3 = (& 'B906_ReadFileSnapshot' $s).Text | ConvertFrom-Json
        $doc3.dashboard | Add-Member NoteProperty enableDashboard $true
        [void](& 'B906_MergeTrialRefresh' -TargetSettings $doc3 -Hz 90 -ClearDashboardValue $true)
        if ($doc3.dashboard.PSObject.Properties['enableDashboard']) { throw 'restore-clear must remove enableDashboard' }
        Assert-Equal 'dash-keep' $doc3.dashboard.foreignDashKey 'restore-clear keeps sibling dashboard keys'
    }
    Invoke-TestCase 'merge/mirror baseline sets false and drops the legacy key' {
        $s = Join-Path $fx 'mirror\steamvr.vrsettings'
        Write-FixtureSettings -Path $s
        $doc = (& 'B906_ReadFileSnapshot' $s).Text | ConvertFrom-Json
        $doc.steamvr | Add-Member NoteProperty showMirrorView $true
        $doc.steamvr | Add-Member NoteProperty showLegacyMirrorView $true
        [void](& 'B906_MergeTrialRefresh' -TargetSettings $doc -Hz 90 -RestoreRecordingMirrorSettings)
        Assert-Equal $false $doc.steamvr.showMirrorView 'mirror baseline false'
        if ($doc.steamvr.PSObject.Properties['showLegacyMirrorView']) { throw 'legacy mirror key must be absent' }
        Assert-Equal 'steam-keep' $doc.steamvr.foreignSteamKey 'mirror path keeps sibling steamvr keys'
    }
    Invoke-TestCase 'snap/live safety: fixtures only, production script never invoked' {
        if (-not $fx.StartsWith(($env:TEMP + '\opencode'))) { throw "fixture dir escaped temp: $fx" }
        $self = Get-Content -LiteralPath $PSCommandPath -Raw
        if ($self -match '&\s*\$scriptPath') { throw 'this test must never execute the production script body' }
        foreach ($pat in 'Get-Content -LiteralPath \$liveSettingsDefault', 'Get-FileHash -LiteralPath \$liveSettingsDefault', 'ReadAllBytes\(\$liveSettingsDefault') {
            if ($self -match $pat) { throw "live file must never be read here (pattern $pat)" }
        }
    }
} finally {
    foreach ($fn in 'B906_ReadFileSnapshot', 'B906_MergeTrialRefresh') {
        if (Test-Path "function:global:$fn") { Remove-Item "function:global:$fn" -Force }
    }
    if (Test-Path -LiteralPath $fx) { Remove-Item -LiteralPath $fx -Recurse -Force }
}

if ($script:Failed -gt 0) { Write-Host "$($script:Failed) test case(s) FAILED"; exit 1 }
Write-Host 'PASS trial refresh merge tests (re-read, fingerprint refuse, merge-only commit, dashboard/mirror paths) - no live settings, no SteamVR, no hardware'
exit 0
