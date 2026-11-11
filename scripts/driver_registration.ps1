[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateSet('Install','Remove','Inspect')][string]$Action,
    [Parameter(Mandatory=$true)][string]$SteamVRPath,
    [string]$DriverPath,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
if (-not $DriverPath) {
    $DriverPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'out\driver_psvrpc\psvrpc'
}
$tool = Join-Path $SteamVRPath 'bin\win64\vrpathreg.exe'
if (-not (Test-Path -LiteralPath $tool -PathType Leaf)) { throw "vrpathreg missing: $tool" }
if (-not (Test-Path -LiteralPath $DriverPath -PathType Container)) { throw "Driver package missing: $DriverPath" }
$package = (Resolve-Path -LiteralPath $DriverPath).Path
$manifestPath = Join-Path $package 'driver.vrdrivermanifest'
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw "Manifest missing: $manifestPath" }
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.name -ne 'psvrpc') { throw 'Expected psvrpc driver package (name=psvrpc)' }
if ($Action -eq 'Install' -and -not (Test-Path -LiteralPath (Join-Path $package 'bin\win64\driver_psvrpc.dll') -PathType Leaf)) {
    throw 'Driver DLL missing; build first'
}
# Use OpenVR's path file for exact, idempotent registration checks. Never infer
# membership from a substring in localized vrpathreg console output.
$pathFile = Join-Path $env:LOCALAPPDATA 'openvr\openvrpaths.vrpath'
if (-not (Test-Path -LiteralPath $pathFile -PathType Leaf)) { throw "OpenVR path file missing: $pathFile; initialize SteamVR first" }
$paths = Get-Content -LiteralPath $pathFile -Raw | ConvertFrom-Json
# B14-08: voci null/vuote/whitespace mai passate a GetFullPath (match invariato).
$registered = @($paths.external_drivers | Where-Object {
    (-not [string]::IsNullOrWhiteSpace($_)) -and [string]::Equals(([IO.Path]::GetFullPath($_)).TrimEnd('\'), $package.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)
}).Count -gt 0
if ($Action -eq 'Inspect') {
    [pscustomobject]@{ driver = $package; registered = $registered; vrpathreg = $tool }
    return
}
if (($Action -eq 'Install' -and $registered) -or ($Action -eq 'Remove' -and -not $registered)) {
    "Already satisfied: $Action $package"
    return
}
$verb = if ($Action -eq 'Install') { 'adddriver' } else { 'removedriver' }
# DryRun only prints the command, like Inspect above: it changes nothing, so it
# must not be gated on live SteamVR process state.
if ($DryRun) { "DRY RUN: `"$tool`" $verb `"$package`""; return }
if (Get-Process -Name vrserver,vrcompositor -ErrorAction SilentlyContinue) { throw 'Close SteamVR before changing driver registration' }
& $tool $verb $package
if ($LASTEXITCODE -ne 0) { throw "vrpathreg $verb failed: exit $LASTEXITCODE" }
$after = Get-Content -LiteralPath $pathFile -Raw | ConvertFrom-Json
$found = @($after.external_drivers | Where-Object {
    (-not [string]::IsNullOrWhiteSpace($_)) -and [string]::Equals(([IO.Path]::GetFullPath($_)).TrimEnd('\'), $package.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)
}).Count -gt 0
if ($found -ne ($Action -eq 'Install')) { throw 'vrpathreg returned success but registration state does not match' }
"Verified: $Action $package"
