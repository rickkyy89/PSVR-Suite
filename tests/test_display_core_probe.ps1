# SPDX-License-Identifier: MIT
# Offline tests for the read-only Display Core probe: pure synthetic records
# only. No WinRT, no GPU, no registry, no acquisition. The single child
# process run exits at parameter validation before any API type is loaded.
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
. (Join-Path $root 'scripts\display_core_selection.ps1')
function Assert($Value,[string]$Message) { if (-not $Value) { throw $Message } }
function New-FakeMonitor {
    param([string]$DisplayName,[uint32]$TargetId,[string]$UsageKind)
    $monitor=[pscustomobject]@{
        DeviceId='DISPLAY\PSVR0000\fake-instance'
        DisplayName=$DisplayName
        UsageKind=$UsageKind
        DisplayAdapterTargetId=$TargetId
        ConnectionKind='WiredInternal'
        PhysicalConnector='HighDefinitionMultimediaInterface'
        NativeResolutionInRawPixels=[pscustomobject]@{ Width=1920; Height=1080 }
    }
    return $monitor
}
function New-FakeLiveTarget {
    param([uint32]$RelativeId,[string]$InterfacePath,[bool]$Connected,[bool]$Stale,
          [string]$UsageKind,[uint32]$Low=76797,[uint32]$High=0,[object]$Monitor=$null)
    $target=[pscustomobject]@{
        AdapterRelativeId=$RelativeId
        DeviceInterfacePath=$InterfacePath
        IsConnected=$Connected
        IsStale=$Stale
        UsageKind=$UsageKind
        Adapter=[pscustomobject]@{ Id=[pscustomobject]@{ LowPart=$Low; HighPart=$High } }
        MonitorFixture=$Monitor
    }
    # $this-based ScriptMethod mirrors the live TryGetMonitor contract:
    # documented read-only descriptor, null when nothing is connected.
    $target | Add-Member -MemberType ScriptMethod -Name TryGetMonitor -Value { $this.MonitorFixture } -Force
    return $target
}
$lgPath='\\?\DISPLAY#GSM1ED4#4&22ee8e3&0&UID4354#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}'
$sonyPath='\\?\DISPLAY#SNY6A04#4&22ee8e3&0&UID4352#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}'
$hmdPath='\\?\DISPLAY#SNY6A04#4&22ee8e3&0&UID4353#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}'

# --- Target record normalization (live-shaped synthetic objects) ---
$records=@(
    (Get-DisplayCoreTargetRecord -Target (New-FakeLiveTarget -RelativeId 4354 -InterfacePath $lgPath -Connected $true -Stale $false -UsageKind 'Standard' -Monitor (New-FakeMonitor -DisplayName 'LG HDR 4K' -TargetId 4354 -UsageKind 'Standard')))
    (Get-DisplayCoreTargetRecord -Target (New-FakeLiveTarget -RelativeId 4352 -InterfacePath $null -Connected $false -Stale $false -UsageKind 'Standard' -Monitor $null))
)
Assert ($records[0].adapter_luid_hex -eq '0x0000000000012bfd') 'LUID hex mismatch'
Assert ($records[0].adapter_id_low -eq 76797 -and $records[0].adapter_id_high -eq 0) 'Legacy adapter_id alias fields broken (collect_direct_readiness contract)'
Assert ($records[0].adapter_relative_id -eq 4354 -and $records[0].connected -eq $true) 'Record fields lost'
Assert ($records[0].monitor.display_name -eq 'LG HDR 4K') 'Monitor descriptor not mapped'
Assert ($records[0].monitor.display_adapter_target_id -eq 4354) 'Connector hint not mapped'
Assert ($records[0].monitor.native_resolution_raw.width -eq 1920) 'Native resolution not mapped'
Assert ($null -eq $records[1].device_interface_path -and $records[1].connected -eq $false) 'Disconnected record wrong'
Assert ($null -eq $records[1].monitor) 'TryGetMonitor null must stay null'

