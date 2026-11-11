# SPDX-License-Identifier: MIT
# Pure fixtures: no registry, runtime, GPU, UAC or device operations.
param([string]$PythonExe='python')
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
. (Join-Path $root 'scripts\hmd_edid_plan.ps1')
function Assert($Value,[string]$Message) { if (-not $Value) { throw $Message } }
function Reject([scriptblock]$Action,[string]$Pattern) {
    $message=$null; try { & $Action | Out-Null } catch { $message=$_.Exception.Message }
    Assert ($message -and $message -match $Pattern) ('Expected rejection: '+$Pattern)
}
function BlockChecksum([byte[]]$Block) {
    $sum=0; for($i=0;$i -lt 127;$i++) { $sum+=$Block[$i] }
    $Block[127]=[byte]((256-($sum%256))%256)
    return ,$Block
}
function Hex([byte[]]$Bytes) { ([BitConverter]::ToString($Bytes)).Replace('-','').ToLowerInvariant() }
$base=New-Object byte[] 128
[Array]::Copy([byte[]](0,255,255,255,255,255,255,0,0x4d,0xd9,4,0x6a),$base,12)
$base[126]=1
$cta=New-Object byte[] 128
[Array]::Copy([byte[]](2,3,8,0xc0,0x43,16,31,63),$cta,8)
$source=Hex ((BlockChecksum $base)+(BlockChecksum $cta))
$patch=& $PythonExe (Join-Path $root 'tools\psvr_native_analysis\hmd_edid.py') --input-hex $source --container-guid '12345678-1234-5678-9abc-123456789abc' | ConvertFrom-Json
Assert ($LASTEXITCODE -eq 0) 'Fixture generation failed'
function Fixture {
    [pscustomobject]@{
        schema_version=1;registry_path='SYSTEM\CurrentControlSet\Enum\DISPLAY\SNY6A04\synthetic-instance\Device Parameters'
        instance_id='DISPLAY\SNY6A04\synthetic-instance'
        monitor_path='\\?\DISPLAY#SNY6A04#synthetic-instance#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}'
        raw_edid_hex=$source;effective_edid_hex=$source;patched_block_hex=$patch.patched_block_hex
        old_override_1=$null;other_overrides=[pscustomobject]@{};applied=$true
        container_guid='12345678-1234-5678-9abc-123456789abc'
    }
}
$plan=Fixture
$override=@{'1'=$plan.patched_block_hex}
$mutation=Get-HmdEdidMutation $plan $source $override Restore
Assert ($mutation.delete -and -not $mutation.applied) 'Restore original cache failed'
$mutation=Get-HmdEdidMutation $plan $patch.candidate_edid_hex $override Restore
Assert ($mutation.delete) 'Restore must accept exactly our effective EDID cache'
Reject { Get-HmdEdidMutation $plan ($source.Substring(0,$source.Length-2)+'00') $override Restore } 'cache changed'
Reject { Get-HmdEdidMutation $plan $source @{'1'=$source.Substring(256)} Restore } 'Restore conflict'
Reject { Get-HmdEdidMutation $plan $source @{'1'=$plan.patched_block_hex;'0'=$source.Substring(0,256)} Restore } 'Other EDID overrides'
$plan.applied=$false
$mutation=Get-HmdEdidMutation $plan $source @{} Apply
Assert ($mutation.value -ceq $patch.patched_block_hex) 'Apply to original state failed'
Reject { Get-HmdEdidMutation $plan $patch.candidate_edid_hex @{} Apply } 'cache changed'
$plan | Add-Member -NotePropertyName pending_operation -NotePropertyValue Apply
$mutation=Get-HmdEdidMutation $plan $patch.candidate_edid_hex $override Restore
Assert ($mutation.delete) 'Interrupted Apply is not recoverable'
$plan.applied=$true; $plan.pending_operation='Restore'
$mutation=Get-HmdEdidMutation $plan $source @{} Restore
Assert ($mutation.delete -and -not $mutation.applied) 'Interrupted Restore is not idempotently recoverable'
$plan=Fixture
$plan.old_override_1=$source.Substring(256)
$mutation=Get-HmdEdidMutation $plan $patch.candidate_edid_hex $override Restore
Assert (-not $mutation.delete -and $mutation.value -ceq $source.Substring(256)) 'Prior override not preserved'
$plan.instance_id='DISPLAY\GSM0001\synthetic-instance'
Reject { Assert-HmdEdidPlan $plan } 'identity mismatch'
$plan=Fixture; $plan.patched_block_hex=$plan.patched_block_hex.Substring(0,254)+'00'
Reject { Assert-HmdEdidPlan $plan } 'checksum'
$plan=Fixture; $tampered=ConvertFrom-EdidHex $plan.patched_block_hex
$tampered[5]=31
$plan.patched_block_hex=Hex (BlockChecksum $tampered)
Reject { Assert-HmdEdidPlan $plan } 'capabilities/timings'
'PASS 13 pure EDID plan/cache/conflict/recovery/minimal-patch checks; no hardware operations'
