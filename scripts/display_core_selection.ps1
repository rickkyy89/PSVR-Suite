# SPDX-License-Identifier: MIT
# Pure helpers for the read-only Windows Display Core probe. No WinRT calls,
# no GPU access, no registry: safe to unit-test offline with synthetic records.
# API facts verified against Microsoft Learn (Windows.Devices.Display.Core,
# winrt contracts 17763-28000): DisplayTarget.Adapter/AdapterRelativeId/
# DeviceInterfacePath/IsConnected/IsStale/UsageKind; DisplayMonitorUsageKind
# = {Standard, SpecialPurpose, HeadMounted}; DisplayModeQueryOptions =
# {None, OnlyPreferredResolution}; DisplayModeInfo.TargetResolution/
# SourceResolution are SizeInt32; PresentationRate.VerticalSyncRate is a
# rational; DisplayManagerOptions = {None, EnforceSourceOwnership,
# VirtualRefreshRateAware}.
# Selection identity is the exact monitor device interface path ONLY.
# Adapter LUID, adapter-relative id and DisplayAdapterTargetId are diagnostic
# hints; they never prove identity and never authorize acquisition. Nothing
# here claims acquisition success or rendering readiness.
$script:DisplayCoreSchemaVersion=1
$script:DisplayCoreSpecializedUsageKinds=@('SpecialPurpose','HeadMounted')

function Get-DisplayCoreSafeProperty {
    # Reads one property without throwing when it is absent or fails on get.
    param([object]$InputObject,[string]$Name)
    if ($null -eq $InputObject -or [string]::IsNullOrWhiteSpace($Name)) { return $null }
    try {
        $property=$InputObject.PSObject.Properties[$Name]
        if ($null -eq $property) { return $null }
        return $property.Value
    } catch { return $null }
}

function ConvertTo-DisplayCoreLuid {
    # Luid {Id {LowPart, HighPart}} -> {low, high, hex}; $null-safe.
    param([object]$Adapter)
    $id=Get-DisplayCoreSafeProperty $Adapter 'Id'
    $low=Get-DisplayCoreSafeProperty $id 'LowPart'
    $high=Get-DisplayCoreSafeProperty $id 'HighPart'
    if ($null -eq $low -and $null -eq $high) { return $null }
    $hex=$null
    try {
        $hex='0x{0:x8}{1:x8}' -f ([uint64]([uint32]$high)),([uint64]([uint32]$low))
    } catch { $hex=$null }
    return [pscustomobject]@{ low=$low; high=$high; hex=$hex }
}

function Convert-DisplayCoreEnumText {
    # Enum or string -> invariant identifier text; $null stays $null.
    param([object]$Value)
    if ($null -eq $Value) { return $null }
    return ([string]$Value)
}

function Get-DisplayCoreMonitorRecord {
    # Windows.Devices.Display.DisplayMonitor from DisplayTarget.TryGetMonitor():
    # documented read-only descriptor, $null when nothing is currently
    # connected. DisplayAdapterTargetId is a connector HINT, not identity.
    param([object]$Monitor)
    if ($null -eq $Monitor) { return $null }
    $nativeResolution=Get-DisplayCoreSafeProperty $Monitor 'NativeResolutionInRawPixels'
    return [pscustomobject]@{
        device_id=Get-DisplayCoreSafeProperty $Monitor 'DeviceId'
        display_name=Get-DisplayCoreSafeProperty $Monitor 'DisplayName'
        usage_kind=Convert-DisplayCoreEnumText (Get-DisplayCoreSafeProperty $Monitor 'UsageKind')
        display_adapter_target_id=Get-DisplayCoreSafeProperty $Monitor 'DisplayAdapterTargetId'
        display_adapter_target_id_note='Connector hint from monitor EDID enumeration; does not identify the target by itself.'
        connection_kind=Convert-DisplayCoreEnumText (Get-DisplayCoreSafeProperty $Monitor 'ConnectionKind')
        physical_connector=Convert-DisplayCoreEnumText (Get-DisplayCoreSafeProperty $Monitor 'PhysicalConnector')
        native_resolution_raw=if ($null -ne $nativeResolution) {
            [pscustomobject]@{
                width=Get-DisplayCoreSafeProperty $nativeResolution 'Width'
                height=Get-DisplayCoreSafeProperty $nativeResolution 'Height'
            }
        } else { $null }
        native_resolution_note='Monitor EDID native resolution only; not a scanout mode guarantee.'
    }
}

