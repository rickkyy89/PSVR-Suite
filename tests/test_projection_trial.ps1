# SPDX-License-Identifier: MIT
<#
.SYNOPSIS
    Hardware-free tests for scripts/set_projection_trial.ps1.
.DESCRIPTION
    Everything runs against throwaway fixtures under %TEMP%\opencode:
    a fake steamvr.vrsettings plus an isolated -BackupParent per case.
    The live path C:\Program Files (x86)\Steam\config\steamvr.vrsettings
    is never read or written; -SteamVrSettingsPath injection is required
    for every live-affecting call. No SteamVR process, no GUI, no hardware.
    Usage: powershell -ExecutionPolicy Bypass -File tests\test_projection_trial.ps1
    Exit 0 when all cases pass, 1 otherwise.
#>
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$trial = Join-Path $root 'scripts\set_projection_trial.ps1'

$fx = Join-Path $env:TEMP ('opencode\psvr_projection_trial_' + [Guid]::NewGuid().ToString('N').Substring(0, 8))

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
    if ($Actual -notmatch $Pattern) { throw "$What '$Actual' does not match /$Pattern/" }
}
function Assert-Throws {
    param([scriptblock]$Body, [string]$Pattern)
    $msg = $null
    try { & $Body | Out-Null } catch { $msg = $_.Exception.Message }
    if (-not $msg) { throw "expected an error matching /$Pattern/ but none was raised" }
    if ($msg -notmatch $Pattern) { throw "error '$msg' does not match /$Pattern/" }
}
function Assert-ApproxEqual {
    param([double]$Expected, [double]$Actual, [double]$Tolerance, [string]$What = 'value')
    if ([Math]::Abs($Expected - $Actual) -gt $Tolerance) { throw "$What - expected ~'$Expected' (+/- $Tolerance), got '$Actual'" }
}
function Write-Text { param([string]$Path, [string]$Content)
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    Set-Content -LiteralPath $Path -Value $Content
}
function Write-ProjectionSettings {
    param([string]$Path, [string]$DistortionMode = 'identity',
        [double]$Left = -1.0, [double]$Right = 1.0, [double]$Top = -1.0, [double]$Bottom = 1.0)
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    $json = [pscustomobject]@{
        driver_psvrpc = [pscustomobject]@{
            distortionMode        = $DistortionMode
            projectionLeftLeft    = $Left
            projectionLeftRight   = $Right
            projectionLeftTop     = $Top
            projectionLeftBottom  = $Bottom
            projectionRightLeft   = $Left
            projectionRightRight  = $Right
            projectionRightTop    = $Top
            projectionRightBottom = $Bottom
            displayWidth          = 1920
        }
        steamvr = [pscustomobject]@{ showMirrorView = $false }
    } | ConvertTo-Json -Depth 10
    Set-Content -LiteralPath $Path -Value $json -Encoding UTF8
}
function New-CaseDir { param([string]$Name)
    $dir = Join-Path $fx $Name
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $dir 'backups') | Out-Null
    return $dir
}

# Offline isolation: fixture runs must not see a live SteamVR (vrserver/vrcompositor
# is running on dev machines). Shadow Get-Process globally in this test process only
# so the production guard in scripts/set_projection_trial.ps1 keeps working unchanged:
# fixtures see no process ($null), while the live-guard test below flips
# $global:MockSteamVRActive to prove the real guard still throws. No SkipGuard
# bypass option is introduced in production. Script-scoped functions are invisible
# to "& $trial", hence the global: scope (cleaned up in finally).
$global:MockSteamVRActive = $false
function global:Get-Process {
    [CmdletBinding()]
    param(
        [Parameter(Position = 0)][string[]]$Name
    )
    $targetsSteamVR = $false
    if ($null -ne $Name) {
        foreach ($n in $Name) {
            if ($n -eq 'vrserver' -or $n -eq 'vrcompositor') { $targetsSteamVR = $true }
        }
    }
    if ($targetsSteamVR) {
        if ($global:MockSteamVRActive) {
            return [pscustomobject]@{ ProcessName = 'vrserver'; Id = 999999 }
        }
        return $null
    }
    # Passthrough for unrelated queries: preserve real cmdlet behavior.
    if ($null -ne $Name) {
        return Microsoft.PowerShell.Management\Get-Process -Name $Name -ErrorAction $ErrorActionPreference
    }
    return Microsoft.PowerShell.Management\Get-Process -ErrorAction $ErrorActionPreference
}

