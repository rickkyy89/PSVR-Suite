# PS4 owned negotiation failure: offline diagnosis, NOT a hardware fix

## Latest evidence: nominal 120 Hz candidate is eligible for a 60 Hz request

Parent completed the identity-checked read-only probe (reported exit 0).
Read-only review of
`%LOCALAPPDATA%\Temp\opencode\ps4-owned-candidates-20261102-070520\finite-summary.json`
confirms one bind, no prohibited-stage records, owner_done=true and
cleanup_pending=false. Candidate **2** is 1748x408 YUY2 FORMAT_VideoInfo,
biSizeImage=1426368, image/height ratio=3496, nominal AvgTimePerFrame=83333;
caps interval bounds are 83333..1250000, including 166666 and 166667.
The previous prototype rejected it as `interval_not_60hz_quantized` before
SetFormat. This proves a **candidate-selection policy defect**, not that the
driver accepts 60 Hz or that Sample Grabber/ConnectDirect will succeed. Existing
hardware evidence is read only, never rewritten. No new hardware was run here.

Current `configure_transport` separates exact layout eligibility from actual
transport acceptance:

1. Keep every existing layout gate: major/subtype, FORMAT_VideoInfo, complete
   header, 1748x408 positive height, 16 bits/one plane, YUY2 compression, exact
   1426368 image bytes. Caps bounds must remain positive/ordered; inconsistent
   published intervals outside their own bounds remain rejected.
2. Preserve a published 166666/166667 if already 60 Hz and within caps. Otherwise
   choose **166667**, or **166666 only if 166667 is outside the caps range**.
   Min/max membership makes the candidate selectable for a request, not proof
   of discrete rate support. A locked 120 Hz candidate is ineligible.
3. On the first eligible candidate, modify ONLY AvgTimePerFrame in the owned
   GetStreamCaps allocation. Call SetFormat **once**, then GetFormat. Require all
   strict actual-format gates, actual interval within caps and exact equality
   to the requested 166666/166667. Reject ignored/clamped/stale 120 Hz and layout
   changes. Connected source/grabber and dynamic sample gates stay strict 60 Hz.
4. Failure is terminal for this open and propagates to existing owner cleanup.
   **No second 166666 attempt after a failed 166667**, no next-candidate scan,
   reopen, fallback 120 Hz or stale-frame acceptance. This intentionally avoids
   retrying after SetFormat may have partly mutated driver state.
5. Read-only inspection never changes the header or calls SetFormat. It reports
   `candidate_selection`: `published_is_60hz`, `selectable_for_60hz_request`,
   `requested_interval_100ns`, `driver_acceptance="not_tested"`. Media rows keep
   the original nominal interval. Eligible is explicitly **unverified**, not
   accepted. The old ambiguous `exact_matches` summary field is replaced by
   `selectable_candidates`, `published_60hz_candidates` (eligible candidates
   already nominally 60 Hz) and
   `validated_60hz_readbacks` (always zero in read-only mode). A normal graph
   emits separate original `stream.selected`, `stream.requested` and actual
   readback rows. Logging remains bounded at source with reserved footer space.

Added native production-helper fixture definitions cover 120-Hz nominal ->
SetFormat60 -> GetFormat60, the 166666-only bound, failed SetFormat with no retry
or further scan, ignored120/changed60 readback, actual and candidate subtype/
padded-image rejection, and locked120. Existing published60 preservation and
read-only fixtures remain. Current definitions: **22 native groups + 71 Python
methods**, not a pass claim. Junior must run the offline command below and
record a new verified DLL/hash before parent-coordinated live work. Gate remains
OFF by default. No tracker, calibration, config, device or Runtime changes.

