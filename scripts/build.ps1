[CmdletBinding()]
param(
    [ValidateSet('VS2022', 'MinGW')][string]$Toolchain = 'VS2022',
    [string]$ToolchainRoot,
    [string]$DriverPackageRoot,
    [ValidateSet('Debug', 'Release', 'RelWithDebInfo')][string]$Configuration = 'RelWithDebInfo'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $root 'CMakeLists.txt'))) { throw 'Invalid project root' }
if (-not [Environment]::Is64BitOperatingSystem) { throw 'Windows x64 is required' }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Git missing from PATH; install Git for Windows' }
$originalPath = $env:PATH
$pushedLocation = $false
try {
    # B14-04: configure unica via `--preset` (stesso comando usato dalla CI).
    # Autorita' dei default effettivi, identici ai flag manuali precedenti:
    #  generator/arch/binaryDir/staging MSVC dal preset `vs2022`;
    #  generator/binaryDir/build-type/staging MinGW dal preset `mingw`;
    #  fallback staging MinGW da CMakeLists.txt (PSVR_DRIVER_PACKAGE) per
    #  configure dirette senza preset.
    # build.ps1 non duplica piu' tali letterali: aggiunge -D solo per cio' che
    # il preset non copre (compilatori/build-type MinGW, BUILD_TESTING,
    # staging esplicito -DriverPackageRoot).
    # Caveat: passare da -DriverPackageRoot esplicito al default richiede una
    # build dir pulita, altrimenti la cache CMake conserva il valore precedente.
    $presetName = if ($Toolchain -eq 'MinGW') { 'mingw' } else { 'vs2022' }
    if ($Toolchain -eq 'MinGW') {
        if (-not $ToolchainRoot) { throw 'Pass -ToolchainRoot pointing to extracted w64devkit x64' }
        foreach ($name in @('cmake.exe', 'ninja.exe', 'gcc.exe', 'g++.exe')) {
            if (-not (Test-Path -LiteralPath (Join-Path $ToolchainRoot "bin\$name"))) { throw "Toolchain missing bin\$name" }
        }
        $bin = Join-Path $ToolchainRoot 'bin'
        $env:PATH = "$bin;$originalPath"
        $cmake = Join-Path $bin 'cmake.exe'
        $ctest = Join-Path $bin 'ctest.exe'
        $build = Join-Path $root 'build\mingw'
        # Deve restare uguale a binaryDir del preset `mingw`; usato solo per
        # build/test/install (--preset copre gia' il configure).
        $presetArgs = @('--preset', $presetName, "-DCMAKE_BUILD_TYPE=$Configuration",
            "-DCMAKE_C_COMPILER=$(Join-Path $bin 'gcc.exe')", "-DCMAKE_CXX_COMPILER=$(Join-Path $bin 'g++.exe')")
    } else {
        $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
        if (-not (Test-Path -LiteralPath $vswhere)) { throw 'VS 2022 not found. Install Desktop development with C++ (see BUILD.md)' }
        $vs = & $vswhere -latest -version '[17.0,18.0)' -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
        if ($LASTEXITCODE -ne 0 -or -not $vs) { throw 'VS 2022 C++ workload not found' }
        $command = Get-Command cmake -ErrorAction SilentlyContinue
        if ($command) { $cmake = $command.Source }
        else { $cmake = Join-Path $vs 'Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe' }
        if (-not (Test-Path -LiteralPath $cmake)) { throw 'CMake missing; install CMake tools for Windows or CMake >=3.24' }
        $ctest = Join-Path (Split-Path -Parent $cmake) 'ctest.exe'
        $build = Join-Path $root 'build\vs2022'
        # Deve restare uguale a binaryDir del preset `vs2022` (vedi sopra).
        $presetArgs = @('--preset', $presetName)
    }
    if (-not (Test-Path -LiteralPath $ctest)) { throw 'CTest missing from CMake installation' }
    $presetArgs += '-DBUILD_TESTING=ON'
    if ($DriverPackageRoot) {
        $presetArgs += "-DPSVR_DRIVER_PACKAGE=$DriverPackageRoot"
    }
    # `cmake --preset` legge CMakePresets.json dalla source dir: CWD=root obbligatorio
    # (-S/-B/-G/-A sono incompatibili con --preset).
    Push-Location -LiteralPath $root
    $pushedLocation = $true
    & $cmake --version
    if ($LASTEXITCODE -ne 0) { throw 'CMake version check failed' }
    # B14-06: cmake minimo 3.24, come cmake_minimum_required in CMakeLists.txt.
    $cmakeVersionLine = (& $cmake --version | Select-Object -First 1)
    $cmakeVersionMatch = [regex]::Match($cmakeVersionLine, 'cmake version (\d+)\.(\d+)')
    if (-not $cmakeVersionMatch.Success) { throw "Cannot parse CMake version from: $cmakeVersionLine" }
    if ([int]$cmakeVersionMatch.Groups[1].Value -lt 3 -or (([int]$cmakeVersionMatch.Groups[1].Value -eq 3) -and ([int]$cmakeVersionMatch.Groups[2].Value -lt 24))) { throw "CMake >=3.24 required (found: $cmakeVersionLine)" }
    & $cmake @presetArgs
    if ($LASTEXITCODE -ne 0) { throw 'CMake configuration failed (read error above)' }
    Pop-Location
    $pushedLocation = $false
    & $cmake --build $build --config $Configuration --parallel
    if ($LASTEXITCODE -ne 0) { throw 'Compilation failed (read error above)' }
    & $ctest --test-dir $build -C $Configuration --output-on-failure --timeout 300
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed; package not installed' }
    & $cmake --install $build --config $Configuration --prefix (Join-Path $root 'out\diag')
    if ($LASTEXITCODE -ne 0) { throw 'Packaging failed' }
    "Build complete: $(Join-Path $root 'out\diag\psvr_diag.exe')"
    "Driver package: $(if ($DriverPackageRoot) { $DriverPackageRoot } else { '(default preset/CMake, vedi commento B14-04 sopra)' })"
} finally {
    if ($pushedLocation) { Pop-Location }
    $env:PATH = $originalPath
}
