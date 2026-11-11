[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$OutputFile,
    [ValidateRange(10,600)][int]$DurationSeconds = 180,
    [ValidateSet('WGC','GDI')][string]$CaptureBackend = 'WGC',
    [ValidateSet('keep','shown','hidden')][string]$Dashboard = 'keep',
    [switch]$NoTelemetry
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$OutputFile = [IO.Path]::GetFullPath($OutputFile)
$parent = Split-Path -Parent $OutputFile
if (-not (Test-Path -LiteralPath $parent -PathType Container)) { throw 'Recording parent missing' }
if (Test-Path -LiteralPath $OutputFile) { throw 'Recording already exists' }
$ffmpeg = (Get-Command ffmpeg -ErrorAction Stop).Source
$probe = Join-Path $root 'build\vs2022\RelWithDebInfo\psvr_runtime_probe.exe'
$collectorExe = Join-Path $root 'build\vs2022\RelWithDebInfo\psvr_runtime_telemetry.exe'
if (-not $NoTelemetry -and -not (Test-Path -LiteralPath $collectorExe)) { throw 'Build continuous MSVC telemetry first' }
$display = Join-Path $root 'out\display\psvr_display.exe'
$runtime = 'C:\Program Files (x86)\Steam\steamapps\common\SteamVR\bin\win64\openvr_api.dll'
$steamLogs = 'C:\Program Files (x86)\Steam\logs'
$stem = [IO.Path]::Combine($parent,[IO.Path]::GetFileNameWithoutExtension($OutputFile))
$utf8 = New-Object Text.UTF8Encoding($false)
$events = New-Object 'System.Collections.Generic.List[object]'
function Probe-Event($request) {
    $arguments = @('--openvr-dll',$runtime,'--duration-ms','1000')
    if ($request) { $arguments += @('--request',$request) } else { $arguments += '--compositor-status' }
    $lines = @(& $probe @arguments | ForEach-Object { $_ | ConvertFrom-Json })
    $exit = $LASTEXITCODE
    $events.Add([ordered]@{ utc=[DateTime]::UtcNow.ToString('o'); request=$request; exit_code=$exit; lines=$lines })
    if ($exit -ne 0) { throw "Runtime probe failed: $exit" }
    if ($request) {
        $reply = @($lines | Where-Object { $_.event -eq 'request' })
        if ($reply.Count -ne 1 -or -not $reply[0].queued) { throw 'Presence request not acknowledged' }
    }
}
function Copy-Logs($phase) {
    foreach ($name in @('vrserver','vrcompositor')) {
        Copy-Item -LiteralPath (Join-Path $steamLogs "$name.txt") -Destination "$stem.$name-$phase.txt"
    }
}
$inventory = ((& $display --json) -join "`n") | ConvertFrom-Json
$sony = @($inventory.displays | Where-Object { $_.monitor_device_path -like '*SNY6A04*' })
if ($sony.Count -ne 1) { throw 'Exactly one active Sony required' }
$windows = ((& (Join-Path $PSScriptRoot 'inspect_vr_windows.ps1')) -join "`n") | ConvertFrom-Json
$target = @($windows | Where-Object { $_.title -eq 'Headset Window' -and $_.visible -and -not $_.minimized })
if ($target.Count -ne 1) { throw 'Exact visible Headset Window required' }
if ($target[0].x -ne $sony[0].source.x -or $target[0].y -ne $sony[0].source.y -or
    $target[0].width -ne 1920 -or $target[0].height -ne 1080) { throw 'Headset Window does not match Sony bounds' }
$sonySummary = $sony[0] | Select-Object monitor_device_path,source_gdi_name,source,target,refresh
$metadata = [ordered]@{ start_utc=[DateTime]::UtcNow.ToString('o'); requested_duration_seconds=$DurationSeconds
    source='SteamVR Headset Window final stereo image'; window=$target[0]; sony_display=$sonySummary
    synthetic_presence=$true; recording_fps=30; recording_size='1280x720'; audio_recorded=$false
    capture_backend=$CaptureBackend
    driver_sha256=(Get-FileHash -LiteralPath (Join-Path $root 'out\driver_psvrpc_msvc\psvrpc\bin\win64\driver_psvrpc.dll') -Algorithm SHA256).Hash
    continuous_telemetry=(-not [bool]$NoTelemetry); dashboard_requested=$Dashboard
    limitations='Software-window recording, not physical OLED/lens observation or HDMI frame-skipping measurement. 30fps recording can miss sub-frame flashes.' }
Copy-Logs 'before'
$recording = $null
$collector = $null
$collectorError = $null
$clock = [Diagnostics.Stopwatch]::StartNew()
try {
    if ($NoTelemetry) {
        if ($Dashboard -ne 'keep') { throw 'Dashboard control requires continuous telemetry' }
        Probe-Event $null
        Probe-Event 'simulate-worn'
    } else {
        foreach ($suffix in @('telemetry.jsonl','ready.json','stop')) {
            if (Test-Path -LiteralPath "$stem.$suffix") { throw "Collector sidecar already exists: $suffix" }
        }
        $collector = New-Object Diagnostics.Process
        $collector.StartInfo.FileName = $collectorExe
        $collector.StartInfo.Arguments = '--openvr-dll "' + $runtime + '" --output "' + $stem + '.telemetry.jsonl" --ready-file "' + $stem + '.ready.json" --stop-file "' + $stem + '.stop" --duration-ms ' + (($DurationSeconds+60)*1000) + ' --sample-ms 10 --synthetic-presence --dashboard ' + $Dashboard
        $collector.StartInfo.UseShellExecute = $false
        $collector.StartInfo.CreateNoWindow = $true
        $collector.StartInfo.RedirectStandardError = $true
        [void]$collector.Start()
        $collectorError = $collector.StandardError.ReadToEndAsync()
        $readyDeadline = [DateTime]::UtcNow.AddSeconds(15)
        while (-not (Test-Path -LiteralPath "$stem.ready.json") -and -not $collector.HasExited -and [DateTime]::UtcNow -lt $readyDeadline) { Start-Sleep -Milliseconds 100 }
        if (-not (Test-Path -LiteralPath "$stem.ready.json")) { throw 'Continuous collector did not become ready' }
        $metadata.telemetry_ready = [IO.File]::ReadAllText("$stem.ready.json") | ConvertFrom-Json
    }
    if ($CaptureBackend -eq 'WGC') {
        $inputArgs = '-f lavfi -i "gfxcapture=hwnd=' + $target[0].hwnd + ':capture_cursor=0:capture_border=0:display_border=0:max_framerate=30"'
        $filter = 'hwdownload,format=bgra,scale=1280:720,format=yuv420p'
    } else {
        $inputArgs = '-f gdigrab -draw_mouse 0 -framerate 30 -i "title=Headset Window"'
        $filter = 'scale=1280:720'
    }
    $args = '-hide_banner -loglevel warning -n ' + $inputArgs + ' -t ' + $DurationSeconds + ' -vf "' + $filter + '" -an -c:v libx264 -preset ultrafast -crf 22 -pix_fmt yuv420p -movflags +faststart "' + $OutputFile + '"'
    $recording = New-Object Diagnostics.Process
    $recording.StartInfo.FileName = $ffmpeg
    $recording.StartInfo.Arguments = $args
    $recording.StartInfo.UseShellExecute = $false
    $recording.StartInfo.CreateNoWindow = $true
    $recording.StartInfo.RedirectStandardError = $true
    [void]$recording.Start()
    $metadata.video_started_utc = [DateTime]::UtcNow.ToString('o')
    $errorTask = $recording.StandardError.ReadToEndAsync()
    $renewAt = 120
    while (-not $recording.HasExited) {
        Start-Sleep -Seconds 2
        $recording.Refresh()
        if ($collector -and $collector.HasExited -and -not $recording.HasExited) { throw 'Continuous collector terminated during recording' }
        if ($NoTelemetry -and -not $recording.HasExited -and $clock.Elapsed.TotalSeconds -ge $renewAt) {
            Probe-Event 'simulate-worn'
            $renewAt = $clock.Elapsed.TotalSeconds + 120
        }
        if ($clock.Elapsed.TotalSeconds -gt $DurationSeconds+90) { throw 'Recording exceeded expected duration' }
    }
    $recording.WaitForExit()
    $metadata.video_ended_utc = [DateTime]::UtcNow.ToString('o')
    [IO.File]::WriteAllText("$stem.ffmpeg.log",$errorTask.Result,$utf8)
    if ($recording.ExitCode -ne 0) { throw "ffmpeg failed: $($recording.ExitCode)" }
    if ($NoTelemetry) { Probe-Event $null }
    $afterInventory = ((& $display --json) -join "`n") | ConvertFrom-Json
    $metadata.sony_display_after = @($afterInventory.displays | Where-Object { $_.monitor_device_path -eq $sony[0].monitor_device_path } | Select-Object monitor_device_path,source,target,refresh)
    $metadata.recording_completed = $true
} finally {
    if ($recording -and -not $recording.HasExited) { $recording.Kill(); $recording.WaitForExit() }
    if ($NoTelemetry) {
        try { Probe-Event 'simulate-unworn' } catch { $metadata.presence_release_error = "$_" }
    } elseif ($collector) {
        if (-not $collector.HasExited) { [IO.File]::WriteAllText("$stem.stop",[DateTime]::UtcNow.ToString('o'),$utf8) }
        if (-not $collector.WaitForExit(10000)) { $collector.Kill(); $collector.WaitForExit(); $metadata.collector_forced_stop=$true }
        [IO.File]::WriteAllText("$stem.telemetry.stderr.txt",$collectorError.Result,$utf8)
        $metadata.collector_exit_code=$collector.ExitCode
        if ($collector.ExitCode -ne 0) { $metadata.recording_completed=$false }
    }
    Copy-Logs 'after'
    $metadata.end_utc = [DateTime]::UtcNow.ToString('o')
    $metadata.events = $events.ToArray()
    [IO.File]::WriteAllText("$stem.metadata.json",($metadata | ConvertTo-Json -Depth 20),$utf8)
}
if ($collector -and $collector.ExitCode -ne 0) { throw "Continuous telemetry failed: $($collector.ExitCode); see $stem.telemetry.stderr.txt" }
"Recorded: $OutputFile"
