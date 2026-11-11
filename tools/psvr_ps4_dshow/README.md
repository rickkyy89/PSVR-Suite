# PS4 capture-owned DirectShow: staged integration, hardware gate CLOSED

Offline scalar/pixel/mask follow-up: [PIXEL_PARITY.md](PIXEL_PARITY.md) documents
independent BT.601/crop/callback fixtures, junior commands and the explicit
deferral of an actual device-free DirectShow system-converter graph. Production
conversion, detector thresholds and calibration are unchanged; live optical
parity/failure cause is not established by synthetic fixtures.

**Hardware negotiation STOP:** see [NEGOTIATION_DIAGNOSIS.md](NEGOTIATION_DIAGNOSIS.md)
for proven evidence vs hypotheses, actual OpenCV 5.0.0 source comparison,
bounded stage/media diagnostics and a parent-only read-only candidate probe.
No hardware fix is claimed; no probe or retry was launched by the designer.

**ABI v2 follow-up:** see [INTEGRATION.md](INTEGRATION.md) for the current
multi-property transaction contract, staged Suite/Tracking wiring, enable gate,
and expanded junior commands. The sections below describe the original isolated
PoC and its first review; statements about no integration, no initial writes,
single-property-only edits, ABI v1, and unconditional close restoration are
historical, superseded by INTEGRATION.md. Previous junior evidence is 9 native
groups + 14 Python tests; it is NOT evidence that the new v2 changes pass.

## Status and scope

The production OpenCV wheel does not expose its capture-owned filter/moniker.
The guard in `ps4_dshow_controls.py` is correct to refuse index-only native
writes: re-enumerating an index, even with a matching VID/PID/path, does **not**
prove that a second filter is the object delivering the pixels. Existing Suite
legacy startup can therefore continue without applying -7/8/1, while strict
overrides fail. **That regression is NOT fixed by this isolated PoC.**

Search of the repository's C++/Python DirectShow symbols found only the current
Python control binder; no reusable owned graph or sample sink was found.

This standalone CMake project + `tools/psvr_camera/ps4_owned_dshow.py` implement
an alternative acquisition owner, without editing tracking, StereoSession,
Suite defaults, wheel files, profiles, driver bindings, or vrsettings. There is
no download, package install, filter registration, driver install or deployment.

## Ownership and identity

1. Enumerate PS4 runtime monikers (VID 05A9, PID 058A/058B); select an **exact
   moniker display name**, not an index. `psvr_dshow_enumerate` reports names
   without binding them. An external caller must also bound this operation.
2. Bind the selected moniker **once** to `IBaseFilter`. Query
   `IAMCameraControl` and `IAMVideoProcAmp` on **that same object**. Add this
   object to the app-owned graph. Never bind a second filter for controls.
3. `capture pin -> system Sample Grabber -> system Null Renderer`, using only
   `ConnectDirect`. No intelligent-connect converters or alternate camera.
4. Capture and every COM call (including controls, Stop, restoration, Release,
   and CoUninitialize) stay on the Python-created native-owner thread/MTA.
   The ABI additionally rejects calls from another thread.
5. Streaming callback does only media-type/length validation and a copied
   latest-frame slot. It retains no sample/driver buffer. Refcounted callback
   holds shared mailbox storage, not a raw session pointer, so late callback
   lifetime does not depend on a caller's timeout/abandoned Python stack.

The exact moniker is selection metadata; **object reuse**, not the metadata
match, is the capture/control identity guarantee. The fake Windows COM fixture
checks shared `IUnknown` identity and writes reaching that one fake filter.

## Pixel and timing contract

Only YUY2 `FORMAT_VideoInfo`, **1748x408, 16 bits, 1,426,368 bytes**, and a verified
actual 166666 or 166667 100-ns interval (nominal 60 Hz) is accepted. Selection
preserves an already-published 60-Hz interval; a non-60 nominal header with exact
layout is eligible for an explicit 60-Hz request within positive, ordered caps
bounds. Prefer 166667, or 166666 if only that value is in range. Range membership
is not discrete-support proof: one SetFormat and exact matching GetFormat must
succeed. A failed request terminates the open for owner cleanup, with no second
rate/candidate attempt or 120-Hz fallback. Read-only inspection reports eligibility
as unverified and never calls SetFormat. All other transport gates remain;
GetFormat after SetFormat must confirm the selected interval without rounding.
VIDEO_STREAM_CONFIG_CAPS has no temporal-step member: min/max never proves
discrete support, so the explicit 60-Hz request must be driver-validated.
Spatial granularity fields are deprecated by Microsoft;
no resize/crop is negotiated from them, only exact enumerated media types.
Capture pin and grabber connections are also checked; dynamic format changes,
wrong sample length, or allocation capacity below the frame size latch a
terminal error **before** requesting a sample pointer/copy. No scaling,
stride guessing, RGB negotiation, fallback/reopen, or vertical flip. YUV is
top-down even when biHeight is positive (Microsoft documentation below).