# --- Selection: exact interface path only ---
$noRequest=Get-DisplayCoreSelectionOutcome -Targets $records
Assert ($noRequest.status -eq 'not_requested' -and $noRequest.matched_count -eq 0) 'Absence of request must not auto-select'
$wrongConnector=Get-DisplayCoreSelectionOutcome -Targets $records -RequestedInterfacePath $sonyPath
Assert ($wrongConnector.status -eq 'missing' -and $wrongConnector.matched_count -eq 0) 'Stale connector hint must not select the LG target'
$unavailable=Get-DisplayCoreSelectionOutcome -Targets $records -RequestedInterfacePath '\\?\DISPLAY#UNKNOWN#0&0&UID9#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}'
Assert ($unavailable.status -eq 'missing') 'Unknown path must be missing'
$caseInsensitive=Get-DisplayCoreSelectionOutcome -Targets $records -RequestedInterfacePath $lgPath.ToUpperInvariant()
Assert ($caseInsensitive.status -eq 'standard' -and $caseInsensitive.matched_count -eq 1) 'Exact path comparison is case-insensitive'
Assert ($caseInsensitive.matched_targets[0].adapter_relative_id -eq 4354) 'Matched target must carry the record'

$staleRecord=Get-DisplayCoreTargetRecord -Target (New-FakeLiveTarget -RelativeId 4353 -InterfacePath $hmdPath -Connected $true -Stale $true -UsageKind 'HeadMounted')
$stale=Get-DisplayCoreSelectionOutcome -Targets @($staleRecord) -RequestedInterfacePath $hmdPath
Assert ($stale.status -eq 'stale') 'Stale snapshot must not be judged eligible'

$disconnectedWithRecord=Get-DisplayCoreTargetRecord -Target (New-FakeLiveTarget -RelativeId 4353 -InterfacePath $hmdPath -Connected $false -Stale $false -UsageKind 'HeadMounted')
$disconnected=Get-DisplayCoreSelectionOutcome -Targets @($disconnectedWithRecord) -RequestedInterfacePath $hmdPath
Assert ($disconnected.status -eq 'disconnected') 'Connected=false with matching path must be disconnected'

foreach ($specialized in @('HeadMounted','SpecialPurpose')) {
    $eligibleTarget=Get-DisplayCoreTargetRecord -Target (New-FakeLiveTarget -RelativeId 4353 -InterfacePath $hmdPath -Connected $true -Stale $false -UsageKind $specialized -Monitor (New-FakeMonitor -DisplayName 'PSVR' -TargetId 4353 -UsageKind $specialized))
    $eligible=Get-DisplayCoreSelectionOutcome -Targets @($eligibleTarget) -RequestedInterfacePath $hmdPath
    Assert ($eligible.status -eq 'eligible_for_next_probe' -and $eligible.matched_count -eq 1) "Specialized usage $specialized must be eligible_for_next_probe"
    Assert ($eligible.note -notmatch '(?i)\bread\b') 'Eligible note must never claim readiness'
}
$unknownUsage=Get-DisplayCoreSelectionOutcome -Targets @(Get-DisplayCoreTargetRecord -Target (New-FakeLiveTarget -RelativeId 4353 -InterfacePath $hmdPath -Connected $true -Stale $false -UsageKind 'SomethingNew')) -RequestedInterfacePath $hmdPath
Assert ($unknownUsage.status -eq 'unknown') 'Undocumented usage kind must never be treated as eligible'

$duplicateA=Get-DisplayCoreTargetRecord -Target (New-FakeLiveTarget -RelativeId 4353 -InterfacePath $hmdPath -Connected $true -Stale $false -UsageKind 'HeadMounted' -Low 76797)
$duplicateB=Get-DisplayCoreTargetRecord -Target (New-FakeLiveTarget -RelativeId 4399 -InterfacePath $hmdPath -Connected $true -Stale $false -UsageKind 'HeadMounted' -Low 76798)
$duplicates=Get-DisplayCoreSelectionOutcome -Targets @($duplicateA,$duplicateB) -RequestedInterfacePath $hmdPath
Assert ($duplicates.status -eq 'ambiguous' -and $duplicates.matched_count -eq 2) 'Duplicate exact paths must be ambiguous, never auto-picked'

