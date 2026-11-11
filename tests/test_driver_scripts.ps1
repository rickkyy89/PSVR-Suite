# SPDX-License-Identifier: MIT
<#
.SYNOPSIS
    Hardware-free tests for scripts/driver_registration.ps1 and scripts/inspect_steamvr_logs.ps1.
.DESCRIPTION
    Everything runs against throwaway fixtures under %TEMP%\opencode:
    vrpathreg.exe is a fake plain file (never executed), openvrpaths.vrpath lives in
    an isolated LOCALAPPDATA that is saved and restored around the run, and Steam log
    files are synthetic text. No SteamVR process, no GUI, no hardware, no real
    driver registration is read from or written to the machine.
    Usage: powershell -ExecutionPolicy Bypass -File tests\test_driver_scripts.ps1
    Exit 0 when all cases pass, 1 otherwise.
#>
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$regScript = Join-Path $root 'scripts\driver_registration.ps1'
$logScript = Join-Path $root 'scripts\inspect_steamvr_logs.ps1'

# --- fixture layout (all under a unique temp dir, deleted at the end) -----------------
$fx = Join-Path $env:TEMP ('opencode\psvr_driver_scripts_tests_' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
$steamvr = Join-Path $fx 'steamvr'                       # fake SteamVR root
$fakeTool = Join-Path $steamvr 'bin\win64\vrpathreg.exe' # plain file, never a real binary
$package = Join-Path $fx 'packages\psvrpc'               # valid driver package
$noManifest = Join-Path $fx 'packages\nomanifest'
$badName = Join-Path $fx 'packages\badname'
$noDll = Join-Path $fx 'packages\nodll'
$la = Join-Path $fx 'localappdata'                       # isolated LOCALAPPDATA
$laEmpty = Join-Path $fx 'localappdata_empty'            # no openvr subfolder
$pathFile = Join-Path $la 'openvr\openvrpaths.vrpath'
$steam = Join-Path $fx 'steam'                           # Steam\logs for the inspector

$script:Failed = 0
function Invoke-TestCase {
    param([string]$Name, [scriptblock]$Body)
    try { & $Body; Write-Host "PASS $Name" }
    catch { $script:Failed++; Write-Host "FAIL $Name :: $($_.Exception.Message)" }
}
function Assert-Equal {
    param($Expected, $Actual, [string]$What = 'value')
    if ($Expected -ne $Actual) { throw "$What - expected '$Expected', got '$Actual'" }
}
function Assert-Match {
    param([string]$Actual, [string]$Pattern, [string]$What = 'output')
    if ($Actual -notmatch $Pattern) { throw "$What '$Actual' does not match /$Pattern/" }
}
function Assert-Throws {
    param([scriptblock]$Body, [string]$Pattern)
    $msg = $null
    try { & $Body | Out-Null } catch { $msg = $_.Exception.Message }
    if (-not $msg) { throw "expected an error matching /$Pattern/ but none was raised" }
    if ($msg -notmatch $Pattern) { throw "error '$msg' does not match /$Pattern/" }
}
function Write-Text { param([string]$Path, [string]$Content)
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    Set-Content -LiteralPath $Path -Value $Content
}
function Write-Manifest { param([string]$Dir, [string]$Name)
    $json = [pscustomobject]@{ name = $Name; version = 1; alwaysActivate = $false } | ConvertTo-Json
    Write-Text (Join-Path $Dir 'driver.vrdrivermanifest') $json
}
function Write-VrPathFile { param([string[]]$ExternalDrivers)
    # Realistic shape: the script only reads external_drivers. Two entries keep
    # ConvertTo-Json from flattening single-element arrays and exercise filtering.
    $json = [pscustomobject]@{
        config           = @('C:\Program Files (x86)\Steam\config')
        external_drivers = @($ExternalDrivers + 'C:\Other\driver')
    } | ConvertTo-Json
    Write-Text $pathFile $json
}
function Invoke-Registration {
    param([string]$Action, [string]$SteamVRPath, [string]$DriverPath, [switch]$DryRun)
    $a = @{ Action = $Action; SteamVRPath = $SteamVRPath; DriverPath = $DriverPath }
    if ($DryRun) { $a['DryRun'] = $true }
    & $regScript @a
}

# --- environment isolation -------------------------------------------------------------
$realLocalAppData = $env:LOCALAPPDATA
$realPathFile = Join-Path $realLocalAppData 'openvr\openvrpaths.vrpath'
$realPathFileExisted = Test-Path -LiteralPath $realPathFile -PathType Leaf
$realPathFileSnapshot = if ($realPathFileExisted) { Get-Content -LiteralPath $realPathFile -Raw } else { $null }

try {
    New-Item -ItemType Directory -Force -Path $fx | Out-Null
    Write-Text $fakeTool 'FAKE vrpathreg plain file - must never be executed by the tests'
    Write-Manifest $package 'psvrpc'
    Write-Text (Join-Path $package 'bin\win64\driver_psvrpc.dll') 'FAKE driver dll'
    New-Item -ItemType Directory -Force -Path $noManifest | Out-Null
    Write-Manifest $badName 'not-psvrpc'
    Write-Manifest $noDll 'psvrpc'
    New-Item -ItemType Directory -Force -Path $laEmpty | Out-Null
    $env:LOCALAPPDATA = $la
    Write-VrPathFile -ExternalDrivers @()

    # --- AST parsing: both scripts must parse cleanly with the documented parameters ---
    Invoke-TestCase 'ast/driver_registration.ps1 parses with expected params' {
        $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile($regScript, [ref]$null, [ref]$errors)
        if ($errors.Count -ne 0) { throw "parse errors: $($errors[0].Message)" }
        $params = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.ParameterAst] }, $true) |
                  ForEach-Object { $_.Name.VariablePath.UserPath }
        foreach ($p in 'Action', 'SteamVRPath', 'DriverPath', 'DryRun') {
            if ($params -notcontains $p) { throw "param $p not found (got: $($params -join ','))" }
        }
    }
    Invoke-TestCase 'ast/inspect_steamvr_logs.ps1 parses with expected params' {
        $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile($logScript, [ref]$null, [ref]$errors)
        if ($errors.Count -ne 0) { throw "parse errors: $($errors[0].Message)" }
        $params = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.ParameterAst] }, $true) |
                  ForEach-Object { $_.Name.VariablePath.UserPath }
        foreach ($p in 'SteamPath', 'Tail') {
            if ($params -notcontains $p) { throw "param $p not found (got: $($params -join ','))" }
        }
    }

    # --- inspect_steamvr_logs.ps1: filtering, tailing, missing files ---------------------
    $logMatchLines = @(
        '10:00:00.000 - Loaded driver psvrpc v1',
        '10:00:00.001 - ERROR: usb open failed',
        '10:00:00.002 - PSVRPC-HMD attached serial 0',
        '10:00:00.003 - sensor packet OK length=64',
        '10:00:00.004 - driver_psvrpc starting poll',
        '10:00:00.005 - failed to allocate buffer',
        '10:00:00.006 - compositor frame id 42'
    )
    Write-Text (Join-Path $steam 'logs\vrserver.txt') ($logMatchLines -join "`r`n")
    Invoke-TestCase 'logs/filters psvrpc-error-failed case-insensitively, flags missing file' {
        $rows = @(& $logScript -SteamPath $steam)
        $matched = @($rows | Where-Object { $_.status -eq 'match' })
        Assert-Equal 5 $matched.Count 'vrserver.txt match count'
        foreach ($t in $matched.text) {
            if ($t -notmatch '(?i)psvrpc|driver_psvrpc|PSVRPC-HMD|error|failed') { throw "unwanted line matched: $t" }
        }
        if (($matched.text -join ' | ') -match 'sensor packet OK length=64|compositor frame id 42') {
            throw 'unrelated lines were not filtered out'
        }
        $missingRows = @($rows | Where-Object { $_.status -eq 'missing' })
        Assert-Equal 1 $missingRows.Count 'missing rows'
        Assert-Equal (Join-Path $steam 'logs\vrcompositor.txt') $missingRows[0].file 'missing file path'
        if ($null -ne $missingRows[0].text) { throw 'missing row must carry null text' }
    }
    Invoke-TestCase 'logs/-Tail limits how far back the scan reaches' {
        $lines = 1..20 | ForEach-Object { 'psvrpc line {0:d2}' -f $_ }
        Write-Text (Join-Path $fx 'steam2\logs\vrserver.txt') ($lines -join "`r`n")
        $rows = @(& $logScript -SteamPath (Join-Path $fx 'steam2') -Tail 3)
        # vrcompositor.txt is absent here, so the script must also emit one missing row.
        $matched = @($rows | Where-Object { $_.status -eq 'match' })
        Assert-Equal 3 $matched.Count 'match row count'
        Assert-Equal 'psvrpc line 18' $matched[0].text 'first tailed row'
        Assert-Equal 'psvrpc line 20' $matched[2].text 'last tailed row'
    }
    Invoke-TestCase 'logs/tail is applied before matching (old match outside tail is skipped)' {
        Write-Text (Join-Path $fx 'steam3\logs\vrserver.txt') (
            @('psvrpc early entry', 'filler one', 'filler two') -join "`r`n")
        Write-Text (Join-Path $fx 'steam3\logs\vrcompositor.txt') 'failed late entry'
        $rows = @(& $logScript -SteamPath (Join-Path $fx 'steam3') -Tail 2)
        Assert-Equal 1 $rows.Count 'row count'
        Assert-Equal 'failed late entry' $rows[0].text 'only compositor row survives'
        Assert-Equal (Join-Path $fx 'steam3\logs\vrcompositor.txt') $rows[0].file 'file'
    }
    Invoke-TestCase 'logs/missing logs folder yields two missing rows' {
        $rows = @(& $logScript -SteamPath (Join-Path $fx 'steam_nologs'))
        Assert-Equal 2 $rows.Count 'row count'
        foreach ($r in $rows) { Assert-Equal 'missing' $r.status 'status' }
    }
    Invoke-TestCase 'logs/-Tail 0 is rejected by ValidateRange' {
        # Quoted parameter name: binding messages are localized on some hosts.
        Assert-Throws { & $logScript -SteamPath $steam -Tail 0 } "'Tail'"
    }

    # --- driver_registration.ps1: path validation ------------------------------------------
    Invoke-TestCase 'reg/unknown -Action value is rejected by ValidateSet' {
        Assert-Throws { Invoke-Registration -Action 'Bogus' -SteamVRPath $steamvr -DriverPath $package } "'Action'"
    }
    Invoke-TestCase 'reg/missing vrpathreg.exe throws vrpathreg missing' {
        Assert-Throws { Invoke-Registration -Action Inspect -SteamVRPath (Join-Path $fx 'no_such_steamvr') -DriverPath $package } 'vrpathreg missing'
    }
    Invoke-TestCase 'reg/missing driver folder throws Driver package missing' {
        Assert-Throws { Invoke-Registration -Action Inspect -SteamVRPath $steamvr -DriverPath (Join-Path $fx 'packages\ghost') } 'Driver package missing'
    }
    Invoke-TestCase 'reg/driver folder without manifest throws Manifest missing' {
        Assert-Throws { Invoke-Registration -Action Inspect -SteamVRPath $steamvr -DriverPath $noManifest } 'Manifest missing'
    }
    Invoke-TestCase 'reg/manifest name != psvrpc is refused' {
        Assert-Throws { Invoke-Registration -Action Inspect -SteamVRPath $steamvr -DriverPath $badName } 'Expected psvrpc'
    }
    Invoke-TestCase 'reg/Install without built DLL throws, Remove tolerates its absence' {
        Assert-Throws { Invoke-Registration -Action Install -SteamVRPath $steamvr -DriverPath $noDll -DryRun } 'Driver DLL missing; build first'
        # Remove does not require the DLL: a registered dll-less package can dry-run removal.
        Write-VrPathFile -ExternalDrivers @($noDll)
        $out = Invoke-Registration -Action Remove -SteamVRPath $steamvr -DriverPath $noDll -DryRun
        Assert-Match $out 'DRY RUN: .*removedriver.*' 'Remove dry-run without dll'
    }
    Invoke-TestCase 'reg/missing openvrpaths.vrpath throws initialize SteamVR first' {
        $savedLaa = $env:LOCALAPPDATA
        try {
            $env:LOCALAPPDATA = $laEmpty
            Assert-Throws { Invoke-Registration -Action Inspect -SteamVRPath $steamvr -DriverPath $package } 'OpenVR path file missing'
        } finally { $env:LOCALAPPDATA = $savedLaa }
    }

    # --- driver_registration.ps1: Inspect / idempotence / dry-run ---------------------------
    Invoke-TestCase 'reg/Inspect reports registered=$false for unrelated drivers only' {
        Write-VrPathFile -ExternalDrivers @('C:\Other\vendor\driver')
        $info = Invoke-Registration -Action Inspect -SteamVRPath $steamvr -DriverPath $package
        Assert-Equal $false $info.registered 'registered'
        Assert-Equal $package $info.driver 'driver'
        Assert-Equal $fakeTool $info.vrpathreg 'vrpathreg'
    }
    Invoke-TestCase 'reg/Inspect reports registered=$true despite case and trailing-slash differences' {
        # openvr may store a differently-cased path with a trailing separator.
        Write-VrPathFile -ExternalDrivers @(($package.ToLowerInvariant() + '\'))
        $info = Invoke-Registration -Action Inspect -SteamVRPath $steamvr -DriverPath $package
        Assert-Equal $true $info.registered 'registered'
    }
    Invoke-TestCase 'reg/Install is idempotent when already registered (no vrpathreg call)' {
        $toolBefore = Get-Content -LiteralPath $fakeTool -Raw
        Write-VrPathFile -ExternalDrivers @(($package.ToLowerInvariant() + '\'))
        $out = Invoke-Registration -Action Install -SteamVRPath $steamvr -DriverPath $package
        Assert-Equal "Already satisfied: Install $package" $out 'Install output'
        # The fake vrpathreg.exe is a plain non-executable file: had the script
        # reached the real invocation, execution would have thrown here.
        Assert-Equal $toolBefore (Get-Content -LiteralPath $fakeTool -Raw) 'fake vrpathreg untouched'
    }
    Invoke-TestCase 'reg/Remove is idempotent when not registered' {
        Write-VrPathFile -ExternalDrivers @('C:\Other\vendor\driver')
        $out = Invoke-Registration -Action Remove -SteamVRPath $steamvr -DriverPath $package
        Assert-Equal "Already satisfied: Remove $package" $out 'Remove output'
    }
    Invoke-TestCase 'reg/Install dry-run prints adddriver without touching path file or binary' {
        Write-VrPathFile -ExternalDrivers @('C:\Other\vendor\driver')
        $before = Get-Content -LiteralPath $pathFile -Raw
        $out = Invoke-Registration -Action Install -SteamVRPath $steamvr -DriverPath $package -DryRun
        Assert-Match $out ('^DRY RUN: "' + [regex]::Escape($fakeTool) + '" adddriver "' + [regex]::Escape($package) + '"$') 'dry-run line'
        Assert-Equal $before (Get-Content -LiteralPath $pathFile -Raw) 'openvrpaths.vrpath unchanged'
    }
    Invoke-TestCase 'reg/Remove dry-run prints removedriver when registration would change' {
        Write-VrPathFile -ExternalDrivers @($package)
        $out = Invoke-Registration -Action Remove -SteamVRPath $steamvr -DriverPath $package -DryRun
        Assert-Match $out 'DRY RUN: .* removedriver .*' 'dry-run line'
    }
    Invoke-TestCase 'reg/PowerShell -File resolves default DriverPath after parameter binding' {
        Write-VrPathFile -ExternalDrivers @('C:\Other\vendor\driver')
        $expected = Join-Path $root 'out\driver_psvrpc\psvrpc'
        # Child process inherits isolated LOCALAPPDATA. Inspect never invokes
        # vrpathreg; a source-only checkout may lack the default package.
        # Redirect through ProcessStartInfo: Windows PowerShell 5.1 converts
        # native stderr into terminating errors under ErrorActionPreference=Stop,
        # before this test can assert an expected missing-package diagnostic.
        $start = New-Object System.Diagnostics.ProcessStartInfo
        $start.FileName = 'powershell.exe'
        $start.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$regScript`" -Action Inspect -SteamVRPath `"$steamvr`""
        $start.UseShellExecute = $false
        $start.CreateNoWindow = $true
        $start.RedirectStandardOutput = $true
        $start.RedirectStandardError = $true
        $child = [System.Diagnostics.Process]::Start($start)
        $output = $child.StandardOutput.ReadToEnd() + $child.StandardError.ReadToEnd()
        $child.WaitForExit()
        $exitCode = $child.ExitCode
        $child.Dispose()
        if (Test-Path -LiteralPath (Join-Path $expected 'driver.vrdrivermanifest')) {
            Assert-Equal 0 $exitCode 'default-path Inspect exit'
            Assert-Match $output 'psvrpc' 'default package resolution'
        } else {
            Assert-Match $output 'Driver package missing' 'default path absence reported normally'
        }
    }
} finally {
    $env:LOCALAPPDATA = $realLocalAppData
    # Prove the tests never touched the real OpenVR registration file.
    $nowExists = Test-Path -LiteralPath $realPathFile -PathType Leaf
    if ($nowExists -ne $realPathFileExisted) { $script:Failed++; Write-Host 'FAIL real openvrpaths.vrpath existence changed' }
    elseif ($nowExists -and ((Get-Content -LiteralPath $realPathFile -Raw) -ne $realPathFileSnapshot)) {
        $script:Failed++; Write-Host 'FAIL real openvrpaths.vrpath content changed'
    }
    if (Test-Path -LiteralPath $fx) { Remove-Item -LiteralPath $fx -Recurse -Force }
}

if ($script:Failed -gt 0) { Write-Host "$($script:Failed) test case(s) FAILED"; exit 1 }
Write-Host 'PASS driver script tests (AST parsing, log filtering/missing, registration idempotence/dry-run/path validation) - no SteamVR, no vrpathreg execution, no hardware'
exit 0
