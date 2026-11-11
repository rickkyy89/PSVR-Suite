# SPDX-License-Identifier: MIT
# Validate the current restoration gate without elevation or any registry writes.
# B9-08: read-only live registry read, no writes (OpenSubKey read-only, Dispose;
# never SetValue/DeleteValue). Touches live hardware state by reading it.
# Offline path: tests/test_hmd_edid_plan.ps1 (pure fixtures, no registry) plus
# the Get-HmdEdidMutation harness in scripts/hmd_edid_plan.ps1.
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$PlanPath)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'hmd_edid_plan.ps1')
$plan=Get-Content -LiteralPath $PlanPath -Raw | ConvertFrom-Json
Assert-HmdEdidPlan $plan
$key=[Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($plan.registry_path,$false)
if (-not $key) { throw 'Selected monitor registry instance missing' }
try {
    $cache=([BitConverter]::ToString([byte[]]$key.GetValue('EDID'))).Replace('-','').ToLowerInvariant()
    $overrides=@{}
    $override=$key.OpenSubKey('EDID_OVERRIDE',$false)
    if ($override) {
        try { foreach ($name in $override.GetValueNames()) { $overrides[$name]=([BitConverter]::ToString([byte[]]$override.GetValue($name))).Replace('-','').ToLowerInvariant() } }
        finally { $override.Dispose() }
    }
    $expected=$plan.effective_edid_hex.Substring(0,256)+$plan.patched_block_hex+$plan.effective_edid_hex.Substring(512)
    $errorMessage=$null; $mutation=$null
    try { $mutation=Get-HmdEdidMutation $plan $cache $overrides Restore } catch { $errorMessage=$_.Exception.Message }
    [pscustomobject]@{read_only=$true;instance_id=$plan.instance_id;plan_applied=$plan.applied;cache_matches_original=($cache -ceq $plan.raw_edid_hex);cache_matches_own_candidate=($cache -ceq $expected);override_matches_own_candidate=($overrides['1'] -ceq $plan.patched_block_hex);restore_gate_accepted=($null -ne $mutation);restore_would_delete_1=$(if ($mutation) {$mutation.delete} else {$null});error=$errorMessage;note='No restore executed. Administrator access still required for actual write/restart.'} | ConvertTo-Json
} finally { $key.Dispose() }
