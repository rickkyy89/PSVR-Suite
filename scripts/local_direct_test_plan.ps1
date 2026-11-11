# SPDX-License-Identifier: MIT
# Pure local planner: state classification, runtime-backup scope checks and
# command-list construction for the deferred SNY6A04 direct-mode test.
# No registry, process, GPU, UAC or device calls here. The planner script does
# file IO and hashing; the operator later runs generated commands. When executed
# with -ShowPlanJournal this file also performs a whitelisted, read-only plan
# journal print (Get-Content of the plan file only; no EDID bytes emitted).
param(
    [switch]$ShowPlanJournal,
    [string]$JournalPlanPath=''
)
function Get-DirectTestQuotedPath {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { throw 'Generated commands need a concrete path' }
    return "'"+($Path -replace "'","''")+"'"
}
function Assert-DirectTestRuntimeBackup {
    param($Backup,[string]$ExpectedSettingsPath='')
    if ($Backup.schema_version -ne 1) { throw 'Runtime backup schema mismatch' }
    $settings=[string]$Backup.settings_path
    if ([string]::IsNullOrWhiteSpace($settings) -or -not [IO.Path]::IsPathRooted($settings)) { throw 'Runtime backup settings_path must be an absolute path' }
    if ($ExpectedSettingsPath -and $settings -cne $ExpectedSettingsPath) { throw 'Runtime backup targets a different SteamVR settings file than requested' }
    $changes=@($Backup.changes)
    if (-not $changes.Count) { throw 'Runtime backup records no experiment changes' }
    $seen=@{}; $sawVendor=$false; $sawProduct=$false
    foreach ($item in $changes) {
        $full=$item.section+'.'+$item.key
        if ($seen.ContainsKey($full)) { throw "Runtime backup contains a duplicate change entry for $full" }
        $seen[$full]=$true
        if ($item.section -eq 'driver_psvrpc') {
            if ($item.key -notin @('nativeDirectMode','edidVendorId','edidProductId')) { throw "Runtime backup would restore unscoped key $full; whole Steam settings restore is not allowed" }
        } elseif ($item.section -eq 'direct_mode') {
            if ($item.key -notmatch '^(enable|count|edidVid\d+|edidPid\d+)$') { throw "Runtime backup would restore unscoped key $full; whole Steam settings restore is not allowed" }
        } else { throw "Runtime backup touches unknown section '$($item.section)'; only driver_psvrpc/direct_mode experiment keys are restorable" }
        if ($item.key -match '^edid(VendorId|Vid\d+)$') {
            $sawVendor=$true
            if ($null -eq $item.written_value -or [int]$item.written_value -ne 0xd94d) { throw 'Runtime backup sets an EDID vendor identity other than the measured Sony SNY6A04' }
        }
        if ($item.key -match '^edid(ProductId|Pid\d+)$') {
            $sawProduct=$true
            if ($null -eq $item.written_value -or [int]$item.written_value -ne 0x6a04) { throw 'Runtime backup sets an EDID product identity other than the measured Sony SNY6A04' }
        }
    }
    if (-not $sawVendor -or -not $sawProduct) { throw 'Runtime backup does not record the SNY6A04 EDID pair the experiment enables' }
    return [pscustomobject]@{ settings_path=$settings; change_count=$changes.Count }
}
function Get-DirectTestLocalState {
    param($Plan)
    $compatProp=$Plan.PSObject.Properties['compat_non_microsoft_bit']
    $compat=[bool]($compatProp -and $compatProp.Value)
    $pendingProp=$Plan.PSObject.Properties['pending_operation']
    $pending=if ($pendingProp) { [string]$pendingProp.Value } else { '' }
    if ($pending -and $pending -notin @('Apply','Restore')) { throw 'Unknown pending operation recorded in the HMD plan' }
    $applied=[bool]$Plan.applied
    $name=if ($pending) { 'PendingConflict' } elseif ($applied -and $compat) { 'CompatApplied' } elseif ($applied) { 'NormalHmdApplied' } else { 'BaselineNotVerified' }
    $restoreReady=($applied -and -not $pending)
    $means=if ($pending) { 'restore_ready is false while the journal shows an interrupted operation. The restore phase remains the recovery path but only through a conscious operator decision. It never means a restore was performed.' } elseif ($applied) { 'Preconditions for running the administrator HMD restore are ready (SteamVR closed first, plan validated). It never means a restore was performed; only operator-observed evidence in the verify phase can establish that.' } else { 'restore_ready is false: the plan file itself reports neither an applied override nor a journaled operation. That is an unverified baseline label recorded from the journal, not observed evidence. It never means a restore was performed.' }
    return [pscustomobject]@{
        name=$name
        instance_id=[string]$Plan.instance_id
        hmd_override_applied=$applied
        compat_variant=$compat
        pending_operation=$(if ($pending) { $pending } else { $null })
        journal_clean=($pending -eq '')
        restore_ready=$restoreReady
        recorded_source='plan journal file only'
        restore_ready_means=$means
    }
}
function Get-DirectTestPlanJournal {
    param($Plan)
    $compatProp=$Plan.PSObject.Properties['compat_non_microsoft_bit']
    $pendingProp=$Plan.PSObject.Properties['pending_operation']
    $pending=if ($pendingProp) { [string]$pendingProp.Value } else { '' }
    $applied=[bool]$Plan.applied
    return [pscustomobject]@{
        read_only=$true
        instance_id=[string]$Plan.instance_id
        applied=$applied
        pending_operation=$(if ($pending) { $pending } else { $null })
        compat_non_microsoft_bit=[bool]($compatProp -and $compatProp.Value)
        journal_already_restored=$(if (-not $applied -and -not $pending) { $true } else { $false })
        restore_gate_still_applicable=$(if ($applied -or $pending -eq 'Apply') { $true } else { $false })
        note='Whitelisted plan journal metadata read from the plan file only: no EDID bytes, no registry access, no hardware observation.'
    }
}
function New-DirectTestProcedure {
    param(
        [string]$RepositoryRoot,[string]$ScriptsDirectory,[string]$SteamPath,
        [string]$HmdPlanPath,[string]$HmdPlanSha256,[string]$HmdMonitorPath,
        [string]$RuntimeBackupPath,[string]$RuntimeBackupSha256,
        [string]$RuntimeSettingsPath,[string]$OutputDirectory,[pscustomobject]$State,
        [string]$GeneratedUtc,[string[]]$Warnings
    )
    function Script-Path([string]$Name) { return Get-DirectTestQuotedPath (Join-Path $ScriptsDirectory $Name) }
    $compatPlanPath=Join-Path $OutputDirectory 'hmd_edid_compat_plan.json'
    $capturePath=Join-Path $OutputDirectory 'transition_capture.jsonl'
    $planQ=Get-DirectTestQuotedPath $HmdPlanPath
    $backupQ=Get-DirectTestQuotedPath $RuntimeBackupPath
    $settingsQ=Get-DirectTestQuotedPath $RuntimeSettingsPath
    $compatQ=Get-DirectTestQuotedPath $compatPlanPath
    $captureQ=Get-DirectTestQuotedPath $capturePath
    $steamQ=Get-DirectTestQuotedPath $SteamPath
    $monitorQ=Get-DirectTestQuotedPath $HmdMonitorPath
    $journalPlanCmd="& $(Script-Path 'local_direct_test_plan.ps1') -ShowPlanJournal -JournalPlanPath $planQ"
    $journalCompatCmd="& $(Script-Path 'local_direct_test_plan.ps1') -ShowPlanJournal -JournalPlanPath $compatQ"
    $inspectPlanCmd="& $(Script-Path 'inspect_hmd_edid_plan.ps1') -PlanPath $planQ"
    $coreCmd="& $(Script-Path 'inspect_windows_display_core.ps1') -RequestedMonitorInterfacePath $monitorQ"
    $nvCmd="& $(Script-Path 'inspect_nvidia_displays.ps1') -IncludeDirectEnumeration"
    # Desktop/active evidence comes from the repo's read-only display inventory
    # (same tool and monitor_device_path comparison native_direct_mode.ps1 uses).
    # Exact identity contract: -ceq ordinal equality plus the inspector's own
    # -RequestedMonitorInterfacePath exact selection. Never -like, because the
    # '?' in '\\?\DISPLAY...' is a wildcard. No fabricated parameters.
    $displayToolExe=Join-Path $RepositoryRoot 'out\display\psvr_display.exe'
    $displayToolQ=Get-DirectTestQuotedPath $displayToolExe
    $displayExactCmd="`$d = & $displayToolQ --json | ConvertFrom-Json; @(`$d.displays | Where-Object { `$_.monitor_device_path -ceq $monitorQ })"
    $modesCmd="& $(Script-Path 'inspect_windows_display_core.ps1') -QueryModes"
    function Step-obj([string]$Id,[string]$Kind,[string]$Elevation,[string]$Title,[string[]]$Commands,[string]$ApplyIf,[string[]]$Preconditions,[string]$Expect,[string[]]$DoNotInfer,[string]$OperatorLog) {
        return [ordered]@{ id=$Id; kind=$Kind; elevation=$Elevation; title=$Title; commands=@($Commands); apply_if=$ApplyIf
            preconditions=@($Preconditions); expect=$Expect; do_not_infer=@($DoNotInfer); operator_log=$OperatorLog }
    }
    $phases=@(
        [ordered]@{ sequence=10; id='10_inspect_readonly'; title='Read-only inspection of plans, runtime settings and display paths'
            requires=@('none')
            notes=@('The journal read (10.1) is the positive recorded-state evidence for every state name. The restore-only gate (10.2) legitimately refuses BaselineNotVerified/PendingConflict plans.')
            steps=@(
                (Step-obj '10.1' 'read-only' 'none' 'Snapshot the HMD plan journal (whitelisted metadata, no EDID bytes)' @($journalPlanCmd) 'always' @() 'applied/pending_operation/compat flags exactly as current_state reports; note confirms no registry access' @('the journal is a file record, not live hardware state') ''),
                (Step-obj '10.2' 'read-only' 'none' 'Check the restore gate for the recorded applied/pending state' @($inspectPlanCmd) 'meaningful while the plan reports an applied override or a recoverable pending operation' @() 'restore_gate_accepted plus what the restore would delete or rewrite' @('a conflict message here is the expected outcome for an already-restored or pending journal; it is gate semantics, not a new failure','exit code 0 alone never proves a restore or direct scanout') ''),
                (Step-obj '10.3' 'read-only' 'none' 'Inspect the selective direct-mode runtime settings' @("& $(Script-Path 'native_direct_mode.ps1') -Action Inspect -SettingsPath $settingsQ") 'always' @() 'shows whether driver_psvrpc.nativeDirectMode and the direct_mode section are currently enabled' @('the backup file alone never proves the runtime is enabled or restored; this Inspect is authoritative') ''),
                (Step-obj '10.4' 'read-only' 'none' 'Read-only DisplayCore check with exact HMD interface-path selection' @($coreCmd) 'always' @() 'the inspector reports its selection outcome for the exact -RequestedMonitorInterfacePath plus connected/stale/usage context' @('a disconnected connector is not an available scanout target','a specialized connected match is reported as eligible_for_next_probe, never as rendering-ready') ''),
                (Step-obj '10.5' 'read-only' 'none' 'Snapshot public NVIDIA display/EDID state with optional private direct lists' @($nvCmd) 'always' @() 'GPU-reported EDID identity plus direct enumeration counts' @('os_visible means exposed to the OS, not part of the desktop') '')
            ) },
        [ordered]@{ sequence=20; id='20_steamvr_manual_close'; title='SteamVR must be closed manually by the operator'
            requires=@('10_inspect_readonly')
            steps=@(
                (Step-obj '20.1' 'manual' 'none' 'Close SteamVR normally (UI quit or vrmonitor://quit)' @() 'always' @() 'no vrserver, vrcompositor or vrmonitor process remains' @('never kill processes from a script; the restore scripts themselves refuse to continue while SteamVR runs') 'time and method of the manual close.'),
                (Step-obj '20.2' 'read-only' 'none' 'Confirm SteamVR is really closed before any restore' @('@(Get-Process -Name vrserver,vrcompositor,vrmonitor -ErrorAction SilentlyContinue).Id') 'always' @('20.1 recorded') 'empty id list' @() '')
            ) },
        [ordered]@{ sequence=30; id='30_hmd_admin_restore'; title='Administrator restore of the exact SNY6A04 EDID override'
            requires=@('20_steamvr_manual_close','current_state.restore_ready is true, or an explicit operator decision to use restore as interrupted-operation recovery')
            steps=@(
                (Step-obj '30.1' 'elevated' 'administrator PowerShell' 'Restore the original HMD override journal' @("& $(Script-Path 'hmd_edid_override.ps1') -Action Restore -PlanPath $planQ") 'run while the original plan reports applied=true or a journaled interrupted operation; otherwise the guard is not applicable to this device' @('SteamVR closed','planner snapshot hashes still match') 'EDID_OVERRIDE value 1 removed/reverted per journal on the one instance only' @('"restore_ready" only lists preconditions; it never means this restore already happened','the script restarts the monitor device node internally, never a GPU adapter or global display restart') 'exit status plus observed device state.'),
                (Step-obj '30.2' 'read-only' 'none' 'Read the plan journal as positive restore-completion evidence' @($journalPlanCmd) 'always' @('30.1 recorded') 'applied=false with pending_operation=null means the journal completed the restore; restore_gate_still_applicable=false is the expected follow-up state' @('the restore-only gate in inspect_hmd_edid_plan.ps1 now intentionally conflicts for this plan; that refusal is the guard no longer applying, not a failure','journal state is file evidence; physical desktop evidence comes from phase 40') '')
            ) },
        [ordered]@{ sequence=40; id='40_verify_restore_before_compat'; title='Verify Windows/NVIDIA/DisplayCore after the restore, before any compat trial'
            requires=@('30_hmd_admin_restore')
            steps=@(
                (Step-obj '40.1' 'read-only' 'none' 'Actual desktop evidence: repo display inventory row for the exact monitor_device_path' @($displayExactCmd) 'always' @() 'one row whose monitor_device_path matches the plan ordinally, showing its desktop-active state (active/source/target/refresh/edid_ids)' @('WmiMonitorID or PnP monitor enumeration alone is not proof: it lists devices including hidden ones; only this inventory row carries desktop/active state','-ceq ordinal equality is the identity contract here; -like would silently wildcard the ? in the monitor path') ''),
                (Step-obj '40.2' 'read-only' 'none' 'NVIDIA snapshot after restore' @($nvCmd) 'always' @() 'EDID identity still 0xD94D/0x6A04; record whether the private direct lists are still empty' @() ''),
                (Step-obj '40.3' 'read-only' 'none' 'DisplayCore exact-path check after restore' @($coreCmd) 'always' @() 'selection outcome for the exact monitor interface path, read without any acquisition' @('the connected flag here is DisplayCore state reporting, not desktop proof; 40.1 carries desktop/active evidence') ''),
                (Step-obj '40.4' 'read-only' 'none' 'Optional DisplayCore mode query via the same read-only inspector' @($modesCmd) 'optional: run only if the operator wants mode detail; never as desktop proof' @() 'mode records per connected target from the documented read-only state calls, or an explicit not_queried/query_failed reason' @('mode data comes from a driver query on a read-only state, not from PnP/WMI enumeration and not proof of an active desktop mode','slow or empty results are query outcomes, not hardware judgements') ''),
                (Step-obj '40.5' 'manual' 'none' 'Windows desktop confirmation' @() 'always' @() 'SNY6A04 visible in Windows display settings at its UID/bounds and 120Hz candidate, matching the historical working state and 40.1' @('reappearance must be verified, never presumed from a device-restart exit code') 'date/UTC, what was seen, and the explicit decision to unblock 50_compat_trial. Until this is recorded, phase 50 stays blocked.')
            ) },
        [ordered]@{ sequence=50; id='50_compat_trial_after_verified_restore'; title='Single-variable compat trial via elevated wrapper, only after the restore is verified'
            requires=@('40_verify_restore_before_compat','20_steamvr_manual_close','journal clean for the original plan at run time')
            notes=@('The convenience script try_hmd_edid_compat.ps1 restores and applies atomically and assumes the previous plan is still applied=true; that contradicts this verify-in-between sequence, so it is NOT emitted as a workflow step here. It may still be run manually in a separate, explicitly chosen session.')
            steps=@(
                (Step-obj '50.1' 'elevated' 'administrator PowerShell' 'Prepare the compat plan inside the new output directory' @("& $(Script-Path 'hmd_edid_override.ps1') -Action Prepare -PlanPath $compatQ -CompatNonMicrosoftBit") 'compat plan path must not already exist' @('SteamVR closed','40.5 operator evidence recorded') 'new plan file with compat_non_microsoft_bit=true; original plan untouched by this command' @('Prepare reads registry/device properties only; the plan is not active until Apply') ''),
                (Step-obj '50.2' 'elevated' 'administrator PowerShell' 'Apply the compat plan (value 1 only, bit5 usage 0x27)' @("& $(Script-Path 'hmd_edid_override.ps1') -Action Apply -PlanPath $compatQ") 'always' @('50.1 succeeded') 'override installed for the exact instance; identity/timings unchanged' @('installation of the override is not proof of any GPU behaviour') '')
            ) },
        [ordered]@{ sequence=60; id='60_steamvr_manual_start'; title='Explicit manual SteamVR start request (captured, never automated)'
            requires=@('50_compat_trial_after_verified_restore')
            steps=@(
                (Step-obj '60.1' 'manual' 'none' 'Operator explicitly launches SteamVR' @() 'always' @() 'SteamVR started by a deliberate human action after the compat override is applied' @('this procedure contains no automated start: -StartSteamVR and Start-Process are forbidden commands') 'name, UTC time, reason for the explicit start request. Keep this line with procedure.md as the capture of the manual request.')
            ) },
        [ordered]@{ sequence=70; id='70_observe_visual_and_real_hz'; title='Observation: image in the lenses and the real scanout refresh'
            requires=@('60_steamvr_manual_start')
            steps=@(
                (Step-obj '70.1' 'read-only' 'none' 'Capture Windows/NVIDIA/SteamVR evidence while the runtime runs' @("& $(Script-Path 'capture_direct_mode_transition.ps1') -DurationSeconds 60 -IntervalSeconds 2 -OutputPath $captureQ") 'write each trial to a brand-new capture filename; never rename, overwrite or delete existing evidence files' @() 'JSONL records of desktop presence, NVAPI flags/direct lists, processes and log evidence' @('a log success line such as Successfully set display visibility is not proof of direct acquisition') ''),
                (Step-obj '70.2' 'read-only' 'none' 'Tail the SteamVR logs for driver/compositor errors' @("& $(Script-Path 'inspect_steamvr_logs.ps1') -SteamPath $steamQ") 'always' @() 'matching vrserver/vrcompositor lines including compositor error codes' @() ''),
                (Step-obj '70.3' 'manual' 'none' 'Visual test through the lenses' @() 'always' @() 'image present, correct geometry, no desktop mirroring artifacts' @('the monitor being hidden from the desktop is not proof of a working direct image') 'what was actually seen.'),
                (Step-obj '70.4' 'manual' 'none' 'Real refresh measurement' @() 'always' @() 'actual scanout Hz measured from the capture/runtime summary (for example Updated HMD Prop_DisplayFrequency), distinct from the Windows desktop mode' @('previous desktop 120Hz does not demonstrate 120Hz in Direct Mode') 'measured Hz and source line.')
            ) },
        [ordered]@{ sequence=80; id='80_rollback_to_baseline'; title='Rollback: HMD override first, then selective runtime restore, only while SteamVR is closed'
            requires=@('20_steamvr_manual_close','70_observe_visual_and_real_hz or an operator decision to stop')
            steps=@(
                (Step-obj '80.1' 'elevated' 'administrator PowerShell' 'Restore the compat plan if it is the currently applied override' @("& $(Script-Path 'hmd_edid_override.ps1') -Action Restore -PlanPath $compatQ") 'only if the compat plan exists and reports applied=true' @('SteamVR closed') 'value 1 removed per its journal (old_override_1 was null), returning to the baseline override state' @() ''),
                (Step-obj '80.2' 'elevated' 'administrator PowerShell' 'Restore the original plan if it is still applied' @("& $(Script-Path 'hmd_edid_override.ps1') -Action Restore -PlanPath $planQ") 'only if the original plan still reports applied=true (compat path skipped and phase 30 not completed)' @('SteamVR closed') 'same single-value revert for the exact instance' @() ''),
                (Step-obj '80.3' 'standard' 'none' 'Selective runtime restore from the recorded backup' @("& $(Script-Path 'native_direct_mode.ps1') -Action Restore -SettingsPath $settingsQ -BackupPath $backupQ") 'run when phase 10.3 showed the experiment enabled and the operator wants baseline back' @('SteamVR closed') 'only the journaled driver_psvrpc/direct_mode keys revert; unrelated settings stay untouched' @('never replace steamvr.vrsettings with a full-file copy') 'restore result.'),
                (Step-obj '80.4' 'read-only' 'none' 'Final journal and desktop verification of the baseline' @($journalPlanCmd,$journalCompatCmd,$displayExactCmd) 'journal-read every plan this session touched; the compat journal read is skipped when no compat plan was created' @('80.1-80.3 recorded as applicable') 'each touched plan reports applied=false with no pending operation; the exact monitor_device_path inventory row shows the desktop state again' @('rollback completion is proven by these observations, not by exit codes alone') '')
            ) }
    )
    return [ordered]@{
        schema_version=1
        kind='psvr.local_direct_test_procedure'
        purpose='Deferred local procedure for the exact-instance SNY6A04 direct-mode test. The preparing script generated these commands and executed nothing.'
        generated_utc=$GeneratedUtc
        prepared_without=@('UAC elevation','registry writes','process control','GPU/NVIDIA setters','hardware I/O','SteamVR start or stop')
        repository_root=$RepositoryRoot
        inputs=[ordered]@{
            hmd_plan=[ordered]@{ path=$HmdPlanPath; sha256=$HmdPlanSha256; role='existing original HMD EDID plan; read-only input, never mutated by the planner' }
            runtime_backup=[ordered]@{ path=$RuntimeBackupPath; sha256=$RuntimeBackupSha256; role='existing selective SteamVR direct-mode backup; read-only input, never mutated by the planner' }
            runtime_settings_path=$RuntimeSettingsPath
            steam_path=$SteamPath
            output_directory=[ordered]@{ path=$OutputDirectory; compat_plan=$compatPlanPath; transition_capture=$capturePath; reserved_files_not_created_by_planner=$true }
        }
        device_guard=[ordered]@{
            vendor_id=55629; product_id=27140; edid_identity='0xD94D/0x6A04 = Sony SNY6A04 (measured)'
            instance_id=$State.instance_id
            monitor_path=$HmdMonitorPath
            identity_contract='Exact ordinal comparison (-ceq) of monitor_device_path against this monitor path in the read-only out\display inventory, plus the DisplayCore inspector parameter -RequestedMonitorInterfacePath whose exact selection the inspector performs itself. -like is invalid: the ? in the path is a wildcard. WmiMonitorID/PnP presence alone is not desktop proof.'
            scope='Exact monitor instance only. Never a whole device class, never the LG/main-TV desktop target, never a GPU adapter.'
        }
        named_states=[ordered]@{
            baseline=[ordered]@{ hmd_edid_override='No EDID_OVERRIDE value 1: monitor reports its original EDID'; windows_desktop='SNY6A04 visible and usable on the desktop (historical working HDMI 120 candidate)'; steamvr_runtime='nativeDirectMode/EDID pair and direct_mode experiment section absent'; reached_by='HMD restore first, then selective runtime restore, both only with SteamVR closed, then operator verification' }
            normal_hmd=[ordered]@{ hmd_edid_override='EDID_OVERRIDE value 1 = Microsoft HMD v2 VSDB, usage 0x07 (the original plan)'; windows_desktop='SNY6A04 hidden from desktop paths; absence alone is not proof of direct scanout'; steamvr_runtime='selective experiment enabled per the runtime backup'; known_outcome='Historic: NVIDIA direct list stayed empty, compositor error 422; direct mode NOT proven' }
            compat=[ordered]@{ hmd_edid_override='Same VSDB with bit5 non-Microsoft usage (0x27); one data bit, identity/timings untouched'; windows_desktop='expected like normal_hmd until actually observed'; steamvr_runtime='as normal_hmd'; gate='Allowed only after 40_verify_restore_before_compat has operator evidence; the compat plan lives inside the new output directory' }
        }
        current_state=$State
        claims=[ordered]@{
            classification='planned ordering from journal files only; not physical proof of any machine state'
            restore_performed=$null
            hmd_back_on_desktop=$null
            compat_applied=$null
            steamvr_running=$null
            direct_mode_proven=$null
            planner_self_actions='The planner executed no restore, apply, start or inspection command itself; every claim above stays null until the operator records evidence.'
            recorded_flags='current_state is the accurate transcription of the plan journal file; it is deliberately not re-checked against hardware here.'
        }
        sequence_guards=@(
            'Run phases in listed sequence; stop at the first unmet requires.',
            'Never run 30 or any 50/80 elevated step before SteamVR is manually closed (20).',
            'Never run any 50 step before operator evidence exists in 40.',
            'current_state.restore_ready is not restore evidence; claims stay null until operator observations are recorded.',
            'Rollback order is HMD (80.1/80.2) then runtime (80.3), only while SteamVR is closed.',
            'Re-run the planner instead of editing this file when inputs change; hashes in inputs must match at run time.'
        )
        forbidden_operations=@(
            'Restoring or overwriting the entire steamvr.vrsettings; only the scoped backup keys in driver_psvrpc/direct_mode',
            'GPU/display-adapter global restart, Windows reboot or driver resets',
            'DisplayCore target acquisition/ownership or any change of the main TV/LG desktop monitor',
            'Automatic UAC elevation, Start-Process of SteamVR or process kills from generated commands',
            '-StartSteamVR on capture commands: starting SteamVR is always an explicit manual operator action',
            'Device-class-wide EDID edits, firmware writes or actions on non-SNY6A04 instances',
            'Renaming or deleting existing captures or plans to reuse a filename; new trials get new filenames'
        )
        phases=$phases
        operator_workflow_notes=@(
            'The planner ran unprivileged and only wrote procedure.json and procedure.md into the new output directory.',
            'Execute the phases in order later; every command in this file is for the operator, never pre-run.',
            'After each elevated step, re-run the read-only inspections and record observations beside the checklist in procedure.md.',
            'procedure.json is immutable planner output; notes and evidence go into procedure.md.'
        )
        warnings=@($Warnings)
    }
}
function Format-DirectTestProcedureMarkdown {
    param($Procedure)
    # One full concatenation per appended line: never mix a top-level comma
    # with + inside array literals (PowerShell 5.1 comma precedence splits them).
    $out=@()
    $out+=('# PSVR local direct-mode test procedure (planner output)')
    $out+=('')
    $out+=('Generated: '+$Procedure.generated_utc)
    $out+=('')
    $out+=('> '+$Procedure.purpose)
    $out+=('')
    foreach ($w in @($Procedure.warnings)) { $out+=('- WARNING: '+$w) }
    $out+=('')
    $out+=('## Inputs (read-only, never mutated)')
    $out+=('- HMD plan: '+$Procedure.inputs.hmd_plan.path+' (sha256 '+$Procedure.inputs.hmd_plan.sha256+')')
    $out+=('- Runtime backup: '+$Procedure.inputs.runtime_backup.path+' (sha256 '+$Procedure.inputs.runtime_backup.sha256+')')
    $out+=('- SteamVR settings file: '+$Procedure.inputs.runtime_settings_path)
    $out+=('- Output directory: '+$Procedure.inputs.output_directory.path)
    $out+=('')
    $out+=('## Current state (accurate at plan time)')
    $out+=('')
    $s=$Procedure.current_state
    $out+=('- State name: '+$s.name)
    $out+=('- HMD override applied: '+$s.hmd_override_applied)
    $out+=('- Pending operation: '+$(if ($s.pending_operation) { $s.pending_operation } else { 'none' }))
    $out+=('- Compat variant: '+$s.compat_variant)
    $out+=('- Instance scope: '+$s.instance_id)
    $out+=('- RestoreReady: '+$s.restore_ready+' - '+$s.restore_ready_means)
    $out+=('- Recorded source: '+$s.recorded_source)
    $out+=('')
    $out+=('Claims: '+$Procedure.claims.classification+'. '+$Procedure.claims.planner_self_actions+' '+$Procedure.claims.recorded_flags)
    $out+=('')
    $out+=('## Named states to distinguish')
    foreach ($name in @('baseline','normal_hmd','compat')) {
        $out+=('')
        $state=$Procedure.named_states.$name
        $out+=('### '+$name)
        $props=if ($state -is [System.Collections.IDictionary]) { @($state.GetEnumerator() | ForEach-Object { [pscustomobject]@{ Name=$_.Key; Value=$_.Value } }) } else { @($state.PSObject.Properties) }
        foreach ($prop in $props) { $out+=('- '+$prop.Name+': '+$prop.Value) }
    }
    $out+=('')
    $out+=('## Forbidden operations')
    foreach ($f in @($Procedure.forbidden_operations)) { $out+=('- '+$f) }
    $out+=('')
    $out+=('## Sequence guards')
    foreach ($g in @($Procedure.sequence_guards)) { $out+=('- '+$g) }
    foreach ($phase in @($Procedure.phases)) {
        $out+=('')
        $out+=('## Phase '+$phase.sequence+' - '+$phase.title)
        $out+=('')
        $out+=('Requires: '+(@($phase.requires) -join '; '))
        foreach ($n in @($phase.notes)) { if ($n) { $out+=('- Note: '+$n) } }
        foreach ($step in @($phase.steps)) {
            $out+=('')
            $out+=('### '+$step.id+' ['+$step.kind+', '+$step.elevation+'] '+$step.title)
            $out+=('')
            if ($step.apply_if) { $out+=('- Apply if: '+$step.apply_if) }
            if (@($step.commands).Count) {
                $out+=('```powershell')
                foreach ($c in @($step.commands)) { $out+=($c) }
                $out+=('```')
            } else { $out+=('(no command: manual operator action)') }
            foreach ($p in @($step.preconditions)) { $out+=('- Precondition: '+$p) }
            $out+=('- Expect: '+$step.expect)
            foreach ($d in @($step.do_not_infer)) { $out+=('- Do not infer: '+$d) }
            if ($step.operator_log) { $out+=('- Operator log: '+$step.operator_log) }
        }
    }
    $out+=('')
    $out+=('## Operator evidence checklist')
    $out+=('- [ ] 20 SteamVR closed manually at ____ (UTC)')
    $out+=('- [ ] 30/40 After the restore, journal read shows applied=false without pending; SNY6A04 back on the Windows desktop with expected UID/bounds/120Hz; decision to unblock compat by ____')
    $out+=('- [ ] 60 SteamVR started by an explicit manual request (name/time/reason): ____')
    $out+=('- [ ] 70 Visual image observed: ____; real scanout Hz measured: ____ (new capture filename, nothing deleted)')
    $out+=('- [ ] 80 Rollback HMD then runtime completed with SteamVR closed and re-verified read-only: ____')
    return ($out -join "`r`n")+"`r`n"
}
if ($ShowPlanJournal) {
    if (-not $JournalPlanPath) { throw '-ShowPlanJournal requires -JournalPlanPath' }
    $journalPlan=Get-Content -LiteralPath $JournalPlanPath -Raw | ConvertFrom-Json
    Get-DirectTestPlanJournal $journalPlan | ConvertTo-Json
}
