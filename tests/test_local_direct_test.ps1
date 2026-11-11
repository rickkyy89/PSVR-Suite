# SPDX-License-Identifier: MIT
<#
.SYNOPSIS
    Hardware-free tests for scripts/prepare_local_direct_test.ps1 and the pure
    module scripts/local_direct_test_plan.ps1.
.DESCRIPTION
    Everything runs against throwaway fixtures under %TEMP%\opencode, with
    deliberate spaces and apostrophes in directory/file names. The planner is
    invoked in-process; generated command strings are only parsed back with the
    PowerShell AST to prove quoting, and are never executed. The module's
    -ShowPlanJournal entry point only ever Get-Content-reads a fixture plan.
    No registry, GPU, NVIDIA setter, UAC, SteamVR or device access happens here.
    Usage: powershell -ExecutionPolicy Bypass -File tests\test_local_direct_test.ps1
    Exit 0 when all cases pass, 1 otherwise.
#>
param([string]$PythonExe='python')
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
. (Join-Path $root 'scripts\hmd_edid_plan.ps1')
. (Join-Path $root 'scripts\local_direct_test_plan.ps1')
$prepare=Join-Path $root 'scripts\prepare_local_direct_test.ps1'
$module=Join-Path $root 'scripts\local_direct_test_plan.ps1'

function Assert($Value,[string]$Message) { if (-not $Value) { throw $Message } }
function Reject([scriptblock]$Action,[string]$Pattern) {
    $message=$null
    try { & $Action | Out-Null } catch { $message=$_.Exception.Message }
    Assert ($message -and $message -match $Pattern) ('Expected rejection /'+$Pattern+'/ got: '+$message)
}
$script:Failed=0
function Invoke-TestCase([string]$Name,[scriptblock]$Body) {
    try { & $Body | Out-Null; Write-Host ('PASS '+$Name) }
    catch { $script:Failed++; Write-Host ('FAIL '+$Name+' :: '+$_.Exception.Message) }
}

# --- synthetic EDID fixtures (same construction as tests/test_hmd_edid_plan.ps1) ------
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
$script:Source=Hex ((BlockChecksum $base)+(BlockChecksum $cta))
$guid='12345678-1234-5678-9abc-123456789abc'
$script:Patch=& $PythonExe (Join-Path $root 'tools\psvr_native_analysis\hmd_edid.py') --input-hex $script:Source --container-guid $guid | ConvertFrom-Json
Assert ($LASTEXITCODE -eq 0) 'Fixture generation failed'
$script:PatchCompat=& $PythonExe (Join-Path $root 'tools\psvr_native_analysis\hmd_edid.py') --input-hex $script:Source --container-guid $guid --compat-non-microsoft-bit | ConvertFrom-Json
Assert ($LASTEXITCODE -eq 0) 'Compat fixture generation failed'

