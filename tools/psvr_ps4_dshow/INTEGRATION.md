# ABI v2 staged PS4 integration: review/offline gate before hardware

Hardware STOP/negotiation follow-up: see [NEGOTIATION_DIAGNOSIS.md](NEGOTIATION_DIAGNOSIS.md).
The parent observed open 0x80040200 before controls, clean owner release and zero
frames. Its specific stage/cause remains UNKNOWN. The new metadata diagnostics
and separately gated read-only selected-filter probe do not relax the YUY2,
stride/length, frame timing or crop contract and are not a claimed hardware fix.
Current offline definitions: **23 native groups + 78 unique Python tests**;
new changes still await junior build/tests and parent review before any probe.

Offline pixel-parity follow-up: [PIXEL_PARITY.md](PIXEL_PARITY.md). Added an
independent scalar BT.601 limited-range oracle, known BGR anchors, coordinate-rich
packed YUY2 boundary/crop fixtures, detached-eye checks, unchanged-detector mask
comparisons and production native callback padded-sample guards. No conversion,
threshold, crop or calibration change. Actual DirectShow system-converter parity
is explicitly deferred (virtual-source/allocator/sink infrastructure required),
not replaced by cv2-to-itself comparisons and not claimed as hardware evidence.

Latest parent read-only probe found exact-layout candidate 2 nominally 120 Hz
with caps including 60 Hz. Candidate selection now preserves published60 or
requests 166667 (166666 only if 166667 is out of bounds), changing only the
interval before one SetFormat + exact GetFormat verification. Range eligibility
does not establish driver support. No retry after SetFormat failure; actual,
connected and dynamic formats remain strictly 60 Hz with all layout gates.
Read-only diagnostics distinguish selectable/unverified from nominal fps and
validated readback. Parent probe evidence is preserved; no hardware fix claimed.

Probe identity/ownership review correction: `inspect_index` now requires an
expected exact **IMoniker::GetDisplayName**, not IPropertyBag DevicePath/PnP ID.
Index AND display name match before BindToObject; missing/mismatch means no bind.
Native uses a new required export; old index-only probe is fail-closed/retired.
The flag is now `--operator-owner-release-attested`, explicitly NOT cross-process
exclusion. No PS4 camera OS lease was found; the existing HMD DeviceSession mutex
is unrelated and unchanged. Internal Suite startup/cleanup and adapter owner
latches reject probing; legacy/external owners require parent process checks.
No global exclusivity is claimed. See the diagnosis document for evidence,
limitations, new fixture definitions and updated parent command. Hardware was
already authorized; review/verification remains outstanding, not user permission.

## P1 atomic commit/result completion (latest follow-up)

Scalar/admission follow-up: set_manual returns its transaction's separately
preallocated frame-16 readback, revalidated by native commit. It performs NO
post-commit get_control RPC: a later poll timeout cannot falsely fail that Set.
Queue admission preallocates pending metadata and reserves capacity under the
same condition used by all producers/owner dequeue. Revision, active decision,
slot and deadline change only after successful queue insertion. Full/failed put
or stop/expiry during registration cancels and unregisters only the new decision,
without poisoning acquisition or leaving a phantom revision. Duplicate readiness
admission cannot replace an existing pending decision. Nine added fake
regressions cover post-commit barriers/failing getters, queue saturation/retry,
Full after registration, and stop races for transactions and readiness ACKs.
Current runner: **15 native groups + 62 unique Python methods**, not yet executed
for this revision. Focused junior command:

```powershell
& .\.venv-camera\Scripts\python.exe -m unittest -v tests.test_camera_ps4_owned_admission
```

Watchdog follow-up: `_check_deadline` no longer poisons from a stale pending
status sample. Expiration performs the same decision cancel CAS as RPC waiters:
CommittedSuccess publishes the prepared result and returns without error/stop;
Cancelled/Failed follows fail-closed cleanup. Before an acceptance decision
exists, startup remains nonpersistent/rollbackable. The watchdog only tries the
condition nonblocking and never waits on an owner/COM lock. Four deterministic
regressions suspend its pending read, allow startup/runtime commit to win before
timeout arbitration while the owner is paused before publication, and cover the
opposite cancel-wins outcomes. No runtime test execution is claimed.

The reviewer found a REAL window after persistence ACK but before dispatcher
done.set: a timeout could falsely fail the same operation whose rollback
originals had already been discarded. Moving a done check under a Python lock
alone does not close the native-commit/result-publication window.

