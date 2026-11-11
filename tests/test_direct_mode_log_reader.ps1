# SPDX-License-Identifier: MIT
# Isolated synthetic logs; no SteamVR, GPU or registry access.
$ErrorActionPreference='Stop'
. (Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\direct_mode_log_reader.ps1')
$parent=Join-Path $env:TEMP 'opencode'
if (-not (Test-Path -LiteralPath $parent -PathType Container)) { throw 'Test temp parent missing' }
$temp=Join-Path $parent ('direct-log-test-'+[Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($temp) | Out-Null
try {
    $path=Join-Path $temp 'vrcompositor.txt'
    [IO.File]::WriteAllText($path,"Headset display is on desktop (historical)`n")
    $state=New-DirectModeLogReader $temp
    if (@(Read-DirectModeEvidence $state).Count) { throw 'Historical lines must not be replayed' }
    $line="Failed to start compositor: prova "+[char]0xe0+"`n"
    $bytes=[Text.Encoding]::UTF8.GetBytes($line)
    $file=[IO.File]::Open($path,[IO.FileMode]::Append)
    try { $file.Write($bytes,0,$bytes.Length-2) } finally { $file.Dispose() }
    if (@(Read-DirectModeEvidence $state).Count) { throw 'Incomplete UTF8 line emitted too early' }
    $file=[IO.File]::Open($path,[IO.FileMode]::Append)
    try { $file.Write($bytes,$bytes.Length-2,2) } finally { $file.Dispose() }
    $result=@(Read-DirectModeEvidence $state)
    if ($result.Count -ne 1 -or $result[0].line -cne $line.TrimEnd()) { throw 'Split UTF8/error line lost or corrupted' }
    if (@(Read-DirectModeEvidence $state).Count) { throw 'Completed line duplicated' }
    [IO.File]::WriteAllText($path,"Invalid adapter output specified! (-1)`n")
    $result=@(Read-DirectModeEvidence $state)
    if ($result.Count -ne 1 -or $result[0].line -notmatch 'Invalid adapter output') { throw 'Truncation/error evidence missed' }
    'PASS synthetic historical/split-UTF8/error/deduplication/truncation log checks'
} finally { [IO.Directory]::Delete($temp,$true) }
