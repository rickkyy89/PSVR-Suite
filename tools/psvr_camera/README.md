# psvr_camera — M9 webcam-side tools

Autonomous, additive Python tools that turn an explicitly selected camera into a
**metric position stream** for the PSVR driver agent. They never touch the PSVR
driver and never modify the Windows device binding. The optional native PS3 Eye
backend reads the camera's *existing* libusb-win32 binding; everything else goes
through the OS/OpenCV stack.

## Scope and safety rules

* **No implicit camera selection.** `inventory` lists every probed index;
  `probe`, `calibrate live`, `track` and `pseye probe` require an explicit
  `--index`. There is no "first working camera" fallback.
* **No driver changes.** The native backend (`pseye`) talks to the camera's
  already-installed libusb-win32 driver through `tools/psvr_pseye`. It never
  installs, replaces, rebinds or reflashes a driver and needs no administrator
  rights.
* **Bounded capture.** Every capture is capped by frames *and* seconds, and the
  JSON report states the negotiated mode, achieved frame rate, timeouts and
  errors.
* **Metric only with evidence.** Position is emitted only from explicit
  calibrated intrinsics plus a measured marker geometry. The PSVR built-in LED
  ring geometry is **not** calibrated and a `psvr_builtin` geometry is refused.
* **OpenCV is optional.** It is imported lazily. If it is missing the camera
  commands exit with a clear message; nothing is installed. Geometry,
  calibration math and the UDP protocol run on NumPy/stdlib alone.

## Dependencies

* Required: Python 3.11+, NumPy (already used by the project).
* Optional (camera/detection only): OpenCV. Use the wheel that matches your
  environment; for ArUco you need `opencv-contrib-python`.
* The parent environment owns dependency installation. This package never calls
  `pip` or writes outside its own paths.

## Commands

Run from the repository root (or add `tools` to `PYTHONPATH`):

```powershell
python -m psvr_camera --help
python -m psvr_camera protocol

python -m psvr_camera inventory --max-index 6
python -m psvr_camera probe --index 0 --backend dshow --frames 180 --max-seconds 3

# Offline calibration from already-detected corners (no OpenCV needed):
python -m psvr_camera calibrate offline --cols 9 --rows 6 --square-mm 25 `
    --detections detections.json --out intrinsics.json

# Offline calibration from chessboard images (OpenCV needed for detection):
python -m psvr_camera calibrate offline --cols 9 --rows 6 --square-mm 25 `
    --images .\calib\ --out intrinsics.json

# Live calibration from a camera (OpenCV or native PS3 Eye):
python -m psvr_camera calibrate live --cols 9 --rows 6 --square-mm 25 `
    --index 0 --backend dshow --views 15 --out intrinsics.json
python -m psvr_camera calibrate live --cols 9 --rows 6 --square-mm 25 `
    --index 0 --backend pseye --width 640 --height 480 --fps 60 `
    --views 15 --out intrinsics.json

# Guided desktop preview (works with opencv-contrib-python-headless):
python -m psvr_camera calibrate live --guided --cols 9 --rows 6 --square-mm 20 `
    --index 0 --backend pseye --width 640 --height 480 --fps 75 `
    --views 15 --max-seconds 180 --out intrinsics_guided.json

python -m psvr_camera verify --intrinsics intrinsics.json --image-size 640x480

# Printable targets at a measured size (ArUco needs OpenCV contrib):
python -m psvr_camera pattern chessboard --cols 9 --rows 6 --square-mm 25 `
    --out chessboard.svg
python -m psvr_camera pattern aruco --dictionary DICT_4X4_50 --id 0 `
    --marker-mm 100 --out aruco_0.png --geometry-out geometry_aruco.json

# Live ArUco tracking; emits versioned JSON UDP packets (OpenCV or PS Eye):
python -m psvr_camera track --intrinsics intrinsics.json `
    --geometry geometry_example_aruco.json --index 0 --backend dshow `
    --udp 127.0.0.1:4271
python -m psvr_camera track --intrinsics intrinsics.json `
    --geometry geometry_aruco.json --index 0 --backend pseye `
    --width 640 --height 480 --fps 60 --udp 127.0.0.1:4271