The current protocol uses a 16-byte aligned, shared native decision. One
lock-free 64-bit atomic word contains phase + failure HRESULT: Pending ->
CommittedSuccess / Cancelled / Failed. Terminal results cannot be overwritten.
Native compile-time assertions require lock-free atomics and trivial storage
lifetime. Required new symbols reject older DLL revisions before capture open.

Lifecycle/linearization:

1. Owner prepares the complete success payload BEFORE entering the native final
   ACK. Runtime transaction return data is separately preallocated, not copied
   from mutable status after commit. Registered decisions remain strongly held
   by command/owner/caller/token and keep their ctypes storage + DLL alive.
2. COM open/Set/Get/readback/Stop and ALL native final ACK calls are OUTSIDE the
   Python condition. No external/native call can hold the condition while a
   driver blocks. Status/cancel decision calls touch atomics only, never COM or
   camera/session handles; those metadata operations are explicitly thread-safe.
3. The native final ACK validates everything first. Its successful CAS publishes
   COMPLETED SUCCESS before discarding originals. After CAS there are no fallible
   operations, allocations, COM calls or mutex acquisitions, only fixed owner
   state assignments. This is the single authoritative commit/completion point.
4. Python owner normally materializes accepted state/result and signals done
   under one condition lock before unlocking. A waiter/status observer can help
   materialize the same already-committed record if the owner is suspended after
   native CAS. Python done/Event notification is a hint, NOT completion truth.
5. Waiters poll the atomic outcome with bounded event waits. At deadline they
   CAS Pending -> Cancelled, not blindly poison after a missed Event. If commit
   won, they return the preallocated success even when notification is absent;
   a later stop/stream error cannot retroactively turn that RPC into failure.
   If cancel won, the native ACK cannot discard originals after unblocking.
6. No timeout branch waits for an owner condition. Helping metadata uses only a
   nonblocking acquire; cancelled/failure paths latch stop + queued fault without
   waiting for the owner lock. Therefore a blocked COM call does NOT extend the
   caller deadline or cause foreign-thread close. Cleanup still stays pending
   until the actual owner returns and verifies restoration/Stop.
7. Stop latches the admission Event and cancels registered decisions without
   acquiring the owned condition. Registration rechecks Event, so a racing stop
   cannot miss a new decision. For an in-flight ACK, commit vs cancellation is
   decided by the CAS winner (overlapping calls may linearize either way). A
   later stop of committed acquisition is normal shutdown, not failed startup.

This protocol also covers persistent runtime transaction finish, not just the
startup ACK. Startup still finishes with persist=False until post-commit sample
validation, decoding and pump acceptance; those earlier P1 protections remain.
The rule is not a claim of hard real-time OS scheduling or interruptible drivers:
an indefinitely paused/blocked owner remains retained. No caller waits for that
COM execution merely to resolve a command's timeout/immutable outcome.

Six new deterministic Python regressions pause the fake native path AFTER CAS
but BEFORE notification, exercise real pump readiness, race stop before/after
CAS, and block simulated COM verification BEFORE commit. They distinguish a
committed-but-unnotified success from genuinely pending COM and check restoration
and owner retention. Native fixtures execute production CAS/ACK bodies, including
cross-thread metadata cancellation races and immutable first-error publication.

The runner now defines **23 native groups + 78 unique Python test methods**.
Helper TestCase classes are imported through fixture modules rather than exposed
as module-level classes: this avoids unintentionally collecting the same baseline
tests multiple times; baseline coverage remains in its original module. No tests
or build were executed by the designer for this revision. Earlier pass counts do
NOT verify this change. Gate defaults remain OFF; offline children force OFF.

Junior verifier (same existing environment; no live enablement):

```powershell
powershell.exe -NoProfile -File .\tools\psvr_ps4_dshow\verify_offline.ps1 -PythonExe "$PWD\.venv-camera\Scripts\python.exe" -BuildTimeoutSeconds 120
& .\.venv-camera\Scripts\python.exe -m unittest -v tests.test_camera_ps4_owned_completion
```

The prior sections below retain design history; their old lock-based ACK and
test-count statements are superseded by this atomic completion section.

## P1 readiness/cancellation follow-up (supersedes early-startup ACK below)

Both review findings were real: a stop while open_index was blocked could miss
the not-yet-published owner, and finish(persist=True) discarded startup originals
before the first post-commit frame/decode/pump acceptance. Neither behavior is
considered acceptable merely because the earlier offline fixtures passed.