`decode_pair` uses the existing OpenCV `COLOR_YUV2BGR_YUY2` conversion and
`ps4_camera.split_stereo`: x=48..687 and x=688..1327, first 400 rows, detached
640x400 BGR eyes. No Suite reference-eye remapping occurs here. Synthetic
fixtures compare conversion/crops; **actual wheel/OV580 byte parity remains
unverified**, as does measured 60 Hz delivery.

The returned time is `perf_counter()` immediately after native read completion,
not exposure time, USB arrival time or the driver's floating-point sample time.
The callback may have completed earlier. Sequence gaps intentionally drop old
frames. Python also keeps only a single immutable raw sample, not a queue.

## Controls and cleanup

No settings are written at open. `get_control` reads the native range/step/
capabilities + value/flags. `set_manual` validates the native integer range and
step, writes Manual=2, and requires immediate exact value/flag readback. Failed
or mismatching Set is terminal; no subsequent frames. This version deliberately
does not call delayed readback a success and does not implement an atomic
three-property profile transaction or settle-frame barrier. In-flight samples
can predate a confirmed setting. Integration must add a verified settle policy.

For isolated experiments, changed values **and original auto/manual flags are
restored explicitly on close**, even if Set failed. Stop must complete before
graph cleanup. Restoration readback mismatch/failure is visible and retains
ownership. This is not the Suite's intended persistent-acquisition policy.
Restoration is best-effort across all changed properties: preserve the first
failure HRESULT, still attempt every remaining property/readback, and clear
only confirmed properties. Explicit retry writes only the unconfirmed ones.

Native COM open/Set/Stop cannot be safely interrupted. Caller waits are bounded;
on timeout the stop request is latched, sample publication stops, and the owner,
DLL, callback, COM apartment and handle remain strongly held until actual owner
cleanup. No foreign-thread free, unload, forcekill or automatic reopen.
If cleanup returns an error, the owner thread stays alive; a later explicit
`release()` retries cleanup on that **same** owner. A truly hung driver can
therefore retain an owner indefinitely; bounded caller latency does not claim
bounded kernel/driver execution or guaranteed restoration on process exit.

The Python module reserves its owner process-wide. This is **not** a mutex over
other processes or legacy/OpenCV backends: the parent must coordinate release
of every existing owner first. `owner_release_confirmed=True` is a caller
attestation, not a device-exclusivity probe. No hardware check is launched merely
by importing this module or running the offline fixtures.

The read-only probe uses the explicit name `operator_owner_release_attested`
(`--operator-owner-release-attested`). It also requires `--expected-moniker` from
prior metadata-only **IMoniker::GetDisplayName** enumeration, not DevicePath/PnP
ID. Index plus exact name must match before BindToObject. Missing/mismatch fails
closed; old index-only native probing is disabled. Internal Suite startup/cleanup
and adapter latches reject probing; **no cross-process/global exclusivity is
established**. See NEGOTIATION_DIAGNOSIS.md for repository ownership findings and
parent process-check obligations. No new user hardware authorization is needed.

## Offline build/tests: junior execution only

Requirements: existing VS 2022 Build Tools (C++/Windows SDK), x64 Python, local
NumPy/OpenCV for conversion fixtures. No global changes. Source syntax-only
validation (`cl /Zs /W4 /permissive-`) passed for both C++ units on 2026-11-02;
Python AST parse passed. **The designer did not link, run fixtures, open devices,
launch SteamVR, or perform hardware tests.** The first syntax pass exposed a
missing SDK `CLSID_NullRenderer` declaration; the documented CLSID is now local
alongside the legacy Sample Grabber interface declarations.

From the repo root, a junior should run:

```powershell
powershell -NoProfile -File tools\psvr_ps4_dshow\verify_offline.ps1 `
  -PythonExe "$PWD\.venv-camera\Scripts\python.exe" -BuildTimeoutSeconds 120
