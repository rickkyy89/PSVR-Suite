# SPDX-License-Identifier: MIT
# Generates a readable/JSON local test procedure for the deferred SNY6A04
# direct-mode test while the operator cannot answer UAC or touch hardware.
# This script validates with the pure checkers, hashes inputs read-only and
# writes only procedure.json/procedure.md into a brand-new absolute directory.
# It never executes generated commands, never elevates, and never mutates the
# existing backup plans.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$PlanPath,
    [Parameter(Mandatory=$true)][string]$RuntimeBackupPath,
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [string]$SteamPath='C:\Program Files (x86)\Steam'
)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'hmd_edid_plan.ps1')
. (Join-Path $PSScriptRoot 'local_direct_test_plan.ps1')
if (-not [IO.Path]::IsPathRooted($OutputDirectory)) { throw 'OutputDirectory must be an absolute path' }
$OutputDirectory=[IO.Path]::GetFullPath($OutputDirectory)
if (Test-Path -LiteralPath $OutputDirectory) { throw "Refusing to overwrite the existing OutputDirectory: $OutputDirectory" }
$parent=Split-Path -Parent $OutputDirectory
if (-not (Test-Path -LiteralPath $parent -PathType Container)) { throw "OutputDirectory parent missing: $parent" }
$planFull=[IO.Path]::GetFullPath($PlanPath)
$backupFull=[IO.Path]::GetFullPath($RuntimeBackupPath)
if (-not (Test-Path -LiteralPath $planFull -PathType Leaf)) { throw "Missing PlanPath file: $planFull" }
if (-not (Test-Path -LiteralPath $backupFull -PathType Leaf)) { throw "Missing RuntimeBackupPath file: $backupFull" }
$plan=Get-Content -LiteralPath $planFull -Raw | ConvertFrom-Json
try { Assert-HmdEdidPlan $plan } catch { throw "HMD plan rejected by the pure checker: $_" }
$backup=Get-Content -LiteralPath $backupFull -Raw | ConvertFrom-Json
$backupInfo=Assert-DirectTestRuntimeBackup $backup
$state=Get-DirectTestLocalState $plan
$warnings=@()
if (-not $state.journal_clean) { $warnings+=('Interrupted '+$state.pending_operation+' is journaled in the HMD plan; resolve it through the restore phase before the compat phase can run') }
if ($state.compat_variant -and $state.hmd_override_applied) { $warnings+='The supplied plan itself records the compat variant; the procedure keeps it distinct from the normal HMD override' }
if ($state.name -eq 'BaselineNotVerified') { $warnings+='The HMD plan file reports neither an applied override nor a journaled operation: an unverified baseline label only, never re-checked against hardware by the planner' }
$warnings+='Runtime enable state cannot be inferred from the backup file alone; the phase 10 Inspect command is authoritative before any restore'
$repoRoot=Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $repoRoot 'out\display\psvr_display.exe') -PathType Leaf)) { $warnings+='Verify (40.1) and capture phases reference out\display\psvr_display.exe, which is currently missing; rebuild it before running those phases' }
$shaPlan=(Get-FileHash -LiteralPath $planFull -Algorithm SHA256).Hash
$shaBackup=(Get-FileHash -LiteralPath $backupFull -Algorithm SHA256).Hash
$procedure=New-DirectTestProcedure -RepositoryRoot $repoRoot -ScriptsDirectory $PSScriptRoot -SteamPath ([IO.Path]::GetFullPath($SteamPath)) `
    -HmdPlanPath $planFull -HmdPlanSha256 $shaPlan -HmdMonitorPath ([string]$plan.monitor_path) `
    -RuntimeBackupPath $backupFull -RuntimeBackupSha256 $shaBackup `
    -RuntimeSettingsPath ([string]$backupInfo.settings_path) -OutputDirectory $OutputDirectory -State $state `
    -GeneratedUtc ([DateTime]::UtcNow.ToString('o')) -Warnings $warnings
[IO.Directory]::CreateDirectory($OutputDirectory) | Out-Null
$jsonPath=Join-Path $OutputDirectory 'procedure.json'
$mdPath=Join-Path $OutputDirectory 'procedure.md'
$utf8=New-Object Text.UTF8Encoding($false)
[IO.File]::WriteAllText($jsonPath,($procedure | ConvertTo-Json -Depth 32),$utf8)
[IO.File]::WriteAllText($mdPath,(Format-DirectTestProcedureMarkdown $procedure),$utf8)
[pscustomobject]@{ procedure_json=$jsonPath; procedure_markdown=$mdPath; current_state=$state.name
    restore_ready=$state.restore_ready; pending_operation=$state.pending_operation; compat_variant=$state.compat_variant
    phases=@($procedure.phases).Count; warnings=$warnings
    note='Commands were generated, never executed. No UAC prompt, registry/process/GPU/hardware call, no mutation of the input plans.' } | ConvertTo-Json -Depth 8