# --- Modes: dual-shape extraction, unknown shape stays unknown ---
$newShapeMode=Get-DisplayCoreModeRecord -Mode ([pscustomobject]@{
    TargetResolution=[pscustomobject]@{ Width=1920; Height=1080 }
    PresentationRate=[pscustomobject]@{ VerticalSyncRate=[pscustomobject]@{ Numerator=120; Denominator=1 }; VerticalSyncsPerPresentation=1 }
    IsInterlaced=$false
})
Assert ($newShapeMode.target_width -eq 1920 -and $newShapeMode.target_height -eq 1080) 'New-shape resolution not extracted'
Assert ($newShapeMode.vertical_refresh_numerator -eq 120 -and $newShapeMode.vertical_refresh_denominator -eq 1) 'New-shape rate not extracted'
$oldShapeMode=Get-DisplayCoreModeRecord -Mode ([pscustomobject]@{
    Width=960; Height=1080; VerticalRefreshRateNumerator=60000; VerticalRefreshRateDenominator=1001; ScanoutState='InUse'; IsInterlaced=$false
})
Assert ($oldShapeMode.target_width -eq 960 -and $oldShapeMode.vertical_refresh_numerator -eq 60000) 'Old-shape mode not extracted'
Assert ($oldShapeMode.scanout_state -eq 'InUse') 'Old-shape scanout state passthrough lost'
$unknownShapeMode=Get-DisplayCoreModeRecord -Mode ([pscustomobject]@{ Unexpected='shape' })
Assert ($null -eq $unknownShapeMode.target_width -and -not $unknownShapeMode.shape_recognized) 'Unknown mode shape must be reported as not recognized'

