# SPDX-License-Identifier: MIT
# Incremental log evidence reader. Pure file observation; no runtime operations.
function New-DirectModeLogReader([string]$LogsPath) {
    $state=[pscustomobject]@{path=$LogsPath;offsets=@{};pending=@{}}
    foreach ($name in @('vrserver.txt','vrcompositor.txt','vrmonitor.txt')) {
        $path=Join-Path $LogsPath $name
        $state.offsets[$name]=if (Test-Path -LiteralPath $path) { (Get-Item -LiteralPath $path).Length } else { 0L }
        $state.pending[$name]=[byte[]]@()
    }
    return $state
}
function Read-DirectModeEvidence($State) {
    $evidence=@()
    foreach ($name in @('vrserver.txt','vrcompositor.txt','vrmonitor.txt')) {
        $path=Join-Path $State.path $name
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $stream=[IO.File]::Open($path,[IO.FileMode]::Open,[IO.FileAccess]::Read,([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
        try {
            if ($stream.Length -lt $State.offsets[$name]) { $State.offsets[$name]=0L; $State.pending[$name]=[byte[]]@() }
            $length=$stream.Length-$State.offsets[$name]
            if ($length -gt 16777216) { throw 'Log delta exceeds capture limit; start a new capture' }
            $stream.Position=$State.offsets[$name]
            $chunk=New-Object byte[] ([int]$length)
            $read=0
            while ($read -lt $chunk.Length) {
                $n=$stream.Read($chunk,$read,$chunk.Length-$read)
                if ($n -eq 0) { break }
                $read+=$n
            }
            $State.offsets[$name]=$stream.Position
            [byte[]]$bytes=if ($read) { $State.pending[$name]+$chunk[0..($read-1)] } else { $State.pending[$name] }
            $last=-1
            for ($i=$bytes.Length-1;$i -ge 0;$i--) { if ($bytes[$i] -eq 10) { $last=$i; break } }
            if ($last -ge 0) {
                $text=[Text.Encoding]::UTF8.GetString($bytes,0,$last+1)
                foreach ($line in ($text -split "`n")) {
                    if ($line -match 'Enabling direct mode|set display visibility|Headset display|Failed.*direct|DirectMode.*[Ee]rror|Failed to init graphics|Failed to start compositor|Invalid adapter output|Updated HMD Prop_DisplayFrequency|psvrpc:.*(IMU state=|capture invalid|native direct display|desktop property)|Status Alert') {
                        $evidence += [pscustomobject]@{file=$name;line=$line.TrimEnd()}
                    }
                }
            }
            $State.pending[$name]=if ($last+1 -lt $bytes.Length) { [byte[]]$bytes[($last+1)..($bytes.Length-1)] } else { [byte[]]@() }
            if ($State.pending[$name].Length -gt 65536) { throw 'Unterminated log line exceeds limit' }
        } finally { $stream.Dispose() }
    }
    return $evidence
}
