# 6DoF driver/camera bridge — implementation and deferred operator trial

2026-10-13 follow-up: [Kalman optimization](KALMAN_OPTIMIZATION_20261013.md)
adds optional camera-only estimator/publication policies. The bridge, optical
reference and IMU/display paths remain unchanged. Historical results below are
preserved; the new algorithm is not headset-accepted.

## Implemented

The SteamVR driver is the single owner of PSVR HID. Its opt-in nonblocking
export sends the physical IMU quaternion **before both neutral head offset and
world recenter**, with session, epoch and driver sample time, to loopback 4272.
Camera consumes it via `--driver-imu`; no diagnostic subprocess or second
sensor claim. Standalone `--imu` remains available for the accepted optical
prototype when SteamVR is stopped.

Camera position output is bound `psvr.position` v2 on loopback 4271, enabled
only by `--send-position`. Session/epoch and driver sample timestamps prevent
old output being treated as fresh. Source and receive ages are both checked;
wrong session/epoch packets do not establish an origin. The first coherent
frame after activation/recenter establishes seated origin. V1 remains supported
for legacy sources when bound driver mode is not required.

Camera transforms the selected tracked point into the unrecentered IMU world;
driver applies world reference exactly once. `positionCameraYawOffsetDeg` is
ignored for v2. Scale and physical model-space head offset are configurable;
the offset is not multiplied by the nominal-model scale. Lost/invalid input
and shutdown emit bound invalidation, never synthetic position zero.

Absolute camera alignment survives optical loss/reacquisition and same-session
recenter. A new driver activation session requires a new alignment. Sequences
remain monotonic through sender errors/invalidations. Angular velocity remains
available in valid 6DoF poses; recenter transforms its EMA coherently.

## Build and evidence

Experimental package (not registered/activated by this implementation):

```
out/driver_psvrpc_6dof/psvrpc
```

Built DLL SHA-256:
`284ECF913DF43B151F1780F6FD6962372D5BD64F1B4DF638E415D9BD83BA3ADE`
(brief-loss grace / orientation timestamp revision).

Reproduce the MSVC build:

```powershell
.\build.ps1 -Toolchain VS2022 `
    -DriverPackageRoot "$PWD\out\driver_psvrpc_6dof\psvrpc"