```

`--no-send --json-lines` prints the packets instead of sending them, for
inspection. `--allow-uncalibrated` exists for diagnostics only and still refuses
to claim a metric position. `track` checks the live frame size against the
loaded intrinsics `image_size` before estimating, counts `--frames` as captured
frames, stops after repeated read failures, and always emits a final
`valid:false` packet on exit or disconnect.

`calibrate live --guided` opens a Tk camera preview before collecting anything.
Press **AVVIA RACCOLTA** when ready. Detected chessboard corners are overlaid;
the guide requests a stationary hold for two seconds, confirms each capture,
then gives three seconds to reposition. Missing corners or movement reset the
hold timer, and a near-duplicate of the preceding view is not captured. The
timer starts at the button press. Detection uses OpenCV's sector-based detector
where available. The final RMS/result remains visible until the window is closed.
Collected corners are retained in a sibling `*.views.json` even if the solve
fails, enabling offline analysis without repeating the physical collection.
Use a new output filename for each session; existing outputs are not replaced.

### Native PS3 Eye (libusb-win32, no OpenCV)

When the camera's video interface is bound to libusb-win32, DirectShow/OpenCV
cannot see the stream. Use the native backend instead (build it first with
`tools/psvr_pseye/build.ps1`):

```powershell
python -m psvr_camera pseye inventory
python -m psvr_camera pseye probe --index 0 --width 640 --height 480 --fps 60 --frames 120
python -m psvr_camera pseye probe --index 0 --width 320 --height 240 --fps 187 --frames 240
```

`pseye inventory` is read-only (DLL provenance + connected count). `pseye probe`
prints the same `psvr.camera.capture` JSON as `probe`, with `achieved_fps`
measured over the steady-state capture loop. `calibrate live` and `track` reach
the same backend with `--backend pseye`; the adapter exposes
`read()/release()`, waits with a bounded timeout, refuses a requested mode the
shim would snap to a different resolution, and never reports a frame for the
wrong size. See `tools/psvr_pseye/README.md`.

## Protocol

### Experimental front LED pose preview

After the accepted blue-lens calibration:

```powershell
python -m psvr_camera led-track --preview --index 0 `
    --intrinsics configurations/pseye_blue_640x480_calibrated.json `
    --rig configurations/psvr_front_leds_experimental.json `
    --log logs/led_trial.jsonl
```

Start roughly upright and front-facing with all five front lamps visible.
Initial association enumerates five-point permutations and rejects solutions
incompatible with an upright/front view or with competing plausible mappings.
After acquisition, projected model positions maintain correspondence; missing
lamps, ambiguous matches and excessive reprojection errors produce loss.
Acquisition resets after 350ms without an accepted pose. All five LEDs are
currently required, with a limited front-view rotation envelope; rear/side LED
tracking is not implemented. The preview shows IDs, reprojection RMS, optical
position and relative displacement, with a relative-origin reset button.

The rig is explicitly an **external ruler estimate** from PSMoveService
`getTrackingShape`, pinned in the JSON source URL, not a manufacturer datasheet
or a measurement of this headset. Its origin is the centre front LED, not the
anatomical head origin. Model scale, depth and head lever arm need validation.
This command does **not** send UDP poses or enable SteamVR position.
JSONL logs record candidate counts, accepted correspondences, loss reasons and
pose estimates; logs are opened exclusively and are not overwritten.
Without `--preview`, supply `--frames` or `--max-seconds` for bounded diagnostics.

The accepted LED rotation trial uses minimum HSV brightness 60, saturation 90,
and an initial area of 6 pixels. White cores attached to a blue halo are included.
After acquisition, search is limited to an expanded projected headset ROI and
minimum area adapts with scale down to 3 pixels. Opening is disabled by default
to preserve small lamps. Four successive matches confirm acquisition.
Override with `--value-min`,
`--saturation-min`, `--min-area`, `--opening-size`. The preview includes live
brightness and area sliders: increasing either reduces sensitivity, but can
also remove distant/dim lamps. Threshold changes reset correspondence
acquisition and are logged with every pose. Sliders affect only detection, not
the positional scale or gain.

`led-track` now sets native PS Eye exposure manually (default `--exposure 13`,
device units 0..255) and gain (default `--gain 0`, range 0..63), disabling
auto gain/exposure and auto white balance. The GUI adds exposure and gain
sliders. Lower exposure to avoid white/saturated lamps; a dark room background
is expected for LED tracking. Keep the lens setting and resolution unchanged.
PS4 -7/8/1 validated in current light; dark room is not a prerequisite.
Optional shim controls are applied by the capture reader thread between grabs.
Requested settings and upstream cached readback are logged; unsupported
controls produce an explicit error. Exposure changes preserve optical intrinsics.

The experimental rig-fit limit now accounts for image size: the default 2.5px
noise floor scales with constellation span above 100px, up to 8px. This avoids
systematically rejecting close views of the approximate ruler model; it does
not establish metric accuracy. Preview/logs report RMS and applied limit, with
normalized RMS and raw candidates. Missing lamps are reported separately from
incompatible model fits.

An experimental outline refinement is available without SciPy:

```powershell
python -m psvr_camera.refine_led_rig --log logs/led_scale_preview_20261011.jsonl `
    --intrinsics configurations/pseye_blue_640x480_calibrated.json `
    --rig configurations/psvr_front_leds_experimental.json `
    --out configurations/psvr_front_leds_refined_20261011.json
