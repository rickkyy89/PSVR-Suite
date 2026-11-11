param(
    [string]$PythonExe = '',
    [ValidateRange(5, 300)][int]$BuildTimeoutSeconds = 120
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if (-not $PythonExe) { $PythonExe = Join-Path $root '.venv-camera\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw 'Provide an existing camera-environment Python; no dependencies will be installed.'
}
$PythonExe = (Resolve-Path -LiteralPath $PythonExe).Path
$approved = 'C:\Users\rickk\AppData\Local\Temp\opencode'
if (-not (Test-Path -LiteralPath $approved -PathType Container)) { throw 'Approved temp directory missing' }
$build = Join-Path $approved ('psvr-ps4-dshow-offline-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
New-Item -ItemType Directory -Path $build | Out-Null
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (-not (Test-Path -LiteralPath $vswhere)) { throw 'Existing Visual Studio Build Tools required; no install attempted' }
$vs = (& $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath | Select-Object -First 1)
if (-not $vs) { throw 'Existing MSVC x64 toolchain missing' }
$cmake = Join-Path $vs 'Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe'
if (-not (Test-Path -LiteralPath $cmake)) {
    $cmake = (Get-Command cmake -ErrorAction Stop).Source
}

function Invoke-BoundedOffline {
    param([string]$Name, [string]$Program, [string[]]$Arguments, [int]$Seconds)
    $stdout = Join-Path $build ($Name + '.out.log')
    $stderr = Join-Path $build ($Name + '.err.log')
    # Own the Process created by Process.Start: Start-Process -PassThru can
    # return an object with a null ExitCode on Windows PowerShell even after
    # WaitForExit. Never coerce that null to zero (or mistake it for exit 1).
    # cmd only provides native file redirection: no async managed pipe pumps,
    # output decoding, or unbounded second WaitForExit() to drain streams.
    # /d disables AutoRun; /v:off disables delayed environment expansion.
    $allArguments = @($Program, $stdout, $stderr)
    if ($Arguments.Count -gt 0) { $allArguments += $Arguments }
    foreach ($argument in $allArguments) {
        if ($null -eq $argument -or $argument -match '["%\r\n]') {
            throw 'Runner arguments must not contain quotes, percent expansion or newlines'
        }
    }
    $argumentLine = ''
    if ($Arguments.Count -gt 0) {
        $argumentLine = ($Arguments | ForEach-Object { '"' + $_ + '"' }) -join ' '
    }
    $nativeCommand = '"' + $Program + '"'
    if ($argumentLine) { $nativeCommand += ' ' + $argumentLine }
    $nativeCommand += ' 1>"' + $stdout + '" 2>"' + $stderr + '"'
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $env:ComSpec
    $startInfo.Arguments = '/d /v:off /s /c "' + $nativeCommand + '"'
    $startInfo.WorkingDirectory = $root
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    # Preserve raw child bytes in the logs. Python emits UTF-8; English VS
    # diagnostics avoid the usual OEM/ANSI mismatch. No text determines status.
    $startInfo.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
    $startInfo.EnvironmentVariables['VSLANG'] = '1033'
    # Offline children must never inherit a later live opt-in from the parent.
    # This changes only child environment, never preferences or parent gates.
    $startInfo.EnvironmentVariables['PSVR_PS4_DSHOW_ENABLE'] = '0'
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) { throw "$Name could not start" }
        # This is the original CreateProcess handle, not a later PID lookup.
        $null = $process.Handle
        if (-not $process.WaitForExit($Seconds * 1000)) {
            # Never forcekill. cmd/child keep their direct log-file handles;
            # disposing our observation handle does not terminate either one.
            throw "$Name deadline expired; wrapper PID $($process.Id) / child not killed. Stop workflow; logs: $build"
        }
        $exitCode = $process.ExitCode
        if ($null -eq $exitCode) { throw "$Name exited but its exit code is unavailable; refusing later stages" }
    } finally {
        $process.Dispose()
    }
    Write-Output ($Name + ': exit=' + $exitCode)
    Get-Content -LiteralPath $stdout -Encoding UTF8 -Tail 12
    Get-Content -LiteralPath $stderr -Encoding UTF8 -Tail 12
    if ($exitCode -ne 0) { throw "$Name failed (exit $exitCode); full bounded-run logs: $build" }
}

Write-Output ('Offline artifacts only: ' + $build)
Invoke-BoundedOffline 'configure' $cmake @('-S', $PSScriptRoot, '-B', $build,
    '-G', 'Visual Studio 17 2022', '-A', 'x64', '-DPSVR_DSHOW_OFFLINE_TESTS=ON') $BuildTimeoutSeconds
Invoke-BoundedOffline 'build' $cmake @('--build', $build, '--config', 'Release', '--parallel', '2') $BuildTimeoutSeconds
Invoke-BoundedOffline 'native-fixtures' (Join-Path $build 'Release\psvr_ps4_dshow_offline.exe') @() 15
Invoke-BoundedOffline 'python-fixtures' $PythonExe @('-m', 'unittest', '-v',
    'tests.test_camera_ps4_owned_dshow', 'tests.test_camera_ps4_owned_transactions',
    'tests.test_camera_ps4_owned_wiring', 'tests.test_camera_ps4_owned_readiness',
    'tests.test_camera_ps4_owned_completion', 'tests.test_camera_ps4_owned_admission',
    'tests.test_camera_ps4_owned_diagnostics', 'tests.test_camera_ps4_pixel_parity') 60
Write-Output ('PoC DLL built but NOT loaded/deployed/registered: ' + (Join-Path $build 'Release\psvr_ps4_dshow.dll'))
Write-Output 'Offline fixtures are not end-to-end hardware proof. No defaults changed.'