```

This builds **only this standalone project** under the approved
`C:\Users\rickk\AppData\Local\Temp\opencode\psvr-ps4-dshow-offline-*` directory,
not the root solution. Configure/build have 120-second caller deadlines; native
fixture execution has 15 seconds, Python fixtures 30 seconds. Logs are retained
there and only their last lines printed. Timeout does not forcekill; stop the
workflow and report the retained offline PID. Nothing loads the resulting DLL
into the application. The native fixture executable includes production helpers
but never calls enumerate/open/CoCreateInstance; it uses fake COM objects.

Manual equivalents in an existing x64 Developer PowerShell (`$b` must be a new
approved temp directory; use the script above for outer deadlines):

```powershell
cmake -S tools/psvr_ps4_dshow -B $b -G 'Visual Studio 17 2022' -A x64
cmake --build $b --config Release --parallel 2
& "$b\Release\psvr_ps4_dshow_offline.exe"
& .\.venv-camera\Scripts\python.exe -m unittest -v tests.test_camera_ps4_owned_dshow
```

Fixtures cover same-filter COM identity, real production property routing/
Manual flags, range rejection before write, foreign-owner guard, original-state
restoration, failed-restoration retention/retry, copied buffers, callback storage
lifetime, stop publication guard, invalid sample length/dynamic type latching;
Python fake-native open/control/Stop deadlines, no reopen over retained owner,
no default writes, malformed samples/targets, timestamps and exact crop geometry.
They do **not** simulate a complete DirectShow graph or prove runtime filter
availability. Link/build errors remain possible until the junior run succeeds.

### Runner follow-up (2026-11-02)

The first junior configure run completed CMake generation but printed
`configure: exit=`: Start-Process's returned object had a null ExitCode, and
PowerShell's `$null -ne 0` wrongly entered the failure branch. This was a runner
failure, not evidence of CMake failure. The runner now owns the original
System.Diagnostics.Process handle and reads its exit code once after the bounded
wait; null remains an explicit harness error, never coerced to success.
cmd.exe `/d /v:off /s /c` supplies direct native stdout/stderr file redirection,
avoiding managed async pipe pumps and a second, unbounded WaitForExit drain.
Native command status is propagated by cmd; log text never determines status.
Timeout closes only the observation handle, never kills the wrapper/child.
Raw logs preserve bytes; Python output is UTF-8 and VS diagnostics request
English. Tail previews use UTF-8; localized native tools may still have another
encoding, which cannot change pass/fail. Parent Windows PowerShell logs created
with default Out-File can be UTF-16; read those using `-Encoding Unicode` or
write the parent log with explicit `-Encoding utf8`.

The added review fixtures exercise the production configure_transport helper
for fixed 166666 and 166667, rejected rates/bounds and clamped readback; the
production SampleCB with a full claimed length but a tiny allocation (zero
GetPointer calls); and exported close with all three properties, failures in
each position, later mismatch, first-error retention and pending-only retry.
There are now **9 native fixture groups**. These additions and the runner fix
still require the junior's runtime execution; static validation is not a pass.

## Separate hardware gate (parent coordination required)

Do not load the DLL/open a camera until the parent confirms all previous owners
are released and explicitly schedules the probe. No SteamVR is needed. Before
default integration, evidence must include: filter availability (Sample Grabber
is deprecated and may be absent), negotiated media types, consistent bytes/crops
against baseline, actual frame cadence, successful -7/8/1 Manual and native
readbacks, failed-write behavior, clean owner release/restoration, hotplug/error
paths and repeat-open only after confirmed cleanup. No hardware probe command
is bundled here to avoid accidental execution during the offline gate.

Sample Grabber is an **unsupported legacy API**, accepted here as an install-free
PoC only when already present. If missing, fail closed; do not register a filter
or install anything. A production replacement could implement an in-process
DirectShow sink (larger COM surface) or evaluate Media Foundation native-control
support, but neither is claimed implemented/tested by this patch.

## Official sources

* Microsoft, [Selecting a Capture Device](https://learn.microsoft.com/en-us/windows/win32/directshow/selecting-a-capture-device): monikers, unique DevicePath, BindToObject/AddFilter.
* Microsoft, [IAMCameraControl](https://learn.microsoft.com/en-us/windows/win32/api/strmif/nn-strmif-iamcameracontrol): query the filter controlling the camera.
* Microsoft, [IAMVideoProcAmp](https://learn.microsoft.com/en-us/windows/win32/api/strmif/nn-strmif-iamvideoprocamp).
* Microsoft, [IAMStreamConfig](https://learn.microsoft.com/en-us/windows/win32/api/strmif/nn-strmif-iamstreamconfig): native stream format negotiation.
* Microsoft, [VIDEO_STREAM_CONFIG_CAPS](https://learn.microsoft.com/en-us/windows/win32/api/strmif/ns-strmif-video_stream_config_caps): min/max frame interval (no temporal step), deprecated spatial members, SetFormat may round, GetFormat readback required.
* Microsoft, [Using the Sample Grabber](https://learn.microsoft.com/en-us/windows/win32/directshow/using-the-sample-grabber): unsupported status, callback constraints, connected format, Null Renderer, no VideoInfo2 support.
* Microsoft, [Top-Down vs. Bottom-Up DIBs](https://learn.microsoft.com/en-us/windows/win32/directshow/top-down-vs--bottom-up-dibs): YUV orientation does not follow RGB biHeight flipping.
* Microsoft, [CoInitializeEx](https://learn.microsoft.com/en-us/windows/win32/api/combaseapi/nf-combaseapi-coinitializeex): balance successful initialization on the same thread.