```

It selects diverse five-point observations, fixes intrinsics, top width and
lamp depth, and jointly refines upper/lower height, lower width and each view's
pose. One third of selected views are held out; a file is written only when
their median improves at least 20% and all held-out fits are below 2.5px.
This refines relative proportions; it does not measure physical scale, lamp
depth or head-origin offset. Use a new output path. The accepted session used
24 fitting views and 12 held-out views: median RMS 2.87px -> 0.84px, maximum
held-out RMS 2.38px. Use the refined rig with `led-track --rig ...` for live
validation before positional output.

### Rotation trial: partial patterns guided by the IMU

Final-position filtering in driver mode is exclusive: `--position-filter one_euro`
or `kalman`. Optional `--position-config` loads the initial or optimized profile.
The optimized profile uses quasi-static Q, camera-axis covariance tuning and GLS
recovery, with `driver_hold` publication to avoid stacked prediction/grace.
No optical/IMU rotation changes. Benchmark/replay commands, negative results and
rollback: [Kalman optimization report](../../docs/KALMAN_OPTIMIZATION_20261013.md).
This profile is experimental; depth stability and flashes still need headset tests.

```powershell
python -m psvr_camera led-track --preview --imu --index 0 `
    --intrinsics configurations/pseye_blue_640x480_calibrated.json `
    --rig configurations/psvr_front_leds_refined_20261011.json `
    --imu-rig configurations/psvr_nine_leds_refined_sides_20261011.json `
    --log logs/led_imu_rotation_trial.jsonl
