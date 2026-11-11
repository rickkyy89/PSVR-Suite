# SPDX-License-Identifier: MIT
# Read-only Direct Mode readiness bundle: invokes the existing inspect scripts
# sequentially and records each snapshot with its own begin/end timestamps and
# status. Deliberately NOT done here: no Steam runtime start or probe, no
# elevation prompt, no settings or machine config writes, no target
# acquisition, no power commands. Snapshots are
# independent points in time, not one atomic cross-API view. Steam settings,
# logs and install identifiers are deliberately not collected.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    # Optional: read-only validation of an EDID restoration plan via the
    # existing inspector (it opens keys without write intent).
    [string]$PlanPath,
    # Optional: a previously captured runtime summary provided by the consumer.
    # This collector never performs the probe itself.
    [string]$RuntimeSummaryPath,
    # Optional: explicit device interface path used for the exact-path Display
    # Core comparison. Nothing is probed to discover it.
    [string]$HmdInterfacePath,
    # Optional: ask the read-only Display Core inspector for mode metadata.
    # Forwarded only if the inspector exposes that option; no target is ever
    # acquired or modified here.
    [switch]$QueryModes
)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
if (-not [IO.Path]::IsPathRooted($OutputDirectory)) { throw 'OutputDirectory must be an absolute path' }
if (-not (Test-Path -LiteralPath $OutputDirectory -PathType Container)) {
    New-Item -ItemType Directory -Path $OutputDirectory | Out-Null
}
$createdUtc=[DateTime]::UtcNow.ToString('o')
$bundleDir=Join-Path ([IO.Path]::GetFullPath($OutputDirectory)) ('direct_readiness_'+[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ'))
if (Test-Path -LiteralPath $bundleDir) { throw 'Do not overwrite an existing bundle directory' }
New-Item -ItemType Directory -Path $bundleDir | Out-Null

function Resolve-ReadOnlyApplication {
    # Bare command names ('python','py') must resolve through the Application
    # command type only, so aliases or functions can never masquerade as the
    # executable. Absolute paths are accepted only when the leaf exists.
    param([string]$Name)
    $candidates=@(Get-Command -Name $Name -CommandType Application -ErrorAction SilentlyContinue)
    if ($candidates.Count) { return $candidates[0].Source }
    if ([IO.Path]::IsPathRooted($Name) -and (Test-Path -LiteralPath $Name -PathType Leaf)) { return $Name }
    throw "Read-only application not found: $Name"
}

function Invoke-ReadOnlyNative {
    # Runs a read-only executable, separating stdout JSON from stderr text so
    # stderr can be preserved in the bundle instead of polluting stdout.
    param([string]$Exe,[string[]]$Arguments)
    $resolved=Resolve-ReadOnlyApplication -Name $Exe
    $previous=$ErrorActionPreference
    $ErrorActionPreference='Continue'
    try { $output=& $resolved @Arguments 2>&1 } finally { $ErrorActionPreference=$previous }
    $stderr=((@($output | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] } | ForEach-Object { $_.ToString() })) -join "`r`n")
    if ($LASTEXITCODE -ne 0) { throw "Exited with code $LASTEXITCODE. stderr: $stderr" }
    $stdout=((@($output | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] } | ForEach-Object { [string]$_ })) -join "`r`n")
    @{ stdout=$stdout; stderr=$stderr }
}

function Import-ReadOnlyJsonFile {
    param([string]$Path)
    try { (Get-Content -LiteralPath $Path -Raw) | ConvertFrom-Json }
    catch { throw "Unreadable JSON input: $Path ($($_.Exception.Message))" }
}

