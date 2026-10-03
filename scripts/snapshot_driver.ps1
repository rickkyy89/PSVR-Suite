[CmdletBinding()]
param([string]$LogDirectory)
$ErrorActionPreference = 'Stop'
if (-not $LogDirectory) { $LogDirectory = Join-Path (Split-Path -Parent $PSScriptRoot) 'logs' }
$LogDirectory = [System.IO.Path]::GetFullPath($LogDirectory)
if (-not (Test-Path -LiteralPath (Split-Path -Parent $LogDirectory))) { throw 'Snapshot parent directory does not exist' }
[void][System.IO.Directory]::CreateDirectory($LogDirectory)
$devices = @(Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like 'USB\VID_054C&PID_09AF*' })
if (-not $devices.Count) { throw 'PSVR USB device not present' }
$records = foreach ($device in $devices) {
    $values = @{}
    foreach ($key in @('DEVPKEY_Device_Service', 'DEVPKEY_Device_DriverInfPath',
            'DEVPKEY_Device_DriverVersion', 'DEVPKEY_Device_DriverProvider', 'DEVPKEY_Device_HardwareIds')) {
        $property = Get-PnpDeviceProperty -InstanceId $device.InstanceId -KeyName $key -ErrorAction SilentlyContinue
        $values[$key] = if ($property) { $property.Data } else { $null }
    }
    [pscustomobject]@{ InstanceId = $device.InstanceId; Name = $device.FriendlyName; Status = "$($device.Status)"; Properties = $values }
}
$file = Join-Path $LogDirectory ("driver_snapshot_{0}_{1}.json" -f (Get-Date -Format 'yyyyMMddTHHmmssfff'), $PID)
[System.IO.File]::WriteAllText($file, (ConvertTo-Json -InputObject @($records) -Depth 6), [System.Text.UTF8Encoding]::new($false))
"Driver inventory saved: $file"
$records | Format-Table Name, Status, InstanceId -AutoSize
'This is a binding inventory, not a driver-package export. Original MI_05 uses Windows inbox input.inf; see docs/WINUSB_CONTROL.md for restoration.'
