# PS Eye webcam tracking (M9, webcam side)

This document covers the autonomous, webcam-side half of M9: turning an
explicitly selected camera into a **metric** position stream for the driver
agent. It complements, and does not replace, the driver-side receiver and the
IMU orientation work in M3. The tools live in `tools/psvr_camera/` and are
documented for operators in `tools/psvr_camera/README.md`.

The **native capture path** (`python -m psvr_camera pseye`) *was* exercised on a
physical PS3 Eye through its existing libusb-win32 binding and delivered real
frames; the measured frame rates are in `tools/psvr_pseye/README.md`. The same
native backend is now also available to **`calibrate live`** and **`track`**
through `--backend pseye`, using an OpenCV-compatible `read()/release()` adapter
(verified on the same hardware: 640x480 and 320x240 real BGR frames). The metric
half is still not validated against a physical marker: the geometry, calibration
math and protocol are covered by synthetic tests only. Treat every metric number
a real device reports as something to verify, not as a known constant.

## 1. What "metric" means here, and what it does not

A position is emitted in **metres** only when both of these hold:

1. `calibration.Intrinsics.calibrated` is true and was loaded from a strict
   intrinsics file produced by a chessboard calibration; and
2. the marker geometry has provenance `user_measured` or `datasheet`.

There is no face tracking, no learned model and no arbitrary unit. Nothing in
this package maps "pixels of a face" or "AITrack gains" to metres. If the
intrinsics are not calibrated, or the geometry is not measured, the estimator
refuses and no metric packet is produced.

The PSVR built-in LED ring is a real physical feature, but **its 3D layout is
not calibrated here**. A geometry with `provenance: "psvr_builtin"` is rejected
by `MarkerGeometry`, so no code path can quietly treat the headset LEDs as a
known rig. Tracking the headset LEDs is possible only after the operator
measures the actual LED positions of their own unit and supplies them as a
`blob_led` geometry.

## 2. Recommended default: a measured ArUco marker

The reasoned default is an externally measurable **ArUco** marker, because:

* its corners are detected with a stable, defined order (top-left, top-right,
  bottom-right, bottom-left), so the object/image correspondence needs no
  manual annotation;
* its size is something the operator can measure with calipers, giving a
  genuine metric scale;
* a single square marker supplies a 6DoF pose, but two physically different
  planar poses can both lie in front of the lens. With OpenCV IPPE available,
  the estimator compares their reprojection scores and rejects genuine
  front/front ambiguity; positive depth alone does not resolve it;
* it is independent of Sony firmware and of the headset's undocumented LED
  layout.

`tools/psvr_camera/geometry_example_aruco.json` is an **example**, not a
default: its `marker_length_m` must be replaced with a real measurement. Print
`DICT_4X4_50` id 0 with the built-in generator (OpenCV contrib needed for ArUco;
the chessboard needs no OpenCV):

```powershell
# ArUco marker PNG plus a matching user_measured geometry JSON:
python -m psvr_camera pattern aruco --dictionary DICT_4X4_50 --id 0 `
    --marker-mm 100 --out aruco_0.png --geometry-out geometry_aruco.json

# Chessboard SVG at a measured square size (prints dimensionally true):
python -m psvr_camera pattern chessboard --cols 9 --rows 6 --square-mm 25 `
    --out chessboard.svg
```

Mount the marker rigidly on the headset and **measure the printed black square
side with calipers**; pass that measured value (not the requested one) as
`marker_length_m`. The generator never invents a size: `--marker-mm` and
`--square-mm` are values you measured.

The alternative is a `blob_led` geometry: a list of measured 3D LED/blob
positions in metres. It is solved with DLT PnP plus RANSAC. It requires the
operator to actually measure each emitter's position; a guessed layout is not
accepted.

## 3. Hardware notes and limitations

* **PS Eye basics (documented, not verified here).** Sony PS Eye is a
  USB camera (VID `1415`, PID `2000`). Its commonly quoted modes are
  640x480 at 60/75 fps and 320x240 at 120/187 fps, with a wide-angle lens
  (about 75 degrees diagonal). Do not trust these numbers as measured; run
  `probe` and read the negotiated mode and achieved frame rate.
* **Wide-angle distortion.** The lens distortion is significant, which is
  exactly why a chessboard calibration is mandatory before any metric claim.
* **USB driver.** The OpenCV paths read through the OS capture stack; the
  native PS3 Eye path talks directly to the interface's existing
  **libusb-win32** binding via `tools/psvr_pseye`. Neither installs, replaces,
  rebinds or reflashes a USB driver, and neither needs administrator rights. If
  the camera does not open, fix the environment manually; do not expect this
  package to change the binding.
* **DirectShow is not enough.** When the video interface (interface 0) is bound
  to libusb-win32, DirectShow/OpenCV does not expose the native video stream.
  The PS3 Eye delivers video over a **bulk** endpoint (`0x81`, 512-byte
  packets); the native backend reuses the upstream `inspirit/PS3EYEDriver`
  OV534/OV772x initialisation and UVC-payload reassembly instead of guessing
  register writes. See `tools/psvr_pseye/README.md`.
