# Controlled, reversible symmetric projection trial. Not physical lens calibration.
# Legacy -FovDegrees sets horizontal and vertical together; -HorizontalFovDegrees
# and -VerticalFovDegrees must be given as a pair for independent lateral/vertical
# diagnostics (Assetto/MSFS narrow-tall check). -SteamVrSettingsPath/-BackupParent
# are offline/debug injection points; live defaults are unchanged.
[CmdletBinding(DefaultParameterSetName='Set')]
param(
    [Parameter(Mandatory=$true,ParameterSetName='Set')][ValidateRange(70,110)][double]$FovDegrees,
    [Parameter(Mandatory=$true,ParameterSetName='SetHV')][ValidateRange(70,110)][double]$HorizontalFovDegrees,
    [Parameter(Mandatory=$true,ParameterSetName='SetHV')][ValidateRange(70,110)][double]$VerticalFovDegrees,
    [Parameter(Mandatory=$true,ParameterSetName='Restore')][string]$RestoreFrom,
    [Parameter(ParameterSetName='Set')][Parameter(ParameterSetName='SetHV')][Parameter(ParameterSetName='Restore')][string]$SteamVrSettingsPath = 'C:\Program Files (x86)\Steam\config\steamvr.vrsettings',
    [Parameter(ParameterSetName='Set')][Parameter(ParameterSetName='SetHV')][Parameter(ParameterSetName='Restore')][string]$BackupParent = ''
)
$ErrorActionPreference = 'Stop'
if (Get-Process -Name vrserver,vrcompositor -ErrorAction SilentlyContinue) { throw 'Close SteamVR before changing frozen projection' }
$root = Split-Path -Parent $PSScriptRoot
$settingsPath = $SteamVrSettingsPath
$settings = [IO.File]::ReadAllText($settingsPath) | ConvertFrom-Json
$keys = @('projectionLeftLeft','projectionLeftRight','projectionLeftTop','projectionLeftBottom',
          'projectionRightLeft','projectionRightRight','projectionRightTop','projectionRightBottom')
$before = [ordered]@{}
foreach ($key in $keys) {
    if (-not $settings.driver_psvrpc.PSObject.Properties[$key]) { throw "Projection key missing: $key" }
    $before[$key] = $settings.driver_psvrpc.$key
}
$parent = if ([string]::IsNullOrWhiteSpace($BackupParent)) { Join-Path $root 'logs' } else { $BackupParent }
if (-not (Test-Path -LiteralPath $parent -PathType Container)) { throw 'Backup parent missing' }
$backup = Join-Path $parent ('projection_before_' + (Get-Date -Format 'yyyyMMdd_HHmmss') + '.json')
if (Test-Path -LiteralPath $backup) { throw 'Projection backup already exists' }
Copy-Item -LiteralPath $settingsPath -Destination $backup
if ($PSCmdlet.ParameterSetName -eq 'Restore') {
    $previous = [IO.File]::ReadAllText([IO.Path]::GetFullPath($RestoreFrom)) | ConvertFrom-Json
    foreach ($key in $keys) {
        if (-not $previous.driver_psvrpc.PSObject.Properties[$key]) { throw "Restore projection key missing: $key" }
        $settings.driver_psvrpc.$key = $previous.driver_psvrpc.$key
    }
} else {
    if ($settings.driver_psvrpc.distortionMode -ne 'identity') { throw 'This diagnostic trial requires identity mode; calibrated profiles need their own projection' }
    if ($PSCmdlet.ParameterSetName -eq 'SetHV') {
        $tangentH = [Math]::Tan($HorizontalFovDegrees * [Math]::PI / 360)
        $tangentV = [Math]::Tan($VerticalFovDegrees * [Math]::PI / 360)
        foreach ($eye in @('Left','Right')) {
            $settings.driver_psvrpc.('projection'+$eye+'Left') = -$tangentH
            $settings.driver_psvrpc.('projection'+$eye+'Right') = $tangentH
            $settings.driver_psvrpc.('projection'+$eye+'Top') = -$tangentV
            $settings.driver_psvrpc.('projection'+$eye+'Bottom') = $tangentV
        }
    } else {
        $tangent = [Math]::Tan($FovDegrees * [Math]::PI / 360)
        foreach ($eye in @('Left','Right')) {
            $settings.driver_psvrpc.('projection'+$eye+'Left') = -$tangent
            $settings.driver_psvrpc.('projection'+$eye+'Right') = $tangent
            $settings.driver_psvrpc.('projection'+$eye+'Top') = -$tangent
            $settings.driver_psvrpc.('projection'+$eye+'Bottom') = $tangent
        }
    }
}
$temporary = "$settingsPath.psvr-projection.tmp"
if (Test-Path -LiteralPath $temporary) { throw 'Projection temporary file already exists' }
try {
    [IO.File]::WriteAllText($temporary,($settings | ConvertTo-Json -Depth 30),(New-Object Text.UTF8Encoding($false)))
    [IO.File]::Replace($temporary,$settingsPath,"$backup.replaced.json")
} finally {
    if (Test-Path -LiteralPath $temporary) { [IO.File]::Delete($temporary) }
}
$after = [ordered]@{}
foreach ($key in $keys) { $after[$key] = $settings.driver_psvrpc.$key }
[ordered]@{ projection_trial=$PSCmdlet.ParameterSetName; nominal_fov_degrees=$(if ($PSCmdlet.ParameterSetName -eq 'Set') { $FovDegrees } else { $null }); nominal_horizontal_fov_degrees=$(if ($PSCmdlet.ParameterSetName -eq 'SetHV') { $HorizontalFovDegrees } else { $null }); nominal_vertical_fov_degrees=$(if ($PSCmdlet.ParameterSetName -eq 'SetHV') { $VerticalFovDegrees } else { $null }); before=$before; after=$after; backup=$backup; optical_calibration_verified=$false } | ConvertTo-Json -Depth 4
