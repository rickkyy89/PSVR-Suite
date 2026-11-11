# Explicit temporary Windows refresh switch on the driver-selected Sony only.
# Does not create custom timings or EDID overrides. SteamVR must be closed for apply.
[CmdletBinding()]
param([Parameter(Mandatory=$true)][ValidateSet(90,120)][int]$Hz, [switch]$TestOnly, [switch]$PersistForTrial, [switch]$RestoreRecordingMirrorSettings,
    [switch]$DisableDashboard, [string]$RestoreDashboardFrom)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$settingsPath = 'C:\Program Files (x86)\Steam\config\steamvr.vrsettings'
function Read-FileSnapshot([string]$Path) {
    # Single byte snapshot (B9-05 pattern): the same bytes feed both JSON
    # parsing and SHA256, so values and fingerprint never come from two
    # different concurrent reads.
    $bytes=[IO.File]::ReadAllBytes($Path)
    $stream=New-Object IO.MemoryStream(,$bytes)
    try {
        $reader=New-Object IO.StreamReader($stream,[Text.Encoding]::UTF8,$true)
        try { $text=$reader.ReadToEnd() } finally { $reader.Close() }
    } finally { $stream.Dispose() }
    $sha=[Security.Cryptography.SHA256]::Create()
    try { $hashBytes=$sha.ComputeHash($bytes) } finally { $sha.Dispose() }
    $hash=($hashBytes | ForEach-Object { $_.ToString('X2') }) -join ''
    return [pscustomobject]@{Bytes=$bytes;Text=$text;Sha256=$hash}
}
function Merge-TrialRefreshSettings {
    # Applies ONLY the authorized trial keys onto the given (recent) document.
    # Every other key/section is preserved untouched from that document, so a
    # concurrent writer on a foreign key is never overwritten by a stale copy.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true)]$TargetSettings,
        [Parameter(Mandatory=$true)][ValidateSet(90,120)][int]$Hz,
        [switch]$DisableDashboard,
        [bool]$HasRestoreDashboardValue=$false,
        $RestoreDashboardValue=$null,
        [bool]$ClearDashboardValue=$false,
        [switch]$RestoreRecordingMirrorSettings
    )
    $TargetSettings.driver_psvrpc.displayRefreshNumerator=$Hz
    $TargetSettings.driver_psvrpc.displayRefreshDenominator=1
    if ($DisableDashboard) {
        if (-not $TargetSettings.PSObject.Properties['dashboard']) { $TargetSettings | Add-Member NoteProperty dashboard ([pscustomobject]@{}) }
        if ($TargetSettings.dashboard.PSObject.Properties['enableDashboard']) { $TargetSettings.dashboard.enableDashboard=$false }
        else { $TargetSettings.dashboard | Add-Member NoteProperty enableDashboard $false }
    }
    if ($HasRestoreDashboardValue) {
        if (-not $TargetSettings.PSObject.Properties['dashboard']) { $TargetSettings | Add-Member NoteProperty dashboard ([pscustomobject]@{}) }
        if ($TargetSettings.dashboard.PSObject.Properties['enableDashboard']) { $TargetSettings.dashboard.enableDashboard=$RestoreDashboardValue }
        else { $TargetSettings.dashboard | Add-Member NoteProperty enableDashboard $RestoreDashboardValue }
    } elseif ($ClearDashboardValue) {
        if ($TargetSettings.dashboard) {
            $TargetSettings.dashboard.PSObject.Properties.Remove('enableDashboard')
            if (@($TargetSettings.dashboard.PSObject.Properties).Count -eq 0) { $TargetSettings.PSObject.Properties.Remove('dashboard') }
        }
    }
    if ($RestoreRecordingMirrorSettings) {
        # Baseline recorded before the mirror-opening experiment: false, legacy key absent.
        $TargetSettings.steamvr.showMirrorView=$false
        $TargetSettings.steamvr.PSObject.Properties.Remove('showLegacyMirrorView')
    }
    return $TargetSettings
}
$settingsSnapshot=Read-FileSnapshot $settingsPath
$settings=$settingsSnapshot.Text | ConvertFrom-Json
$inventoryExe = Join-Path $root 'out\display\psvr_display.exe'
function Get-SelectedSony {
    $inventory = ((& $inventoryExe --json) -join "`n") | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw 'Inventory failed' }
    $targets = @($inventory.displays | Where-Object {
        $_.monitor_device_path -eq $settings.driver_psvrpc.displayMonitorPath -and $_.monitor_device_path -like '*SNY6A04*'
    })
    if ($targets.Count -ne 1) { throw 'The exact selected Sony must be active and unique' }
    return $targets[0]
}
$sony = Get-SelectedSony
if ($sony.source.width -ne 1920 -or $sony.source.height -ne 1080 -or
    $sony.source.x -ne $settings.driver_psvrpc.displayX -or $sony.source.y -ne $settings.driver_psvrpc.displayY) {
    throw 'Sony geometry differs from driver configuration'
}
if (-not $TestOnly -and (Get-Process -Name vrserver,vrcompositor -ErrorAction SilentlyContinue)) { throw 'Close SteamVR before applying refresh' }
if (-not ('PSVR.RefreshTrial' -as [type])) { Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace PSVR {
    [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)] public struct DisplayMode {
        [MarshalAs(UnmanagedType.ByValTStr,SizeConst=32)] public string deviceName;
        public ushort specVersion,driverVersion,size,driverExtra; public uint fields;
        public int x,y; public uint orientation,fixedOutput;
        public short color,duplex,yResolution,ttOption,collate;
        [MarshalAs(UnmanagedType.ByValTStr,SizeConst=32)] public string formName;
        public ushort logPixels; public uint bitsPerPixel,width,height,displayFlags,frequency;
        public uint icmMethod,icmIntent,mediaType,ditherType,reserved1,reserved2,panningWidth,panningHeight;
    }
    public static class RefreshTrial {
        [DllImport("user32.dll",CharSet=CharSet.Unicode)] private static extern bool EnumDisplaySettings(string device,int mode,ref DisplayMode value);
        [DllImport("user32.dll",CharSet=CharSet.Unicode)] private static extern int ChangeDisplaySettingsEx(string device,ref DisplayMode mode,IntPtr hwnd,uint flags,IntPtr parameter);
        public static DisplayMode Current(string device) {
            var value = new DisplayMode(); value.size=(ushort)Marshal.SizeOf(typeof(DisplayMode));
            if (value.size!=220 || !EnumDisplaySettings(device,-1,ref value)) throw new InvalidOperationException("Cannot read exact current DEVMODE");
            if (value.driverExtra!=0) throw new InvalidOperationException("Unexpected DEVMODE private data");
            return value;
        }
        public static DisplayMode Stored(string device) {
            var value = new DisplayMode(); value.size=(ushort)Marshal.SizeOf(typeof(DisplayMode));
            if (!EnumDisplaySettings(device,-2,ref value) || value.driverExtra!=0) throw new InvalidOperationException("Cannot read stored DEVMODE");
            return value;
        }
        public static int Set(string device,DisplayMode mode,uint hz,bool test,bool persist) {
            mode.frequency=hz; mode.fields |= 0x00400000;
            return ChangeDisplaySettingsEx(device,ref mode,IntPtr.Zero,test ? 2u : (persist ? 1u : 0u),IntPtr.Zero);
        }
    }
}
'@ }
$device = $sony.source_gdi_name
$original = [PSVR.RefreshTrial]::Current($device)
$stored = [PSVR.RefreshTrial]::Stored($device)
$testResult = [PSVR.RefreshTrial]::Set($device,$original,[uint32]$Hz,$true,$false)
if ($testResult -ne 0) { throw "Windows rejects ${Hz}Hz on selected Sony (CDS_TEST result=$testResult); no mode/config change" }
if ($TestOnly) {
    [ordered]@{ selected_monitor=$sony.monitor_device_path; gdi=$device; current_hz=$original.frequency; stored_hz=$stored.frequency; requested_hz=$Hz; windows_mode_test='accepted'; applied=$false } | ConvertTo-Json
    return
}
$backupParent = Join-Path $root 'logs'
if (-not (Test-Path -LiteralPath $backupParent -PathType Container)) { throw 'Backup parent missing' }
$backup = Join-Path $backupParent ('refresh_settings_before_' + $Hz + 'hz_' + (Get-Date -Format 'yyyyMMdd_HHmmss') + '.json')
if (Test-Path -LiteralPath $backup) { throw 'Backup already exists' }
Copy-Item -LiteralPath $settingsPath -Destination $backup
$temporary = "$settingsPath.psvr-refresh.tmp"
if (Test-Path -LiteralPath $temporary) { throw 'Temporary settings file already exists' }
try {
    $result = [PSVR.RefreshTrial]::Set($device,$original,[uint32]$Hz,$false,[bool]$PersistForTrial)
    if ($result -ne 0) { throw "Windows refresh apply failed: $result" }
    Start-Sleep -Seconds 2
    $after = Get-SelectedSony
    if ($after.refresh.numerator -ne $Hz -or $after.refresh.denominator -ne 1 -or
        $after.target.signal_refresh.numerator -ne $Hz -or $after.target.signal_refresh.denominator -ne 1 -or
        $after.source.width -ne 1920 -or $after.source.height -ne 1080 -or
        $after.source.x -ne $sony.source.x -or $after.source.y -ne $sony.source.y) {
        throw 'Applied display mode did not verify exact path/signal refresh and unchanged geometry'
    }
    if ($DisableDashboard -and $RestoreDashboardFrom) { throw 'Cannot disable and restore dashboard together' }
    # B9-06: re-read the recent document and refuse the commit when the
    # expected fingerprint changed. The commit below merges only the
    # authorized trial keys onto the recent document; everything else is
    # preserved from that re-read, never from the stale initial copy.
    $recentSnapshot=Read-FileSnapshot $settingsPath
    if ($recentSnapshot.Sha256 -ne $settingsSnapshot.Sha256) { throw "Settings changed concurrently; commit refused; settings backup=$backup" }
    $recent=$recentSnapshot.Text | ConvertFrom-Json
    $restoreDashboardValue=$null
    $hasRestoreDashboardValue=$false
    $clearDashboardValue=$false
    if ($RestoreDashboardFrom) {
        $previous = [IO.File]::ReadAllText([IO.Path]::GetFullPath($RestoreDashboardFrom)) | ConvertFrom-Json
        if ($previous.dashboard -and $previous.dashboard.PSObject.Properties['enableDashboard']) {
            $restoreDashboardValue=$previous.dashboard.enableDashboard
            $hasRestoreDashboardValue=$true
        } elseif ($recent.dashboard) {
            $clearDashboardValue=$true
        }
    }
    [void](Merge-TrialRefreshSettings -TargetSettings $recent -Hz $Hz -DisableDashboard:$DisableDashboard -HasRestoreDashboardValue $hasRestoreDashboardValue -RestoreDashboardValue $restoreDashboardValue -ClearDashboardValue $clearDashboardValue -RestoreRecordingMirrorSettings:$RestoreRecordingMirrorSettings)
    [IO.File]::WriteAllText($temporary,($recent | ConvertTo-Json -Depth 30),(New-Object Text.UTF8Encoding($false)))
    # PowerShell 5.1 coerces null string arguments to an empty path; use a real backup path.
    [IO.File]::Replace($temporary,$settingsPath,"$backup.replaced.json")
    [ordered]@{ applied=$true; persisted_for_trial=[bool]$PersistForTrial; previous_current_hz=$original.frequency; previous_stored_hz=$stored.frequency; selected_monitor=$after.monitor_device_path; source=$after.source; path_refresh=$after.refresh; signal_refresh=$after.target.signal_refresh; driver_refresh=$Hz; settings_backup=$backup } | ConvertTo-Json -Depth 5
} catch {
    if ($PersistForTrial) { [void][PSVR.RefreshTrial]::Set($device,$stored,$stored.frequency,$false,$true) }
    $rollback = [PSVR.RefreshTrial]::Set($device,$original,$original.frequency,$false,$false)
    if (Test-Path -LiteralPath $temporary) { [IO.File]::Delete($temporary) }
    throw "Refresh trial failed: $_. Display rollback result=$rollback; settings backup=$backup"
}