* **IR and LED visibility.** Whether the PS Eye's IR/visible response makes the
  PSVR LEDs easy to detect is unmeasured here and is not assumed. The ArUco
  path avoids relying on it.
* **Latency and frame rate.** Not measured. Measure achieved frame rate with
  `probe` and measure end-to-end latency before enabling any prediction.
* **Single camera.** One camera gives position and orientation only while the
  marker is visible; occlusion causes a `lost` frame, and the receiver must
  freeze or fade rather than hold a stale pose.

## 4. Procedure

### 4.1 Inventory and probe

```powershell
python -m psvr_camera inventory --max-index 6
python -m psvr_camera probe --index 0 --backend dshow --frames 180 --max-seconds 3
```

`inventory` reports every probed index; it never auto-selects. `probe` opens the
one index you chose and reports the negotiated resolution/FPS, frames captured
and dropped, timeouts, achieved frame rate and errors. Pick the index and
backend from that evidence, not from a guess.

### 4.2 Native PS3 Eye capture (libusb-win32, no OpenCV)

When interface 0 is bound to libusb-win32, OpenCV cannot see the stream. Build
and use the native backend instead:

```powershell
powershell -File tools/psvr_pseye/build.ps1            # stages out/pseye/
python -m psvr_camera pseye inventory                  # read-only, no capture
python -m psvr_camera pseye probe --index 0 --width 640 --height 480 --fps 60 --frames 120
```

`pseye inventory` reports the DLL provenance and the connected count without
starting the camera. `pseye probe` opens one explicit index, captures up to
`--frames`/`--max-seconds`, and prints the standard `psvr.camera.capture` JSON
(negotiated mode, frames captured/dropped, timeouts, achieved fps, errors).
`achieved_fps` measures the steady-state capture loop (open/init and warmup are
excluded); `duration_s` and `started_utc`/`ended_utc` bound the loop.

### 4.3 Calibrate the intrinsics

Print a chessboard, measure the square size, and calibrate either from saved
images or live:

```powershell
# From images (OpenCV detects the board):
python -m psvr_camera calibrate offline --cols 9 --rows 6 --square-mm 25 `
    --images .\calib\ --out intrinsics.json

# Or live through the OpenCV stack:
python -m psvr_camera calibrate live --cols 9 --rows 6 --square-mm 25 `
    --index 0 --backend dshow --views 15 --out intrinsics.json

# Or live through the native PS3 Eye (libusb-win32):
python -m psvr_camera calibrate live --cols 9 --rows 6 --square-mm 25 `
    --index 0 --backend pseye --width 640 --height 480 --fps 60 `
    --views 15 --out intrinsics.json

python -m psvr_camera verify --intrinsics intrinsics.json --image-size 640x480
```

