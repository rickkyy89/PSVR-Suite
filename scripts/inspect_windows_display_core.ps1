# SPDX-License-Identifier: MIT
# Read-only WinRT Display Core inventory and selection. Enumerates
# DisplayManager targets (adapter LUID, adapter-relative id, interface path,
# connected/stale/usage kind), optionally queries modes through the
# documented atomic-read state API, and selects ONLY by exact monitor device
# interface path passed as parameter. No TryAcquire*, no TryApply, no
# Start/Stop, no CreateDisplayDevice, no registry writes, no UAC, no
# ownership changes: a specialized connected match is reported as
# 'eligible_for_next_probe', never as ready for rendering.
# Exit codes: 0 = report produced (any selection outcome);
# 1 = enumeration/probe failure; 2 = Display Core WinRT API unavailable;
# 3 = invalid parameters.
[CmdletBinding()]
param(
    [string]$RequestedMonitorInterfacePath,
    [switch]$QueryModes
)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'display_core_selection.ps1')

function Write-DisplayCoreJsonError([int]$ExitCode,[string]$Stage,[string]$Message) {
    $report=Get-DisplayCoreReport -ErrorStage $Stage -ErrorMessage $Message
    $report | ConvertTo-Json -Depth 8
    exit $ExitCode
}

# Parameter validation runs before any WinRT type is loaded.
if ($PSBoundParameters.ContainsKey('RequestedMonitorInterfacePath') -and
    [string]::IsNullOrWhiteSpace($RequestedMonitorInterfacePath)) {
    Write-DisplayCoreJsonError 3 'parameters' '-RequestedMonitorInterfacePath was given but is empty. Selection requires the exact monitor device interface path; the probe never substitutes first/largest target or connector hints.'
}

$managerType=$null
try { $managerType=[Windows.Devices.Display.Core.DisplayManager,Windows.Devices.Display,ContentType=WindowsRuntime] } catch { $managerType=$null }
if (-not $managerType) {
    Write-DisplayCoreJsonError 2 'api-availability' 'Windows.Devices.Display.Core.DisplayManager WinRT type unavailable on this Windows build.'
}
$optionsType=$null
try { $optionsType=[Windows.Devices.Display.Core.DisplayManagerOptions,Windows.Devices.Display,ContentType=WindowsRuntime] } catch { $optionsType=$null }
if (-not $optionsType) {
    Write-DisplayCoreJsonError 2 'api-availability' 'Windows.Devices.Display.Core.DisplayManagerOptions enum unavailable on this Windows build.'
}