```

User confirmed stable tracking and successful 180-degree rotation with the
current defaults: `--value-min 60 --min-area 6 --exposure 13 --gain 0`.
Reference settings: `configurations/pseye_led_capture_reference.json`.

The driver-owned path is now implemented: `led-track --driver-imu` consumes
absolute IMU via UDP 4272 without a diagnostic subprocess or HID claim.
`--send-position` opts into bound position v2 on 4271; configurable
`--position-scale` and `--head-offset-model-m X Y Z` select the tracked point.
Default driver export and position settings remain disabled. Live tracking is
started from the Suite (`.\run_suite.ps1`, Tracking tab, 6dof), the sole
camera owner, writing `logs/suite_tracking_*.jsonl`. (The old standalone
launcher `scripts/run_driver_camera.ps1` was removed on 2026-10-18.) Full configuration, implementation evidence and
postponed operator checks: [SIXDOF_DRIVER_BRIDGE.md](../../docs/SIXDOF_DRIVER_BRIDGE.md).

SteamVR must be stopped: this experiment owns the sensor through the existing
C++ diagnostic/fusion process. Press **AVVIA IMU**, keep the headset stationary
(preferably resting on a surface) for the configured **8-second** calibration,
then face it towards the camera to align optical and IMU frames. The UI shows
a calibration countdown and allows a retry without losing the camera preview.
Once the front pattern is acquired, rotate slowly towards 90 degrees and the
rear view. `RIALLINEA CAMERA / IMU` requests a new front alignment; relative
position reset does not recenter the IMU.

The nine-point model uses the refined front outline and external estimated
side/rear coordinates. Outward normals are approximate visibility hypotheses.
Projected visible LED positions, IMU orientation and previous translation
guide correspondence; translation is solved at fixed IMU-guided rotation from
two or more separated lamps. Missing, stale, ambiguous or poorly conditioned
data remains invalid. A prolonged loss requests frontal reacquisition; an IMU
recenter invalidates the old alignment. Synthetic tests cover four lamps at
90 degrees, an additional missing side-view lamp, and two rear lamps at 180
degrees. The user has accepted real tracking through 180 degrees; physical
scale, anatomical pivot and side/rear metric accuracy still need validation.

Camera frames carry host-delivery timestamps; IMU event elapsed time is mapped
to the same high-resolution Python clock. Nearby IMU quaternions are
interpolated at camera delivery time without crossing recenter epochs. This
is host-level synchronization, not measured sensor exposure timing or
motion-to-photon latency. Quaternion/age, predicted and matched LED names and
raw candidates are logged. Closing the trial stops its owned IMU capture via
the existing stop event. No UDP or SteamVR position output is enabled.

For physical LED projection the new diagnostic `quaternion_sensor_wxyz` is
used, before fusion's right-hand neutral-head offset. `quaternion_wxyz` remains
the existing recentered 3DoF pose. Both are interpolated/logged independently.
This avoids treating an operator's tilted calibration posture as the physical
sensor-to-LED mounting frame. Rebuild the diagnostic for the new field; older
data falls back to the published quaternion and cannot correct this offset.

The partial-pattern matcher now tests translation hypotheses from pairs and
scores each against the whole visible constellation, refitting inliers instead
of accepting a greedy nearest-point assignment. Grazing visibility is treated
as uncertain. Short-horizon bounded translation prediction helps movement;
depth, residual, conditioning, continuity and competing-solution checks remain.
Synthetic tests include 300deg/s rotation at a 0.55m centre distance with clutter.

`refine_rotation_rig` can update side/rear coordinates using recorded IMU-guided
rotations and translation solved from front lamps only. At least 18 independent
angular views are required; one third are held out and updates need >20% median
improvement with held-out p90 below 4px and a 6cm plausibility bound. The accepted
reference `configurations/psvr_nine_leds_refined_sides_20261011.json` updates only
the right-side lamp (6.71px -> 0.45px held-out median); left/rear points are not
refined without adequate data. Absolute physical scale is still unvalidated.

`python -m psvr_camera.replay_rotation_log --log ... --intrinsics ... --rig ...`
replays recorded detections/IMU without hardware. Its results are regression
evidence, not a new physical trial. Idle pre-IMU preview logs are limited to
one record per second; active capture retains per-frame diagnostics.

### PSVR blue LED inspection

The blue-patch detector is available for the native PS Eye backend:

```powershell
python -m psvr_camera led-probe --index 0 --frames 300 --max-seconds 8
python -m psvr_camera led-probe --index 0 --snapshot leds.png
```

Point the powered headset at the camera. The command reports candidate centres,
areas, bounding boxes and a count histogram; `--snapshot` saves the frame with
the most candidates, annotated. It refuses to overwrite an existing snapshot.
HSV/area thresholds can be adjusted via `--hue-min`, `--hue-max`,
`--saturation-min`, `--value-min`, `--min-area` and `--max-area`.
Clipped patches and isolated noise pixels are excluded. Blue objects in the
room can still be candidates; these detections are not identified headset LEDs.

If front LEDs remain off in VR mode, the diagnostic tool now supports
`psvr_diag --leds-on` (use its normal `--config` argument). This sends documented
report 0x15, mask 0x01ff, all nine intensities 100/100. A successful exit confirms
command acceptance, not physical brightness; verify visually or with the camera.
Protocol reference: https://github.com/gusmanb/PSVRFramework/wiki/Report-0x15---LED-Brightness

This inspection command does not emit UDP poses or assign identities. The
separate `led-track --imu` pipeline implements model correspondence and partial
patterns using the accepted blue-lens calibration. Physical scale and measured
coordinates relative to the anatomical head origin still need validation before
SteamVR positional integration. Sorting raw blobs by screen position is not
a valid identity assignment during rotation or occlusion.

The wire format is defined once in `protocol.py`, mirrored by `protocol.json`
and documented in `PROTOCOL.md`. A test asserts the JSON file equals the code.
Coordinate frame: right-handed metres, **+X right, +Y up, -Z forward**, WXYZ
`world_from_body` quaternion. Default UDP endpoint `127.0.0.1:4271`.

## Layout

| File | Role |
|---|---|
| `protocol.py` / `protocol.json` / `PROTOCOL.md` | versioned UDP contract |
| `udp_sender.py` | validated datagram emitter |
| `camera_backend.py` | optional-OpenCV inventory and explicit opening |
| `camera_source.py` | backend dispatch: OpenCV or native PS3 Eye, one `read()/release()` shape |
| `pseye_backend.py` | native PS3 Eye backend (libusb-win32) via `tools/psvr_pseye`, plus the frame adapter |
| `capture.py` | bounded capture and JSON report |
| `calibration.py` | chessboard detection, Zhang/OpenCV solve, strict I/O, live collection |
| `geometry.py` | pure-NumPy PnP, homography, RANSAC, ambiguity math |
| `estimator.py` | marker geometry policy, metric pose, rejection, filter |
| `tracker.py` | testable tracking loop (bounds, size gate, final invalid sample) |
| `markers.py` | ArUco detection adapter |
| `targets.py` | printable chessboard (SVG) and ArUco (PNG) targets |
| `cli.py` / `__main__.py` | `python -m psvr_camera` |

## Tests

```powershell
python -B -m unittest discover -s tests -p "test_camera*.py" -v
```

The tests are synthetic and hardware-free: known projections are recovered,
outliers are rejected, missing data is reported, OpenCV absence is exercised,
and a fake `cv2` covers inventory/probe. The native backend is covered with a
fake `psvr_pseye.dll` function table, including the `read()/release()` adapter;
`test_camera_pipeline.py` runs synthetic frames through the real estimator,
tracker loop and UDP sender (fake socket) to check the frame bound, the live
image-size gate, the read-failure stop and the terminating invalid packet. A
real capture runs only with `PSVR_PSEYE_HARDWARE=1`. See
`docs/PSEYE_TRACKING.md` and `tools/psvr_pseye/README.md`.