Static review: strict actual/connected/dynamic gates still call the full
60-Hz predicate; only candidate eligibility was separated. A positive caps range
is never used as a readback substitute. No hardware fix is claimed. This
revision has not been compiled or tested by the designer. Static checks only:
Python AST parsing 9 files OK, 71 Python test methods defined, 22 native fixture
functions and 22 main calls counted, PowerShell runner parser 0 errors. These
counts do not establish C++ compilation or any test pass. The previous depth-limit restriction still
prevents junior delegation from this session; junior execution is a handoff.

## Historical STOP evidence / downstream cause still unproven

The parent hardware report `docs/CAMERA_OWNED_PS4_HARDWARE_20261102.md` and
`%LOCALAPPDATA%\Temp\opencode\ps4-owned-hardware-20261102-0627\phase_ab_result.json`
show an open failure 0x80040200 with transaction idle, zero frames, no control
targets/readbacks, followed by owner_done=true / cleanup_pending=false. DLL hash
was `f1e44f52a9643b999ed49e9babd98af1f264c47164fcb2485b18db9df638eaef`.
The old report did NOT contain caps/media types or an internal failure stage.
No retry was performed by this designer. Existing evidence is not rewritten.

The installed SDK `10.0.26100.0\um\vfwmsgs.h` defines VFW_E_INVALIDMEDIATYPE as
0x80040200 (line 57), distinct from VFW_E_TYPE_NOT_ACCEPTED 0x8004022A (line 435).
The owned code also produces VFW_E_INVALIDMEDIATYPE for its own validations.
Therefore the HRESULT cannot identify SetFormat, GetFormat, ConnectDirect,
connected-media validation or candidate rejection. **No specific device cause
is proven. In particular, biSizeImage/stride is NOT assumed to be the cause.**

The initial patch addressed diagnostic ambiguity. The latest caps evidence and
candidate-policy correction above supersede the historical published-60-only
selection rule; exact actual transport gates remain unchanged.

## Actual OpenCV 5.0.0 source comparison

Text-only installed metadata identifies opencv-contrib-python-headless
**5.0.0.93**, also cv2/version.py. No cv2 import/capture was run. Comparison uses
the actual upstream **5.0.0 tag**, not master or an invented API:
https://github.com/opencv/opencv/blob/5.0.0/modules/videoio/src/cap_dshow.cpp

| Upstream tag code | Owned code / implication |
|---|---|
| Lines 1159, 2624-2630: requestedFrameTime=10000000/fps and writes AvgTimePerFrame before SetFormat | Owned now preserves a published 60-Hz interval, or explicitly requests 60 Hz within caps from an exact-layout non-60 nominal entry. Driver support still requires SetFormat + exact GetFormat. Candidate 2 confirms the former published-only policy rejected before SetFormat. |
| Lines 2596-2618: starts from GetFormat, modifies dimensions/subtype/rectangles and lSampleSize; YUY2 size is w*h*2 | Owned accepts exact enumerated YUY2 metadata and checks biSizeImage; unknown/stale/zero image size is rejected, not guessed or normalized. |
| Lines 2892-2909: requests **RGB24** at Sample Grabber for ordinary YUY2 input | Owned requests **YUY2** at the grabber and decodes with cv2 YUY2->BGR. The two graph paths are not interchangeable proof of output bytes/parity. |
| Lines 2933-2939: RenderStream(PIN_CATEGORY_PREVIEW, ...), intelligent graph building | Owned FindPin capture category, ConnectDirect capture->grabber->null. It forbids silently inserted converters and validates both connected media types. |
| Lines 1459-1476: FOURCC comes from selected videoType; FPS from requestedFrameTime | Historical reported YUY2 / 60.00024 does not prove the grabber received YUY2 or expose negotiated raw caps. 60.00024 is consistent with 10000000/166666. |
| Lines 1543-1585: ordinary grabber bytes processed as RGB24/BGR; special NV12 conversion differs | No assertion that our cvtColor path is byte-identical to a DirectShow RGB converter without an explicit real baseline comparison. |
| Lines 155, 172 and 197-237: Sample Grabber CLSID/IID/vtable | Same C1F400A0-3F08-11D3-9F0B-006008039E37 / 6B652FFF-11FE-4FCE-92AD-0266B5D7C78F and methods. Missing/unsupported system filter must fail; never install/register one. |