# --- Read-only state reader chain: fallback on failure, never on mutation ---
function New-FakeReadOnlyState { param([bool]$ReadOnly=$true) [pscustomobject]@{ IsReadOnly=$ReadOnly } }
function New-FakeReadResult { param([string]$Code='Success',[object]$State) [pscustomobject]@{ ErrorCode=$Code; ExtendedErrorCode=0; State=$State } }
function New-FakeManager {
    param([object]$ModeQuery=$null,[object]$AllTargets=$null,[switch]$ModeQueryThrows,[switch]$AllTargetsThrows)
    # Results ride as note properties and are returned via $this: ScriptMethod
    # blocks resolve locals dynamically and would lose function-scope values.
    $manager=[pscustomobject]@{ ModeQueryResult=$ModeQuery; AllTargetsResult=$AllTargets }
    if ($ModeQueryThrows) {
        $manager | Add-Member -MemberType ScriptMethod -Name TryReadCurrentStateForModeQuery -Value { throw 'synthetic mode-query failure' }
    } elseif ($null -ne $ModeQuery) {
        $manager | Add-Member -MemberType ScriptMethod -Name TryReadCurrentStateForModeQuery -Value { $this.ModeQueryResult }
    }
    if ($AllTargetsThrows) {
        $manager | Add-Member -MemberType ScriptMethod -Name TryReadCurrentStateForAllTargets -Value { throw 'synthetic all-targets failure' }
    } elseif ($null -ne $AllTargets) {
        $manager | Add-Member -MemberType ScriptMethod -Name TryReadCurrentStateForAllTargets -Value { $this.AllTargetsResult }
    }
    return $manager
}
$emptyChain=Invoke-DisplayCoreStateReadChain -Manager (New-FakeManager)
Assert ($emptyChain.status -eq 'no_state' -and $null -eq $emptyChain.state) 'Manager without readers must yield no_state'
Assert (@($emptyChain.attempts).Count -eq 2 -and @($emptyChain.attempts)[0].outcome -eq 'unavailable' -and @($emptyChain.attempts)[1].outcome -eq 'unavailable') 'Both readers must be recorded unavailable'
$allTargetsChain=Invoke-DisplayCoreStateReadChain -Manager (New-FakeManager -AllTargets (New-FakeReadResult -State (New-FakeReadOnlyState)))
Assert ($allTargetsChain.status -eq 'state_ok' -and $allTargetsChain.read_via -eq 'TryReadCurrentStateForAllTargets') 'AllTargets reader must be accepted'
$failedPrimaryChain=Invoke-DisplayCoreStateReadChain -Manager (New-FakeManager -ModeQuery (New-FakeReadResult -Code 'AccessDenied' -State $null) -AllTargets (New-FakeReadResult -State (New-FakeReadOnlyState)))
Assert ($failedPrimaryChain.status -eq 'state_ok' -and $failedPrimaryChain.read_via -eq 'TryReadCurrentStateForAllTargets') 'Failing mode-query read must fall back to all-targets read'
Assert (@($failedPrimaryChain.attempts)[0].outcome -eq 'error_code' -and @($failedPrimaryChain.attempts)[0].detail -match 'AccessDenied') 'Failed primary attempt cause must be preserved'
Assert (@($failedPrimaryChain.attempts)[1].outcome -eq 'success') 'Successful fallback attempt missing'
$throwingChain=Invoke-DisplayCoreStateReadChain -Manager (New-FakeManager -ModeQueryThrows -AllTargets (New-FakeReadResult -State (New-FakeReadOnlyState)))
Assert ($throwingChain.status -eq 'state_ok' -and @($throwingChain.attempts)[0].outcome -eq 'exception') 'Thrown primary read must fall back and record the exception'
$nullStateChain=Invoke-DisplayCoreStateReadChain -Manager (New-FakeManager -ModeQuery (New-FakeReadResult -State $null) -AllTargets (New-FakeReadResult -State (New-FakeReadOnlyState)))
Assert ($nullStateChain.status -eq 'state_ok' -and @($nullStateChain.attempts)[0].outcome -eq 'null_state') 'Success with null State must fail over, not crash'
$mutablePrimaryChain=Invoke-DisplayCoreStateReadChain -Manager (New-FakeManager -ModeQuery (New-FakeReadResult -State (New-FakeReadOnlyState $false)) -AllTargets (New-FakeReadResult -State (New-FakeReadOnlyState)))
Assert ($mutablePrimaryChain.status -eq 'state_ok' -and $mutablePrimaryChain.read_via -eq 'TryReadCurrentStateForAllTargets' -and @($mutablePrimaryChain.attempts)[0].outcome -eq 'not_read_only') 'Non-read-only state from first reader must fall back, guard kept'
$bothMutableChain=Invoke-DisplayCoreStateReadChain -Manager (New-FakeManager -ModeQuery (New-FakeReadResult -State (New-FakeReadOnlyState $false)) -AllTargets (New-FakeReadResult -State (New-FakeReadOnlyState $false)))
Assert ($bothMutableChain.status -eq 'no_state' -and $null -eq $bothMutableChain.state) 'If all states are not read-only the chain must refuse: not_queried, never FindModes'
$bothFailChain=Invoke-DisplayCoreStateReadChain -Manager (New-FakeManager -ModeQueryThrows -AllTargets (New-FakeReadResult -Code 'DisplayUnavailable' -State $null))
Assert ($bothFailChain.status -eq 'no_state' -and @($bothFailChain.attempts)[1].detail -match 'DisplayUnavailable') 'Exhausted chain must preserve every cause'
$reason=Get-DisplayCoreStateReadReason -Chain $bothFailChain
Assert ($reason -match 'TryReadCurrentStateForModeQuery=exception' -and $reason -match 'TryReadCurrentStateForAllTargets=error_code' -and $reason -match 'No acquisition API') 'Reason trace incomplete'
$noManagerChain=Invoke-DisplayCoreStateReadChain -Manager $null
Assert ($noManagerChain.status -eq 'no_state' -and @($noManagerChain.attempts)[0].outcome -eq 'no_manager') 'Null manager must fail closed with structured attempt'

# --- Strict LUID+ARID join key: incomplete identities never guessed ---
$key=New-DisplayCoreTargetKey -Record $records[0]
Assert ($key -eq '0x0000000000012bfd|4354') 'Composite join key wrong'
Assert ($null -eq (New-DisplayCoreTargetKey -Record ([pscustomobject]@{ adapter_luid_hex=$null; adapter_relative_id=4354 }))) 'Missing LUID must not produce a key'
Assert ($null -eq (New-DisplayCoreTargetKey -Record ([pscustomobject]@{ adapter_luid_hex='0x0000000000012bfd'; adapter_relative_id=$null }))) 'Missing adapter-relative id must not produce a key'