function New-PlanFixture {
    param([string]$Directory,[string]$FileName,[string]$Vendor='SNY6A04',[bool]$Applied=$true,[bool]$Compat=$false,[string]$Pending='')
    $instance='DISPLAY\'+$Vendor+'\synthetic-instance'
    $plan=[pscustomobject]@{ schema_version=1
        registry_path='SYSTEM\CurrentControlSet\Enum\'+$instance+'\Device Parameters'
        instance_id=$instance
        monitor_path='\\?\DISPLAY#'+$Vendor+'#synthetic-instance#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}'
        container_guid=$guid
        raw_edid_hex=$script:Source; effective_edid_hex=$script:Source
        patched_block_hex=$(if ($Compat) { $script:PatchCompat.patched_block_hex } else { $script:Patch.patched_block_hex })
        old_override_1=$null; other_overrides=[pscustomobject]@{}; applied=$Applied
        compat_non_microsoft_bit=$Compat; note='synthetic test plan' }
    if ($Pending) { $plan | Add-Member -NotePropertyName pending_operation -NotePropertyValue $Pending }
    $path=Join-Path $Directory $FileName
    [IO.File]::WriteAllText($path,($plan | ConvertTo-Json -Depth 12),(New-Object Text.UTF8Encoding($false)))
    return $path
}
function New-BackupFixture {
    param([string]$Directory,[string]$FileName,[int]$VendorId=55629,[string]$ExtraSection='')
    $change=@()
    $change+=([pscustomobject]@{ section='driver_psvrpc'; key='nativeDirectMode'; section_existed=$true; existed=$false; old_value=$null; written_value=$true })
    $change+=([pscustomobject]@{ section='driver_psvrpc'; key='edidVendorId'; section_existed=$true; existed=$false; old_value=$null; written_value=$VendorId })
    $change+=([pscustomobject]@{ section='driver_psvrpc'; key='edidProductId'; section_existed=$true; existed=$false; old_value=$null; written_value=27140 })
    $change+=([pscustomobject]@{ section='direct_mode'; key='enable'; section_existed=$false; existed=$false; old_value=$null; written_value=$true })
    $change+=([pscustomobject]@{ section='direct_mode'; key='edidVid4'; section_existed=$true; existed=$false; old_value=$null; written_value=$VendorId })
    $change+=([pscustomobject]@{ section='direct_mode'; key='edidPid4'; section_existed=$true; existed=$false; old_value=$null; written_value=27140 })
    $change+=([pscustomobject]@{ section='direct_mode'; key='count'; section_existed=$true; existed=$false; old_value=$null; written_value=5 })
    if ($ExtraSection) { $change+=([pscustomobject]@{ section=$ExtraSection; key='anything'; section_existed=$true; existed=$true; old_value=1; written_value=2 }) }
    $backup=[pscustomobject]@{ schema_version=1; settings_path='C:\Program Files (x86)\Steam\config\steamvr.vrsettings'; before_sha256='AB'; changes=$change; created_utc='2026-10-05T07:56:57.3003273Z' }
    $path=Join-Path $Directory $FileName
    [IO.File]::WriteAllText($path,($backup | ConvertTo-Json -Depth 12),(New-Object Text.UTF8Encoding($false)))
    return $path
}
function StateFixture {
    param([bool]$Applied,[bool]$Compat=$false,[string]$Pending='')
    $plan=[pscustomobject]@{ instance_id='DISPLAY\SNY6A04\synthetic-instance'; applied=$Applied; compat_non_microsoft_bit=$Compat }
    if ($Pending) { $plan | Add-Member -NotePropertyName pending_operation -NotePropertyValue $Pending }
    return $plan
}
function Get-ProcedureCommands($Parsed) {
    $all=@()
    foreach ($phase in @($Parsed.phases)) {
        foreach ($step in @($phase.steps)) {
            foreach ($c in @($step.commands)) { if ($c) { $all+=$c } }
        }
    }
    return , $all
}
function Get-CommandStringTokens {
    param([string]$Command)
    $tokens=$null; $errors=$null
    $null=[System.Management.Automation.Language.Parser]::ParseInput($Command,[ref]$tokens,[ref]$errors)
    Assert (@($errors).Count -eq 0) ('command does not parse cleanly: '+$Command)
    $values=@()
    foreach ($tok in @($tokens)) {
        # PS 5.1 also reports bareword arguments as StringLiteralToken; only the
        # apostrophe-delimited ones are quoted literals. Decode '' escaping.
        if ($tok.GetType().Name -eq 'StringLiteralToken' -and $tok.Text.StartsWith("'") -and $tok.Text.EndsWith("'") -and $tok.Text.Length -ge 2) {
            $values+=($tok.Text.Substring(1,$tok.Text.Length-2).Replace("''","'"))
        }
    }
    return , $values
}
function Assert-CommandsQuoted {
    param([string[]]$Commands,[string[]]$Paths)
    # Proof of quoting: remove every genuinely apostrophe-quoted literal from
    # the command, then no target path (raw or ''-escaped form) may survive.
    foreach ($cmd in $Commands) {
        $tokens=$null; $errors=$null
        $null=[System.Management.Automation.Language.Parser]::ParseInput($cmd,[ref]$tokens,[ref]$errors)
        Assert (@($errors).Count -eq 0) ('command does not parse cleanly: '+$cmd)
        $rest=$cmd
        foreach ($tok in @($tokens)) {
            if ($tok.GetType().Name -eq 'StringLiteralToken' -and $tok.Text.StartsWith("'") -and $tok.Text.EndsWith("'") -and $tok.Text.Length -ge 2) {
                $rest=$rest.Replace($tok.Text,[string]::Empty)
            }
        }
        foreach ($p in $Paths) {
            Assert (-not ($rest.Contains($p) -or $rest.Contains($p.Replace("'","''")))) ('path appears unquoted in: '+$cmd)
        }
    }
}
function Assert-NoForbiddenTokens([string[]]$Commands) {
    foreach ($cmd in $Commands) {
        foreach ($bad in @('-StartSteamVR','Start-Process','Stop-Process','pnputil','TryAcquire','CreateDisplayDevice','Restart-Computer','Remove-Item','Set-ItemProperty','Disable-PnpDevice','nvidia-smi')) {
            Assert (-not $cmd.Contains($bad)) ('forbidden token '+$bad+' in command: '+$cmd)
        }
    }
}
function Assert-OnlyReviewableScripts([string[]]$Commands) {
    $allowed=@('inspect_hmd_edid_plan.ps1','local_direct_test_plan.ps1','native_direct_mode.ps1','inspect_windows_display_core.ps1','inspect_nvidia_displays.ps1','hmd_edid_override.ps1','capture_direct_mode_transition.ps1','inspect_steamvr_logs.ps1')
    foreach ($cmd in $Commands) {
        foreach ($value in (Get-CommandStringTokens $cmd)) {
            if ($value -like '*.ps1') {
                Assert ($allowed -contains (Split-Path -Leaf $value)) ('unexpected script target: '+$value)
            }
            if ($value -like '*.exe') {
                # Only the repo's read-only display inventory may be invoked, and
                # only with --json; any other native executable is rejected.
                Assert ((Split-Path -Leaf $value) -eq 'psvr_display.exe') ('unexpected native executable: '+$value)
                Assert ($cmd.Contains('--json')) ('display tool must be called with --json only: '+$cmd)
            }
        }
    }
}

