# SPDX-License-Identifier: MIT
# Offline regression tests for B9-07 (unknown stale must never be eligible).
# Synthetic records only. No WinRT, no GPU, no registry, no acquisition,
# no real displays. Fixtures live under %TEMP%\opencode and are removed.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_display_core_stale_unknown.ps1
# Exit 0 when all cases pass, 1 otherwise.
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
. (Join-Path $root 'scripts\display_core_selection.ps1')

$fx=Join-Path $env:TEMP ('opencode\psvr_stale_unknown_'+[Guid]::NewGuid().ToString('N').Substring(0,8))
$script:Passed=0
$script:Failed=0
function Invoke-Case {
    param([string]$Name,[scriptblock]$Body)
    try { & $Body; $script:Passed++; Write-Host "PASS $Name" }
    catch { $script:Failed++; Write-Host "FAIL $Name :: $($_.Exception.Message)" }
}
function Assert($Value,[string]$Message) { if (-not $Value) { throw $Message } }
function Assert-Equal($Expected,$Actual,[string]$What='value') {
    if ($Expected -ne $Actual) { throw "$What - expected '$Expected', got '$Actual'" }
}
function New-SelectionRecord {
    # Normalized record shaped like Get-DisplayCoreTargetRecord output.
    param([string]$Path,[object]$Stale=[bool]$false,[bool]$Connected=$true,[string]$Usage='HeadMounted',[switch]$OmitStale)
    $rec=[pscustomobject]@{
        device_interface_path=$Path
        connected=$Connected
        usage_kind=$Usage
    }
    if (-not $OmitStale) { $rec | Add-Member -NotePropertyName 'stale' -NotePropertyValue $Stale -Force }
    return $rec
}

