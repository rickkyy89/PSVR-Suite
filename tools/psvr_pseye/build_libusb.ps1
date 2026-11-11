# SPDX-License-Identifier: MIT
<# Build pinned, patched userspace libusb. No driver install or system DLL writes.
   Source checkout is copied into BuildDir; the fetched checkout stays untouched.
   Requires the existing MSVC 2022 Build Tools. #>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$SourceDir,
    [string]$BuildDir = (Join-Path $PSScriptRoot '..\..\build\pseye_libusb'),
    [string]$MSBuild = '',
    [string]$StageDir = ''
)
$ErrorActionPreference = 'Stop'
$pin = 'd52e355daa09f17ce64819122cb067b8a2ee0d4b'
$head = & git -C $SourceDir rev-parse HEAD
if ($LASTEXITCODE -ne 0 -or "$head".Trim() -ne $pin) { throw 'libusb source pin mismatch' }
$dirty = & git -C $SourceDir status --porcelain -- libusb msvc
if ($LASTEXITCODE -ne 0 -or $dirty) { throw 'libusb input source must be clean' }
if (-not $MSBuild) {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    $MSBuild = & $vswhere -latest -products '*' -requires Microsoft.Component.MSBuild -find 'MSBuild\**\Bin\MSBuild.exe' | Select-Object -First 1
}
if (-not $MSBuild -or -not (Test-Path $MSBuild)) { throw 'MSBuild not found' }
$BuildDir = [IO.Path]::GetFullPath($BuildDir)
$source = [IO.Path]::GetFullPath($SourceDir)
if ($BuildDir -eq $source -or $BuildDir.StartsWith($source + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'BuildDir must be outside the fetched source checkout'
}
$work = Join-Path $BuildDir 'source'
if ($work -eq $source -or
        $work.StartsWith($source + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or
        $source.StartsWith($work + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Generated work tree and fetched source must be disjoint'
}
New-Item -ItemType Directory -Path $work -Force | Out-Null
foreach ($dir in @('libusb', 'msvc')) {
    Copy-Item (Join-Path $source $dir) $work -Recurse -Force
}
$patch = Join-Path $PSScriptRoot 'patches\libusb-0001-control-overlapped-length.patch'
& git -C $work apply --whitespace=nowarn $patch
if ($LASTEXITCODE -ne 0) { throw 'libusb patch failed' }
$previousCl = $env:_CL_
try {
    # The pinned source's enum expression warns on MSVC 14.44. Keep the workaround
    # scoped to this process/build, not project or global compiler settings.
    $env:_CL_ = "$previousCl /wd5287"
    & $MSBuild (Join-Path $work 'msvc\libusb_dll.vcxproj') /nologo /m:4 `
        /p:Configuration=Release /p:Platform=x64 /p:PlatformToolset=v143 /p:WindowsTargetPlatformVersion=10.0
    if ($LASTEXITCODE -ne 0) { throw 'libusb build failed' }
} finally { $env:_CL_ = $previousCl }
$runtime = Join-Path $work 'build\v143\x64\Release\dll'
if ($StageDir) {
    New-Item -ItemType Directory -Path $StageDir -Force | Out-Null
    Copy-Item (Join-Path $runtime 'libusb-1.0.dll') $StageDir
    Copy-Item $patch $StageDir
}
[pscustomobject]@{
    Dll = Join-Path $runtime 'libusb-1.0.dll'
    Lib = Join-Path $runtime 'libusb-1.0.lib'
    Include = Join-Path $work 'libusb'
    SourcePin = $pin
}