`--cols/--rows` are **inner corners** (squares minus one). The loader is strict:
wrong schema/version, non-finite values, a malformed camera matrix or an
image-size mismatch are hard errors, including a nonzero lower-triangular
`K[1,0]` or a singular K. The live collector fixes the image size on
the first frame and refuses a mid-stream resolution change. The offline
image collector takes the size from the first accepted chessboard view,
not a rejected photograph, and refuses to mix accepted resolutions. Both calibration
paths fail closed on a high reprojection RMS (`--max-rms-px`, default 1 px;
OpenCV's own RMS when OpenCV is used) instead of writing an unusable file.
The NumPy quality score reprojects per-view orthonormal poses using the shared
returned K; independently fitted homographies are not a calibration-quality
gate. Invalid/non-finite solver outputs are refused, not marked calibrated.
Without OpenCV, the deterministic Zhang linear estimate is used; it is intended
for tests and for pre-checking, and should be verified before metric use.

For planar estimates, `PositionResult.physical_ambiguity_checked` states
whether physical branches were checked. IPPE uses the generic solver on
rays normalized with the full K inverse (including skew), and scores in
pixels with K. Duplicate numeric branches and alternatives behind the camera
do not invalidate an otherwise good pose. When IPPE is unavailable or fails,
the original NumPy diagnostics remain usable but report
`physical_ambiguity_checked=false`: `ambiguity_ratio=0` is then not proof of
uniqueness. This planar-marker policy does not change the suite's non-planar
LED matcher, head-rotation limits or accepted profiles.

### 4.4 Track and emit

```powershell
# Through the OpenCV stack:
python -m psvr_camera track --intrinsics intrinsics.json `
    --geometry geometry_example_aruco.json --index 0 --backend dshow `
    --udp 127.0.0.1:4271

# Through the native PS3 Eye (libusb-win32):
python -m psvr_camera track --intrinsics intrinsics.json `
    --geometry geometry_aruco.json --index 0 --backend pseye `
    --width 640 --height 480 --fps 60 --udp 127.0.0.1:4271
```

Use `--no-send --json-lines` to inspect packets without a receiver. The
estimator rejects frames above the reprojection limit (`--max-reprojection-px`,
default 2 px), frames whose planar pose is ambiguous, and non-planar rigs with
weak depth conditioning; rejected frames are emitted as `valid:false` with a
  `state` of `lost`, `ambiguous` or `error` and a short `note`.

Two safety behaviours matter for the wire:

* **The intrinsics `image_size` is checked live, before any estimate.** The
  loaded calibration must match the actual frame size; a mismatch (for example a
  320x240 stream with 640x480 intrinsics) aborts with `ok:false` and no metric
  packet is ever emitted. The native adapter additionally refuses to open a
  requested mode the shim would snap to a different resolution, and `read()`
  never reports success for a frame whose size differs from the calibration.
* **The stream always ends with a `valid:false` sample.** On `--max-seconds`,
  `--frames`, a camera disconnect/read-failure run, or Ctrl-C, the sender emits
  one terminating invalid packet so the receiver cannot hold a stale pose. A
  run of consecutive read failures stops the loop instead of spinning.

`--frames` counts **captured** frames, not valid ones, so a bounded run always
terminates even while the marker is not visible. Bounds and smoothing values are
validated as non-negative/positive at parse time.

## 5. Coordinate convention and UDP protocol

The stream is the versioned `psvr.position` protocol; the authoritative copy is
`tools/psvr_camera/PROTOCOL.md` plus the machine-readable
`tools/psvr_camera/protocol.json` (asserted equal to `protocol.py` by a test).

* Right-handed metres: **+X right, +Y up, -Z forward**.
* The OpenCV optical frame (+X right, +Y down, +Z forward) is converted by the
  sender with `(x, y, z) -> (x, -y, -z)`, so the receiver never needs to know
  the camera convention.
* `orientation_wxyz` is `world_from_body`, WXYZ, unit norm, and is optional
  (the driver already owns orientation from the IMU).
* Default UDP endpoint `127.0.0.1:4271`, configurable; chosen to avoid AITrack
  `4242`.
* The position is relative to the camera optical centre; the driver applies its
  own recenter offset.

### Driver agent coordination (required before enabling)

This package does not decide the driver's receiver; it defines the contract and
reports it. The driver agent must confirm, via its own file/report:

1. the UDP bind host/port (default `127.0.0.1:4271`) and whether loopback is
   sufficient;
2. that `psvr_world` (+X right, +Y up, -Z forward) matches the driver pose
   frame;
3. the staleness timeout applied to `monotonic_s`;
4. whether `orientation_wxyz` is ignored (recommended for M9);
5. how `valid:false` frames are handled (freeze/fade, no stale hold).

Until that agreement exists, the stream stays off. `protocol.json` and
`PROTOCOL.md` are the hand-off artifacts.

## 6. Tests and registration

All tests are hardware-free and synthetic:

```powershell
python -B -m unittest discover -s tests -p "test_camera*.py" -v
```

They cover: known projection recovered to metric pose, planar two-fold
ambiguity and positive-depth rejection, RANSAC outlier rejection, missing and
non-finite data, strict intrinsics acceptance/rejection, Zhang recovery of a
known camera matrix, protocol round-trip/validation, the coordinate transform,
bounded capture reporting, explicit index selection with an injected fake
`cv2`, and OpenCV-absence handling. The native backend is covered the same way
with an injected fake `psvr_pseye.dll` function table, including the
`read()/release()` adapter (negotiated shape, wrong-resolution refusal, bounded
read timeout, release). `tests/test_camera_pipeline.py` drives the whole
pipeline with a fake source, detector and socket: synthetic projections through
the real estimator, filter, tracker loop and UDP sender, asserting the captured
frame bound, the live image-size gate, the read-failure stop and the terminating
`valid:false` packet. A real capture runs only when `PSVR_PSEYE_HARDWARE=1` is
set.

The parent owns CMake. To register the suite (no root file is edited by this
work), add inside the existing `if(BUILD_TESTING)`/`if(Python3_FOUND)` block:

```cmake
add_test(NAME camera_python COMMAND ${Python3_EXECUTABLE} -B -m unittest discover
    -s "${CMAKE_SOURCE_DIR}/tests" -p "test_camera*.py" -v)
set_tests_properties(camera_python PROPERTIES WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})
```

## 7. What is explicitly not claimed

* A physical PS3 Eye **was** exercised for native capture and for the new
  `read()/release()` adapter (real 640x480 and 320x240 BGR frames, and the
  fail-closed wrong-resolution refusal), but no physical marker, calibration
  target or driver receiver was exercised. A hands-off environment cannot
  perform a physical chessboard calibration, so no real intrinsics are produced
  or claimed here.
* No camera intrinsics, marker size or LED layout is known a priori. The
  `pattern` command only prints a target at the size *you* measured; it never
  substitutes a guessed size.
* No PSVR built-in LED geometry is calibrated; such a geometry is refused.
* No face tracking, no arbitrary-unit pose and no IMU orientation is produced
  here.
* No USB driver, device binding or firmware is modified.
* No achieved frame rate or latency is promised; measure it.