try {
    New-Item -ItemType Directory -Force -Path $fx | Out-Null

    # --- AST: script parses with legacy + independent pair + restore, no ambiguous mix ---
    Invoke-TestCase 'ast/parses with expected params and default set' {
        $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile($trial, [ref]$null, [ref]$errors)
        if ($errors.Count -ne 0) { throw "parse errors: $($errors[0].Message)" }
        $params = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.ParameterAst] }, $true) |
                  ForEach-Object { $_.Name.VariablePath.UserPath }
        foreach ($p in 'FovDegrees', 'HorizontalFovDegrees', 'VerticalFovDegrees', 'RestoreFrom', 'SteamVrSettingsPath', 'BackupParent') {
            if ($params -notcontains $p) { throw "param $p not found (got: $($params -join ','))" }
        }
        $text = Get-Content -LiteralPath $trial -Raw
        Assert-Match $text 'DefaultParameterSetName\s*=\s*.Set.' 'default set'
        Assert-Match $text '\[double\]\s*\$FovDegrees' 'FovDegrees type'
        Assert-Match $text '\[double\]\s*\$HorizontalFovDegrees' 'Horizontal type'
        Assert-Match $text '\[double\]\s*\$VerticalFovDegrees' 'Vertical type'
    }
    Invoke-TestCase 'ast/parameter sets keep legacy apart from independent pair' {
        $text = Get-Content -LiteralPath $trial -Raw
        # Legacy uniform lives only in Set; H/V live only together in SetHV; restore isolated.
        Assert-Match $text "ParameterSetName=.Set." 'Set set present'
        Assert-Match $text "ParameterSetName=.SetHV." 'SetHV set present'
        Assert-Match $text "ParameterSetName=.Restore." 'Restore set present'
        Assert-Match $text '\$FovDegrees' 'FovDegrees present'
        Assert-Match $text '\$HorizontalFovDegrees' 'Horizontal present'
        Assert-Match $text '\$VerticalFovDegrees' 'Vertical present'
        Assert-Match $text '\$RestoreFrom' 'RestoreFrom present'
        $fovSetHV = ([regex]::Matches($text, "SetHV[^`r`n]*FovDegrees[^H]")).Count
        if ($text -match '\$FovDegrees[^,]*SetHV|SetHV[^,]*\$FovDegrees' -and $text -notmatch 'HorizontalFovDegrees') { throw 'FovDegrees must not belong to SetHV' }
        $rangeCount = ([regex]::Matches($text, 'ValidateRange\s*\(\s*70\s*,\s*110\s*\)')).Count
        if ($rangeCount -lt 3) { throw "expected ValidateRange(70,110) on all 3 FOV params, found $rangeCount" }
        # Precise set membership via AST attributes (avoids regex spanning lines).
        $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile($trial, [ref]$null, [ref]$errors)
        $paramAsts = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.ParameterAst] }, $true)
        $setsOf = @{}
        foreach ($p in $paramAsts) {
            $name = $p.Name.VariablePath.UserPath
            $sets = @()
            foreach ($a in $p.Attributes) {
                if ($a.TypeName.FullName -eq 'Parameter') {
                    foreach ($na in $a.NamedArguments) {
                        if ($na.ArgumentName -eq 'ParameterSetName') {
                            $sets += $na.Argument.Value
                        }
                    }
                }
            }
            $setsOf[$name] = $sets
        }
        Assert-Equal 'Set' ($setsOf['FovDegrees'] -join ',') 'FovDegrees set'
        Assert-Equal 'SetHV' ($setsOf['HorizontalFovDegrees'] -join ',') 'Horizontal set'
        Assert-Equal 'SetHV' ($setsOf['VerticalFovDegrees'] -join ',') 'Vertical set'
        Assert-Equal 'Restore' ($setsOf['RestoreFrom'] -join ',') 'Restore set'
    }
    Invoke-TestCase 'ast/live defaults unchanged, injection optional' {
        $text = Get-Content -LiteralPath $trial -Raw
        Assert-Match $text 'SteamVrSettingsPath' 'injection param present'
        Assert-Match $text 'Program Files \(x86\).*steamvr\.vrsettings' 'live settings default'
        Assert-Match $text 'BackupParent' 'backup injection present'
        Assert-Match $text 'vrserver' 'SteamVR guard'
        Assert-Match $text 'identity' 'identity require'
        Assert-Match $text 'tangentH' 'H tangent'
        Assert-Match $text 'tangentV' 'V tangent'
    }

    # --- pure math: tan(FOV/2), 90 -> 1, 83.2671 -> 8/9, bounds 70..110 ---
    Invoke-TestCase 'math/tan half-angle maps 90 to 1 and 83.2671 to 8/9' {
        $t90 = [Math]::Tan(90 * [Math]::PI / 360)
        Assert-ApproxEqual 1.0 $t90 1e-9 'tan(90/2)'
        $tDiag = [Math]::Tan(83.2671 * [Math]::PI / 360)
        Assert-ApproxEqual (8.0 / 9.0) $tDiag 1e-3 'tan(83.2671/2) ~= 8/9'
        $t70 = [Math]::Tan(70 * [Math]::PI / 360)
        $t110 = [Math]::Tan(110 * [Math]::PI / 360)
        Assert-ApproxEqual 0.7002 $t70 1e-3 'lower bound'
        Assert-ApproxEqual 1.4281 $t110 1e-3 'upper bound'
        if (-not ($t70 -lt $t90 -and $t90 -lt $t110)) { throw 'tangents must grow monotonically over 70..110' }
    }

    # --- binding: pair required, mixing rejected, range enforced (offline paths) ---
    Invoke-TestCase 'bind/single Horizontal without Vertical is rejected' {
        $dir = New-CaseDir 'bind_single'
        $settings = Join-Path $dir 'steamvr.vrsettings'
        Write-ProjectionSettings -Path $settings
        Assert-Throws { & $trial -HorizontalFovDegrees 83.2671 -SteamVrSettingsPath $settings -BackupParent (Join-Path $dir 'backups') } 'VerticalFovDegrees'
    }
    Invoke-TestCase 'bind/mixing legacy FovDegrees with independent pair is ambiguous' {
        $dir = New-CaseDir 'bind_mix'
        $settings = Join-Path $dir 'steamvr.vrsettings'
        Write-ProjectionSettings -Path $settings
        Assert-Throws { & $trial -FovDegrees 90 -HorizontalFovDegrees 83.2671 -VerticalFovDegrees 90 -SteamVrSettingsPath $settings -BackupParent (Join-Path $dir 'backups') } 'set di parametri|parameter set'
    }
    Invoke-TestCase 'bind/out-of-range FOV rejected' {
        $dir = New-CaseDir 'bind_range'
        $settings = Join-Path $dir 'steamvr.vrsettings'
        Write-ProjectionSettings -Path $settings
        Assert-Throws { & $trial -FovDegrees 69 -SteamVrSettingsPath $settings -BackupParent (Join-Path $dir 'backups') } 'FovDegrees'
        Assert-Throws { & $trial -HorizontalFovDegrees 83.2671 -VerticalFovDegrees 111 -SteamVrSettingsPath $settings -BackupParent (Join-Path $dir 'backups') } 'VerticalFovDegrees'
    }

    # --- functional offline: legacy uniform sets both axes together ---
    Invoke-TestCase 'set/legacy FovDegrees 90 writes +-1 on all axes' {
        $dir = New-CaseDir 'set_legacy'
        $settings = Join-Path $dir 'steamvr.vrsettings'
        $backups = Join-Path $dir 'backups'
        Write-ProjectionSettings -Path $settings -Left -0.5 -Right 0.5 -Top -0.5 -Bottom 0.5
        $report = (& $trial -FovDegrees 90 -SteamVrSettingsPath $settings -BackupParent $backups | Out-String) | ConvertFrom-Json
        Assert-Equal 'Set' $report.projection_trial 'trial set'
        Assert-Equal 90 $report.nominal_fov_degrees 'nominal'
        Assert-Equal $false $report.optical_calibration_verified 'not calibration'
        if ($null -ne $report.nominal_horizontal_fov_degrees) { throw 'legacy run must leave horizontal nominal null' }
        $after = Get-Content -LiteralPath $settings -Raw | ConvertFrom-Json
        foreach ($k in @('projectionLeftLeft','projectionRightLeft','projectionLeftTop','projectionRightTop')) {
            Assert-ApproxEqual -1.0 $after.driver_psvrpc.$k 1e-9 $k
        }
        foreach ($k in @('projectionLeftRight','projectionRightRight','projectionLeftBottom','projectionRightBottom')) {
            Assert-ApproxEqual 1.0 $after.driver_psvrpc.$k 1e-9 $k
        }
        $backs = @(Get-ChildItem -LiteralPath $backups -Filter 'projection_before_*.json' | Where-Object { $_.Name -notlike '*.replaced.json' })
        Assert-Equal 1 $backs.Count 'one backup'
        Assert-Equal 1920 $after.driver_psvrpc.displayWidth 'unrelated key preserved'
        Assert-Equal $false $after.steamvr.showMirrorView 'other section preserved'
    }

    # --- functional offline: independent H83.2671 V90 narrows only lateral ---
    Invoke-TestCase 'set/independent H83.2671 V90 writes 8-9 lateral and +-1 vertical' {
        $dir = New-CaseDir 'set_hv'
        $settings = Join-Path $dir 'steamvr.vrsettings'
        $backups = Join-Path $dir 'backups'
        Write-ProjectionSettings -Path $settings
        $expectedH = [Math]::Tan(83.2671 * [Math]::PI / 360)
        $expectedV = [Math]::Tan(90 * [Math]::PI / 360)
        $report = (& $trial -HorizontalFovDegrees 83.2671 -VerticalFovDegrees 90 -SteamVrSettingsPath $settings -BackupParent $backups | Out-String) | ConvertFrom-Json
        Assert-Equal 'SetHV' $report.projection_trial 'trial set'
        Assert-ApproxEqual 83.2671 $report.nominal_horizontal_fov_degrees 1e-9 'nominal H'
        Assert-ApproxEqual 90 $report.nominal_vertical_fov_degrees 1e-9 'nominal V'
        if ($null -ne $report.nominal_fov_degrees) { throw 'independent run must leave uniform nominal null' }
        $after = Get-Content -LiteralPath $settings -Raw | ConvertFrom-Json
        Assert-ApproxEqual (-$expectedH) $after.driver_psvrpc.projectionLeftLeft 1e-9 'LeftLeft'
        Assert-ApproxEqual $expectedH $after.driver_psvrpc.projectionLeftRight 1e-9 'LeftRight'
        Assert-ApproxEqual (-$expectedH) $after.driver_psvrpc.projectionRightLeft 1e-9 'RightLeft'
        Assert-ApproxEqual $expectedH $after.driver_psvrpc.projectionRightRight 1e-9 'RightRight'
        Assert-ApproxEqual (-$expectedV) $after.driver_psvrpc.projectionLeftTop 1e-9 'LeftTop'
        Assert-ApproxEqual $expectedV $after.driver_psvrpc.projectionLeftBottom 1e-9 'LeftBottom'
        Assert-ApproxEqual (-$expectedV) $after.driver_psvrpc.projectionRightTop 1e-9 'RightTop'
        Assert-ApproxEqual $expectedV $after.driver_psvrpc.projectionRightBottom 1e-9 'RightBottom'
        Assert-ApproxEqual (8.0/9.0) $expectedH 1e-3 'diagnostic is panel aspect, not optics'
    }

    # --- guards: identity required, missing key fails closed ---
    Invoke-TestCase 'guard/non-identity distortion refuses SetHV' {
        $dir = New-CaseDir 'guard_identity'
        $settings = Join-Path $dir 'steamvr.vrsettings'
        Write-ProjectionSettings -Path $settings -DistortionMode 'profile'
        Assert-Throws { & $trial -HorizontalFovDegrees 83.2671 -VerticalFovDegrees 90 -SteamVrSettingsPath $settings -BackupParent (Join-Path $dir 'backups') } 'identity mode'
    }
    Invoke-TestCase 'guard/missing projection key throws before backup replace' {
        $dir = New-CaseDir 'guard_missing'
        $settings = Join-Path $dir 'steamvr.vrsettings'
        Write-ProjectionSettings -Path $settings
        $broken = Get-Content -LiteralPath $settings -Raw | ConvertFrom-Json
        $broken.driver_psvrpc.PSObject.Properties.Remove('projectionRightBottom')
        $broken | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $settings -Encoding UTF8
        Assert-Throws { & $trial -FovDegrees 90 -SteamVrSettingsPath $settings -BackupParent (Join-Path $dir 'backups') } 'Projection key missing'
    }
    Invoke-TestCase 'guard/SteamVR active still blocks fixture run (live guard preserved)' {
        $dir = New-CaseDir 'guard_live'
        $settings = Join-Path $dir 'steamvr.vrsettings'
        Write-ProjectionSettings -Path $settings
        $global:MockSteamVRActive = $true
        try {
            Assert-Throws { & $trial -FovDegrees 90 -SteamVrSettingsPath $settings -BackupParent (Join-Path $dir 'backups') } 'Close SteamVR'
        } finally { $global:MockSteamVRActive = $false }
    }
    Invoke-TestCase 'guard/no SkipGuard bypass, live guard text unchanged' {
        $text = Get-Content -LiteralPath $trial -Raw
        if ($text -match 'SkipGuard|BypassGuard|NoGuard|IgnoreSteamVR') { throw 'production guard must not expose a bypass switch' }
        Assert-Match $text 'Get-Process.*vrserver' 'live guard present'
        Assert-Match $text 'Close SteamVR before changing frozen projection' 'live guard message'
    }

    # --- restore: selective 8 keys, everything else preserved ---
    Invoke-TestCase 'restore/selective 8 keys, preserves identity and unrelated settings' {
        $dir = New-CaseDir 'restore'
        $settings = Join-Path $dir 'steamvr.vrsettings'
        $backups = Join-Path $dir 'backups'
        Write-ProjectionSettings -Path $settings -Left -0.7 -Right 0.7 -Top -0.7 -Bottom 0.7
        $before = Get-Content -LiteralPath $settings -Raw | ConvertFrom-Json
        # Restore source carries the diagnostic pair plus a decoy key that must not leak.
        $source = Join-Path $dir 'restore_source.json'
        $tH = [Math]::Tan(83.2671 * [Math]::PI / 360)
        $srcObj = [pscustomobject]@{
            driver_psvrpc = [pscustomobject]@{
                projectionLeftLeft = -$tH; projectionLeftRight = $tH; projectionLeftTop = -1.0; projectionLeftBottom = 1.0
                projectionRightLeft = -$tH; projectionRightRight = $tH; projectionRightTop = -1.0; projectionRightBottom = 1.0
                displayWidth = 9999
            }
        } | ConvertTo-Json -Depth 10
        Write-Text $source $srcObj
        $report = (& $trial -RestoreFrom $source -SteamVrSettingsPath $settings -BackupParent $backups | Out-String) | ConvertFrom-Json
        Assert-Equal 'Restore' $report.projection_trial 'trial restore'
        $after = Get-Content -LiteralPath $settings -Raw | ConvertFrom-Json
        Assert-ApproxEqual (-$tH) $after.driver_psvrpc.projectionLeftLeft 1e-9 'restored H'
        Assert-ApproxEqual 1.0 $after.driver_psvrpc.projectionLeftBottom 1e-9 'restored V'
        Assert-Equal 1920 $after.driver_psvrpc.displayWidth 'selective restore keeps live unrelated key'
        Assert-Equal $before.driver_psvrpc.distortionMode $after.driver_psvrpc.distortionMode 'distortion untouched'
        Assert-Equal $false $after.steamvr.showMirrorView 'other section untouched'
        $backs = @(Get-ChildItem -LiteralPath $backups -Filter 'projection_before_*.json' | Where-Object { $_.Name -notlike '*.replaced.json' })
        Assert-Equal 1 $backs.Count 'restore also backups first'
    }
} finally {
    if (Test-Path -LiteralPath $fx) { Remove-Item -LiteralPath $fx -Recurse -Force }
    if (Test-Path 'function:global:Get-Process') { Remove-Item 'function:global:Get-Process' -Force }
    $global:MockSteamVRActive = $false
}

if ($script:Failed -gt 0) { Write-Host "$($script:Failed) test case(s) FAILED"; exit 1 }
Write-Host 'PASS projection trial tests (AST sets, tan math/bounds, offline set/restore/guards) - no live settings, no SteamVR, no hardware'
exit 0
