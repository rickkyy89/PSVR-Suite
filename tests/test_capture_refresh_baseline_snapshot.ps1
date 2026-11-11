# SPDX-License-Identifier: MIT
<#
.SYNOPSIS
    Offline regression tests for B9-05 (atomic snapshot in capture_refresh_trial_baseline.ps1).
.DESCRIPTION
    Everything runs against throwaway fixtures under %TEMP%\opencode. The
    production script is NEVER invoked (that path needs SteamVR-closed guards
    plus the live psvr_display.exe inventory and real displays), the live
    settings path is never read or written, and no SteamVR process, GUI,
    hardware or real manifest is touched. The two production snapshot helpers
    are imported from the real script via AST (renamed, not copy-pasted) so the
    behavioral cases exercise production code; static cases pin the single-read
    pattern in place.
    Usage: powershell -ExecutionPolicy Bypass -File tests\test_capture_refresh_baseline_snapshot.ps1
    Exit 0 when all cases pass, 1 otherwise.
#>
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$scriptPath = Join-Path $root 'scripts\capture_refresh_trial_baseline.ps1'
$liveSettingsDefault = 'C:\Program Files (x86)\Steam\config\steamvr.vrsettings'

$fx = Join-Path $env:TEMP ('opencode\psvr_refresh_baseline_snap_' + [Guid]::NewGuid().ToString('N').Substring(0, 8))

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
function Write-FixtureSettings {
    param([string]$Path, [int]$Numerator, [int]$Denominator = 1)
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    $json = [pscustomobject]@{
        driver_psvrpc = [pscustomobject]@{
            displayRefreshNumerator   = $Numerator
            displayRefreshDenominator = $Denominator
            nativeDirectMode          = $true
        }
    } | ConvertTo-Json -Depth 10
    # No BOM, like the real vrsettings writer path in this repo.
    [IO.File]::WriteAllText($Path, $json, (New-Object Text.UTF8Encoding($false)))
}
function Write-FixtureManifest {
    param([string]$Path, [string]$Monitor = 'DISPLAY\\SNY6A04\\5&test&0&UID123')
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    $json = [pscustomobject]@{
        kind                           = 'refresh90_trial_manifest'
        trial_identity                 = [pscustomobject]@{ monitor_device_path = $Monitor }
    } | ConvertTo-Json -Depth 10
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
    Import-ProductionFunction $ast 'Read-FileSnapshot' 'B905_ReadFileSnapshot'
    Import-ProductionFunction $ast 'Test-FileSnapshotCurrent' 'B905_TestFileSnapshotCurrent'

    Invoke-TestCase 'ast/script parses with unchanged live params and output keys' {
        $params = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.ParameterAst] }, $true) |
                  ForEach-Object { $_.Name.VariablePath.UserPath }
        foreach ($p in 'OutputDirectory', 'SettingsPath', 'ManifestPath') {
            if ($params -notcontains $p) { throw "param $p not found (got: $($params -join ','))" }
        }
        Assert-Match $text ([regex]::Escape($liveSettingsDefault)) 'live settings default kept'
        foreach ($k in 'refresh_trial_pretrial_backup', 'before_sha256', 'manifest_sha256', 'inventory_before\.json', 'runtime_refresh_before\.json') {
            Assert-Match $text $k "output/journal key $k kept"
        }
        Assert-Match $text 'Settings changed during capture; retry before editing the refresh' 'settings race message kept'
    }
    Invoke-TestCase 'ast/single snapshot feeds both parsing and digest, staleness checked for both files' {
        # Values and digest must come from one ReadAllBytes snapshot each, hashed
        # in memory; no second Get-Content/Get-FileHash read of either file.
        Assert-Match $text 'function Read-FileSnapshot' 'snapshot helper present'
        Assert-Match $text 'function Test-FileSnapshotCurrent' 'staleness helper present'
        Assert-Match $text '\$manifestSnapshot=Read-FileSnapshot \$manifestPath' 'manifest single snapshot'
        Assert-Match $text '\$settingsSnapshot=Read-FileSnapshot \$SettingsPath' 'settings single snapshot'
        Assert-Match $text 'ComputeHash\(\$bytes\)' 'digest computed over snapshot bytes'
        Assert-Match $text '\$settingsHash=\$settingsSnapshot\.Sha256' 'settings digest from snapshot'
        Assert-Match $text 'manifest_sha256=\$manifestSnapshot\.Sha256' 'manifest digest from snapshot'
        Assert-Match $text 'Test-FileSnapshotCurrent \$settingsSnapshot\.Bytes \$SettingsPath' 'settings staleness check'
        Assert-Match $text 'Test-FileSnapshotCurrent \$manifestSnapshot\.Bytes \$manifestPath' 'manifest staleness check'
        if ($text -match 'Get-Content -LiteralPath \$(SettingsPath|manifestPath)') { throw 'stale two-read pattern still present (Get-Content on live file)' }
        if ($text -match 'Get-FileHash -LiteralPath \$(SettingsPath|manifestPath)') { throw 'stale two-read pattern still present (Get-FileHash on live file)' }
    }
    Invoke-TestCase 'snap/snapshot digest matches file digest and values come from the same bytes' {
        $s = Join-Path $fx 'coherent\steamvr.vrsettings'
        Write-FixtureSettings -Path $s -Numerator 120
        $snap = & 'B905_ReadFileSnapshot' $s
        Assert-Equal (Get-FileHash -LiteralPath $s -Algorithm SHA256).Hash $snap.Sha256 'snapshot digest equals file digest'
        $parsed = $snap.Text | ConvertFrom-Json
        Assert-Equal 120 $parsed.driver_psvrpc.displayRefreshNumerator 'parsed old_value from snapshot bytes'
        if (-not (& 'B905_TestFileSnapshotCurrent' $snap.Bytes $s)) { throw 'unchanged fixture must verify current' }
    }
    Invoke-TestCase 'snap/concurrent write between snapshot and verify is rejected, never mixed' {
        # Reviewer scenario: settings change from 120 to 90 after the values
        # were read. The old code hashed the NEW file while keeping the OLD
        # old_value (120) and still accepted the capture. The new code must
        # refuse: snapshot still says 120, current file says 90.
        $s = Join-Path $fx 'race\steamvr.vrsettings'
        Write-FixtureSettings -Path $s -Numerator 120
        $snap = & 'B905_ReadFileSnapshot' $s
        Write-FixtureSettings -Path $s -Numerator 90
        $parsedOld = ($snap.Text | ConvertFrom-Json).driver_psvrpc.displayRefreshNumerator
        $parsedNew = (Get-Content -LiteralPath $s -Raw | ConvertFrom-Json).driver_psvrpc.displayRefreshNumerator
        Assert-Equal 120 $parsedOld 'snapshot keeps pre-race value'
        Assert-Equal 90 $parsedNew 'live file has post-race value'
        $staleDigest = (Get-FileHash -LiteralPath $s -Algorithm SHA256).Hash
        if ($staleDigest -eq $snap.Sha256) { throw 'post-race digest must differ from snapshot digest' }
        if (& 'B905_TestFileSnapshotCurrent' $snap.Bytes $s) { throw 'post-race file must NOT verify current; capture has to throw' }
    }
    Invoke-TestCase 'snap/same-length concurrent write is also rejected' {
        $s = Join-Path $fx 'racelen\steamvr.vrsettings'
        Write-FixtureSettings -Path $s -Numerator 120
        $snap = & 'B905_ReadFileSnapshot' $s
        # Same digit count keeps the byte length identical: comparison must be
        # content-based, not length-only.
        Write-FixtureSettings -Path $s -Numerator 110
        if (& 'B905_TestFileSnapshotCurrent' $snap.Bytes $s) { throw 'same-length change must NOT verify current' }
    }
    Invoke-TestCase 'snap/manifest follows the same snapshot rule' {
        $m = Join-Path $fx 'manifest\trial.json'
        Write-FixtureManifest -Path $m -Monitor 'DISPLAY\\SNY6A04\\5&before&0&UID1'
        $snap = & 'B905_ReadFileSnapshot' $m
        Assert-Equal (Get-FileHash -LiteralPath $m -Algorithm SHA256).Hash $snap.Sha256 'manifest snapshot digest equals file digest'
        if (-not (& 'B905_TestFileSnapshotCurrent' $snap.Bytes $m)) { throw 'unchanged manifest must verify current' }
        Write-FixtureManifest -Path $m -Monitor 'DISPLAY\\SNY6A04\\5&after&0&UID2X'
        if (& 'B905_TestFileSnapshotCurrent' $snap.Bytes $m) { throw 'changed manifest must NOT verify current' }
    }
    Invoke-TestCase 'snap/live safety: fixtures only, production script never invoked' {
        if (-not $fx.StartsWith(($env:TEMP + '\opencode'))) { throw "fixture dir escaped temp: $fx" }
        # The live file is referenced only as a literal for the AST assertion
        # above; it is never read, hashed, written or executed here.
        $self = Get-Content -LiteralPath $PSCommandPath -Raw
        if ($self -match '&\s*\$scriptPath') { throw 'this test must never execute the production script body' }
        foreach ($pat in 'Get-Content -LiteralPath \$liveSettingsDefault', 'Get-FileHash -LiteralPath \$liveSettingsDefault', 'ReadAllBytes\(\$liveSettingsDefault') {
            if ($self -match $pat) { throw "live file must never be read here (pattern $pat)" }
        }
    }
} finally {
    foreach ($fn in 'B905_ReadFileSnapshot', 'B905_TestFileSnapshotCurrent') {
        if (Test-Path "function:global:$fn") { Remove-Item "function:global:$fn" -Force }
    }
    if (Test-Path -LiteralPath $fx) { Remove-Item -LiteralPath $fx -Recurse -Force }
}

if ($script:Failed -gt 0) { Write-Host "$($script:Failed) test case(s) FAILED"; exit 1 }
Write-Host 'PASS refresh baseline snapshot tests (single-read snapshot, race rejected, manifest parity) - no live settings, no SteamVR, no hardware'
exit 0
