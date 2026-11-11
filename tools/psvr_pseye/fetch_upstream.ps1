# SPDX-License-Identifier: MIT
<#
.SYNOPSIS
    Fetch the pinned PS3 Eye capture sources into an isolated deps directory.

.DESCRIPTION
    Clones inspirit/PS3EYEDriver (MIT + GPLv2) and libusb/libusb (LGPL-2.1)
    at exact commits. Nothing is installed system-wide; everything lands under
    -DepsDir (default tools/psvr_pseye/.deps, git-ignored).

.PARAMETER DepsDir
    Destination for the checkouts.
.PARAMETER Force
    Delete and re-clone existing checkouts.
#>
[CmdletBinding()]
param(
    [string]$DepsDir = (Join-Path $PSScriptRoot '.deps'),
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git is required to fetch the pinned sources"
}

$pins = @(
    @{
        Name   = 'PS3EYEDriver'
        Url    = 'https://github.com/inspirit/PS3EYEDriver.git'
        Commit = 'fe03494e648d0bf2aaa77869e5b50965e2e23620'
    },
    @{
        Name   = 'libusb'
        Url    = 'https://github.com/libusb/libusb.git'
        Commit = 'd52e355daa09f17ce64819122cb067b8a2ee0d4b'
    }
)

New-Item -ItemType Directory -Force -Path $DepsDir | Out-Null

foreach ($pin in $pins) {
    $dir = Join-Path $DepsDir $pin.Name
    if ($Force -and (Test-Path $dir)) {
        Remove-Item -Recurse -Force $dir
    }
    if (-not (Test-Path (Join-Path $dir '.git'))) {
        Write-Host "clone $($pin.Url) -> $dir"
        git clone --quiet $pin.Url $dir
        if ($LASTEXITCODE -ne 0) { throw "git clone failed for $($pin.Name)" }
    }
    git -C $dir checkout --quiet $pin.Commit 2>$null
    if ($LASTEXITCODE -ne 0) {
        git -C $dir fetch --quiet origin
        git -C $dir checkout --quiet $pin.Commit
    }
    $head = (git -C $dir rev-parse HEAD).Trim()
    if ($head -ne $pin.Commit) {
        throw "pin mismatch for $($pin.Name): got $head want $($pin.Commit)"
    }
    Write-Host "$($pin.Name) @ $head"
}

Write-Host "pinned deps ready in $DepsDir"
