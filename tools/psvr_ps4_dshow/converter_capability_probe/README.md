# DirectShow converter capability probe — NOT a pixel-comparison harness

## Outcome / stop point (2026-11-07)

The full identical-YUY2 -> actual DirectShow RGB24 -> detector comparison is
**not implemented**. This standalone target only instantiates two explicit COM
filter classes and calls `QueryAccept` on their **unconnected** pins. It creates
no graph, source, sink, allocator or sample. It is deliberately not a CTest test,
not part of `verify_offline.ps1`, and does not load/link the production backend.
Exit zero means the capability observations completed, **not** conversion or
pixel/detector agreement. An unavailable candidate is printed as `unavailable`,
not counted as a conversion pass. Unexpected COM failures return nonzero.

Observed on the existing Windows x64 installation:

| Candidate | Actual `GetClassID` | `CoCreateInstance` | Input YUY2 `QueryAccept` | Output RGB24 `QueryAccept` |
| --- | --- | --- | --- | --- |
| `CLSID_Colour` | `{1643E180-90F5-11CE-97D5-00AA0055595A}` | `S_OK` | `S_FALSE` | `S_FALSE` |
| `CLSID_AVIDec` | `{CF49D4E0-1115-11CE-B03A-0020AF0BA770}` | `S_OK` | `S_OK` | `S_FALSE` |

The proposed types are `MEDIATYPE_Video` / `FORMAT_VideoInfo`, 1748x408,
positive height, 166667 x 100ns frame interval, fixed samples. Input is packed
16-bit YUY2, 3496 bytes/row; proposed output is BI_RGB RGB24, 5244 bytes/row.
No colorimetry is specified by this proposed VideoInfo format. **None of these
types was negotiated.** In particular the unconnected AVI output's `S_FALSE`
does not prove that RGB24 will be unsupported after connecting its input.
Neither filter is identified as the historical OpenCV converter; AVI Decoder
can also delegate to a runtime-selected VfW codec.

Release build and probe both exited 0; stderr was empty. The build emitted
MSBuild `MSB8029` warnings about placing incremental-build artifacts in the
approved temporary directory, no C++ compiler warnings. The probe completed
inside a 15-second process deadline; no pending owner or forced termination.
Execution artifacts (local, not repository assets):

`C:\Users\rickk\AppData\Local\Temp\opencode\dshow-converter-capability-20261107-123837-588`

`probe.out.jsonl` is the final 60Hz proposal run; `build.log` and
`probe.err.log` are retained there. There are **no** RGB24 samples, normalized
BGR files, pixel metrics or detector results from this probe. Existing scalar
tests were not rerun as a substitute. No device enumeration/open, preference or
calibration read/write, driver installation/registration, Runtime or hardware
operation was requested or performed by this code.

## Why the full harness stops here

The repository's fake COM fixtures return `E_NOTIMPL` for streaming pin
enumeration and do not implement source connection/delivery. No reusable
synthetic source or RGB24-normalizing sink implementation was found in
`tools/psvr_ps4_dshow`; production's Sample Grabber callback accepts YUY2 only.
Scoped checks found
no `streams.h` in the installed SDK 10.0.26100.0 `um`, `shared` or `ucrt` include
directories; this is not an exhaustive machine-wide BaseClasses search.
The existing SDK/MSVC x64 toolchain **does** work, so compiler availability or
total converter absence is not the blocker.

The remaining work is a new streaming COM component and its lifecycle tests,
not an extension of the fake `IMediaSample` fixture. Connecting an incomplete
pin or passing a made-up sample to a system filter would not establish reliable
evidence, particularly across allocator negotiation, COM reference cycles and
blocking `Stop`/delivery. It is not safe to claim completion within this scoped
change without implementing and validating those contracts. No incomplete
source/sink, fake transform, fallback MFT/DMO or nominal passing parity test is
introduced. This is a resource/reliability stop, **not** a claim that a genuine
device-free graph is impossible.

## Concrete next implementation

Use an independent `converter_comparison` target, not production CMake/backend
or the Python parity module currently under review. Sequence the work as follows:

1. **Endpoint contract tests first.** Implement an unregistered in-process
   one-output-pin YUY2 `IBaseFilter` plus `IPin`, `IEnumPins`, `IEnumMediaTypes`.
   Complete `QueryInterface` identity/refcounts, cloned enumeration position,
   graph membership (weak graph reference), clock/state transitions, exact
   media-type deep copy/free, connection rollback, disconnect and downstream
   flush/EOS propagation. Obtain a real `IMemAllocator` from downstream, or use
   the system memory allocator; validate returned properties, notify downstream,
   commit/decommit on transitions and deliver actual allocator `IMediaSample`s.
   Do not implement or install a driver, or treat current fake samples as a
   streaming allocator. Test rejection, failed NotifyAllocator rollback,
   refcounts and repeated connect/stop/disconnect device-free.
2. **Deterministic sink.** Either implement a validated `IMemInputPin` sink, or
   directly instantiate the existing system Sample Grabber + Null Renderer
   (with explicit unavailable status if absent). The latter avoids writing a
   second allocator endpoint. A test-only callback must copy the sample before
   returning, retain no borrowed pointer, cap byte counts, reject unhandled
   dynamic format changes and record timestamp/actual size/allocation capacity.
   No `RenderStream`/intelligent-connect fallback to an unidentified chain.
