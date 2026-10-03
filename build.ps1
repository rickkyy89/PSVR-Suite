[CmdletBinding()]
param(
    [ValidateSet('VS2022', 'MinGW')][string]$Toolchain = 'VS2022',
    [string]$ToolchainRoot,
    [ValidateSet('Debug', 'Release', 'RelWithDebInfo')][string]$Configuration = 'RelWithDebInfo'
)
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'scripts\build.ps1') -Toolchain $Toolchain -ToolchainRoot $ToolchainRoot -Configuration $Configuration
