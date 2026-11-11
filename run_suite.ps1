[CmdletBinding()]
param([switch]$StartVR,[switch]$Attach,[switch]$Check)
$ErrorActionPreference='Stop'
$python=Join-Path $PSScriptRoot '.venv-camera\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Camera virtualenv missing.' }
$arguments=@('-m','tools.psvr_suite','--root',$PSScriptRoot)
if ($StartVR) { $arguments+='--start-vr' }
if ($Attach) { $arguments+='--attach' }
if ($Check) { $arguments+='--headless-check' }
$old=$env:PYTHONPATH
try {
    $env:PYTHONPATH=$PSScriptRoot
    & $python @arguments
    if ($LASTEXITCODE -ne 0) { throw "Suite exited with code $LASTEXITCODE" }
} finally { $env:PYTHONPATH=$old }