The wheel's exact build/submodule commit was not independently proven; the
comparison is explicitly the published 5.0.0 source plus installed version
metadata, not a claim of binary identity. Historical logs referenced by parent:
`logs/ps4_camera/ps4-led-20261023T223359952187Z.json` and
`ps4-stereo-points-20261028T173528721315Z.json`.

Microsoft documents that YUV is top-down regardless of biHeight sign, but the
legacy Sample Grabber explicitly rejects negative biHeight and VideoInfo2 even
when SetMediaType succeeds. Neither blindly flipping height nor changing to
RGB24 is an acceptable unverified fix.

## New bounded diagnostics

Native session stores JSONL metadata in <=64 KiB, at most 64 candidate media rows
plus selected/actual/connected rows. Extra rows are counted, not dumped. Reserved
footer space always retains terminal_stage/terminal_hr even when logs truncate.
No device paths, monikers, names, serials, samples, properties or image data.

Stages distinguish:
* selection/bind, control-interface QI (normal capture only), capture pin/config;
* GetNumberOfCapabilities (count/struct size), each bounded GetStreamCaps row;
* exact candidate rejection, SetFormat, GetFormat and its separate validation;
* Sample Grabber creation/SetMediaType, each ConnectDirect;
* grabber GetConnectedMediaType, capture ConnectionMediaType and validations;
* callback registration, Run, and failed Stop cleanup.

Rows include major/subtype/format GUIDs, format-block size/presence/header_parsed, sample-size
metadata, bitmap header size, signed width/height, bits/planes/compression,
biSizeImage, AvgTimePerFrame, rectangles, caps min/max intervals/output ranges/
steps. `image_div_abs_height` is ONLY a ratio of reported metadata, **not a claim
of actual allocator stride**. Required YUY2 stride 3496 is a nominal contract.
VideoInfo2/short/null blocks are described without treating them as accepted.
The same production predicate supplies explicit rejection reasons.

Initial GetFormat is a best-effort diagnostic observation, never an automatic
SetFormat/fallback. `first_failed_observation` may therefore be advisory;
`terminal_stage` identifies the actual returned failure. C diagnostics getter is
owner-only metadata copy, not a second device query. Python report reads a cached
snapshot. Copy failure cannot replace the original open HRESULT; earlier cached
records are retained. While native open is still blocked, new native progress
cannot be fetched from a foreign thread; ownership stays retained on timeout.

No changes to exact 1748x408 YUY2 / 166666-166667 policy, GetSize/actual-length
checks, source/grabber validations, row stride, x=48 crop, 640x400 eyes, timing,
COM ownership, transaction/cancellation/persistence, calibration or controls.

## Read-only candidate probe (parent-coordinated, after review)

`OwnedDshowCapture.inspect_index` requires `expected_moniker` from a **prior
metadata-only DirectShow enumeration**, explicitly `IMoniker::GetDisplayName`.
This API does NOT accept an `IPropertyBag` `DevicePath`, PnP instance ID, numeric
index or VID/PID as an identity substitute. Do not strip prefixes, lowercase,
reconstruct a moniker from PnP or silently resolve the current index at probe
time: that would defeat the independent expected-identity check. If parent only
has PnP DevicePath, obtain the actual DirectShow display name first, without
binding any filter (the existing C `psvr_dshow_enumerate` export returns PS4
display names and does not BindToObject; its callback is PS4-only and does NOT
report full-category numeric indices).

The native export `psvr_dshow_inspect_index_exact_moniker` requires **both** the
full video-category index and that exact display name to match. Missing identity
is rejected before session allocation; mismatched index/name fails before any
BindToObject. A production guard rereads GetDisplayName on the selected moniker
immediately before its single bind. A reordered index does not fall through to
search/bind the expected camera elsewhere. The old index-only C probe export is
retired and always returns E_INVALIDARG/no handle; the Python loader requires
the new export, so stale DLLs cannot silently downgrade the identity check.

