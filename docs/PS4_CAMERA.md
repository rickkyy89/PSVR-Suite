# PS4 Camera: acquisition and experimental Suite stereo tracking

The Suite **Camera** tab has a **PS4 Camera · stereo capture** button. This
opens an independent panel with USB diagnostics, explicit OpenCV device index,
MSMF/DirectShow selection, per-eye mode/FPS and a bounded L/R preview (60 s,
ESC or Stop to close). It does not change the current tracking camera,
SteamVR settings, headset drivers or PS3 Eye calibration.

The panel also offers **Check LEDs (10 s)**: finite, per-eye candidate counts,
signal checks and gated static patch-motion statistics. It saves the complete
JSON plus one annotated L/R image in `logs/ps4_camera/`; the panel displays a
bounded summary. No exposure/gain changes or positions are sent to the driver.
See [PS4_LED_STATIC_CHECK.md](PS4_LED_STATIC_CHECK.md) for the real static test.

**Guided stereo calibration** reuses the PS Eye monitor UI and timed capture
guide, with manual Start, both-eye corner overlays and temporary bright controls.
Instructions: [PS4_GUIDED_CALIBRATION.md](PS4_GUIDED_CALIBRATION.md).

## Existing projects researched (2026-10-24)

| Project | Role / decision |
| --- | --- |
| [psxdev/OrbisEyeCam](https://github.com/psxdev/OrbisEyeCam) | Original Windows firmware loader; UVC through Windows Media Framework. Chosen external preparation path. |
| [Hackinside/PS4-CAMERA-DRIVERS](https://github.com/Hackinside/PS4-CAMERA-DRIVERS) | Repackages the original loader/installer with a missing DLL; not an independent capture implementation. |
| [Hackinside/Playstation-camera-drivers](https://github.com/Hackinside/Playstation-camera-drivers) | Links original Windows work and PS4/PS5 firmware; PS5 camera is a different device. |
| [bigboss-ps3dev/PS4EYECam](https://github.com/bigboss-ps3dev/PS4EYECam) | libusb reference, originally tested on macOS; documents transport layout and both eyes. Used as format documentation, no source copied. |
| [tu-darmstadt-ros-pkg/ps4_stereo_camera](https://github.com/tu-darmstadt-ros-pkg/ps4_stereo_camera) | Experimental Linux V4L2/ROS stereo driver; not a Windows backend. |

Technical reference: [PlayStation Camera for Windows 10, part 2](https://psxdev.github.io/luke2.html).
No upstream installer, firmware, DLL or source is bundled or downloaded by the
integration. Upstream redistribution/licensing must be reviewed before bundling.

## Connection / firmware (manual)

1. Connect via a PS4 Camera **AUX → USB 3 adapter**, e.g. Sony PSVR adapter,
   directly to a working USB 3 SuperSpeed port. This camera has no USB 2 data
   fallback; an adapter/hub can be visible without the camera enumerating.
2. **Detect USB** reads present PnP devices only:
   - `05A9:0580`: boot mode; firmware required. Boot driver errors may need
     the original external WinUSB setup procedure.
   - `05A9:058A` or `05A9:058B`: runtime/UVC identity (firmware/model dependent).
     `uvc_present` is NOT proof of successful video capture.
   - `not_detected`: Windows does not currently enumerate these identities.
     Check cable/adapter/USB 3 port, reconnect camera after connecting adapter.
   - `device_error`: runtime identity found but Windows reports a problem.
3. If boot mode is present, follow the **original OrbisEyeCam** instructions
   to install its boot binding and upload firmware. This is a separate manual
   action; do not bind WinUSB to runtime UVC or unrelated devices. Firmware is
   volatile and may need reloading after disconnection. Inspect/trust external
   executables yourself; the Suite does not execute them.
4. Select the camera's explicit UVC/OpenCV index and backend. PnP enumeration
   does not map Windows identities to OpenCV indices. Generic inventory can
   help identify indices; no first-camera fallback is used.
5. Select **DirectShow (`dshow`)**, the default backend and the one verified
   below. Start preview, confirm both views and their physical order, then stop.
   Close Windows Camera/OBS if they own the camera; check Windows camera
   privacy permission for desktop applications if UVC is present but cannot open.

The `PS VR 3D Audio`, `PS VR BulkIn`, `PS VR H.264` devices belong to the
**headset**, not this camera. Their warning icons do not diagnose camera setup.
Do not change their drivers for this integration.

## CLI

```powershell
.\.venv-camera\Scripts\python.exe -m tools.psvr_camera.ps4_camera --inventory
.\.venv-camera\Scripts\python.exe -m tools.psvr_camera inventory --backend dshow --max-index 4
.\.venv-camera\Scripts\python.exe -m tools.psvr_camera.ps4_camera --index 0 --backend dshow --mode 640x400 --fps 60 --seconds 5
.\.venv-camera\Scripts\python.exe -m tools.psvr_camera.ps4_camera --index 0 --backend dshow --mode 640x400 --fps 60 --seconds 60 --preview
.\.venv-camera\Scripts\python.exe -m tools.psvr_camera.ps4_led_probe --index 0 --backend dshow --seconds 10 --output-dir logs/ps4_camera
```

Index `0` is an example, **not automatic camera identification**. All capture
commands emit requested/negotiated mode, errors, timeouts and delivered pair
count as JSON. `ps4_stereo.stream_pair_fps` measures delivered host callback
intervals (not exposure timestamps), separately from startup-inclusive wall FPS.
PS4 cleanup has a bounded 500 ms wait in addition to capture duration.
Preview uses Tk, so headless OpenCV builds are supported without HighGUI.
Exit 0 = successful capture (or UVC identity present for inventory),
2 = unavailable/failure, argparse errors = 2. LED probe adds exit 3 for delivered
images with no bilateral LED candidates (not necessarily a capture failure).
Optional OpenCV is already a Suite
dependency; no new native library is required.

## Stereo contract / future 6DoF

### Chessboard controls versus LED controls

The short-exposure LED profile is not appropriate for detecting a printed chessboard.
For calibration collection, use temporary exposure/gain/brightness controls
that make both black/white squares visible, then restore the saved PS4 values
before returning to LED tracking. These are two different acquisition tasks.
A dark-looking background can help isolation, but **a dark room is not a
prerequisite**. Brightness is not exposure: brightness 0 alone does not undo
the longer checkerboard integration time. OpenCV/DirectShow
control units are device-dependent: do not copy PS3 Eye exposure `13` to PS4.
The PS Eye board already available in `out/pseye/chessboard_9x6_20mm_A4.svg`
can be reused, with its real printed square size checked; new paired PS4
observations are still required. The existing PS3 calibration stays intact.

`tools.psvr_camera.ps4_board_preview` provides a bounded temporary-control
preview. All control changes are explicit; originals are saved after mode
negotiation, rollback writes are always attempted on the owning capture/thread,
and failures/pending cleanup are reported. ESC/window close restores normally;
forcibly killing the process or unplugging USB can prevent restoration.

On this OV580/DirectShow setup, exposure `-3`, gain `4`, brightness `4` were
the observed LED baseline. A temporary gain/brightness `8` trial made the
headset/environment more visible. The driver accepted writes but immediate
readback still showed old values. This does **not** establish whether the
cause is delayed device reporting, firmware, or another driver behaviour.
`--allow-delayed-readback` explicitly permits such an unconfirmed observation;
the default is to abort. After the trial, a new capture independently read
`-3/4/4` and the images returned to their dark baseline. These numbers are
device-specific, not portable defaults. That preview alone does not calibrate;
the subsequent paired-board collection is documented in the guided workflow.

The persistent Suite path now additionally selects a frame-bounded manual
exposure from the actual DirectShow range, rather than inheriting exposure
when the JSON profile leaves it null. It uses IAMCameraControl Manual=2 (and
IAMVideoProcAmp Manual=2 for requested brightness/gain), not OpenCV's numeric
exposure setter with Flags=0. The temporary checkerboard wrapper above keeps
its separate restoration contract. Matching numeric readback without Manual
flags is not exposure confirmation; short exposure alone is not valid 6DoF.
See [PS4_EXPOSURE_20261101.md](PS4_EXPOSURE_20261101.md) for measured controls
and the initial failed tests. The subsequent matched-clutter correction and
explicit −7/8/1 profile produced valid Suite/driver 6DoF in the current light,
with remaining losses: [PS4_FRONT5_DIAGNOSTICS_20261101.md](PS4_FRONT5_DIAGNOSTICS_20261101.md).

```powershell
.\.venv-camera\Scripts\python.exe -m tools.psvr_camera.ps4_board_preview --index 0 --seconds 60 --gain 8 --brightness 8 --allow-delayed-readback --preview
```

### Transport modes and pair contract

| Per eye | UVC transport | Supported nominal FPS |
| --- | --- | --- |
| 1280×800 | 3448×808 | 60, 30, 15, 8 |
| 640×400 | 1748×408 | 120, 60, 30, 15, 8 |
| 320×192 | 898×200 | 240, 120, 60, 30 |

These are documented nominal rates; only 640×400 at 60 FPS has been measured
on this PC (see verification below). OpenCV must deliver
unscaled BGR uint8 at the expected transport dimensions. For each row, crop
48 transport pixels (96 bytes in YUY2), then one left and one right image;
discard auxiliary columns and eight trailing rows. **Do not split the whole
frame in half.** Unknown/raw/scaled formats fail closed. L/R follows upstream
naming, not a physically validated order. Each callback receives independent
arrays from the **same UVC frame**, not two independently timed captures.

`capture_stereo(..., on_pair=callback)` is the acquisition extension point.
No camera exposure timestamps, calibration or pose are claimed. The bounded
diagnostic loop is not yet a production low-latency tracking source.
Paired chessboard calibration has subsequently been completed; the validated
reference is `configurations/ps4_stereo_640x400_calibrated.json` (details in the
guided workflow). Next phase: verify physical eye order, rectify,
detect corresponding headset LEDs, triangulate
with uncertainty/reprojection checks, then fuse with time-aligned IMU using
the existing driver bridge. Never reuse PS3 Eye intrinsics for either PS4 eye.

## Verification status (historical checkpoint 2026-10-24, superseded by PS4_SUITE_SOURCE_20261028 + FRONT5_20261101)

Initial local PnP query on 2026-10-24: **no PS4 Camera boot/runtime identity
enumerated**. At that stage, acquisition and preview were implemented but real
stereo capture and FPS were **not hardware-verified**; the subsequent hardware
results below supersede that initial limitation. Synthetic tests cover all crops, atomic
pairs, incompatible frames, invalid modes/rates, timeouts, cleanup and PnP
classification. No tracking/calibration settings have been changed.

Local verification: 261 tests passed, 4 skipped across the targeted camera and
Suite regression groups (plus subtests). A real Tk panel create/update/destroy
smoke test passed. Independent read-only PnP/CIM checks found no camera-class
device or unknown USB descriptor failure; the three headset warnings in Device
Manager are code 28 (missing drivers), not PS4 Camera devices. Historical PS3
Eye instances are non-present. These checks do not prove a camera hardware fault.

### Subsequent hardware verification (harness date: 2026-10-24, historical checkpoint superseded by PS4_SUITE_SOURCE_20261028 + FRONT5_20261101)

- With user authorization, external preparation files were downloaded to
  temporary storage from `psxdev/OrbisEyeCam` at commit
  `b823725f34fcf67d83f398a88b7ca48ed84ad746`. They are **not bundled** with
  this integration. Upstream MD5 checksums matched; this is a consistency
  check, **not signature-based security verification**. The installer wrapper
  reported `NotSigned`; the extracted `dpinst64` and `dpscat` reported `Valid`.
- Setup was executed. Boot device `05A9:0580` had a working WinUSB driver.
  `firmware.bin` (68,032 bytes, MD5 `24afe3c941dc599aafd23ac5170a4d14`)
  was uploaded, after which runtime device `05A9:058B` appeared as
  `USB Camera-OV580`, in the Camera class, with status OK.
- MSMF could not open index `0`. DirectShow at index `0` negotiated
  **1748×408 YUY2 at 60 FPS**. A 10-second capture delivered **482 stereo
  pairs at 640×400 per eye**, with **60.126 FPS between callbacks** and
  **48.294 FPS over total wall time including startup**. No capture errors
  or read timeouts were reported. These are different timing measures, not
  evidence of a sustained 48 FPS sensor mode.
- The initial run reported `cleanup_pending: true` with the 50 ms release
  wait. PS4 capture now allows a bounded 500 ms cleanup wait and stops before
  scheduling a read at its intentional end deadline. Subsequent 60 FPS tests
  completed with no errors, no timeouts and `cleanup_pending: false`.
- Additional 5-second DirectShow probes (FPS measured between delivered callbacks):
  **640×400 at 120 FPS**: 364 pairs, 120.069 FPS; **1280×800 at 60 FPS**:
  183 pairs, 59.867 FPS. Both completed without errors or pending cleanup.
  **320×192 at 240 FPS failed** after one pair with a read timeout despite
  successful mode negotiation; do not treat 240 FPS as verified or recommended.
  A subsequent 640×400/60 probe succeeded, confirming recovery without reconnect.
  These are short acquisition checks, not sustained tracking benchmarks.
- A photo confirmed images from both eyes. The images were dark; the user
  explicitly confirmed that a dark background is desirable for LED isolation,
  as with the previous camera. No exposure increase or image brightening was
  applied. LED visibility in both eyes remains to be verified.
  Physical left/right order remains unvalidated. At that acquisition-only stage
  stereo calibration and stereo tracking/6DoF were not implemented. The later
  paired-board calibration is now saved, but stereo tracking/6DoF remains to be
  implemented; successful acquisition alone does not establish tracking readiness.

### Static stereo-points trial (2026-10-24, visor still ~30 s)

1688 pairs at 60.08 callback FPS, 1687 `points` states (1662 with 5 points),
reprojection RMS 0.145 px, depth 0.746–0.843 m, with
`configurations/ps4_stereo_640x400_calibrated.json`. No head pose, anatomical
IDs, UDP or 6DoF; left-camera coordinates only; host (not exposure) timestamps.
Details and files: [PS4_STEREO_POINTS.md](PS4_STEREO_POINTS.md).
