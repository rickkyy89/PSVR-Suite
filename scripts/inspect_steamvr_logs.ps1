[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$SteamPath,
    [ValidateRange(1,100000)][int]$Tail = 1000
)
$ErrorActionPreference = 'Stop'
$logs = Join-Path $SteamPath 'logs'
foreach ($name in @('vrserver.txt','vrcompositor.txt')) {
    $file = Join-Path $logs $name
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        [pscustomobject]@{ file = $file; status = 'missing'; text = $null }
        continue
    }
    Get-Content -LiteralPath $file -Tail $Tail | Where-Object { $_ -match '(?i)psvrpc|driver_psvrpc|PSVRPC-HMD|error|failed' } | ForEach-Object {
        [pscustomobject]@{ file = $file; status = 'match'; text = $_ }
    }
}
