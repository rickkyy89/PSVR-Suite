# Offline pixel/crop evidence — not actual DirectShow converter parity

## Scope and current evidence

`docs/CAMERA_OWNED_PS4_HARDWARE_20261102.md` records the latest parent live
baseline: 0/4504 optical-valid frames, at most four stereo matches, despite
confirmed transport/control acquisition. That is not proof of a pixel, crop,
orientation, physical eye-order or calibration defect. No live frame bytes were
saved, so this patch cannot retrospectively compare those pixels.

Independent source review found no proven flip/crop bug: packed YUY2 is top-down
regardless of positive height, and production crops start at x48, x688 and end
before x1328, using rows 0..399. OpenCV's older DirectShow path requested RGB24
through graph conversion; the owned graph supplies YUY2 and production uses
`COLOR_YUV2BGR_YUY2`. These two conversion paths are **not proven pixel-equivalent**.
No production conversion, crop, detector, threshold, preference, calibration,
driver, Runtime, UI or default hardware gate is changed by this patch.

## Implemented Python fixtures

`tests/test_camera_ps4_pixel_parity.py` has seven new tests. The old conversion
test in `test_camera_ps4_owned_dshow.py` no longer compares cv2 against cv2: it
now uses the same independent scalar oracle with a distinct unequal-luma pair.

* The scalar oracle derives BT.601 from Kr=.299, Kb=.114, Kg=1-Kr-Kb, limited-range
  Y=16..235 / Cb,Cr=16..240, full 8-bit output, half-up rounding and saturation.
  No cv2 conversion, implementation coefficients, production crop or capture
  code computes expected pixels. Hand-anchored BGR black, white, grey, red,
  green and blue vectors first check the oracle itself. The legal signal domain
  is explicit; no claim is made about superwhite/footroom or driver colorimetry.
* Known packed Y0 U Y1 V pairs have distinct lumas and non-neutral unequal U/V
  cases; each output pixel is compared against scalar BGR, including channel
  mapping. Neutral anchors must agree exactly. Independent color arithmetic
  allows **at most 1 LSB per channel**, only for rounding/coefficient precision;
  there is no percentage-outlier allowance, geometric tolerance or fitted
  offset/gain. Counts of differing channels/pixels and maximum difference are
  printed even when within that bound. They are not forced to zero in reports.
* A 1748x408, 3496-byte-row synthetic packed frame uses a 251-entry asymmetric
  coordinate field, with full-frame expected BGR derived from scalar palette
  entries. Marker pairs straddle x47/48, x687/688 and x1327/1328, with distinct
  y399/400 and other rows. Both complete 640x400 eyes are compared, with explicit
  corner/seam-row/column anchors. Mutation of prefix/tail/every excluded row must
  leave outputs equal to the original scalar oracle. Perturbations are checked
  to actually change excluded bytes and leave included bytes intact.
* Oracle sensitivity checks ensure vertical/horizontal flips, eye swaps,
  one-column/row shifts, dropping the prefix incorrectly, including trailing rows
  and RGB/BGR swaps are not accidental equivalent fixtures. Independent scalar
  counterexamples distinguish Y0/Y1 and U/V swaps. This establishes synthetic
  sensitivity, not physical left/right sensor identity.
* Eyes must own detached writable memory; mutation cannot alter the other eye
  or a new decode of the immutable raw sample.
* A stable blue/red/white-patch fixture compares the **unchanged production
  BlueLedDetector** on scalar BGR versus owned decoded BGR. A test-only forwarding
  module observes the actual final connected-components mask. Default bounds
  are identical for both inputs; the existing white-core branch is exercised
  off and on without preference reads/writes. Mask disagreement must be exactly
  zero; candidate dictionaries must match, one nonempty candidate per eye must
  exist, and mask area must be 384 or 400 pixels. These deliberately stable
  known colors do not prove invariance for threshold-edge colors, user-specific
  settings, actual DirectShow output, scene optics or the five-match gate.

## Implemented native callback fixture

`test_callback_coordinate_payload_exact_and_padded_sample_guards` calls the
production `SampleCB` with a fake IMediaSample, never a camera/filter graph.
The byte-rich packed payload is checked in full and at x47/48, 687/688,
1327/1328 and rows 0/399/400/407; source mutation must not change the mailbox.

An exact-length sample succeeds with either exact allocation or **spare
allocation capacity**. This preserves the existing GetSize >= frame contract;
allocator capacity is not actual image stride. Padded rows with padded actual
length, extra actual trailing bytes, and a full claimed length with insufficient
allocation all fail with VFW_E_BUFFER_OVERFLOW before GetPointer/copy. No sample
sequence is published and the mailbox stays untouched. This cannot detect a
driver dishonestly claiming a tight layout/length for incorrectly arranged
bytes; it is a guard/copy test, not an actual allocator measurement.