# --- Per-target mode merge (pure) ---
$merged=Set-DisplayCoreTargetModes -Targets $records -ModesByTarget @{
    ('0x0000000000012bfd|4354')=[pscustomobject]@{ status='queried'; mode_count=1; modes=@($newShapeMode); truncated=$false }
}
Assert ($merged[0].modes.status -eq 'queried' -and $merged[0].modes.modes[0].target_width -eq 1920) 'Mode merge lost data'
Assert ($merged[0].monitor.display_name -eq 'LG HDR 4K' -and $merged[0].connected -eq $true) 'Merge dropped original fields'
Assert ($null -eq $merged[1].modes -or $merged[1].modes.Count -eq 0) 'Unkeyed target must not receive modes'

# --- Report envelope ---
$selection=Get-DisplayCoreSelectionOutcome -Targets $records -RequestedInterfacePath $lgPath
$modesSummary=[pscustomobject]@{ status='not_queried'; reason='Mode query not requested.'; read_via=$null; per_target=@{} }
$stamp=[datetime]::SpecifyKind([datetime]'2026-10-05T12:34:56',[System.DateTimeKind]::Utc)
$report=Get-DisplayCoreReport -Targets $records -Selection $selection -Modes $modesSummary -TimestampUtc $stamp
$json=$report | ConvertTo-Json -Depth 10
$roundtrip=$json | ConvertFrom-Json
Assert ($roundtrip.schema_version -eq 1) 'schema_version missing'
Assert ($roundtrip.timestamp_utc -eq '2026-10-05T12:34:56Z') 'timestamp_utc must be ISO-8601 UTC'
Assert ($roundtrip.read_only -eq $true -and $roundtrip.ok -eq $true) 'read_only/ok envelope broken'
Assert ($roundtrip.target_count -eq 2 -and $roundtrip.targets.Count -eq 2) 'target inventory broken'
Assert ($roundtrip.selection.status -eq 'standard') 'selection outcome not embedded'
Assert ($roundtrip.modes.status -eq 'not_queried') 'modes non-claim not embedded'
Assert (@($roundtrip.notes | Where-Object { $_ -like '*modes.attempts*' }).Count -eq 1) 'Reader-chain note missing from envelope'
$allowed=@('not_requested','missing','disconnected','stale','standard','ambiguous','eligible_for_next_probe','unknown')
foreach ($outcome in @($noRequest,$wrongConnector,$unavailable,$caseInsensitive,$stale,$disconnected,$duplicates,$unknownUsage,$selection)) {
    Assert ($allowed -contains $outcome.status) "status outside documented vocabulary: $($outcome.status)"
    Assert ($outcome.note -notmatch '(?i)"?ready"?') 'No outcome may declare ready-for-rendering'
}
Assert ($json -notmatch 'TryAcquireTarget[^,]*":\s*true') 'Envelope must never report acquisition'
$errorReport=Get-DisplayCoreReport -ErrorStage 'enumerate' -ErrorMessage 'synthetic failure'
Assert ($errorReport.ok -eq $false -and $errorReport.error.message -eq 'synthetic failure' -and -not $errorReport.PSObject.Properties['targets']) 'Error envelope shape broken'