```

CMake's interpreter was set to `.venv-camera/Scripts/python.exe` so OpenCV
regressions run as well. Verification: **55/55 CTest passed**, camera suite
**199 tests, seven intentional skips, no failures**. Two preexisting C++
warnings remain (local `path` shadowing and a size_t conversion).

`driver_camera_interop` uses the real C++ exporter/receiver/bridge over UDP
with the Python source/builder/sender, synthetic IMU snapshots and no HID or
headset. It checks zero origin, transformed movement, single yaw application,
wrong session/epoch, stale input, loss/terminal invalidation and recovery.
Additional unit tests cover atomic physical sensor basis, fresh receive but old
source, malformed/duplicate JSON, epoch/session boundaries, shutdown, optical
reacquisition without alignment drift and velocity after recenter.

## Prepared configuration (apply only for the future operator trial)

`configurations/steamvr_6dof_bridge_proposal.json` is a **merge proposal**, not
a replacement SteamVR settings file. No live SteamVR configuration or driver
registration was changed. It enables IMU export and optical position together.
Package defaults leave both disabled, preserving orientation-only operation.
Keep the previously verified display/presenter configuration when selecting
the trial package; this change does not choose a new display mode.

For driver-export-only preview, enable `imuExportEnabled` while leaving
`positionEnabled=false`. For the later positional trial, enable both from the
proposal and register the experimental package while SteamVR is closed.
Close the standalone diagnostic IMU preview before starting the driver.

The launcher reads the saved user preset (60/6/13/0, blue lens, calibrated
640x480 and accepted nine-point model). Tracking is started from the Suite,
which is the sole camera owner; it does not start SteamVR, register a driver,
change settings or claim HID:

```powershell
# Suite GUI (Tracking tab, 6dof): preview + driver-IMU + position bridge,
# fresh automatic log logs/suite_tracking_*.jsonl:
powershell -NoProfile -ExecutionPolicy Bypass -File .\run_suite.ps1
# .\run_suite.ps1 -Attach   # track toward already-running VR, no mode change
```

The standalone script `scripts/run_driver_camera.ps1` has been removed
(2026-10-18): use the Suite tracking path above for all live trials.

Zero head offset is the **front-centre LED origin**, not a measured anatomical
head origin. The current external-scale rig is experimental. Camera exposure,
intrinsics and front/rotation acceptance are saved independently of these
pending metric quantities. Changing scale/head offset or explicit camera
realignment during a trial requires recentering before comparing motion.

## Live trial: brief-loss stabilization

The first in-lens trial reported coherent movement but poor smoothness and
flashes associated with momentary optical loss, particularly lateral movement
to the user's right. The revised trial sets `positionGraceMs=80`:
last-good translation may be held for a bounded interval, with live IMU
orientation. Invalid frames do not extend that interval, stale source/receive
hard limits remain, and recenter/session changes or calibration/error discard
the hold. Default grace is zero for compatibility. Valid frames are not delayed.

6DoF `poseTimeOffset` now uses the IMU age, not the older translation age, so a
held position does not cause excessive angular extrapolation. The camera sender
filters the final unrecentered head point with a time-based One Euro filter
(minimum 8Hz, beta 15, maximum 40Hz, derivative 4Hz). It resets on identity,
alignment and long gaps; short invalid input remains invalid rather than
inventing position. Raw/filtered world positions and settings are logged.
Lightweight depth-noise profile introduced afterwards as Suite default:
`min_cutoff_hz=2.5` / `beta=8` (max 40Hz, deriv 4Hz) in the `TrackingWorker`
prefs; legacy defaults 8/15 conserved for rollback. DLL unchanged (same
SHA-256 below, grace 80ms).

Revised verification: **55/55 CTest**, camera **210 tests with seven intentional
skips**, no failures. Includes jitter/step response, variable dt, brief loss,
expiry, recenter and the angular timestamp regression. Live comparison still
required; the software tests do not prove flashes or judder are resolved in lenses.

## Operator verification queue

### Session closure: depth stability is the remaining priority

Final user clarification: lateral translations are fairly accurate; the main
problem is depth, with perceived object distance changing even when nearly
stationary. The latest physical comparison was only slightly better and had
**one flash**. Earlier "flashes resolved" feedback applies to the earlier trial,
not the final outcome. 6DoF smoothness/stability is **not accepted**.

The last 180s analyzed in `logs/driver_camera_20261012_234154_50e426.jsonl`
contained 13,503 frames / 13,468 valid, cadence median 13.3265ms / p99 14.164ms,
filtered position step p99 10.943mm / maximum 27.81mm, and completed invalid
gaps up to 79.826ms. This is not a controlled physical before/after comparison.
The last compositor probe still reported fullscreen true, but position was
missing and the pose invalid; do not describe the final runtime as fully tracked.

At the user's request a lighter stationary-noise profile is the Suite
default (`min_cutoff_hz 2.5 / beta 8.0`, max cutoff 40Hz, derivative cutoff
4Hz, selectable in the Tracking tab with `kalman_initial`/`kalman_optimized`
rollback). The filter acts on the final head position, uses actual dt and increases its
cutoff with motion. No new frame queue or validity promotion was introduced.
Camera suite **213 tests, seven skips, no failures**: a 75Hz synthetic 3mm
depth-noise test verifies >20% lower standard deviation than the previous
profile; a 10cm step reaches >95% in 80ms. These are software checks, not
in-lens acceptance, and smoothing does not remove slow geometric depth bias.

**Not yet loaded in the live camera:** it was launched before this profile
change and still uses 8Hz / beta 15. Session ended before another camera restart
or physical comparison. Driver DLL remains
`284ECF913DF43B151F1780F6FD6962372D5BD64F1B4DF638E415D9BD83BA3ADE`,
runtime grace 80ms. No processes were closed at session closure.

Next session:
1. Check current IMU and compositor/fullscreen state; recover only if needed.
2. With headset front-facing to the camera, start tracking from the Suite
   (`.\run_suite.ps1`, Tracking tab, 6dof; fresh automatic
   `logs/suite_tracking_*.jsonl`). Confirm the new log's session block
   shows filter 2.5Hz / beta 8.
3. Verify optical lock, recenter, then hold almost still for 20–30s and test
   known 10cm translations, particularly depth; compare raw/filtered depth.
4. If slow depth drift remains, inspect LED correspondence and depth
   conditioning (especially two-LED views) before increasing filtering.
   Scale and anatomical head offset remain unvalidated; Assetto Corsa deferred.

### In-lens follow-up: flashes resolved, optical jumps persist

User feedback after recenter: "flash spariti gli scatti persistono". The live
camera log `logs/driver_camera_20261012_232623_d31499.jsonl` shows regular camera
cadence (median 13.326ms, p99 14.043ms over the analyzed 180s), short completed
invalid gaps up to 40.16ms, but filtered position jumps up to 146.28mm in a
13.60ms frame, associated with changed LED correspondence. This establishes an
optical discontinuity component; compositor frame-rate has not been measured
in this trial and other contributors are not excluded.

`ImuLedTracker.match_pattern` had a minimum 250mm displacement allowance even
between adjacent frames, and its continuity cost saturated above 30mm. The
revision uses `min(0.25, 0.015 + 2*elapsed)` metres (noise allowance plus 2m/s
motion), checks both seeds and refitted solutions, and removes cost saturation.
Rejected hypotheses do not update translation/velocity. An additionally tight
forecast innovation gate was tested and discarded: it caused excessive loss
on partial views in replay.

Fixed comparison: replay last 14,000 eligible frames ending at monotonic time
12600s, same intrinsics/rig and recorded candidates. Baseline reproduced
13,952 valid poses; revised matcher 13,874 (99.1%). Raw translation steps over
50mm decreased **111 → 21**, p99 **46.07 → 32.51mm**. Maximum remained 167.98mm
across short recovery gaps; this is a partial mitigation, not elimination of
all jumps. Camera suite: **212 tests, seven skips, OK**, including teleport
rejection without prior poisoning and genuine 1.2m/s motion/short loss recovery.
Camera alone restarted to apply this revision; driver DLL and IMU calibration
unchanged. New physical comparison remains pending.

Post-restart probe revealed an independent IMU loss already at 23:36:04,
before the camera restart at 23:41:54: `sensor_tick_gap` of 8501us against
the configured 5000us maximum; driver explicitly marks tracking-lost and
disconnected. The new camera log `logs/driver_camera_20261012_234154_50e426.jsonl`
receives invalid tracking-lost export. No recenter application was verified
after this fault. Need stationary recalibration/restart before comparing the
new matcher in lenses. This fault is not evidence of a physical USB disconnect.

Stationary recovery: queued recalibration did not revive the stopped reader;
SteamVR restart recovered IMU tracking. User then reported error 202 and the
probe confirmed `fullscreen:false`. BringToFront, verified Win32 foreground
activation and Alt+Enter did not restore it. Restarting only `vrcompositor.exe`
restored `fullscreen:true` before/100ms/final with **5/5 valid poses**, retaining
the running sensor and camera. This is verified immediate recovery, not proof
the desktop fullscreen problem cannot recur.

### In-lens follow-up: lateral OK, depth oscillation persists (NON accettato)

Final user feedback (verbatim intent): "flash ne ha fatto solo uno, comunque
lo fa. continua a cambiare la distanza delle cose, anche stando pressochè
fermi. un leggero noise filter?" plus "sugli spostamenti laterali lo vedo
anche abbastanza preciso, è più sulla profondità che ha problemi".
Recorded state: **NON accettato**. Laterals fairly precise; main problem is
depth/distance oscillation while nearly still. Jerks possibly reduced and only
**ONE flash** in the last run. No controlled physical before/after comparison.

Matcher correction already active in this run: gate
`min(0.25, 0.015 + 2*elapsed)` m and unsaturated continuity cost (see above).
New lightweight final-position One Euro profile `min cutoff 2.5Hz / beta 8`
(max 40Hz, deriv 4Hz) introduced as launcher default afterwards; camera suite
**213 tests, 7 skips, no failures**, including new depth-noise test (3mm noise
at 75Hz, >20% std reduction vs old profile) and 10cm step >95% within 80ms.
**NEW PROFILE NOT YET LOADED: live camera was started before the change, it
still uses 8/15. Do not claim in-lens verification of the new profile.**

Last camera log `logs/driver_camera_20261012_234154_50e426.jsonl`, last-180s
analysis: 13503 frames / 13468 valid, dt median 13.3265ms p99 14.164ms,
filtered max step 27.81mm p99 10.943mm, invalid gaps max 79.826ms. Last
fullscreen probe `true` but poses invalid `position_missing` (IMU tracking),
so NOT verified active tracking now. Last 202 recovery by restarting ONLY
vrcompositor; no persistence guarantee. No process closed at session end.

Next session: headset frontal to camera, restart ONLY camera with launcher for
new profile, verify log settings 2.5/8, verify IMU+fullscreen, recenter, still
test 20-30s for depth oscillation and 10cm XYZ moves. If slow drift remains,
filter does not fix geometry/2-LED correspondence bias; investigate that
before adding heavier filters. No scale/head-offset calibration validated;
Assetto Corsa deferred.

1. **Physical scale:** known translations in X/Y/Z; compare measured distance
   and exported position, then choose `PositionScale` if needed.
2. **Head origin/offset:** measure the rig-to-head origin (centre between eyes /
   intended HMD pose origin). Validate against a known pivot; do not assume
   normal neck/head rotations produce zero real eye translation.
3. **Directions and recenter:** confirm right/up/forward in SteamVR, seated
   origin, yaw/full recenter, long optical loss/reacquisition and restart.
4. **Lenses and presentation:** 6DoF movement/rotation, near/far/full visibility,
   tracking loss/recovery, fluidity, flashes/fullscreen/-202 and effective delay.
5. **Geometry:** additional left-side/rear independent observations if metric
   accuracy requires refining points still based on external estimates.

These tests were not performed and are not required to repeat the automated
verification. Accepted optical tracking through 180 degrees does not substitute
for physical scale, head-origin or in-lens 6DoF acceptance.