try {
    New-Item -ItemType Directory -Force -Path $fx | Out-Null
    Assert ($fx.StartsWith(($env:TEMP+'\opencode'))) "fixture dir escaped temp: $fx"
    # Fixture in temp: the interface path under test is persisted here, never a live display.
    $hmdPath='\\?\DISPLAY#SNY6A04#4&22ee8e3&0&UID4353#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}'
    $fixtureFile=Join-Path $fx 'stale_fixture.json'
    [IO.File]::WriteAllText($fixtureFile,(@{ requested_interface_path=$hmdPath } | ConvertTo-Json))
    $fixturePath=(Get-Content -LiteralPath $fixtureFile -Raw | ConvertFrom-Json).requested_interface_path
    Assert-Equal $hmdPath $fixturePath 'temp fixture round trip'

    Invoke-Case 'stale-true stays rejected-stale' {
        $rec=New-SelectionRecord -Path $hmdPath -Stale ([bool]$true)
        $out=Get-DisplayCoreSelectionOutcome -Targets @($rec) -RequestedInterfacePath $hmdPath
        Assert-Equal 'stale' $out.status 'status'
        Assert ($out.note -match '(?i)stale') 'stale note must mention stale'
        Assert ($out.status -ne 'eligible_for_next_probe') 'true must never be eligible'
        Assert-Equal 1 $out.matched_count 'matched_count'
        Assert ($null -ne $out.identity_basis) 'identity_basis kept'
    }

    Invoke-Case 'stale-false connected specialized stays eligible' {
        $rec=New-SelectionRecord -Path $hmdPath -Stale ([bool]$false)
        $out=Get-DisplayCoreSelectionOutcome -Targets @($rec) -RequestedInterfacePath $hmdPath
        Assert-Equal 'eligible_for_next_probe' $out.status 'status'
        Assert ($out.note -notmatch '(?i)\bread\b') 'eligible note must never claim readiness'
    }

    Invoke-Case 'stale-missing property is unknown, never eligible' {
        $rec=New-SelectionRecord -Path $hmdPath -OmitStale
        Assert ($null -eq $rec.PSObject.Properties['stale']) 'fixture must lack stale property'
        $out=Get-DisplayCoreSelectionOutcome -Targets @($rec) -RequestedInterfacePath $hmdPath
        Assert-Equal 'unknown' $out.status 'status'
        Assert ($out.note -match '(?i)unknown') 'unknown note must say unknown'
        Assert ($out.note -match '(?i)stale') 'unknown note must name the stale flag'
        Assert ($out.status -ne 'eligible_for_next_probe') 'missing stale must never be eligible'
        Assert-Equal 1 $out.matched_count 'matched_count kept'
        Assert ($null -ne $out.identity_basis) 'format kept'
    }

    Invoke-Case 'stale-null is unknown, never eligible' {
        $rec=New-SelectionRecord -Path $hmdPath -Stale $null
        $out=Get-DisplayCoreSelectionOutcome -Targets @($rec) -RequestedInterfacePath $hmdPath
        Assert-Equal 'unknown' $out.status 'status'
        Assert ($out.note -match '(?i)unknown') 'unknown note must say unknown'
        Assert ($out.note -match '(?i)stale') 'unknown note must name the stale flag'
        Assert ($out.status -ne 'eligible_for_next_probe') 'null stale must never be eligible'
    }

    Invoke-Case 'stale getter that throws is unknown, never eligible' {
        $rec=New-SelectionRecord -Path $hmdPath -Stale ([bool]$false)
        $rec.PSObject.Properties.Remove('stale')
        $rec | Add-Member -MemberType ScriptProperty -Name 'stale' -Value { throw 'synthetic stale getter failure' } -Force
        # Safe getter swallows the throw into $null; selection must still refuse eligibility.
        $safe=Get-DisplayCoreSafeProperty $rec 'stale'
        Assert ($null -eq $safe) 'safe getter must yield null on throwing getter'
        $out=Get-DisplayCoreSelectionOutcome -Targets @($rec) -RequestedInterfacePath $hmdPath
        Assert-Equal 'unknown' $out.status 'status'
        Assert ($out.note -match '(?i)unknown') 'unknown note must say unknown'
        Assert ($out.status -ne 'eligible_for_next_probe') 'throwing getter must never be eligible'
    }

    Invoke-Case 'stale non-boolean is unknown, never eligible' {
        foreach ($bad in @('false',0,1)) {
            $rec=New-SelectionRecord -Path $hmdPath -Stale $bad
            $out=Get-DisplayCoreSelectionOutcome -Targets @($rec) -RequestedInterfacePath $hmdPath
            Assert ($out.status -eq 'unknown') ("non-boolean '$bad' must be unknown, got $($out.status)")
            Assert ($out.status -ne 'eligible_for_next_probe') 'non-boolean stale must never be eligible'
        }
    }

    Invoke-Case 'offline safety: no live display, registry or acquisition calls' {
        Assert ($fx.StartsWith(($env:TEMP+'\opencode'))) "fixtures must stay in temp: $fx"
        $self=Get-Content -LiteralPath $PSCommandPath -Raw
        # Patterns built dynamically so this guard itself does not contain the literal under test.
        foreach ($pat in @('Get-'+'PnpDevice','Get-'+'CimInstance','HK'+'LM','Win'+'RT','TryAcquire'+'Target','inspect_windows_display_'+'core')) {
            if ($self -match [regex]::Escape($pat)) { throw "this test must not reference live surface '$pat'" }
        }
    }
} finally {
    if (Test-Path -LiteralPath $fx) { Remove-Item -LiteralPath $fx -Recurse -Force -ErrorAction SilentlyContinue }
}

if ($script:Failed -gt 0) { Write-Host "$($script:Failed) case(s) FAILED, $($script:Passed) passed"; exit 1 }
Write-Host "PASS stale-unknown regression: $($script:Passed) cases (true=rejected-stale, false=eligible, missing/null/throw/non-boolean=unknown, never eligible); fixtures in temp, no real displays"
exit 0
