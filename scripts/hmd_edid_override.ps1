# SPDX-License-Identifier: MIT
# Minimal Windows monitor EDID override, scoped to the exact selected SNY6A04.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateSet('Prepare','Apply','Restore')][string]$Action,
    [Parameter(Mandatory=$true)][string]$PlanPath,
    [string]$SettingsPath='C:\Program Files (x86)\Steam\config\steamvr.vrsettings',
    [switch]$CompatNonMicrosoftBit
)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'hmd_edid_plan.ps1')
function Save-Plan($Plan) {
    $parent=Split-Path -Parent $PlanPath
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) { throw 'Plan parent missing' }
    $temporary=Join-Path $parent ([IO.Path]::GetRandomFileName())
    try {
        [IO.File]::WriteAllText($temporary,($Plan | ConvertTo-Json -Depth 12),(New-Object Text.UTF8Encoding($false)))
        if (Test-Path -LiteralPath $PlanPath) { [IO.File]::Replace($temporary,$PlanPath,[System.Management.Automation.Language.NullString]::Value) }
        else { [IO.File]::Move($temporary,$PlanPath) }
    } finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary } }
}
function Hex([byte[]]$Bytes) { return ([BitConverter]::ToString($Bytes)).Replace('-','').ToLowerInvariant() }
function Bytes([string]$Text) {
    if ($Text.Length -ne 256 -or $Text -notmatch '^[0-9a-fA-F]+$') { throw 'A 128-byte hex EDID block is required' }
    $result=New-Object byte[] 128
    for ($i=0;$i -lt 128;$i++) { $result[$i]=[Convert]::ToByte($Text.Substring($i*2,2),16) }
    return ,$result
}
if ($Action -eq 'Prepare') {
    if (Test-Path -LiteralPath $PlanPath) { throw 'Do not overwrite an existing plan/backup' }
    $settings=Get-Content -LiteralPath $SettingsPath -Raw | ConvertFrom-Json
    $path=[string]$settings.driver_psvrpc.displayMonitorPath
    if ($path -notmatch '^\\\\\?\\DISPLAY#(SNY6A04)#([^#]+)#\{[0-9a-fA-F-]+\}$') { throw 'Exact selected SNY6A04 path required' }
    $instance='DISPLAY\'+$Matches[1]+'\'+$Matches[2]
    $keyPath='SYSTEM\CurrentControlSet\Enum\'+$instance+'\Device Parameters'
    $key=[Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($keyPath,$false)
    if (-not $key) { throw 'Selected monitor registry instance missing' }
    try {
        [byte[]]$raw=$key.GetValue('EDID')
        if (-not $raw) { throw 'Original EDID unavailable' }
        [byte[]]$effective=$raw.Clone()
        $override=$key.OpenSubKey('EDID_OVERRIDE',$false)
        $old=$null
        $otherOverrides=@{}
        if ($override) {
            try {
                foreach ($name in $override.GetValueNames()) {
                    if ($name -notmatch '^\d+$') { throw 'Unrecognized override value; inspect manually' }
                    [byte[]]$block=$override.GetValue($name)
                    $index=[int]$name
                    if ($block.Length -ne 128 -or ($index+1)*128 -gt $effective.Length) { throw 'Existing override layout unsupported' }
                    [Array]::Copy($block,0,$effective,$index*128,128)
                    if ($name -eq '1') { $old=Hex $block } else { $otherOverrides[$name]=Hex $block }
                }
            } finally { $override.Dispose() }
        }
        $container=(Get-PnpDeviceProperty -InstanceId $instance -KeyName 'DEVPKEY_Device_ContainerId').Data
        if (-not $container -or [Guid]$container -eq [Guid]::Empty) { throw 'Existing monitor container ID required' }
        $extra=@()
        if ($CompatNonMicrosoftBit) { $extra=@('--compat-non-microsoft-bit') }
        $patch=& python (Join-Path $root 'tools\psvr_native_analysis\hmd_edid.py') --input-hex (Hex $effective) --container-guid ([string]$container) @extra | ConvertFrom-Json
        if ($LASTEXITCODE -ne 0 -or -not $patch.identity_and_base_timings_unchanged) { throw 'Minimal EDID patch failed validation' }
        $plan=[pscustomobject]@{schema_version=1;monitor_path=$path;instance_id=$instance;registry_path=$keyPath;container_guid=[string]$container;raw_edid_hex=(Hex $raw);effective_edid_hex=(Hex $effective);old_override_1=$old;other_overrides=$otherOverrides;patched_block_hex=$patch.patched_block_hex;applied=$false;note='Only Microsoft HMD v2 VSDB inserted in CTA block1. Base identity/timings unchanged; no firmware write.'}
        $plan | Add-Member -NotePropertyName compat_non_microsoft_bit -NotePropertyValue ([bool]$CompatNonMicrosoftBit)
        Save-Plan $plan
    } finally { $key.Dispose() }
    [pscustomobject]@{action=$Action;plan=$PlanPath;instance=$instance;changes='EDID_OVERRIDE value1 only; Microsoft HMD classification, no timing/identity edits'} | ConvertTo-Json
    return
}
if (Get-Process -Name vrserver,vrcompositor,vrmonitor -ErrorAction SilentlyContinue) { throw 'Close SteamVR before applying/restoring EDID' }
$principal=New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Administrator PowerShell required for the monitor override/restart' }
$plan=Get-Content -LiteralPath $PlanPath -Raw | ConvertFrom-Json
Assert-HmdEdidPlan $plan
$key=[Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($plan.registry_path,$true)
if (-not $key) { throw 'Selected monitor registry instance missing' }
try {
    $observed=Hex $key.GetValue('EDID')
    $override=$key.OpenSubKey('EDID_OVERRIDE',$true)
    try {
        $overrides=@{}
        if ($override) { foreach ($name in $override.GetValueNames()) { $overrides[$name]=Hex $override.GetValue($name) } }
        $mutation=Get-HmdEdidMutation $plan $observed $overrides $Action
        # Journal intent before the registry write. A saved pending Apply can be
        # restored if the subsequent final plan save fails or the process exits.
        $plan | Add-Member -NotePropertyName pending_operation -NotePropertyValue $Action -Force
        Save-Plan $plan
        if (-not $override) { $override=$key.CreateSubKey('EDID_OVERRIDE') }
        if ($mutation.delete) { $override.DeleteValue('1',$false) }
        else { $override.SetValue('1',(Bytes $mutation.value),[Microsoft.Win32.RegistryValueKind]::Binary) }
        $actual=if (@($override.GetValueNames()) -contains '1') { Hex $override.GetValue('1') } else { $null }
        if ($actual -cne $mutation.value) { throw 'Registry write verification failed; pending operation preserved' }
        $plan.applied=$mutation.applied
        $plan.pending_operation=$null
        Save-Plan $plan
    } finally { if ($override) { $override.Dispose() } }
} finally { $key.Dispose() }
& pnputil.exe /restart-device $plan.instance_id
$restartCode=$LASTEXITCODE
[pscustomobject]@{action=$Action;instance=$plan.instance_id;device_restart_exit=$restartCode;plan=$PlanPath;note='Recheck actual GPU visibility/direct-mode enumeration; override installation alone is not proof'} | ConvertTo-Json
if ($restartCode -ne 0) { exit $restartCode }
