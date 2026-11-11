# SPDX-License-Identifier: MIT
# Elevated, single-variable compatibility trial. Never changes identity/timings.
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$PreviousPlan,[Parameter(Mandatory=$true)][string]$NewPlan)
$ErrorActionPreference='Stop'
$tool=Join-Path $PSScriptRoot 'hmd_edid_override.ps1'
try {
    & $tool -Action Restore -PlanPath $PreviousPlan
    if ($LASTEXITCODE -ne 0) { throw 'Previous override restoration/restart failed' }
    Start-Sleep -Seconds 2
    & $tool -Action Prepare -PlanPath $NewPlan -CompatNonMicrosoftBit
    & $tool -Action Apply -PlanPath $NewPlan
} catch {
    [IO.File]::WriteAllText(($NewPlan+'.error.txt'),[string]$_)
    throw
}