* StereoSession creates a shared CancellationToken BEFORE factory/open. It wraps
  the existing stop Event. stop() sets it before taking the session condition;
  open wait checks it every <=20ms, and the owner checks it before creation,
  after blocked open, before transactions and at the final acceptance point.
  A blocked owner stays retained; no caller calls its COM or releases its handle.
* The owned condition and token use one RLock, not two inversely ordered locks.
  No blocking COM/driver operation holds this lock. Session never holds its own
  condition while waiting for an acceptance owner RPC. Cancellation therefore
  does not wait behind open, Set, readback, Stop, decode or a pump RPC.
* Startup ALWAYS calls finish_controls(persist=False). It preserves the native
  lifetime originals and goes through awaiting_frame -> awaiting_acceptance.
  The first valid post-commit RAW sample lets open return a candidate, NOT an
  acquisition-ready/persistent camera. Public raw reads do not publish that
  unaccepted startup slot. Decoded candidate pairs are marked readiness_pending.
* Only after a post-commit sample was actually read, validated and decoded does
  the module issue a bounded decoded-candidate lease. The pump validates revision
  and fixed BGR eye geometry, then explicitly asks accept_readiness(sequence,
  revision). No slot is published to tracking before that owner ACK succeeds.
* The new required native accept_persistence symbol only performs a pure owner
  state transition. It rejects sequences at/before the commit barrier or beyond
  frames actually delivered by native read (a barrier alone is not delivery).
  Shared cancellation and ACK have one lock-protected linearization point: an
  earlier stop wins and no persistence happens. A stop AFTER acceptance is an
  ordinary shutdown of an already accepted acquisition; no promise is made that
  a user consumer had time to consume the accepted pair before that later stop.
* If the first post-commit frame is absent, or size/decode/geometry validation,
  open/decode/ACK cancellation, or acceptance fails, originals remain eligible
  for close restoration. Failure is terminal, nothing uncertain is published,
  and failed restoration retains the owner/apartment for explicit owner retry.

The offline runner forces PSVR_PS4_DSHOW_ENABLE=0 in its children only, even if
a parent environment later contains a live opt-in. It does not alter the parent.

The numeric C ABI remains v2 with an additive required readiness symbol; Python
rejects an older v2 DLL missing that symbol BEFORE opening a capture. It never
falls back. Existing gate variables remain unmodified and closed by default.

New deterministic fake regressions cover blocked open/Set + bounded stop, 16 unique
settling frames followed by no frame, invalid-size post-setup sample, undecoded
candidate rejection, decode failure, stop during decoding, cancellation before
ACK, correct explicit acceptance, and restore failure/quarantine/retry. A native
fixture exercises finish(false), rejection of undelivered sequences, persistence
acceptance, close restoration/Auto flags and failed restoration retention.

Current runner defines **14 native groups + 43 Python tests** (32 previous + 11
readiness). Parent's earlier 12-group/43-pytest pass is evidence for the previous
revision only; neither these P1 fixtures nor the C4701 fixture are claimed passed
until the junior reruns. C4701 initialization fix remains in describe(); error
HRESULTs and failed-call output semantics are preserved. No test/build/hardware
execution was performed by the designer for this follow-up.

Junior commands, existing environment, outer timeout/logging as before:

```powershell
powershell.exe -NoProfile -File .\tools\psvr_ps4_dshow\verify_offline.ps1 -PythonExe "$PWD\.venv-camera\Scripts\python.exe" -BuildTimeoutSeconds 120
# Focused readiness regressions only; no native DLL/hardware loaded:
& .\.venv-camera\Scripts\python.exe -m unittest -v tests.test_camera_ps4_owned_readiness
```

## Evidence and gate

The parent reports round2 exit 0: **9 native groups + 14 Python tests** for the
previous PoC. This revision adds transactions and integration. Its tests have
NOT been executed by the designer. No build, runtime fixture, physical camera,
SteamVR, install, registration, driver binding, deploy or vrsettings operation
was performed. Syntax/static checks do not establish runtime correctness.

Real persistent `ps4-stereo` now routes to `OwnedDshowCapture`, not OpenCV capture.
It **fails closed** unless a parent-reviewed process explicitly supplies both
`PSVR_PS4_DSHOW_ENABLE=1` and an existing verified ABI-v2 DLL through
`PSVR_PS4_DSHOW_DLL` (or an explicitly supplied PS4 DLL path). This patch does
not set those variables, copy a DLL, save preferences or create a deployment.
Old ABI-v1 DLLs are rejected before COM capture open. No failure silently falls
back to index-bound OpenCV controls/capture. The source is staged, not declared
hardware-ready or a proven resolution of the original -7/8/1 regression.

