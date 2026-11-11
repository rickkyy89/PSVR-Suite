# SPDX-License-Identifier: MIT
# Pure plan/cache/conflict checks. No registry, processes, UAC or device calls.
function ConvertFrom-EdidHex([string]$Text) {
    if (-not $Text -or $Text.Length -gt 8192 -or $Text.Length % 256 -or $Text -notmatch '^[0-9a-fA-F]+$') { throw 'Invalid complete EDID hex blocks' }
    $bytes=New-Object byte[] ($Text.Length/2)
    for ($i=0;$i -lt $bytes.Length;$i++) { $bytes[$i]=[Convert]::ToByte($Text.Substring($i*2,2),16) }
    for ($offset=0;$offset -lt $bytes.Length;$offset+=128) {
        $sum=0; for ($i=0;$i -lt 128;$i++) { $sum+=$bytes[$offset+$i] }
        if ($sum % 256) { throw 'Invalid EDID block checksum' }
    }
    return ,$bytes
}
function Assert-HmdEdidPlan($Plan) {
    if ($Plan.schema_version -ne 1 -or $Plan.registry_path -notmatch '^SYSTEM\\CurrentControlSet\\Enum\\DISPLAY\\SNY6A04\\([^\\]+)\\Device Parameters$') { throw 'Plan scope/schema mismatch' }
    $suffix=$Matches[1]
    if ($Plan.instance_id -cne ('DISPLAY\SNY6A04\'+$suffix) -or
        $Plan.monitor_path -cne ('\\?\DISPLAY#SNY6A04#'+$suffix+'#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}')) { throw 'Plan device identity mismatch' }
    foreach ($text in @($Plan.raw_edid_hex,$Plan.effective_edid_hex)) {
        $bytes=ConvertFrom-EdidHex $text
        if ($bytes.Length -lt 256 -or $bytes.Length -ne (1+$bytes[126])*128 -or
            $text.Substring(0,16) -cne '00ffffffffffff00' -or $text.Substring(16,8) -cne '4dd9046a') { throw 'Plan descriptor identity/length mismatch' }
    }
    if ($Plan.raw_edid_hex.Length -ne $Plan.effective_edid_hex.Length) { throw 'Plan descriptor sizes disagree' }
    if ($Plan.patched_block_hex.Length -ne 256) { throw 'Patch must be exactly one CTA block' }
    $candidate=ConvertFrom-EdidHex $Plan.patched_block_hex
    $original=(ConvertFrom-EdidHex $Plan.effective_edid_hex)[128..255]
    if ($original[0] -ne 2 -or $candidate[0] -ne 2 -or $candidate[1] -ne 3 -or
        $candidate[2] -ne $original[2]+22 -or $candidate[2] -gt 127) { throw 'Patch is not a minimal CTA insertion' }
    $compat=$Plan.PSObject.Properties['compat_non_microsoft_bit']
    $usage=if ($compat -and $compat.Value) { 0x27 } else { 7 }
    $guid=[Guid]$Plan.container_guid
    if ($guid -eq [Guid]::Empty) { throw 'Missing plan container identifier' }
    [byte[]]$expectedBlock=[byte[]](0x75,0x5c,0x12,0xca,2,$usage)+$guid.ToByteArray()
    $found=-1; $position=4
    while ($position -lt $candidate[2]) {
        $length=$candidate[$position] -band 31
        if (-not $length -or $position+1+$length -gt $candidate[2]) { throw 'Malformed candidate CTA collection' }
        if (($candidate[$position] -shr 5) -eq 3 -and $length -ge 3 -and
            $candidate[$position+1] -eq 0x5c -and $candidate[$position+2] -eq 0x12 -and $candidate[$position+3] -eq 0xca) {
            if ($found -ge 0 -or $length -ne 21 -or [BitConverter]::ToString([byte[]]$candidate[$position..($position+21)]) -cne [BitConverter]::ToString($expectedBlock)) { throw 'Unexpected Microsoft HMD block' }
            $found=$position
        }
        $position+=1+$length
    }
    if ($found -lt 0) { throw 'Candidate HMD block missing' }
    [byte[]]$tail=@()
    if ($found+22 -lt 127) { $tail=[byte[]]$candidate[($found+22)..126] }
    [byte[]]$recovered=$candidate[0..($found-1)]+$tail+(New-Object byte[] 22)+[byte[]]@($original[127])
    $recovered[2]=$original[2]
    if ([BitConverter]::ToString($recovered) -cne [BitConverter]::ToString([byte[]]$original)) { throw 'Patch changes original CTA capabilities/timings' }
    if ($null -ne $Plan.old_override_1) {
        if ($Plan.old_override_1.Length -ne 256) { throw 'Invalid previous CTA override' }
        $null=ConvertFrom-EdidHex $Plan.old_override_1
    }
}
function Get-HmdEdidMutation($Plan,[string]$ObservedCache,$Overrides,[string]$Action) {
    Assert-HmdEdidPlan $Plan
    if ($Action -notin @('Apply','Restore')) { throw 'Unsupported EDID operation' }
    $expected=$Plan.effective_edid_hex.Substring(0,256)+$Plan.patched_block_hex+$Plan.effective_edid_hex.Substring(512)
    if ($ObservedCache -cne $Plan.raw_edid_hex -and ($Action -ne 'Restore' -or $ObservedCache -cne $expected)) { throw 'Monitor EDID cache changed unexpectedly since preparation' }
    $current=if ($Overrides.ContainsKey('1')) { $Overrides['1'] } else { $null }
    $others=@($Overrides.Keys | Where-Object { $_ -ne '1' })
    $expectedOthers=@($Plan.other_overrides.PSObject.Properties)
    if ($others.Count -ne $expectedOthers.Count) { throw 'Other EDID overrides changed since preparation' }
    foreach ($p in $expectedOthers) {
        if (-not $Overrides.ContainsKey($p.Name) -or $Overrides[$p.Name] -cne $p.Value) { throw 'Other EDID override conflict' }
    }
    if ($Action -eq 'Apply') {
        if ($Plan.applied -or $current -cne $Plan.old_override_1) { throw 'Plan already applied or override conflict' }
        return [pscustomobject]@{delete=$false;value=$Plan.patched_block_hex;applied=$true}
    }
    $pending=$Plan.PSObject.Properties['pending_operation']
    $recoverable=$pending -and $pending.Value -eq 'Apply'
    if ($pending -and $pending.Value -eq 'Restore' -and $current -ceq $Plan.old_override_1) {
        return [pscustomobject]@{delete=($null -eq $Plan.old_override_1);value=$Plan.old_override_1;applied=$false}
    }
    if ((-not $Plan.applied -and -not $recoverable) -or $current -cne $Plan.patched_block_hex) { throw 'Restore conflict; current override does not match this experiment' }
    return [pscustomobject]@{delete=($null -eq $Plan.old_override_1);value=$Plan.old_override_1;applied=$false}
}