The probe reuses the owner reservation/gate and binds ONLY the explicitly
selected, identity-matched PS4 runtime filter once. It creates a stopped
graph to locate that capture pin, reads GetFormat/GetStreamCaps, and closes on the
same owner. It does NOT query camera-control interfaces/ranges, SetFormat,
ConnectDirect, Sample Grabber callbacks, Run, image reads or control setters.
Other monikers may be enumerated as selection metadata, never bound.

This is read-only at the API-call level: driver activation/BindToObject can still
initialize device state. Do not promise physically immutable mode just because
our code calls no setter. Parent must coordinate owner release and inspect
device/process state before/after; **global exclusivity is NOT established**.
No other camera, firmware, driver or settings
may be probed. A timeout/failed Release retains the owner, handle/apartment and
child process; report PID, no retry/reopen/foreign Release/forcekill.

### Ownership contract and repository findings

Read-only source search across `driver`, `scripts`, `tools`, `tests` (389 scoped
source files; Drive staging, binaries and large files excluded) found no PS4
camera OS-shared lease. `tools/psvr_diag/device_session.hpp:13` names
`Local\\PSVR-PC-DeviceSession`, used at `tools/psvr_diag/main.cpp:467` and
`driver/driver_psvrpc.cpp:435` for HMD/diagnostic ownership, NOT PS4 camera owners.
It is not reused or changed: coupling a camera probe to HMD/Runtime ownership
would be incorrect and outside scope.

The existing camera guards are `ps4_owned_dshow._active_owner` (strong reservation
through late cleanup), and `ps4_suite_source._active_session` under `_owner_lock`.
Probe admission now also checks the loaded Suite's startup/cleanup latch, with
Suite-lock-before-adapter-lock ordering and no lock held across the caller's
open wait. A detected internal owner fails closed before native construction.
Default owned Suite acquisition participates in the adapter latch. Legacy
OpenCV/board-preview/tools and arbitrary external apps do NOT share it. Extending
a new OS mutex to every legacy acquisition/control path is not a small safe
probe-only correction, and an adapter-only mutex would give false assurance.

Therefore the explicitly chosen permitted contract is **operator attestation**:
`--operator-owner-release-attested` / `operator_owner_release_attested=True`.
It does not query other processes, does not lock across processes, and cannot
prove that untrusted/non-participating apps released the device. The ordinary
capture compatibility argument `owner_release_confirmed=True` means the SAME
operator attestation, never a measured exclusivity check. Parent checks known
Suite/adapter/probe/legacy processes and device state before/after; if another
owner is found or release is uncertain, STOP. No process termination is added.
Reports explicitly record `cross_process_exclusivity_established=false`; native
identity strings are not written to the metadata report.

The command below NEVER enables a gate itself. It is **not executed by the
designer**. Hardware authorization already exists; no additional user request
is needed. The outstanding gate is junior verification and parent review. Parent
must already set the child-local enable gate, verify a newly built DLL/hash, and
coordinate known owners' release, without claiming universal exclusion. Keep
the failed known DLL/artifacts unchanged.

```powershell
# Existing gate remains OFF until parent approval; this command alone cannot enable it.
& .\.venv-camera\Scripts\python.exe -m tools.psvr_ps4_dshow.probe_candidates --index 0 --expected-moniker '<exact-prior-IMoniker-GetDisplayName>' --dll '<absolute-new-verified-DLL>' --output "$env:LOCALAPPDATA\Temp\opencode\ps4-owned-candidates-reviewed.json" --timeout 4 --operator-owner-release-attested
```

Metadata output is restricted to approved temp .json. No images are written. On
pending cleanup the script waits to retain the owner; an outer deadline must NOT
be interpreted as owner release or permission to kill/retry it.

