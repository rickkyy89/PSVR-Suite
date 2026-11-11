# Installa il collegamento Start Menu utente "PSVR Suite.lnk" (avvio senza console).
[CmdletBinding()]
param(
    [switch]$Force
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$pythonw = Join-Path $repo '.venv-camera\Scripts\pythonw.exe'
$launcher = Join-Path $repo 'tools\psvr_suite\psvr_suite_launcher.py'
$icon = Join-Path $repo 'tools\psvr_suite\assets\psvr_suite.ico'
foreach ($p in @($pythonw, $launcher, $icon)) {
    if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { throw "File richiesto mancante: $p" }
}
$programs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
if (-not (Test-Path -LiteralPath $programs)) { throw "Cartella Start Programs non trovata: $programs" }
$link = Join-Path $programs 'PSVR Suite.lnk'
if ((Test-Path -LiteralPath $link) -and (-not $Force)) { throw "Collegamento esistente: $link (usare -Force per sovrascrivere)" }
$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($link)
$sc.TargetPath = $pythonw
$sc.Arguments = "`"$launcher`""
$sc.WorkingDirectory = $repo
$sc.IconLocation = $icon
$sc.Description = 'PSVR Suite - GUI persistente (senza avvio VR automatico)'
$sc.WindowStyle = 1
$sc.Save()
'Collegamento creato: ' + $link
'TargetPath: ' + $sc.TargetPath
'Arguments: ' + $sc.Arguments
'WorkingDirectory: ' + $sc.WorkingDirectory
'IconLocation: ' + $sc.IconLocation