## Actual Windows DirectShow converter: comparison deferred, capability queried

This repository's isolated CMake target links SDK COM/DirectShow interfaces and
tests production helpers with fake COM objects. It has no test virtual source,
pin/allocator implementation or RGB24 sink. A scoped static lookup found no
`streams.h` at the installed SDK's `10.0.26100.0/um` include directory; this is
not a claim that BaseClasses are absent everywhere. SDK declarations alone do
not implement a streaming source or sink.

A subsequent **separate device-free capability probe** was built and run on
2026-11-07. See [`converter_capability_probe/README.md`](converter_capability_probe/README.md)
for source, exact observations, artifacts, bounded reproduction and the concrete
remaining implementation plan. It does not link/load the production backend,
enumerate devices, create a graph or deliver any sample. Both `CLSID_Colour`
(`{1643E180-90F5-11CE-97D5-00AA0055595A}`) and `CLSID_AVIDec`
(`{CF49D4E0-1115-11CE-B03A-0020AF0BA770}`) instantiated with their requested
identities. With proposed 1748x408 VideoInfo / 166667 x 100ns types, unconnected
input `QueryAccept(YUY2)` returned `S_FALSE` and `S_OK` respectively; unconnected
output `QueryAccept(RGB24)` returned `S_FALSE` on both. These are **not negotiated
types**; notably the AVI output rejection before connecting its input does not
establish that RGB24 is unsupported. Probe/build exit zero means completed
capability discovery, not pixel/detector comparison. No sample was converted.
Neither candidate is asserted to be the historical OpenCV conversion chain.

A genuine device-free graph is feasible in principle but not a small safe
fixture addition here. It needs an unregistered, in-process virtual source with
complete pin/media-type/refcount/allocator/sample-delivery semantics, the actual
system conversion chain, and a deterministic sink, all with bounded owner-thread
graph lifecycle/cleanup. It must record actual input/output types and inserted
filter CLSIDs: intelligent RenderStream can choose a chain different from a
directly instantiated converter. Replacing this with an MFT/DMO conversion or a
fake transform would not satisfy the requested actual-DirectShow comparison.
Even a successful comparison would characterize only the identified converter,
not establish that it is the historical OpenCV conversion chain.

Future acceptance requirements, before claiming this comparison:

1. Instantiate test source/sink directly, without registration/install, device
   enumeration/bind, camera access, driver, control writes or Runtime. Keep it a
   separate test-only target. No third-party/BaseClasses installation is implied.
2. Feed identical immutable synthetic YUY2 bytes to the actual graph and owned
   software path. Record actual converter identity and negotiated color metadata.
   Interpret RGB24 DIB channel order, signed-height orientation and aligned row
   size explicitly in the test sink (1748*3=5244 is already 4-byte aligned).
   Do not infer a flip merely from positive **YUV** height or modify production.
3. Report raw and normalized BGR maximum/mean error, differing channels/pixels,
   and unchanged detector mask/candidate differences under the exact same
   configuration. Report differences, do not tune thresholds to hide them.
4. Missing converter/unsupported connection must be reported as unavailable,
   not a parity pass. Bound waits and retain the graph owner after pending
   cleanup; never forcekill/release COM from a foreign thread.

**No actual DirectShow sample-conversion fixture or its comparison results are
supplied in this patch.** Only the independent capability probe is supplied.
The full fixture is explicitly deferred for the above complexity, as allowed
by the task. The scalar/mask fixtures close the cv2-self-comparison gap, not the
actual-system-converter evidence gap or the live optical failure.

## Junior commands / review gate

Current definitions: **23 native groups + 78 unique Python test methods**.
The offline runner includes the new module and still forces
`PSVR_PS4_DSHOW_ENABLE=0` only in its child environments. The new parity module
requires existing NumPy/OpenCV; missing dependencies are errors, not silently
skipped parity passes. No dependency installation is added.

```powershell
powershell.exe -NoProfile -File .\tools\psvr_ps4_dshow\verify_offline.ps1 -PythonExe "$PWD\.venv-camera\Scripts\python.exe" -BuildTimeoutSeconds 120
# Focused device-free Python checks; these do not load the owned DLL/open hardware:
& .\.venv-camera\Scripts\python.exe -m unittest -v tests.test_camera_ps4_pixel_parity tests.test_camera_ps4_owned_dshow.DecodeTests
```

Designer review is source/static only: no build, tests, hardware, converter graph,
SteamVR, deployment or configuration changes executed. Junior must report the
actual results and bounded pixel/mask metrics, including failures; a definition
count is not a pass. Delegation was attempted but blocked by the session depth
limit, so execution remains a junior handoff. No causal fix is claimed.