## Capture/control ownership

One native owner thread/MTA binds one selected moniker and reuses that exact
IBaseFilter for capture, IAMCameraControl and IAMVideoProcAmp. `open_index`
enumerates the full video category once and binds only the selected PS4 runtime
device: an index is **selection**, never proof of identity between two captures.
Wrong VID/PID fails before bind. No parallel control binder is created. Exact
moniker selection remains available. There is no automatic alternate-device
choice/reopen. The application/parent still coordinates other owners/processes;
the module reservation is not a cross-process exclusivity guarantee.

The callback registration/flags are unchanged. Transport stays 1748x408 YUY2,
verified actual 166666/166667 interval (requested within caps if nominal differs),
exact size/capacity validation and copied latest
buffer. Decode remains OpenCV YUY2->BGR, row crop x=48, first 400 rows, two
640x400 eyes. Suite right/left reference mapping, detector thresholds,
calibration/rig files and their hashes are unchanged. Completion time remains
host perf_counter immediately after native read, never exposure time.

## Multi-property transaction

1. Request takes a revision, clears the Python latest slot immediately and
   blocks publication while pending/applying. One edit at a time. Inputs are
   copied; caller threads never call COM. Startup uses revision 0.
2. Owner snapshots all three native ranges/current values/flags. Validate ALL
   requested integer targets, range, step and Manual capability before ANY Set.
   Exposure None resolves using the existing 60-Hz LED policy; unspecified gain
   is untouched. C++ begin independently repeats requested-property validation
   on the SAME capture filter, and snapshots originals before the first write.
3. Apply every requested target with explicit Manual=2. Register even a failed
   Set as potentially mutating. Native begin flushes the current native slot and
   returns a write-boundary sequence. A driver HRESULT failure is terminal.
4. Immediate readback is recorded, NOT treated as sufficient settlement. Drain
   **16 unique new samples** strictly beyond that boundary; duplicates/stale
   sequences do not advance the count. Inspect every requested value/Manual
   flag at frames **4 and 16**. All settling samples are discarded. Mismatch at
   frame 4 may resolve by 16; any getter failure or mismatch at 16 fails closed.
5. Native commit rechecks all values/flags and returns another flushed boundary.
   It keeps the rollback snapshot ACTIVE. Only after the owner checks deadline/
   cancellation does a nonblocking, COM-free native finish acknowledge success.
   That acknowledgement is linearized against caller cancellation using the
   Python condition lock. No blocking driver operation holds that lock.
6. Publication resumes only for a strictly newer post-commit sequence. Conversion
   carries a revision token; a request accepted during conversion causes that
   old result to be discarded. StereoSession checks revision/state again before
   publishing. Ps4SuiteSource serializes read vs accepted request and clears its
   last_pair snapshot. TrackingWorker independently rejects unsettled/failed or
   revision-mismatching pairs before pose processing.

Transaction deadline defaults to 1.5 seconds for asynchronous edits. Startup
shares the open/first-frame deadline. No sleeps/reopens count as settling.
Consumers can observe `pending` without falsely treating a short polling timeout
as camera detachment; an expired transaction poisons the source even if its COM
call is still blocked. Readiness requires confirmed startup plus a fresh pair.

The 4/16-frame policy is host-delivery based, NOT proof of sensor exposure
latency. Real OV580 confirmation/latency, 60-Hz cadence and image parity still
require the coordinated hardware review gate.

## Failure, rollback and persistence

Every transaction error clears publication and is terminal, even if rollback
succeeds. Native rollback attempts every touched property best-effort with its
original Auto/Manual flags and verifies **every requested property**, including
untouched ones after partial apply. It preserves the first HRESULT, retains
unconfirmed snapshots, and returns per-property confirmation bits. Later errors
never erase the first error. No uncertain frame is sent to tracking.

Stop/cleanup happens only on the real owner. Failed rollback/Stop/restoration
retains the session, DLL, callback storage AND owner apartment; cleanup_pending
is not cleared merely because the StereoSession pump has ended. Status polling
is read-only and never calls release(0). An explicit release/stop may retry on
that same owner; bounded caller waits do not claim the driver was interrupted.
No foreign Release, forcekill, emergency unload, automatic reopen or unbounded
caller wait is used. A permanently stuck driver can retain its owner forever.