function Get-DisplayCoreTargetRecord {
    # Normalizes one DisplayTarget (live or synthetic) into JSON-safe fields.
    # Only documented read-only members are touched: no TryAcquire*, no
    # TryApply, no Start/Stop, no state mutation.
    param([object]$Target,[switch]$IncludeMonitor=$true)
    if ($null -eq $Target) { return $null }
    $luid=ConvertTo-DisplayCoreLuid (Get-DisplayCoreSafeProperty $Target 'Adapter')
    $interfacePath=[string](Get-DisplayCoreSafeProperty $Target 'DeviceInterfacePath')
    if ([string]::IsNullOrWhiteSpace($interfacePath)) { $interfacePath=$null }
    $monitor=$null
    if ($IncludeMonitor -and $Target.PSObject.Methods['TryGetMonitor']) {
        try { $monitor=Get-DisplayCoreMonitorRecord $Target.TryGetMonitor() } catch { $monitor=$null }
    }
    return [pscustomobject]@{
        adapter_luid_low=if ($luid) { $luid.low } else { $null }
        adapter_luid_high=if ($luid) { $luid.high } else { $null }
        adapter_luid_hex=if ($luid) { $luid.hex } else { $null }
        # Legacy aliases kept byte-identical for the unversioned output shape
        # consumed by collect_direct_readiness.ps1/readiness.py.
        adapter_id_low=if ($luid) { $luid.low } else { $null }
        adapter_id_high=if ($luid) { $luid.high } else { $null }
        adapter_relative_id=Get-DisplayCoreSafeProperty $Target 'AdapterRelativeId'
        device_interface_path=$interfacePath
        connected=Get-DisplayCoreSafeProperty $Target 'IsConnected'
        stale=Get-DisplayCoreSafeProperty $Target 'IsStale'
        usage_kind=Convert-DisplayCoreEnumText (Get-DisplayCoreSafeProperty $Target 'UsageKind')
        monitor=$monitor
        modes=$null
    }
}

function Get-DisplayCoreModeRecord {
    # Dual-shape DisplayModeInfo extractor (new rational-rate build and older
    # Width/Height/VerticalRefreshRate build). Unknown shape yields nulls with
    # shape_recognized=false; never fabricates a mode.
    param([object]$Mode)
    if ($null -eq $Mode) { return $null }
    $width=$null; $height=$null
    $targetResolution=Get-DisplayCoreSafeProperty $Mode 'TargetResolution'
    $width=Get-DisplayCoreSafeProperty $targetResolution 'Width'
    $height=Get-DisplayCoreSafeProperty $targetResolution 'Height'
    if ($null -eq $width) { $width=Get-DisplayCoreSafeProperty $Mode 'Width' }
    if ($null -eq $height) { $height=Get-DisplayCoreSafeProperty $Mode 'Height' }
    $refreshNumerator=$null; $refreshDenominator=$null
    $presentationRate=Get-DisplayCoreSafeProperty $Mode 'PresentationRate'
    $verticalSyncRate=Get-DisplayCoreSafeProperty $presentationRate 'VerticalSyncRate'
    $refreshNumerator=Get-DisplayCoreSafeProperty $verticalSyncRate 'Numerator'
    $refreshDenominator=Get-DisplayCoreSafeProperty $verticalSyncRate 'Denominator'
    if ($null -eq $refreshNumerator) { $refreshNumerator=Get-DisplayCoreSafeProperty $Mode 'VerticalRefreshRateNumerator' }
    if ($null -eq $refreshDenominator) { $refreshDenominator=Get-DisplayCoreSafeProperty $Mode 'VerticalRefreshRateDenominator' }
    $verticalSyncsPerPresentation=Get-DisplayCoreSafeProperty $presentationRate 'VerticalSyncsPerPresentation'
    $scanoutState=Convert-DisplayCoreEnumText (Get-DisplayCoreSafeProperty $Mode 'ScanoutState')
    return [pscustomobject]@{
        target_width=$width
        target_height=$height
        vertical_refresh_numerator=$refreshNumerator
        vertical_refresh_denominator=$refreshDenominator
        vertical_syncs_per_presentation=$verticalSyncsPerPresentation
        scanout_state=$scanoutState
        is_interlaced=Get-DisplayCoreSafeProperty $Mode 'IsInterlaced'
        shape_recognized=(($null -ne $width -and $null -ne $height) -or ($null -ne $refreshNumerator -and $null -ne $refreshDenominator))
    }
}

