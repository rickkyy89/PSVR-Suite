# Runtime capture; -SimulateWorn explicitly renews a temporary synthetic presence lease.
# Never changes focus, display modes or persistent SteamVR settings.
[CmdletBinding()]
param(
    [ValidateRange(5,3600)][int]$DurationSeconds = 180,
    [ValidateRange(1,60)][int]$IntervalSeconds = 5,
    [string]$SteamPath = 'C:\Program Files (x86)\Steam',
    [string]$OutputDirectory,
    [switch]$SimulateWorn
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $root ('logs\presentation_' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
}
$parent = Split-Path -Parent $OutputDirectory
if (-not (Test-Path -LiteralPath $parent -PathType Container)) { throw 'Capture parent directory missing' }
if (Test-Path -LiteralPath $OutputDirectory) { throw 'Use a new output directory' }
$probe = Join-Path $root 'build\vs2022\RelWithDebInfo\psvr_runtime_probe.exe'
$display = Join-Path $root 'build\vs2022\RelWithDebInfo\psvr_display.exe'
$runtimeDll = Join-Path $SteamPath 'steamapps\common\SteamVR\bin\win64\openvr_api.dll'
foreach ($path in @($probe, $display, $runtimeDll)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing: $path" }
}
if (-not ('PSVRPresentationForeground' -as [type])) { Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class PSVRPresentationForeground {
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
}
'@
}
New-Item -ItemType Directory -Path $OutputDirectory | Out-Null
$utf8 = New-Object System.Text.UTF8Encoding($false)
function Save-Json($name, $value) {
    [IO.File]::WriteAllText((Join-Path $OutputDirectory $name), ($value | ConvertTo-Json -Depth 20), $utf8)
}
function Save-Logs($suffix) {
    foreach ($name in @('vrserver', 'vrcompositor')) {
        $path = Join-Path $SteamPath "logs\$name.txt"
        if (Test-Path -LiteralPath $path) { Copy-Item -LiteralPath $path -Destination (Join-Path $OutputDirectory "$name-$suffix.txt") }
    }
}
$packageDll = Join-Path $root 'out\driver_psvrpc_msvc\psvrpc\bin\win64\driver_psvrpc.dll'
Save-Json 'metadata.json' ([ordered]@{
    start_utc = [DateTime]::UtcNow.ToString('o'); duration_seconds = $DurationSeconds
    interval_seconds = $IntervalSeconds; package_dll = $packageDll
    sha256 = (Get-FileHash -LiteralPath $packageDll -Algorithm SHA256).Hash
    synthetic_presence_requested = [bool]$SimulateWorn
    limitations = 'Unattended polling, not lens acceptance or event-level focus history. DXGI codes are available only if SteamVR logs them. Snapshot times bound observations, not exact fault onset.'
})
Copy-Item -LiteralPath (Join-Path $env:LOCALAPPDATA 'openvr\openvrpaths.vrpath') -Destination (Join-Path $OutputDirectory 'openvrpaths-before.json')
Copy-Item -LiteralPath (Join-Path $SteamPath 'config\steamvr.vrsettings') -Destination (Join-Path $OutputDirectory 'steamvr-settings-before.json')
Save-Logs 'before'
$clock = [Diagnostics.Stopwatch]::StartNew()
$index = 0
$renewAt = 0
function Request-Presence($command) {
    $lines = @(& $probe --openvr-dll $runtimeDll --duration-ms 1000 --request $command 2>&1 | ForEach-Object { "$_" })
    $exit = $LASTEXITCODE
    Save-Json ('presence-{0}-{1:D4}.json' -f $command, $index) ([ordered]@{
        timestamp_utc = [DateTime]::UtcNow.ToString('o'); command = $command; exit_code = $exit; lines = $lines
    })
    $requests = @($lines | Where-Object { $_.StartsWith('{') } | ForEach-Object { $_ | ConvertFrom-Json } | Where-Object { $_.event -eq 'request' })
    if ($requests.Count -ne 1 -or -not $requests[0].queued) { throw "Presence request not confirmed: $command (exit=$exit, request_count=$($requests.Count))" }
    if ($exit -ne 0) { Write-Warning "Presence queued, but pose probe failed (exit=$exit); retaining this failure in the capture." }
}
try {
    while ($clock.Elapsed.TotalSeconds -lt $DurationSeconds) {
        if ($SimulateWorn -and $clock.Elapsed.TotalSeconds -ge $renewAt) {
            Request-Presence 'simulate-worn'
            $renewAt = $clock.Elapsed.TotalSeconds + 120
        }
        $started = [DateTime]::UtcNow.ToString('o')
        $hwnd = [PSVRPresentationForeground]::GetForegroundWindow()
        [uint32]$owner = 0
        [void][PSVRPresentationForeground]::GetWindowThreadProcessId($hwnd, [ref]$owner)
        $window = & (Join-Path $PSScriptRoot 'inspect_compositor_window.ps1') | ConvertFrom-Json
        $inventory = (& $display --json) -join "`n"
        if ($LASTEXITCODE -ne 0) { throw 'Display inventory failed' }
        # Bounds come from this live inventory, not the historical reference in the window inspector.
        $pose = @(& $probe --openvr-dll $runtimeDll --duration-ms 1000 --compositor-status 2>&1 | ForEach-Object { "$_" })
        $probeExit = $LASTEXITCODE
        Save-Json ('sample-{0:D4}.json' -f $index) ([ordered]@{
            start_utc = $started; end_utc = [DateTime]::UtcNow.ToString('o')
            foreground_hwnd = $hwnd.ToInt64().ToString('X'); foreground_pid = $owner
            compositor_window = $window; displays = ($inventory | ConvertFrom-Json)
            runtime_probe_exit = $probeExit; runtime_probe_lines = $pose
        })
        $index++
        Start-Sleep -Seconds $IntervalSeconds
    }
} finally {
    if ($SimulateWorn) {
        try { Request-Presence 'simulate-unworn'; Start-Sleep -Seconds 1 }
        catch { Write-Warning "Presence release failed; driver lease expires automatically: $_" }
    }
    Save-Logs 'after'
    Copy-Item -LiteralPath (Join-Path $SteamPath 'config\steamvr.vrsettings') -Destination (Join-Path $OutputDirectory 'steamvr-settings-after.json')
    Save-Json 'completion.json' ([ordered]@{ end_utc = [DateTime]::UtcNow.ToString('o'); elapsed_seconds = $clock.Elapsed.TotalSeconds; samples = $index })
}
"Presentation capture: $OutputDirectory ($index samples)"