## Junior offline gate, then retry plan

No build/test/runtime was executed by the designer for this patch. AST/parser
only. New fixtures exercise production selection/trace wrappers with the SAME
HRESULT at different stages, published non-60-Hz intervals, negative heights,
zero image size, VideoInfo2, capped logging, no-SetFormat enumeration, cached
diagnostics, cancellation and owner-only read-only cleanup.

```powershell
powershell.exe -NoProfile -File .\tools\psvr_ps4_dshow\verify_offline.ps1 -PythonExe "$PWD\.venv-camera\Scripts\python.exe" -BuildTimeoutSeconds 120
& .\.venv-camera\Scripts\python.exe -m unittest -v tests.test_camera_ps4_owned_diagnostics
```

Added identity fixtures exercise the actual production pre-bind guard: missing
identity, another PS4 with the same VID/PID, case changes, DevicePath-like values,
failed metadata read, and one bind only after exact equality. Python fixtures
cover missing identity/no native creation, explicit attestation, detected Suite
startup/cleanup, an inspection deadline followed by blocked owner close, adapter
latch retention/no reopen, and failed close followed by explicit owner retry.
They spawn no processes and cannot prove cross-process exclusion; no such claim
is made.

### Historical identity/ownership review (before parent's completed probe)

Intent: prevent index reorder/wrong-unit activation while stating ownership
honestly. The production selector and pre-bind guard are both covered by native
fixture definitions; stale-index tests reject matching names at earlier/later
indices and another same-VID/PID PS4 at the selected index. Required-export
versioning prevents a silent fallback to the retired probe. These are positive
fail-closed properties. No critical/major implementation defect was identified
in this static review under the explicit operator-attestation contract; no
cross-process exclusion proof exists or is promised. No clarification or further
user authorization is needed. **Verdict: comment / pending junior build, fixture
execution and independent parent review; not a live-probe pass.** Subagent
delegation was attempted but blocked by the session depth limit. The designer
ran only Python AST parsing (9 files, 71 test methods defined) and PowerShell
parser validation (0 errors); C++ was read, not compiled. No hardware, tests,
builds, process/device queries or live enumeration were executed for this patch.

Expected definitions now: **22 native groups + 71 unique Python methods**. This
is NOT a claim they pass. Offline children continue forcing the live gate OFF.

1. Junior build/fixtures and independent review; record NEW DLL hash.
2. Parent already completed the identity-checked read-only probe. Do not
   automatically repeat it. Retain exact identity/ownership safeguards for any
   future acquisition; process checks remain operator attestation.
3. After this request-policy correction passes junior verification and review,
   caps alone still cannot establish driver/graph acceptance. Parent may
   coordinate an instrumented normal graph open under existing authorization
   with initial_controls=None / restore_controls=True to identify the precise
   failing graph stage, with no control transaction and no image persistence.
   That step may SetFormat/Run and is NOT the read-only probe or auto fallback.
4. Only after actual 60-Hz transport/graph readiness is observed and a reviewed correction passes offline
   tests may parent retry profile/readiness/cadence/parity. Stop at first failure;
   no phase-B/Tracking/SteamVR escalation from a failed negotiation.

References:
* [OpenCV 5.0.0 cap_dshow.cpp](https://github.com/opencv/opencv/blob/5.0.0/modules/videoio/src/cap_dshow.cpp)
* [Microsoft SetMediaType, matching/limitations](https://learn.microsoft.com/en-us/windows/win32/directshow/isamplegrabber-setmediatype)
* [Microsoft BITMAPINFOHEADER, YUV sign/stride semantics](https://learn.microsoft.com/en-us/windows/win32/api/wingdi/ns-wingdi-bitmapinfoheader)
* [Microsoft VIDEO_STREAM_CONFIG_CAPS](https://learn.microsoft.com/en-us/windows/win32/api/strmif/ns-strmif-video_stream_config_caps)