3. **Explicit real conversion.** Start with the observed AVI Decoder candidate:
   `source -> CLSID_AVIDec -> RGB24 sink`. Use `ConnectDirect`; record requested
   and actual `GetClassID` for every inserted filter. Retrieve connected media
   types at both converter pins, including the complete small VideoInfo block,
   source/target rectangles, compression, dimensions/signed height, sample size,
   bitrate, frame interval, and color flags if VideoInfo2 is supported. Report
   unspecified metadata as unspecified, never infer a color matrix. Failed
   class creation/unsupported connection is `unavailable`, not a pixel pass.
4. **Owner and deadline.** Keep graph construction, control, disconnect, COM
   release and `CoUninitialize` on one native owner thread. Log stage entry and
   result. Send finite immutable frames only; bound sample waits. After failure
   drain/stop/disconnect before releasing callback/allocator storage. An outer
   process observer has a deadline but must not kill or release objects from a
   foreign thread; on timeout report `cleanup_pending` and retain the live
   owner/process. Do not return success with an orphaned pending cleanup.
5. **Exact bytes and artifacts.** Generate input only once, save its SHA-256,
   feed those identical bytes to graph and unchanged owned decode. Check byte
   preservation at the converter input with a pass-through observer or
   a source-side actual-sample hash. Save raw RGB24 sample plus metadata, and a
   separate normalized, tight top-down BGR file. For BI_RGB RGB24, pixel bytes
   are B,G,R; positive DIB height is bottom-up, negative height top-down. Compute
   aligned row bytes as `(width*3+3)&~3`; require actual sample length sufficient
   for all rows, bound larger allocations and report padding/trailing bytes.
   YUY2 input stays top-down irrespective of positive YUV height. Test the
   normalizer independently with both RGB orientations, padded widths and
   deliberately truncated/overflowing samples before using converter output.
6. **Separate Python comparison.** No hardware API/DLL or preferences. Load
   the saved bytes/metadata, verify sizes/hash, normalize raw output independently
   and verify it equals native-normalized BGR. Compare full frame and explicit
   eyes at x48/x688, y0..399 against unchanged owned decode. Report maximum and
   mean absolute channel error, differing channels and pixels, both before and
   after orientation normalization (raw comparison clearly labeled diagnostic,
   not orientation-corrected). Use asymmetric coordinate, legal-range luma and
   chroma ramps/clipping, unequal-luma shared-chroma pairs, abrupt color changes,
   stable blue/white patches and threshold-edge patches, without fitted gains,
   offsets, geometry search or threshold tuning.
7. **Detector comparison.** Run the unchanged `BlueLedDetector` on each eye from
   both paths with identical explicit in-memory configs, library defaults and
   the actual Suite operating point separately, white-core handling off/on.
   Observe final component masks in a test-only forwarding wrapper; report mask
   disagreement and all candidate fields (canonicalize label order only).
   Include nonempty stable detections, clipping and hue/value/saturation-edge
   colors. Do not alter production thresholds to hide a difference. Conversion
   differences are results, not automatically a harness malfunction or proof of
   a live defect. Keep `comparison_complete`, equality and unavailable distinct.

Even successful execution of this plan compares **the identified actual system
converter** against owned conversion on synthetic bytes. It is not historical
chain parity, live-frame evidence, optical diagnosis or the five-match gate.

## Isolated build / bounded probe reproduction

Use the already installed SDK/MSVC x64 toolchain. No packages are installed.
For example, with `$cmake` set to the existing CMake executable and `$build` a
new directory under the approved `...\Temp\opencode` directory:

```powershell
& $cmake -S .\tools\psvr_ps4_dshow\converter_capability_probe -B $build -G 'Visual Studio 17 2022' -A x64
& $cmake --build $build --config Release --parallel 2
```

Observe each exit code before proceeding. Run the resulting executable through
a bounded process observer, not a wrapper that forcekills at its deadline:

```powershell
$exe = Join-Path $build 'Release\converter_capability_probe.exe'
$out = Join-Path $build 'probe.out.jsonl'
$err = Join-Path $build 'probe.err.log'
$si = New-Object System.Diagnostics.ProcessStartInfo
$si.FileName = $env:ComSpec
# These local paths must not contain quotes, %, CR or LF.
foreach ($p in @($exe, $out, $err)) {
    if ($p -match '["%\r\n]') { throw 'Unsafe command path' }
}
$si.Arguments = '/d /v:off /s /c ""' + $exe + '" 1>"' + $out + '" 2>"' + $err + '""'
$si.UseShellExecute = $false
$si.CreateNoWindow = $true
$si.EnvironmentVariables['PSVR_PS4_DSHOW_ENABLE'] = '0'
$p = New-Object System.Diagnostics.Process
$p.StartInfo = $si
try {
    if (-not $p.Start()) { throw 'Probe could not start' }
    $null = $p.Handle
    if (-not $p.WaitForExit(15000)) {
        throw "Pending owner/child; wrapper PID $($p.Id). NOT killed. Stop workflow."
    }
    if ($null -eq $p.ExitCode -or $p.ExitCode -ne 0) { throw 'Capability probe failed' }
} finally { $p.Dispose() } # Disposes observation handle only; never kills child.
Get-Content -LiteralPath $out -Tail 30
```