# --- fixture layout under the preapproved opencode temp --------------------------------
$parent=Join-Path $env:TEMP 'opencode'
if (-not (Test-Path -LiteralPath $parent -PathType Container)) { throw 'Test temp parent missing' }
$fx=Join-Path $parent ('psvr_local_direct_test_'+[Guid]::NewGuid().ToString('N').Substring(0,8))
$dir=Join-Path $fx 'local direct test'   # spaces in the directory name on purpose
[IO.Directory]::CreateDirectory($dir) | Out-Null
try {
    $planP=New-PlanFixture $dir ('It'+[char]39+'s plan with spaces.json')
    $backupP=New-BackupFixture $dir 'runtime backup.json'
    $pendingP=New-PlanFixture $dir 'pending hmd plan.json' -Applied:$false -Pending 'Apply'
    $compatP=New-PlanFixture $dir 'compat hmd plan.json' -Compat:$true
    $baselineP=New-PlanFixture $dir 'baseline hmd plan.json' -Applied:$false
    $badPlan=New-PlanFixture $dir 'wrong device plan.json' -Vendor 'GSM0001'
    $hashPlan=(Get-FileHash -LiteralPath $planP -Algorithm SHA256).Hash
    $hashBackup=(Get-FileHash -LiteralPath $backupP -Algorithm SHA256).Hash
    $outDir=Join-Path $dir 'output procedure'

    Invoke-TestCase 'pure quoting round-trips through the PowerShell parser' {
        $weird='C:\Temp It'+[char]39+'s\with spaces\old hmd plan.json'
        $q=Get-DirectTestQuotedPath $weird
        Assert ($q -eq ("'"+$weird.Replace("'","''")+"'")) 'apostrophe must double inside single quotes'
        Assert-CommandsQuoted @('x -PlanPath '+$q) @($weird)
        Assert-CommandsQuoted @('& '+$q+' -Action Inspect') @($weird)
        Reject { Assert-CommandsQuoted @('y -PlanPath C:\plain path with spaces.json') @('C:\plain path with spaces.json') } 'unquoted'
        Reject { Get-DirectTestQuotedPath ' ' } 'concrete path'
    }
    Invoke-TestCase 'runtime backup scope: every identity checked, duplicates and unscoped keys rejected' {
        $base=@(
            ([pscustomobject]@{ section='driver_psvrpc'; key='nativeDirectMode'; written_value=$true }),
            ([pscustomobject]@{ section='driver_psvrpc'; key='edidVendorId'; written_value=55629 }),
            ([pscustomobject]@{ section='direct_mode'; key='edidPid4'; written_value=27140 })
        )
        $good=[pscustomobject]@{ schema_version=1; settings_path='C:\Program Files (x86)\Steam\config\steamvr.vrsettings'; changes=$base }
        $info=Assert-DirectTestRuntimeBackup $good
        Assert ($info.settings_path -eq $good.settings_path) 'settings path must pass through'
        $wrongId=([pscustomobject]@{ schema_version=1; settings_path='C:\x\vrs'; changes=@(([pscustomobject]@{ section='driver_psvrpc'; key='edidVendorId'; written_value=55628 }),([pscustomobject]@{ section='driver_psvrpc'; key='edidProductId'; written_value=27140 })) })
        Reject { Assert-DirectTestRuntimeBackup $wrongId } 'vendor identity'
        $wrongProduct=([pscustomobject]@{ schema_version=1; settings_path='C:\x\vrs'; changes=@(([pscustomobject]@{ section='driver_psvrpc'; key='edidVendorId'; written_value=55629 }),([pscustomobject]@{ section='driver_psvrpc'; key='edidProductId'; written_value=27141 })) })
        Reject { Assert-DirectTestRuntimeBackup $wrongProduct } 'product identity'
        $badFirst=([pscustomobject]@{ schema_version=1; settings_path='C:\x\vrs'; changes=@(([pscustomobject]@{ section='driver_psvrpc'; key='edidVendorId'; written_value=55628 }),([pscustomobject]@{ section='direct_mode'; key='edidVid4'; written_value=55629 }),([pscustomobject]@{ section='driver_psvrpc'; key='edidProductId'; written_value=27140 })) })
        Reject { Assert-DirectTestRuntimeBackup $badFirst } 'vendor identity'
        $dup=([pscustomobject]@{ schema_version=1; settings_path='C:\x\vrs'; changes=@(([pscustomobject]@{ section='driver_psvrpc'; key='nativeDirectMode'; written_value=$true }),([pscustomobject]@{ section='driver_psvrpc'; key='nativeDirectMode'; written_value=$false }),([pscustomobject]@{ section='driver_psvrpc'; key='edidVendorId'; written_value=55629 }),([pscustomobject]@{ section='direct_mode'; key='edidPid4'; written_value=27140 })) })
        Reject { Assert-DirectTestRuntimeBackup $dup } 'duplicate change entry'
        $offSection=([pscustomobject]@{ schema_version=1; settings_path='C:\x\vrs'; changes=@(([pscustomobject]@{ section='user_settings'; key='anything'; written_value=1 }),([pscustomobject]@{ section='driver_psvrpc'; key='edidVendorId'; written_value=55629 }),([pscustomobject]@{ section='driver_psvrpc'; key='edidProductId'; written_value=27140 })) })
        Reject { Assert-DirectTestRuntimeBackup $offSection } 'unknown section'
        $looseKey=([pscustomobject]@{ schema_version=1; settings_path='C:\x\vrs'; changes=@(([pscustomobject]@{ section='driver_psvrpc'; key='renderWidth'; written_value=960 }),([pscustomobject]@{ section='driver_psvrpc'; key='edidVendorId'; written_value=55629 }),([pscustomobject]@{ section='driver_psvrpc'; key='edidProductId'; written_value=27140 })) })
        Reject { Assert-DirectTestRuntimeBackup $looseKey } 'unscoped key'
        $noIds=([pscustomobject]@{ schema_version=1; settings_path='C:\x\vrs'; changes=@(([pscustomobject]@{ section='driver_psvrpc'; key='nativeDirectMode'; written_value=$true })) })
        Reject { Assert-DirectTestRuntimeBackup $noIds } 'does not record'
        $noChanges=([pscustomobject]@{ schema_version=1; settings_path='C:\x\vrs'; changes=@() })
        Reject { Assert-DirectTestRuntimeBackup $noChanges } 'no experiment changes'
        $rel=([pscustomobject]@{ schema_version=1; settings_path='config\steamvr.vrsettings'; changes=$base })
        Reject { Assert-DirectTestRuntimeBackup $rel } 'absolute'
        Reject { Assert-DirectTestRuntimeBackup $good 'C:\Other\steamvr.vrsettings' } 'different SteamVR settings file'
    }
    Invoke-TestCase 'state classification: accurate names, restore_ready only when a restore can apply' {
        $normal=Get-DirectTestLocalState (StateFixture $true)
        Assert ($normal.name -eq 'NormalHmdApplied' -and $normal.restore_ready) 'normal HMD ready'
        Assert ((Get-DirectTestLocalState (StateFixture $true $true)).name -eq 'CompatApplied') 'compat distinguished'
        Assert ((Get-DirectTestLocalState (StateFixture $true $true)).restore_ready) 'compat still has a real restore to run'
        $st=Get-DirectTestLocalState (StateFixture $false $false 'Apply')
        Assert ($st.name -eq 'PendingConflict' -and $st.pending_operation -eq 'Apply' -and -not $st.journal_clean) 'pending conflict'
        Assert (-not $st.restore_ready) 'pending conflict must not claim restore_ready'
        $b=Get-DirectTestLocalState (StateFixture $false)
        Assert ($b.name -eq 'BaselineNotVerified' -and -not $b.restore_ready) 'unapplied journal is an unverified baseline, not proof of restore'
        foreach ($s in @($normal;$st;$b)) { Assert ($s.restore_ready_means -match 'never means a restore was performed') 'RestoreReady semantics must be explicit' }
        Assert ($b.recorded_source -match 'journal file') 'recorded source wording'
        Reject { Get-DirectTestLocalState (StateFixture $true $false 'Uninstall') } 'Unknown pending operation'
    }
    Invoke-TestCase 'journal reader prints whitelisted metadata only, never EDID bytes' {
        $txt=(& $module -ShowPlanJournal -JournalPlanPath $planP | Out-String)
        $j=$txt | ConvertFrom-Json
        Assert ($j.read_only -and $j.applied -and -not $j.compat_non_microsoft_bit) 'applied plan journal'
        Assert ($null -eq $j.pending_operation -and -not $j.journal_already_restored -and $j.restore_gate_still_applicable) 'gate still applicable while applied'
        Assert (-not $txt.Contains($script:Source.Substring(0,64))) 'EDID hex must not leak'
        Assert ($j.note -match 'Whitelisted') 'note wording'
        $jb=(& $module -ShowPlanJournal -JournalPlanPath $baselineP | Out-String) | ConvertFrom-Json
        Assert ($jb.journal_already_restored -and -not $jb.restore_gate_still_applicable) 'restored journal semantics'
        $jp=(& $module -ShowPlanJournal -JournalPlanPath $pendingP | Out-String) | ConvertFrom-Json
        Assert ($jp.pending_operation -eq 'Apply' -and -not $jp.journal_already_restored) 'pending journal semantics'
        Reject { & $module -ShowPlanJournal } 'requires -JournalPlanPath'
    }

    Invoke-TestCase 'planner happy path: new dir, JSON+MD only, quoted spacey paths, inputs untouched' {
        $result=& $prepare -PlanPath $planP -RuntimeBackupPath $backupP -OutputDirectory $outDir
        $files=@(Get-ChildItem -LiteralPath $outDir -File | ForEach-Object { $_.Name } | Sort-Object)
        Assert ($files.Count -eq 2 -and $files[0] -eq 'procedure.json' -and $files[1] -eq 'procedure.md') ('unexpected outputs: '+($files -join ','))
        Assert (-not (Test-Path -LiteralPath (Join-Path $outDir 'hmd_edid_compat_plan.json'))) 'reserved compat plan must not be created'
        Assert (-not (Test-Path -LiteralPath (Join-Path $outDir 'transition_capture.jsonl'))) 'reserved capture file must not be created'
        $pj=Get-Content -LiteralPath (Join-Path $outDir 'procedure.json') -Raw | ConvertFrom-Json
        $script:Pj=$pj
        $script:Cmds=Get-ProcedureCommands $pj
        Assert ($pj.schema_version -eq 1 -and $pj.kind -eq 'psvr.local_direct_test_procedure') 'schema/kind'
        Assert ($pj.current_state.name -eq 'NormalHmdApplied' -and $pj.current_state.restore_ready) 'state'
        Assert ($pj.current_state.recorded_source -match 'journal file') 'recorded flags stated as journal transcription'
        Assert ($pj.claims.classification -match 'planned ordering') 'claims label themselves planned-only'
        foreach ($c in @('restore_performed','hmd_back_on_desktop','compat_applied','steamvr_running','direct_mode_proven')) {
            Assert ($null -eq $pj.claims.$c) ('claim '+$c+' must stay null/unobserved')
        }
        Assert ($pj.inputs.hmd_plan.sha256 -eq $hashPlan -and $pj.inputs.runtime_backup.sha256 -eq $hashBackup) 'snapshot hashes'
        Assert ($pj.inputs.runtime_settings_path -eq 'C:\Program Files (x86)\Steam\config\steamvr.vrsettings') 'settings path from backup'
        Assert ($pj.device_guard.monitor_path -eq '\\?\DISPLAY#SNY6A04#synthetic-instance#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}') 'exact monitor path recorded'
        Assert (@($script:Cmds).Count -ge 12) 'commands were generated'
        $quoteTargets=@($planP,$backupP,$pj.inputs.output_directory.compat_plan,$pj.inputs.output_directory.transition_capture,$pj.inputs.runtime_settings_path,$pj.device_guard.monitor_path,$pj.inputs.steam_path)
        foreach ($p in $quoteTargets) {
            Assert (@($script:Cmds | Where-Object { (Get-CommandStringTokens $_) -contains $p }).Count -ge 1) ('path never appears as a quoted token: '+$p)
        }
        Assert-CommandsQuoted $script:Cmds $quoteTargets
        $md=Get-Content -LiteralPath (Join-Path $outDir 'procedure.md') -Raw
        $qPlan=Get-DirectTestQuotedPath $planP
        $qBackup=Get-DirectTestQuotedPath $backupP
        Assert ($md.Contains('-JournalPlanPath '+$qPlan)) 'markdown shows the escaped quoted plan path in the journal command'
        Assert ($md.Contains('-BackupPath '+$qBackup)) 'markdown quotes the backup path'
        Assert ($md.Contains('never means a restore was performed')) 'markdown carries the RestoreReady semantics'
        # Readable markdown must keep headings as single lines (PS 5.1 comma/+ trap).
        Assert ($md.Contains('### 10.3 [read-only, none] Inspect the selective direct-mode runtime settings')) 'single-line heading'
        # The planner must not mutate either input file.
        Assert ((Get-FileHash -LiteralPath $planP -Algorithm SHA256).Hash -eq $hashPlan) 'HMD plan mutated'
        Assert ((Get-FileHash -LiteralPath $backupP -Algorithm SHA256).Hash -eq $hashBackup) 'runtime backup mutated'
    }
    Invoke-TestCase 'generated commands are review-only: journal checks, no wrapper, no automation, strict order' {
        $cmds=$script:Cmds
        Assert-NoForbiddenTokens $cmds
        Assert-OnlyReviewableScripts $cmds
        Assert (@($cmds | Where-Object { $_ -match 'try_hmd_edid_compat' }).Count -eq 0) 'atomic wrapper must not be an emitted command'
        $ids=@($script:Pj.phases | ForEach-Object { $_.id })
        Assert (($ids -join ',') -eq '10_inspect_readonly,20_steamvr_manual_close,30_hmd_admin_restore,40_verify_restore_before_compat,50_compat_trial_after_verified_restore,60_steamvr_manual_start,70_observe_visual_and_real_hz,80_rollback_to_baseline') ('phase order: '+($ids -join ','))
        $p10=@($script:Pj.phases)[0]
        Assert ($p10.steps[0].commands[0] -match 'local_direct_test_plan.ps1' -and $p10.steps[0].commands[0] -match '-ShowPlanJournal') 'journal snapshot is the first positive check'
        $p30=@($script:Pj.phases | Where-Object { $_.id -like '30_*' })[0]
        Assert (@(@($p30.steps)[1].commands) -match '-ShowPlanJournal') 'restore proof uses the journal read, not the restore-only gate'
        $p40=@($script:Pj.phases | Where-Object { $_.id -like '40_*' })[0]
        $c40=@($p40.steps | ForEach-Object { @($_.commands) } | Where-Object { $_ })
        Assert (@($c40 | Where-Object { $_ -match 'psvr_display\.exe' -and $_ -match '-ceq' -and $_ -match 'monitor_device_path' }).Count -eq 1) 'verify phase desktop evidence is the exact-ordinal repo display inventory row'
        Assert (@($c40 | Where-Object { $_ -match 'inspect_windows_display_core' -and $_ -match '-RequestedMonitorInterfacePath' }).Count -ge 1) 'DisplayCore steps use the inspector exact-path selection parameter'
        Assert (@($script:Cmds | Where-Object { $_ -match 'WmiMonitor|Win32_PnPEntity|Get-CimInstance' }).Count -eq 0) 'PnP/WMI monitor enumeration must not be presented as desktop proof'
        $modesStep=@($p40.steps | Where-Object { @($_.commands) -match '-QueryModes' })
        Assert (@($modesStep).Count -eq 1 -and $modesStep[0].commands[0] -match 'inspect_windows_display_core' -and $modesStep[0].apply_if -match 'optional') 'mode query invokes the same inspector, separate and optional'
        $monitor=$script:Pj.device_guard.monitor_path
        foreach ($c in $script:Cmds) {
            if ((Get-CommandStringTokens $c) -contains $monitor) { Assert ($c -match '-ceq' -or $c -match '-RequestedMonitorInterfacePath') ('monitor path must be ordinal-compared or inspector-selected, never wildcarded: '+$c) }
        }
        Assert ($script:Pj.device_guard.identity_contract -match 'ceq') 'identity contract documented'
        $p50=@($script:Pj.phases | Where-Object { $_.id -like '50_*' })[0]
        Assert (@($p50.steps).Count -eq 2) 'compat phase only carries prepare and apply'
        Assert ((@($p50.notes) -join ' ') -match 'try_hmd_edid_compat') 'wrapper explained as separate manual option only'
        $p60=@($script:Pj.phases | Where-Object { $_.id -like '60_*' })[0]
        $c60=@($p60.steps | ForEach-Object { @($_.commands) } | Where-Object { $_ })
        Assert ($c60.Count -eq 0) 'manual start phase must not carry any command'
        Assert ((@($p60.steps)[0].expect) -match 'deliberate human action') 'explicit manual start captured'
        $p70=@($script:Pj.phases | Where-Object { $_.id -like '70_*' })[0]
        $cap=@($p70.steps | Where-Object { @($_.commands).Count -and $_.commands[0] -match 'capture_direct_mode_transition' })[0]
        Assert ($cap.apply_if -match 'brand-new') 'each trial gets a new capture filename'
        Assert ($cap.apply_if -match 'never rename, overwrite or delete') 'evidence must never be deleted'
        $p80=@($script:Pj.phases | Where-Object { $_.id -like '80_*' })[0]
        $steps8=@($p80.steps)
        Assert (@($steps8).Count -eq 4) 'rollback keeps four ordered steps'
        Assert ($steps8[0].commands[0] -match 'hmd_edid_override') 'rollback restores HMD override first'
        Assert ($steps8[1].commands[0] -match 'hmd_edid_override') 'second conditional HMD restore'
        Assert ($steps8[2].commands[0] -match 'native_direct_mode') 'runtime restore comes after the HMD steps'
        Assert (@(@($steps8[3].commands) | Where-Object { $_ -match '-ShowPlanJournal' }).Count -ge 1) 'final verification reads journals, not the restore-only gate'
        Assert (@(@($steps8[3].commands) | Where-Object { $_ -match 'psvr_display\.exe' -and $_ -match '-ceq' }).Count -eq 1) 'final verification re-reads the exact desktop inventory row'
        Assert ((@($script:Pj.forbidden_operations) -join ' ') -match 'entire steamvr.vrsettings') 'whole-settings restore forbidden'
        Assert ((@($script:Pj.forbidden_operations) -join ' ') -match 'global restart') 'GPU global restart forbidden'
        Assert ((@($script:Pj.forbidden_operations) -join ' ') -match 'main TV') 'main TV ownership forbidden'
        Assert ((@($script:Pj.forbidden_operations) -join ' ') -match 'Renaming or deleting existing captures') 'evidence-preservation rule stated'
        Assert ($script:Pj.device_guard.scope -match 'Exact monitor instance only') 'device guard wording'
    }

    Invoke-TestCase 'planner refuses existing OutputDirectory and relative paths, never leaves partial dirs' {
        $existing=Join-Path $dir 'existing out'
        [IO.Directory]::CreateDirectory($existing) | Out-Null
        $sentinel=Join-Path $existing 'keep.txt'
        [IO.File]::WriteAllText($sentinel,'do not touch')
        $before=(Get-FileHash -LiteralPath $sentinel -Algorithm SHA256).Hash
        Reject { & $prepare -PlanPath $planP -RuntimeBackupPath $backupP -OutputDirectory $existing } 'Refusing to overwrite'
        Assert ((Get-FileHash -LiteralPath $sentinel -Algorithm SHA256).Hash -eq $before) 'sentinel file mutated'
        Assert (@(Get-ChildItem -LiteralPath $existing -File).Count -eq 1) 'planner wrote into an existing directory'
        Reject { & $prepare -PlanPath $planP -RuntimeBackupPath $backupP -OutputDirectory 'relative\output' } 'absolute path'
        $never=Join-Path $dir 'never created'
        Reject { & $prepare -PlanPath 'missing.json' -RuntimeBackupPath $backupP -OutputDirectory $never } 'Missing PlanPath'
        Assert (-not (Test-Path -LiteralPath $never)) 'failed runs must not create the output directory'
    }

    Invoke-TestCase 'pending conflict surfaces with restore_ready false and unobserved claims' {
        $outPending=Join-Path $dir 'output pending'
        & $prepare -PlanPath $pendingP -RuntimeBackupPath $backupP -OutputDirectory $outPending | Out-Null
        $pj=Get-Content -LiteralPath (Join-Path $outPending 'procedure.json') -Raw | ConvertFrom-Json
        Assert ($pj.current_state.name -eq 'PendingConflict') 'pending name'
        Assert ($pj.current_state.pending_operation -eq 'Apply') 'pending value shown accurately'
        Assert (-not $pj.current_state.restore_ready) 'pending conflict must not advertise restore_ready'
        Assert ((@($pj.warnings) -join ' ') -match 'Interrupted Apply') 'pending warning'
        Assert ($null -eq $pj.claims.restore_performed) 'pending planner never claims a restore happened'
    }
    Invoke-TestCase 'compat-applied input is recorded accurately while physical claims stay unobserved' {
        $outCompat=Join-Path $dir 'output compat'
        & $prepare -PlanPath $compatP -RuntimeBackupPath $backupP -OutputDirectory $outCompat | Out-Null
        $pj=Get-Content -LiteralPath (Join-Path $outCompat 'procedure.json') -Raw | ConvertFrom-Json
        Assert ($pj.current_state.name -eq 'CompatApplied' -and $pj.current_state.compat_variant) 'compat state recorded from journal'
        Assert ($null -eq $pj.claims.compat_applied) 'physical compat claim stays unobserved'
        Assert ((@($pj.warnings) -join ' ') -match 'compat variant') 'compat warning'
        Assert ($pj.named_states.compat.gate -match '40_verify') 'compat state gated behind verification'
    }
    Invoke-TestCase 'never-applied plan becomes BaselineNotVerified, not restored proof' {
        $outBase=Join-Path $dir 'output baseline'
        & $prepare -PlanPath $baselineP -RuntimeBackupPath $backupP -OutputDirectory $outBase | Out-Null
        $pj=Get-Content -LiteralPath (Join-Path $outBase 'procedure.json') -Raw | ConvertFrom-Json
        Assert ($pj.current_state.name -eq 'BaselineNotVerified' -and -not $pj.current_state.hmd_override_applied) 'baseline label'
        Assert (-not $pj.current_state.restore_ready) 'nothing to restore, restore_ready false'
        Assert ((@($pj.warnings) -join ' ') -match 'unverified baseline') 'accuracy warning'
    }
    Invoke-TestCase 'wrong device and out-of-scope backups are blocked' {
        Reject { Assert-HmdEdidPlan (Get-Content -LiteralPath $badPlan -Raw | ConvertFrom-Json) } 'scope/schema mismatch'
        $outBad=Join-Path $dir 'output bad device'
        Reject { & $prepare -PlanPath $badPlan -RuntimeBackupPath $backupP -OutputDirectory $outBad } 'rejected by the pure checker'
        Assert (-not (Test-Path -LiteralPath $outBad)) 'blocked run left no directory'
        $badId=New-BackupFixture $dir 'wrong id backup.json' -VendorId 55628
        Reject { & $prepare -PlanPath $planP -RuntimeBackupPath $badId -OutputDirectory (Join-Path $dir 'output bad id') } 'vendor identity'
        $badSection=New-BackupFixture $dir 'whole settings backup.json' -ExtraSection 'user_settings'
        Reject { & $prepare -PlanPath $planP -RuntimeBackupPath $badSection -OutputDirectory (Join-Path $dir 'output bad section') } 'unknown section'
    }
    if ($script:Failed) { throw ($script:Failed.ToString()+' case(s) failed') }
    Write-Host 'PASS local direct test planner: quoting, overwrite refusal, accurate states/claims, journal reads only, wrong device blocked, no wrapper or GPU-mutating commands'
} finally { [IO.Directory]::Delete($fx,$true) }
exit $(if ($script:Failed) { 1 } else { 0 })