function Invoke-DisplayCoreStateReadChain {
    # Strict read-only fallback chain between the two documented atomic READ
    # state readers: TryReadCurrentStateForModeQuery (winrt-26100+), then
    # TryReadCurrentStateForAllTargets. Acquisition/commit APIs are never
    # attempted. One reader fails over to the next when: the API is not
    # exposed, the call throws, ErrorCode is not Success, State is null, or
    # the returned DisplayState is not IsReadOnly=true. Every attempt is
    # recorded so a 'not_queried' result never hides the underlying cause.
    # $Manager is any object exposing the same read-only surface (live
    # DisplayManager or test fake); no GPU/WinRT code lives here.
    param([object]$Manager)
    $attempts=@()
    if ($null -eq $Manager) {
        $attempts+=[pscustomobject]@{ api='(none)'; outcome='no_manager'; detail='DisplayManager object was not provided.' }
        return [pscustomobject]@{ status='no_state'; read_via=$null; state=$null; attempts=$attempts }
    }
    foreach ($api in @('TryReadCurrentStateForModeQuery','TryReadCurrentStateForAllTargets')) {
        if (-not $Manager.PSObject.Methods[$api]) {
            $attempts+=[pscustomobject]@{ api=$api; outcome='unavailable'; detail='Method not exposed by this Windows build.' }
            continue
        }
        try {
            $result=$Manager.$api()
        } catch {
            $attempts+=[pscustomobject]@{ api=$api; outcome='exception'; detail=$_.Exception.Message }
            continue
        }
        $errorCode=Convert-DisplayCoreEnumText (Get-DisplayCoreSafeProperty $result 'ErrorCode')
        $extended=Get-DisplayCoreSafeProperty $result 'ExtendedErrorCode'
        $state=Get-DisplayCoreSafeProperty $result 'State'
        if ($errorCode -ne 'Success') {
            $attempts+=[pscustomobject]@{ api=$api; outcome='error_code'; detail="ErrorCode=$errorCode ExtendedErrorCode=$extended" }
            continue
        }
        if (-not $state) {
            $attempts+=[pscustomobject]@{ api=$api; outcome='null_state'; detail='ErrorCode=Success but State is null.' }
            continue
        }
        if ((Get-DisplayCoreSafeProperty $state 'IsReadOnly') -ne $true) {
            $attempts+=[pscustomobject]@{ api=$api; outcome='not_read_only'; detail='State.IsReadOnly is not true; rejected to keep the probe strictly read-only.' }
            continue
        }
        $attempts+=[pscustomobject]@{ api=$api; outcome='success'; detail='Read-only DisplayState accepted.' }
        return [pscustomobject]@{ status='state_ok'; read_via=$api; state=$state; attempts=$attempts }
    }
    return [pscustomobject]@{ status='no_state'; read_via=$null; state=$null; attempts=$attempts }
}

function Get-DisplayCoreStateReadReason {
    # Compact human-readable trace of the read-only reader chain.
    param([object]$Chain)
    $parts=@($Chain.attempts | ForEach-Object {
        $_.api+'='+$_.outcome+$(if ($_.detail) { ' ['+$_.detail+']' } else { '' })
    })
    return ('Read-only state readers exhausted without an accepted state: '+($parts -join '; ')+
        '. No acquisition API is attempted; re-run or await a separately authorized probe.')
}