function New-ReadinessSource {
    # One independent snapshot: timestamped, exceptions preserved per source.
    param([string]$Name,[string]$Tool,[scriptblock]$Action)
    $entry=[ordered]@{name=$Name;tool=$Tool;status='failed';begin_utc=$null;end_utc=$null;data=$null;error=$null;stderr=$null}
    $entry['begin_utc']=[DateTime]::UtcNow.ToString('o')
    try {
        $result=& $Action
        if ($result -is [pscustomobject] -and $result.PSObject.Properties['readiness_stdout']) {
            $entry['stderr']=$result.stderr
            $entry['data']=$result.readiness_stdout
        } else { $entry['data']=$result }
        $entry['status']='ok'
    } catch { $entry['error']=$_.Exception.Message }
    $entry['end_utc']=[DateTime]::UtcNow.ToString('o')
    return [pscustomobject]$entry
}

# Source 1: Windows active inventory (prebuilt read-only tool).
$inventory=New-ReadinessSource -Name 'windows_active_inventory' -Tool 'out\display\psvr_display.exe --json' -Action {
    $native=Invoke-ReadOnlyNative -Exe (Join-Path $root 'out\display\psvr_display.exe') -Arguments @('--json')
    [pscustomobject]@{ readiness_stdout=($native.stdout | ConvertFrom-Json); stderr=$native.stderr }
}

# Source 2: public NVAPI displays plus the optional private direct enumeration.
$nvidia=New-ReadinessSource -Name 'nvidia_public_displays' -Tool 'inspect_nvidia_displays.ps1 -IncludeDirectEnumeration' -Action {
    (& (Join-Path $PSScriptRoot 'inspect_nvidia_displays.ps1') -IncludeDirectEnumeration) | ConvertFrom-Json
}

# Source 3: Windows Display Core targets (snapshot only, nothing acquired).
# The external -HmdInterfacePath maps to the inspector's real parameter
# -RequestedMonitorInterfacePath, and options are forwarded by hashtable
# splatting so PowerShell binds named parameters natively (never string
# arrays that would act positionally). If a requested option is unsupported,
# this source fails with a clear error instead of silently dropping the
# exact-path comparison.
$displayCore=New-ReadinessSource -Name 'windows_display_core' -Tool 'inspect_windows_display_core.ps1' -Action {
    $inspector=Join-Path $PSScriptRoot 'inspect_windows_display_core.ps1'
    $accepted=(Get-Command $inspector).Parameters
    $params=@{}
    if ($HmdInterfacePath) {
        if (-not $accepted.ContainsKey('RequestedMonitorInterfacePath')) { throw 'Core inspector does not accept -RequestedMonitorInterfacePath; refusing a pathless comparison' }
        $params['RequestedMonitorInterfacePath']=$HmdInterfacePath
    }
    if ($QueryModes) {
        if (-not $accepted.ContainsKey('QueryModes')) { throw 'Core inspector exposes no read-only -QueryModes option; not requesting mode metadata' }
        $params['QueryModes']=$true
    }
    (& $inspector @params) | ConvertFrom-Json
}

# Source 4: EDID restoration plan inspection, only when a plan is supplied.
if ($PlanPath) {
    $edid=New-ReadinessSource -Name 'edid_plan_inspection' -Tool 'inspect_hmd_edid_plan.ps1' -Action {
        (& (Join-Path $PSScriptRoot 'inspect_hmd_edid_plan.ps1') -PlanPath $PlanPath) | ConvertFrom-Json
    }
} else {
    $edid=[pscustomobject][ordered]@{name='edid_plan_inspection';tool='inspect_hmd_edid_plan.ps1';status='skipped';
        begin_utc=$null;end_utc=$null;data=$null;error='PlanPath not provided';stderr=$null}
}

# Optional consumer input; never generated by probing here.
$runtime=[ordered]@{status='absent';path=$null;data=$null;error=$null}
if ($RuntimeSummaryPath) {
    try {
        $runtime.path=(Resolve-Path -LiteralPath $RuntimeSummaryPath).Path
        $runtime.data=Import-ReadOnlyJsonFile -Path $runtime.path
        $runtime.status='provided'
    } catch { $runtime.status='failed'; $runtime.error=$_.Exception.Message }
}