# --- Static guard: AST of the shipped scripts proves no mutation API ---
function Get-AstCallNames([string]$Path) {
    $parseErrors=$null
    $ast=[System.Management.Automation.Language.Parser]::ParseFile($Path,[ref]$null,[ref]$parseErrors)
    Assert ($parseErrors.Count -eq 0) ("Parse errors in "+$Path)
    # Windows PowerShell 5.1 names these InvokeMemberExpressionAst/CommandAst;
    # PowerShell 7 renamed them to *InvocationExpressionAst. Match both.
    $methodNames=@('InvokeMemberExpressionAst','MemberInvocationExpressionAst')
    $commandNames=@('CommandAst','CommandInvocationExpressionAst')
    $nodes=$ast.FindAll({ param($n)
        $methodNames -contains $n.GetType().Name -or $commandNames -contains $n.GetType().Name },$true)
    $names=@()
    foreach ($node in $nodes) {
        if ($methodNames -contains $node.GetType().Name) {
            if ($node.PSObject.Properties['MemberName'] -and $node.MemberName) { $names+=[string]$node.MemberName }
            elseif ($node.PSObject.Properties['Member']) { $names+=[string]$node.Member }
        } elseif ($node.PSObject.Properties['CommandText'] -and $node.CommandText) {
            $names+=[string]$node.CommandText
        } elseif ($node.PSObject.Properties['CommandElements'] -and @($node.CommandElements).Count -gt 0) {
            $names+=[string]$node.CommandElements[0]
        }
    }
    return ,$names
}
foreach ($scriptName in @('display_core_selection.ps1','inspect_windows_display_core.ps1')) {
    $names=Get-AstCallNames (Join-Path $root ('scripts\'+$scriptName))
    # Negative control for the guard itself: injected violations must be seen.
    $trapPath=Join-Path ([IO.Path]::GetTempPath()) ('psvr-ast-trap-'+[Guid]::NewGuid().ToString('N')+'.ps1')
    try {
        [IO.File]::WriteAllText($trapPath,'$m.TryAcquireTarget($t)'+"`r`n"+'Set-ItemProperty -Path x')
        $trapNames=Get-AstCallNames $trapPath
        Assert ('TryAcquireTarget' -in $trapNames) 'AST guard cannot see injected method violation'
        Assert ('Set-ItemProperty' -in $trapNames) 'AST guard cannot see injected cmdlet violation'
    } finally { Remove-Item -LiteralPath $trapPath -Force -ErrorAction SilentlyContinue }
    # Positive control: the walker must see known benign calls before trusting
    # the absence of forbidden ones (guards against silent empty scans).
    $expected=if ($scriptName -eq 'display_core_selection.ps1') { 'Get-DisplayCoreSafeProperty' } else { 'GetCurrentTargets' }
    Assert ($expected -in $names) ('AST walker missed expected call '+$expected+' in '+$scriptName)
    Assert ($names.Count -gt 5) ('AST scan found too few calls in '+$scriptName+': likely broken walker')
    $forbidden=$names | Where-Object {
        ($_ -match '^(TryAcquire|TryApply|ReleaseTarget|CreateDisplayDevice|ConnectTarget|DisconnectTarget|ApplyPropertiesFromMode|Start|Stop|Register|Unregister)') -or
        ($_ -match '^(Set|New|Remove|Clear|Add)-' -and $_ -notmatch 'DisplayCore') -or
        ($_ -in @('Invoke-Expression','Invoke-Command','Start-Process','reg','reg.exe'))
    }
    Assert (@($forbidden).Count -eq 0) ('Forbidden mutating calls in '+$scriptName+': '+($forbidden -join ', '))
}
# --- Child process: parameter validation fails closed with exit 3, before WinRT ---
$before=[IO.File]::ReadAllBytes((Join-Path $root 'scripts\inspect_windows_display_core.ps1')).Length
$ErrorActionPreference='Continue'
$childOutput=& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'scripts\inspect_windows_display_core.ps1') -RequestedMonitorInterfacePath '   ' 2>&1
$ErrorActionPreference='Stop'
Assert ($LASTEXITCODE -eq 3) "Empty selection path must exit 3, got $LASTEXITCODE"
$childJson=$childOutput | Out-String | ConvertFrom-Json
Assert ($childJson.ok -eq $false -and $childJson.read_only -eq $true -and $childJson.error.stage -eq 'parameters') 'Error JSON not structured'
Assert ($childJson.PSObject.Properties['targets'] -eq $null -or @($childJson.targets).Count -eq 0) 'Failed run must not emit partial inventory'
Assert (([IO.File]::ReadAllBytes((Join-Path $root 'scripts\inspect_windows_display_core.ps1')).Length) -eq $before) 'Test must not modify the probe script'

'PASS display-core read-only probe: records, exact-path selection (missing/disconnected/stale/standard/ambiguous/eligible/unknown/not-requested), dual-shape modes incl. unknown, reader-chain fallback/error recording with read-only guard, strict LUID join keys, merge, envelope vocabulary, AST no-mutation, fail-closed exit 3; no GPU or WinRT access'
