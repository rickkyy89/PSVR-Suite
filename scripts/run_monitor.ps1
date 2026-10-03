param(
    [switch]$StartImu,
    [ValidateRange(0, 180000)][int]$ImuDurationMs = 0,
    [switch]$StationaryTest
)
$ErrorActionPreference = 'Stop'
if (($StationaryTest -or $ImuDurationMs -ne 0) -and -not $StartImu) {
    throw '-StationaryTest / -ImuDurationMs richiedono -StartImu.'
}
if ($StationaryTest -and $ImuDurationMs -eq 0) {
    throw '-StationaryTest richiede durata finita, non 0.'
}
$root = Split-Path -Parent $PSScriptRoot
$app = Join-Path $root 'tools\psvr_monitor\monitor.py'
$exe = Join-Path $root 'out\diag\psvr_diag.exe'
$config = Join-Path $root 'config.json'
foreach ($file in @($app, $exe, $config)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        throw "File necessario non trovato: $file. Compilare psvr_diag prima di avviare il monitor."
    }
}
$launcher = Get-Command py.exe -ErrorAction SilentlyContinue
if (-not $launcher) { throw 'Python Launcher non trovato. Installare Python 3.11 con Tkinter e launcher py.' }
$pythonOutput = & $launcher.Source -3.11 -c 'import sys, tkinter; assert tkinter.TkVersion >= 8.6; print(sys.executable)'
if ($LASTEXITCODE -ne 0 -or -not $pythonOutput) { throw 'Python 3.11 / Tkinter 8.6 non disponibile.' }
$python = ([string]$pythonOutput).Trim()
if ($StationaryTest) {
    # Same validator as direct CLI: calibration_ms + startup_discard_ms +
    # 30000 ms measurement + 1000 ms margin. Default minimum 39200; use 42000.
    # Importing via runpy with another run_name never starts GUI, API or USB.
    $validationCode = @'
import runpy, sys
m = runpy.run_path(sys.argv[1], run_name='monitor_validation')
try:
    m['validate_imu_options'](int(sys.argv[3]), True, m['Path'](sys.argv[2]))
except ValueError as exc:
    print(str(exc))
    raise SystemExit(2)
'@
    $validationOutput = & $python -B -c $validationCode $app $root $ImuDurationMs
    if ($LASTEXITCODE -ne 0) { throw "Parametri test statico non validi: $validationOutput" }
}
$pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) { throw "pythonw.exe non trovato: $pythonw" }
# Quote arguments explicitly: Start-Process joins ArgumentList into one Windows command line.
$arguments = '"{0}" --root "{1}"' -f $app, $root
if ($StartImu) {
    $arguments += ' --start-imu --imu-duration-ms "{0}"' -f $ImuDurationMs
}
if ($StationaryTest) { $arguments += ' --stationary-test' }
Start-Process -FilePath $pythonw -ArgumentList $arguments -WorkingDirectory $root