# HMD path reference for the classifier's exact-path rule: explicit argument
# wins; otherwise, when the HMD is still on the desktop, its monitor path.
$hmd=$null
if ($HmdInterfacePath) {
    $hmd=[ordered]@{device_interface_path=$HmdInterfacePath}
} elseif ($inventory.status -eq 'ok' -and $inventory.data.displays) {
    $sony=@($inventory.data.displays | Where-Object { $_.edid_ids -and $_.edid_ids.manufacturer -eq 0xD94D -and $_.edid_ids.product -eq 0x6A04 })
    if ($sony.Count) {
        $hmd=[ordered]@{device_interface_path=$sony[0].monitor_device_path;
            adapter_luid=[ordered]@{low=$sony[0].source_adapter.low; high=$sony[0].source_adapter.high}}
    }
}

$bundle=[ordered]@{
    schema_version=1
    kind='direct_readiness_bundle'
    created_utc=$createdUtc
    atomic=$false
    note='Independent sequential read-only snapshots taken at different instants; not one atomic cross-API view. No runtime probing or Steam startup was executed; runtime_summary only reflects the consumer-provided file.'
    privacy_note='Steam settings, logs and install identifiers are deliberately not collected.'
    sources=[ordered]@{
        windows_active_inventory=$inventory
        nvidia_public_displays=$nvidia
        windows_display_core=$displayCore
        edid_plan_inspection=$edid
    }
    runtime_summary=$runtime
    hmd=$hmd
}
$bundlePath=Join-Path $bundleDir 'bundle.json'
[IO.File]::WriteAllText($bundlePath,($bundle | ConvertTo-Json -Depth 12))

# Analysis: the pure classifier (no hardware access) over the saved bundle.
$classifier=New-ReadinessSource -Name 'readiness_classifier' -Tool 'tools\psvr_native_analysis\readiness.py' -Action {
    $scriptPath=Join-Path $root 'tools\psvr_native_analysis\readiness.py'
    if (-not (Test-Path -LiteralPath $scriptPath)) { throw 'Classifier script missing' }
    $resultPath=Join-Path $bundleDir 'classifier_result.json'
    $attempts=@()
    if (Get-Command python -CommandType Application -ErrorAction SilentlyContinue) { $attempts+=,([pscustomobject]@{Exe='python';Args=@()}) }
    if (Get-Command py -CommandType Application -ErrorAction SilentlyContinue) { $attempts+=,([pscustomobject]@{Exe='py';Args=@('-3')}) }
    if (-not $attempts.Count) { throw 'No Python interpreter found; run readiness.py against bundle.json manually' }
    $errors=@()
    foreach ($attempt in $attempts) {
        try {
            Invoke-ReadOnlyNative -Exe $attempt.Exe -Arguments ($attempt.Args+@($scriptPath,$bundlePath,'--out',$resultPath)) | Out-Null
            return Import-ReadOnlyJsonFile -Path $resultPath
        } catch { $errors+="$($attempt.Exe): $($_.Exception.Message)" }
    }
    throw ('Classifier failed. '+($errors -join ' | '))
}

$statuses=[ordered]@{}
foreach ($entry in @($inventory,$nvidia,$displayCore,$edid,$classifier)) { $statuses[$entry.name]=$entry.status }
$summary=[ordered]@{
    schema_version=1
    kind='direct_readiness_summary'
    created_utc=[DateTime]::UtcNow.ToString('o')
    bundle_directory=$bundleDir
    statuses=$statuses
    outcome=$(if ($classifier.status -eq 'ok') { $classifier.data.outcome } else { $null })
    note='Read-only prerequisites snapshot; not a Direct Mode working claim and no target acquisition.'
}
$summaryPath=Join-Path $bundleDir 'summary.json'
[IO.File]::WriteAllText($summaryPath,($summary | ConvertTo-Json -Depth 10))
[pscustomobject]$summary | ConvertTo-Json -Depth 10
