param(
    [switch]$StartImu,
    [int]$ImuDurationMs = 0,
    [switch]$StationaryTest
)
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'scripts\run_monitor.ps1') -StartImu:$StartImu -ImuDurationMs $ImuDurationMs -StationaryTest:$StationaryTest
