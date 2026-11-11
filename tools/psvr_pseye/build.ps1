# SPDX-License-Identifier: MIT
<#
.SYNOPSIS
    Build psvr_pseye.dll (MIT shim) and, by default, ps3eye.dll from the pin.

.DESCRIPTION
    Fetches the pinned sources if needed (via fetch_upstream.ps1), configures a
    local CMake build under tools/psvr_pseye/build, and stages the runtime into
    out/pseye. No driver is installed, replaced or rebound; no prebuilt binary
    is downloaded.

.PARAMETER ToolchainBin
    Directory containing cmake/g++/ninja (prepended to PATH).

.PARAMETER DepsDir
    Pinned source checkout directory (default tools/psvr_pseye/.deps).

.PARAMETER BuildDir
    CMake build directory (default tools/psvr_pseye/build).

.PARAMETER StageDir
    Runtime staging directory (default <repo>/out/pseye).

.PARAMETER LibusbDll
    libusb-1.0.dll to link ps3eye.dll against. Auto-detected if omitted.

.PARAMETER LibusbInclude
    Directory containing libusb.h. Taken from the fetched libusb if omitted.

.PARAMETER LibusbLib
    libusb-1.0 import library (.lib) for an MSVC build. MSVC cannot link a .dll
    directly. Auto-detected next to -LibusbDll if omitted.

.PARAMETER Generator
    CMake generator (defaults to Ninja when available).

.PARAMETER NoUpstream
    Build only the MIT shim; do not build ps3eye.dll.

.PARAMETER BuildLibusb
    Build pinned libusb with the libusb0 overlapped-length fix using MSVC 2022.
    Recommended for libusbK 3.0.7 userspace with the existing libusb0 driver.

.PARAMETER Clean
    Delete the build directory first.
#>
[CmdletBinding()]
param(
    [string]$ToolchainBin = $env:PSVR_TOOLCHAIN_BIN,
    [string]$DepsDir = (Join-Path $PSScriptRoot '.deps'),
    [string]$BuildDir = (Join-Path $PSScriptRoot 'build'),
    [string]$StageDir = '',
    [string]$LibusbDll = $env:PSVR_PSEYE_LIBUSB_DLL,
    [string]$LibusbInclude = '',
    [string]$LibusbLib = $env:PSVR_PSEYE_LIBUSB_LIB,
    [string]$Generator = '',
    [switch]$BuildLibusb,
    [switch]$NoUpstream,
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'

if ($ToolchainBin) {
    if (-not (Test-Path $ToolchainBin)) {
        throw "toolchain directory not found: $ToolchainBin"
    }
    $env:Path = "$ToolchainBin;$env:Path"
}

$cmake = (Get-Command cmake -ErrorAction SilentlyContinue).Source
if (-not $cmake) {
    throw "cmake not found; pass -ToolchainBin <dir> or add cmake to PATH"
}
$ninja = (Get-Command ninja -ErrorAction SilentlyContinue).Source

$upstreamDir = Join-Path $DepsDir 'PS3EYEDriver'
$libusbSrc = Join-Path $DepsDir 'libusb'

if (-not $NoUpstream -and (-not (Test-Path (Join-Path $upstreamDir 'src\ps3eye.cpp')) -or
        ($BuildLibusb -and -not (Test-Path (Join-Path $libusbSrc 'libusb\libusb.h'))))) {
    & (Join-Path $PSScriptRoot 'fetch_upstream.ps1') -DepsDir $DepsDir
}

if ($Clean -and (Test-Path $BuildDir)) {
    Remove-Item -Recurse -Force $BuildDir
}

if ($BuildLibusb) {
    if ($NoUpstream) { throw '-BuildLibusb requires an upstream build' }
    if ($LibusbDll -or $LibusbLib) { throw '-BuildLibusb cannot be combined with explicit libusb DLL/import library' }
    $builtUsb = & (Join-Path $PSScriptRoot 'build_libusb.ps1') -SourceDir $libusbSrc `
        -BuildDir (Join-Path $BuildDir 'patched-libusb') | Select-Object -Last 1
    $LibusbDll = $builtUsb.Dll
    $LibusbLib = $builtUsb.Lib
    $LibusbInclude = $builtUsb.Include
}

if (-not $NoUpstream) {
    if (-not $LibusbInclude) {
        $candidate = Join-Path $libusbSrc 'libusb'
        if (Test-Path (Join-Path $candidate 'libusb.h')) {
            $LibusbInclude = $candidate
        }
    }
    if (-not $LibusbDll) {
        foreach ($candidate in @(
            'C:\Program Files (x86)\Steam\libusb-1.0.dll',
            'C:\Program Files\FreeCAD 1.0\bin\libusb-1.0.dll'
        )) {
            if (Test-Path $candidate) { $LibusbDll = $candidate; break }
        }
    }
    if (-not $LibusbDll) {
        throw "libusb-1.0.dll not found; pass -LibusbDll <path> or -NoUpstream"
    }
    if (-not $LibusbInclude) {
        throw "libusb.h not found; pass -LibusbInclude <dir> or -NoUpstream"
    }
}

$configure = @('-S', $PSScriptRoot, '-B', $BuildDir)
if ($Generator) {
    $configure += @('-G', $Generator)
} elseif ($ninja) {
    $configure += @('-G', 'Ninja')
}
if ($Generator -notlike 'Visual Studio*') {
    $configure += @('-DCMAKE_BUILD_TYPE=Release')
}
$configure += "-DPSVR_PSEYE_BUILD_UPSTREAM=$(if ($NoUpstream) { 'OFF' } else { 'ON' })"
if (Test-Path $upstreamDir) {
    $configure += "-DFETCHCONTENT_SOURCE_DIR_PS3EYEDRIVER=$upstreamDir"
}
if ($LibusbDll) { $configure += "-DPSVR_PSEYE_LIBUSB_DLL=$LibusbDll" }
if ($LibusbInclude) { $configure += "-DPSVR_PSEYE_LIBUSB_INCLUDE_DIR=$LibusbInclude" }
if ($LibusbLib) { $configure += "-DPSVR_PSEYE_LIBUSB_LIB=$LibusbLib" }
if ($BuildLibusb) {
    $configure += "-DPSVR_PSEYE_PATCHED_LIBUSB_SOURCE=$LibusbInclude"
} else {
    # Do not keep testing an old patched source while staging an external DLL.
    $configure += '-DPSVR_PSEYE_PATCHED_LIBUSB_SOURCE='
}
if ($StageDir) { $configure += "-DPSVR_PSEYE_STAGE=$StageDir" }

& $cmake @configure
if ($LASTEXITCODE -ne 0) { throw "cmake configure failed" }

& $cmake --build $BuildDir --config Release
if ($LASTEXITCODE -ne 0) { throw "cmake build failed" }

$stage = if ($StageDir) { $StageDir } else { Join-Path (Resolve-Path (Join-Path $PSScriptRoot '..\..')) 'out\pseye' }
Write-Host ""
Write-Host "staged: $stage"
Get-ChildItem $stage | Select-Object Name, Length | Format-Table -AutoSize
