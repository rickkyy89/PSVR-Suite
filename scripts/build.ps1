[CmdletBinding()]
param(
    [ValidateSet('VS2022', 'MinGW')][string]$Toolchain = 'VS2022',
    [string]$ToolchainRoot,
    [ValidateSet('Debug', 'Release', 'RelWithDebInfo')][string]$Configuration = 'RelWithDebInfo'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $root 'CMakeLists.txt'))) { throw 'Invalid project root' }
if (-not [Environment]::Is64BitOperatingSystem) { throw 'Windows x64 is required' }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Git missing from PATH; install Git for Windows' }
$originalPath = $env:PATH
try {
    $configure = @('-S', $root)
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
        $configure += @('-B', $build, '-G', 'Ninja', "-DCMAKE_BUILD_TYPE=$Configuration",
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
        $configure += @('-B', $build, '-G', 'Visual Studio 17 2022', '-A', 'x64')
    }
    if (-not (Test-Path -LiteralPath $ctest)) { throw 'CTest missing from CMake installation' }
    & $cmake --version
    if ($LASTEXITCODE -ne 0) { throw 'CMake version check failed' }
    & $cmake @configure -DBUILD_TESTING=ON
    if ($LASTEXITCODE -ne 0) { throw 'CMake configuration failed (read error above)' }
    & $cmake --build $build --config $Configuration --parallel
    if ($LASTEXITCODE -ne 0) { throw 'Compilation failed (read error above)' }
    & $ctest --test-dir $build -C $Configuration --output-on-failure
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed; package not installed' }
    & $cmake --install $build --config $Configuration --prefix (Join-Path $root 'out\diag')
    if ($LASTEXITCODE -ne 0) { throw 'Packaging failed' }
    "Build complete: $(Join-Path $root 'out\diag\psvr_diag.exe')"
} finally {
    $env:PATH = $originalPath
}