function Get-DisplayCoreModeQueryResult($Manager,[object[]]$Targets) {
    # Read-only mode query ONLY: the module chain walks
    # TryReadCurrentStateForModeQuery -> TryReadCurrentStateForAllTargets
    # (both listed by Microsoft driver docs under "APIs read state atomically
    # from the system"), then DisplayPath.FindModes on the accepted read-only
    # state. Acquisition (TryAcquire*) and commit (TryApply/Connect/Disconnect)
    # are never here.
    $summary=[ordered]@{ status='unknown'; reason=''; read_via=$null; attempts=@(); per_target=@{}; unkeyed=@() }
    $chain=Invoke-DisplayCoreStateReadChain -Manager $Manager
    $summary.attempts=@($chain.attempts)
    if ($chain.status -ne 'state_ok') {
        $summary.status='not_queried'
        $summary.reason=Get-DisplayCoreStateReadReason -Chain $chain
        return [pscustomobject]$summary
    }
    $state=$chain.state
    $summary.read_via=$chain.read_via
    $queryOptionsType=$null
    try {
        $queryOptionsType=[Windows.Devices.Display.Core.DisplayModeQueryOptions,Windows.Devices.Display,ContentType=WindowsRuntime]
    } catch { $queryOptionsType=$null }
    if (-not $queryOptionsType) {
        $summary.status='not_queried'
        $summary.reason='Accepted read-only DisplayState available, but DisplayModeQueryOptions type is not exposed on this build; FindModes not called.'
        return [pscustomobject]$summary
    }
    try {
        $options=[Enum]::Parse($queryOptionsType,'None')
        $stateByKey=@{}
        foreach ($live in @($state.Targets)) {
            $record=Get-DisplayCoreTargetRecord -Target $live -IncludeMonitor:$false
            $stateKey=New-DisplayCoreTargetKey -Record $record
            if ($stateKey) {
                if (-not $stateByKey.ContainsKey($stateKey)) { $stateByKey[$stateKey]=@() }
                $stateByKey[$stateKey]+=,[pscustomobject]@{ Live=$live; Record=$record }
            }
        }
        $byTarget=@{}
        $unkeyed=@()
        $ordinal=0
        foreach ($snapshot in $Targets) {
            $ordinal++
            $key=New-DisplayCoreTargetKey -Record $snapshot
            if (-not $key) {
                $unkeyed+=[pscustomobject]@{
                    ordinal=$ordinal
                    adapter_luid_hex=$snapshot.adapter_luid_hex
                    adapter_relative_id=$snapshot.adapter_relative_id
                    status='target_key_unavailable'
                    reason='Incomplete adapter LUID or adapter-relative id; refusing to join state targets on a guessed key.'
                }
                continue
            }
            if ($snapshot.stale -eq $true) {
                $byTarget[$key]=[pscustomobject]@{ status='skipped_stale'; reason='Snapshot target is stale; modes not queried.'; mode_count=0; modes=@() }
                continue
            }
            if ($snapshot.connected -ne $true) {
                $byTarget[$key]=[pscustomobject]@{ status='skipped_disconnected'; reason='Target not connected; no monitor to query modes for.'; mode_count=0; modes=@() }
                continue
            }
            $candidates=@($stateByKey[$key])
            if ($candidates.Count -eq 0) {
                $byTarget[$key]=[pscustomobject]@{ status='state_target_missing'; reason='No matching target in the read-only system state (adapter LUID + adapter-relative id).'; mode_count=0; modes=@() }
                continue
            }
            if ($candidates.Count -gt 1) {
                $byTarget[$key]=[pscustomobject]@{ status='state_ambiguous'; reason='Multiple state targets match the same adapter LUID and adapter-relative id.'; mode_count=0; modes=@() }
                continue
            }
            try {
                $path=$state.GetPathForTarget($candidates[0].Live)
                if ($null -eq $path) {
                    $byTarget[$key]=[pscustomobject]@{ status='no_active_path'; reason='Connected target has no active path in the read-only system state.'; mode_count=0; modes=@() }
                    continue
                }
                $found=@($path.FindModes($options))
                $modes=@($found | ForEach-Object { Get-DisplayCoreModeRecord -Mode $_ } | Select-Object -First 64)
                $truncated=$found.Count -gt $modes.Count
                if ($found.Count -eq 0) {
                    $byTarget[$key]=[pscustomobject]@{ status='queried_empty'; reason='Driver returned zero applicable modes for the constrained read-only path; empty result is not a hardware judgement.'; mode_count=0; modes=@(); truncated=$false }
                } else {
                    $byTarget[$key]=[pscustomobject]@{ status='queried'; reason='DisplayPath.FindModes on read-only state; current system path properties constrain the result set.'; mode_count=$found.Count; modes=$modes; truncated=$truncated }
                }
            } catch {
                $byTarget[$key]=[pscustomobject]@{ status='find_modes_error'; reason=$_.Exception.Message; mode_count=0; modes=@(); truncated=$false }
            }
        }
        $summary.status='queried_attempted'
        $summary.reason="Mode data, where present, comes only from the accepted read-only state via $($chain.read_via); reader-chain attempts recorded in modes.attempts. FindModes is a driver query; it may be slow."
        $summary.per_target=$byTarget
        $summary.unkeyed=$unkeyed
        return [pscustomobject]$summary
    } catch {
        $summary.status='query_failed'
        $summary.reason="Read-only mode walk threw: $($_.Exception.Message)"
        return [pscustomobject]$summary
    }
}

try {
    $manager=$managerType::Create([Enum]::Parse($optionsType,'None'))
} catch {
    Write-DisplayCoreJsonError 1 'manager-create' "DisplayManager.Create(None) failed: $($_.Exception.Message)"
}
try {
    $targets=@($manager.GetCurrentTargets() | ForEach-Object { Get-DisplayCoreTargetRecord -Target $_ })
    $modes=$null
    if ($QueryModes) {
        $modes=Get-DisplayCoreModeQueryResult -Manager $manager -Targets $targets
        $targets=Set-DisplayCoreTargetModes -Targets $targets -ModesByTarget $modes.per_target
    } else {
        $modes=[pscustomobject]@{
            status='not_queried'
            reason='Mode query not requested. It is available read-only via -QueryModes, which chains TryReadCurrentStateForModeQuery then TryReadCurrentStateForAllTargets plus DisplayPath.FindModes; this run called no state reader and no acquisition API.'
            read_via=$null
            attempts=@()
            per_target=@{}
            unkeyed=@()
        }
    }
    $selection=Get-DisplayCoreSelectionOutcome -Targets $targets `
        -RequestedInterfacePath $RequestedMonitorInterfacePath
    Get-DisplayCoreReport -Targets $targets -Selection $selection -Modes $modes |
        ConvertTo-Json -Depth 10
    exit 0
} catch {
    Write-DisplayCoreJsonError 1 'enumerate' "Read-only enumeration failed: $($_.Exception.Message)"
} finally {
    try { $manager.Dispose() } catch { }
}