Raw PoC default restore_controls=True restores the pre-experiment values on
normal close. Suite explicitly sets restore_controls=False: ONLY a fully
confirmed/settled successful commit persists its acquisition settings on normal
close, matching the existing Suite lifetime policy. Failed/uncommitted edits
always rollback, even in this mode. This device-state policy is explicit in
reports; no profile/configuration/preferences file is changed.

## Observed status vs requested target

`controls_status()` returns detached snapshots:

* requested: last confirmed targets (not merely queued wishes).
* transaction.requested/resolved: pending request and native-policy-resolved
  targets; revision, state, error, write/commit sequence boundaries.
* native_exposure/native_gain/native_brightness: target, native range, before,
  actual readback value/flags, confirmed, readback_after_frames, same-filter
  identity confirmation; rollback_confirmed where relevant.
* transaction.settle_frames/settle_sequences/checkpoints: unique deliveries and
  frame-4/frame-16 confirmation observations.
* transaction.rollback / rollback_confirmed: native per-property verification
  results and retained first error; failure never masquerades as confirmed.
* report.cleanup_pending/cleanup_error/owner_done distinguish pump completion,
  actual cleanup and retained ownership. hardware_verified remains false.

Existing module/exposure_factory injections remain legacy offline-test seams;
an actual cv2 module or default real Suite acquisition uses the owned graph.
Supplying an owned_capture_factory enables offline owner-adapter wiring tests.
strict_controls remains accepted for UI compatibility, but real acquisition is
ALWAYS strict: legacy false cannot weaken capture identity or readback policy.
No UI or PS3 implementation/preferences were changed. Tracking never sends the
PS3 dll preference to PS4; the PS4 DLL has its own explicit source configuration.

## Junior execution commands (do not run hardware)

From the repo root, using the existing local environment:

```powershell
powershell.exe -NoProfile -File .\tools\psvr_ps4_dshow\verify_offline.ps1 -PythonExe "$PWD\.venv-camera\Scripts\python.exe" -BuildTimeoutSeconds 120
```

Builds only this standalone project in a new approved temp directory, then runs
**23 native groups and 78 unique Python tests** (14 baseline + 10 transactions + 8
wiring + 11 readiness + 10 completion + 9 admission + 9 diagnostics + 7 pixel parity). It neither loads/deploys the built DLL into the Suite nor enables the
hardware gate. Configure/build deadlines 120s, native 15s, Python 60s; on timeout
report the retained offline PID, do not forcekill or proceed to later stages.

Additional existing offline regression commands for the junior, with an outer
timeout of 180 seconds and log tail capture in their runner:

```powershell
& .\.venv-camera\Scripts\python.exe -m unittest -v tests.test_camera_ps4_stereo_session tests.test_camera_ps4_suite_controls tests.test_camera_ps4_suite_source tests.test_camera_ps4_dshow_controls tests.test_camera_ps4_startup_review tests.test_suite_camera_source tests.test_suite_camera_settings_ui
```

The new fixtures exercise the actual production Python transaction methods,
native begin/commit/finish/rollback functions with fake Windows COM, default
source wiring, missing decoder/DLL no-fallback behavior, target switches,
simultaneous Set/rollback/cleanup failures, duplicate-frame rejection, deferred
readback, preflight zero writes, stale conversion suppression and retained
cleanup ownership. They are NOT end-to-end live results or a complete fake
DirectShow graph. Parent must inspect results and perform independent safety
review BEFORE considering explicit enablement or any live probe.

## Official references

* [IAMCameraControl](https://learn.microsoft.com/en-us/windows/win32/api/strmif/nn-strmif-iamcameracontrol): query the filter controlling the camera.
* [Device selection](https://learn.microsoft.com/en-us/windows/win32/directshow/selecting-a-capture-device): moniker bind, exact capture filter, AddFilter.
* [VIDEO_STREAM_CONFIG_CAPS](https://learn.microsoft.com/en-us/windows/win32/api/strmif/ns-strmif-video_stream_config_caps): min/max, no temporal step, SetFormat may round; verify GetFormat.
* [Sample Grabber](https://learn.microsoft.com/en-us/windows/win32/directshow/using-the-sample-grabber): deprecated/unsupported filter, minimal callbacks; fail closed if unavailable, never install/register it.
* [CoInitializeEx](https://learn.microsoft.com/en-us/windows/win32/api/combaseapi/nf-combaseapi-coinitializeex): apartment ownership and balanced lifetime.