function New-DisplayCoreTargetKey {
    # Composite join key adapter LUID + adapter-relative id. Incomplete
    # identities return $null so state/snapshot joins never fall back to a
    # guessed key.
    param([object]$Record)
    if ($null -eq $Record) { return $null }
    $luid=[string](Get-DisplayCoreSafeProperty $Record 'adapter_luid_hex')
    $relative=Get-DisplayCoreSafeProperty $Record 'adapter_relative_id'
    if ([string]::IsNullOrWhiteSpace($luid) -or $null -eq $relative) { return $null }
    return ($luid.ToLowerInvariant()+'|'+[string]$relative)
}

function Get-DisplayCoreSelectionOutcome {
    # Selects from normalized target records by exact monitor device interface
    # path only (OrdinalIgnoreCase, full string, no wildcard, no substring,
    # never by size/first monitor/LUID/connector hint).
    # B9-08: case-insensitive by design here; the native gate
    # (display_inventory.cpp validate_extended_display) is case-sensitive
    # exact (std::string ==). The difference is documented, not unified,
    # pending senior decision.
    param([object[]]$Targets,[string]$RequestedInterfacePath)
    $targets=@($Targets)
    $basis='exact monitor device interface path only; adapter LUID and connector hints are diagnostics, never identity or acquisition authorization'
    if (-not $PSBoundParameters.ContainsKey('RequestedInterfacePath') -or [string]::IsNullOrWhiteSpace($RequestedInterfacePath)) {
        return [pscustomobject]@{
            requested_interface_path=$null
            status='not_requested'
            matched_count=0
            matched_targets=@()
            identity_basis=$basis
            note='No selection attempted. Selection requires -RequestedMonitorInterfacePath with the exact monitor interface path; the probe never auto-picks the first or largest target.'
        }
    }
    $requested=$RequestedInterfacePath.Trim()
    $comparer=[System.StringComparer]::OrdinalIgnoreCase
    $pathMatches=@($targets | Where-Object {
        $path=[string](Get-DisplayCoreSafeProperty $_ 'device_interface_path')
        -not [string]::IsNullOrWhiteSpace($path) -and $comparer.Equals($path,$requested)
    })
    $base=[ordered]@{
        requested_interface_path=$requested
        matched_count=$pathMatches.Count
        matched_targets=$pathMatches
        identity_basis=$basis
    }
    if ($pathMatches.Count -eq 0) {
        $base.status='missing'
        $base.note='No enumerated target exposes this exact monitor interface path. A stale connector hint, adapter LUID or adapter-relative id from an older snapshot is not identity proof.'
        return [pscustomobject]$base
    }
    if ($pathMatches.Count -gt 1) {
        $base.status='ambiguous'
        $base.note='Multiple targets expose the same exact interface path. Manual disambiguation required; the probe never chooses between duplicates.'
        return [pscustomobject]$base
    }
    $target=$pathMatches[0]
    # B9-07: stale must be explicitly boolean false to proceed. The safe
    # getter returns $null for a missing property or a failed getter, so
    # testing only '-eq $true' would treat unknown freshness as non-stale
    # (false diagnostic eligibility). Unknown freshness is never eligible.
    $staleValue=$null
    $staleReadError=$null
    try {
        $staleValue=Get-DisplayCoreSafeProperty $target 'stale'
    } catch {
        $staleReadError=$_.Exception.Message
    }
    if ($staleValue -is [bool]) {
        if ($staleValue -eq $true) {
            $base.status='stale'
            $base.note='Snapshot is stale (IsStale=true): the system display state changed since enumeration. Re-enumerate before judging this result.'
            return [pscustomobject]$base
        }
    } else {
        $base.status='unknown'
        if ($null -ne $staleReadError) {
            $base.note='Snapshot freshness is unknown: reading the stale flag threw ('+$staleReadError+'). The target is never treated as eligible on a guess; re-enumerate before judging this result.'
        } elseif ($null -eq $staleValue) {
            $base.note='Snapshot freshness is unknown: the stale flag is missing or null (IsStale unreadable). The target is never treated as eligible on a guess; re-enumerate before judging this result.'
        } else {
            $typeName='unknown'
            try { $typeName=$staleValue.GetType().Name } catch { $typeName='unknown' }
            $base.note='Snapshot freshness is unknown: the stale flag is not a boolean (type '+$typeName+'). The target is never treated as eligible on a guess; re-enumerate before judging this result.'
        }
        return [pscustomobject]$base
    }
    if ((Get-DisplayCoreSafeProperty $target 'connected') -ne $true) {
        $base.status='disconnected'
        $base.note='Target with this exact interface path is currently not connected (IsConnected=false). A disconnected connector is not an available scanout target.'
        return [pscustomobject]$base
    }
    $usage=Convert-DisplayCoreEnumText (Get-DisplayCoreSafeProperty $target 'usage_kind')
    if ($script:DisplayCoreSpecializedUsageKinds -contains $usage) {
        $base.status='eligible_for_next_probe'
        $base.note='Connected, non-stale, specialized target ('+$usage+') matching the exact interface path. Eligible for a separately authorized acquisition probe. This does NOT certify acquisition success or rendering readiness.'
        return [pscustomobject]$base
    }
    if ($usage -eq 'Standard') {
        $base.status='standard'
        $base.note='Connected, non-stale target is a Standard desktop monitor, not a specialized (HeadMounted/SpecialPurpose) target.'
        return [pscustomobject]$base
    }
    $base.status='unknown'
    $base.note='UsageKind is not one of the documented Standard/SpecialPurpose/HeadMounted values; it is never treated as eligible on a guess.'
    return [pscustomobject]$base
}

