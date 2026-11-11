# SPDX-License-Identifier: MIT
# Observes Windows/NVIDIA/SteamVR together. Only -StartSteamVR starts the runtime.
[CmdletBinding()]
param(
    [ValidateRange(2,180)][int]$DurationSeconds=30,
    [ValidateRange(1,10)][int]$IntervalSeconds=2,
    [Parameter(Mandatory=$true)][string]$OutputPath,
    [switch]$StartSteamVR,
    [string]$SteamVRPath='C:\Program Files (x86)\Steam\steamapps\common\SteamVR'
)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$display=Join-Path $root 'out\display\psvr_display.exe'
$probe=Join-Path $root 'out\runtime_probe\psvr_runtime_probe.exe'
$parent=Split-Path -Parent ([IO.Path]::GetFullPath($OutputPath))
if (-not (Test-Path -LiteralPath $parent -PathType Container)) { throw 'Output parent missing' }
if (Test-Path -LiteralPath $OutputPath) { throw 'Do not overwrite an existing capture' }
if ($StartSteamVR -and (Get-Process -Name vrserver,vrcompositor,vrmonitor -ErrorAction SilentlyContinue)) { throw 'Close SteamVR before a startup capture' }
# Compile the query helper once. Both public and optional private queries are read-only.
. (Join-Path $PSScriptRoot 'inspect_nvidia_displays.ps1') -IncludeDirectEnumeration | Out-Null
$logs=Join-Path (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $SteamVRPath))) 'logs'
. (Join-Path $PSScriptRoot 'direct_mode_log_reader.ps1')
$logReader=New-DirectModeLogReader $logs
$writer=New-Object IO.StreamWriter($OutputPath,$false,(New-Object Text.UTF8Encoding($false)))
$writer.AutoFlush=$true
try {
    $clock=[Diagnostics.Stopwatch]::StartNew()
    $started=$false
    do {
        $begin=$clock.Elapsed.TotalSeconds
        $inventory=& $display --json | ConvertFrom-Json
        if ($LASTEXITCODE -ne 0) { throw 'Windows inventory failed' }
        $processes=@(Get-Process -Name vrserver,vrcompositor,vrmonitor -ErrorAction SilentlyContinue | Select-Object Name,Id)
        $summary=$null
        $probeExit=$null
        if (Get-Process -Name vrserver -ErrorAction SilentlyContinue) {
            $lines=& $probe --openvr-dll (Join-Path $SteamVRPath 'bin\win64\openvr_api.dll') --compositor-status --duration-ms 300
            $probeExit=$LASTEXITCODE
            $summary=@($lines | ForEach-Object { $_ | ConvertFrom-Json } | Where-Object { $_.event -eq 'summary' })
        }
        $record=[ordered]@{
            timestamp_utc=[DateTime]::UtcNow.ToString('o'); elapsed_s=$clock.Elapsed.TotalSeconds
            phase=$(if (-not $started -and $StartSteamVR) {'before_start'} else {'observe'})
            windows=@($inventory.displays | Select-Object monitor_device_path,edid_ids,source,target,refresh)
            nvidia=@([PsvrNvDisplayProbe]::Inspect()); direct_enumeration=@([PsvrNvDisplayProbe]::InspectDirect())
            processes=$processes; runtime_probe_exit=$probeExit; runtime_summary=$summary; new_log_evidence=@(Read-DirectModeEvidence $logReader)
        }
        $writer.WriteLine(($record | ConvertTo-Json -Depth 16 -Compress))
        if ($StartSteamVR -and -not $started) {
            Start-Process -FilePath (Join-Path $SteamVRPath 'bin\win64\vrmonitor.exe')
            $started=$true
        }
        $remaining=$IntervalSeconds-($clock.Elapsed.TotalSeconds-$begin)
        if ($remaining -gt 0) { Start-Sleep -Milliseconds ([int]($remaining*1000)) }
    } while ($clock.Elapsed.TotalSeconds -lt $DurationSeconds)
} finally { $writer.Dispose() }
[pscustomobject]@{capture=$OutputPath;duration_s=$clock.Elapsed.TotalSeconds;runtime_started=$started;note='Observed states; a request/log success or IsFullscreen alone does not establish direct scanout'} | ConvertTo-Json