function Set-DisplayCoreTargetModes {
    # Attaches per-target mode query results onto records (pure merge, keyed by
    # adapter_luid_hex + adapter_relative_id). Results come from the caller;
    # nothing here queries hardware.
    param([object[]]$Targets,[hashtable]$ModesByTarget)
    $targets=@($Targets)
    if ($null -eq $ModesByTarget) { return $targets }
    $result=@()
    foreach ($target in $targets) {
        $key=New-DisplayCoreTargetKey -Record $target
        if ($key -and $ModesByTarget.ContainsKey($key)) {
            $copy=[ordered]@{}
            foreach ($property in $target.PSObject.Properties) { $copy[$property.Name]=$property.Value }
            $copy.modes=$ModesByTarget[$key]
            $result+=,[pscustomobject]$copy
        } else { $result+=,$target }
    }
    return ,$result
}

function Get-DisplayCoreReport {
    # Top-level JSON envelope. ok=true means the read-only probe completed,
    # NOT that any target is usable for rendering.
    param(
        [object[]]$Targets,
        [object]$Selection,
        [object]$Modes,
        [datetime]$TimestampUtc,
        [string]$ErrorStage,
        [string]$ErrorMessage
    )
    # PS5.1 does not reliably bind Nullable[datetime], so presence is checked.
    $ts=if ($PSBoundParameters.ContainsKey('TimestampUtc')) { $TimestampUtc.ToUniversalTime() } else { (Get-Date).ToUniversalTime() }
    $isError=($PSBoundParameters.ContainsKey('ErrorStage') -and -not [string]::IsNullOrWhiteSpace($ErrorStage))
    $report=[ordered]@{
        schema_version=$script:DisplayCoreSchemaVersion
        timestamp_utc=$ts.ToString("yyyy-MM-ddTHH:mm:ss'Z'")
        read_only=$true
        ok=(-not $isError)
        source='Windows.Devices.Display.Core.DisplayManager'
        manager_options='None'
        actions_not_taken=@('TryAcquireTarget','TryAcquireTargetsAnd*','ReleaseTarget','CreateDisplayDevice','TryApply','Start','Stop','ConnectTarget','DisconnectTarget','ApplyPropertiesFromMode','registry writes','UAC elevation','display ownership changes')
        notes=@('Snapshot only; no target acquired.',
                'Disconnected connector is not an available scanout target.',
                'Adapter LUID, adapter_relative_id and DisplayAdapterTargetId are diagnostic hints only.',
                'eligible_for_next_probe is a read-only snapshot verdict; acquisition and rendering readiness require separately authorized checks.',
                'Mode query chains the two documented read-only state readers; every attempt is recorded in modes.attempts.')
    }
    if ($isError) {
        $report.error=[pscustomobject]@{ stage=$ErrorStage; message=$ErrorMessage }
    } else {
        $targets=@($Targets)
        $report.target_count=$targets.Count
        $report.targets=$targets
        $report.selection=$Selection
        $report.modes=$Modes
    }
    return [pscustomobject]$report
}
