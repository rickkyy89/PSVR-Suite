# PS4 Camera: static PSVR LED check

Session: harness date 2026-10-24; machine capture files are timestamped UTC
2026-10-23T22:xx. The headset was stationary in front of the camera while the
user was away. These are static acquisition/detection checks, **not 6DoF tests**.

## Safe scope

- Kept the dark images requested by the user: **no camera exposure, gain or
  brightness changes**, no image brightening.
- Did not change PS3 Eye calibration, tracker preferences, distortion, fallback,
  position filter, SteamVR registration or runtime tracking mode.
- No UDP positions, LED identities, stereo correspondences or metric poses.
- One JSON report and one annotated last stereo image per explicit saved probe;
  no continuous video or unbounded image history.

## Initial absence of LEDs and recovery

The first 360 pairs had no detected LEDs; both cropped eyes were all-zero.
This differs from a dark but useful image containing bright LEDs. The USB
stream itself still delivered frames, with nonzero transport metadata.

Passive headset control status reported `power=off, mode=cinema`. The existing
`psvr_diag --power-on` command was accepted (A0 result 0), but fresh F0 state
confirmation timed out: **do not count that command alone as verified power-on**.
The standard `--leds-on` command was then accepted. Subsequent images visibly
confirmed five front LED patches in both eyes. No VR mode command was sent.

## Measured static results

At index 0, DirectShow, 640×400 per eye / nominal 60 FPS:

| Probe | Delivered pairs | Host callback FPS | Read errors / timeouts |
| --- | ---: | ---: | --- |
| 12 s | 599 | 60.022 | none |
| 30 s | 1,685 | 60.040 | none |
| 20 s, including area-20 comparison | 1,074 | 59.620 | none |

Both probes completed camera cleanup. Every analyzed pair contained candidates
in both eyes. With the unmodified suite-style detector (`V>=60`, `S>=90`,
minimum area 6, white-core inclusion), the 30-second test had five candidates
in the right eye for **1,685/1,685** frames; the left had five in **503** and
six in **1,182**, consistent with an extra small patch/fragment. This is not
reported as six known headset LEDs.

A **comparison-only** minimum area 10 reduced the extra left fragment: five
in **1,673/1,685** frames, six in 12; the right remained five throughout.
For frames admitted by the static five-patch association gate, RMS displacement
from the initial per-eye reference was approximately **0.177 px left** for this
profile. This is a gated image-domain stability measure, not tracking accuracy,
camera noise in metres, or a result covering rejected frames.

An offline comparison of 20 sampled stereo pairs also tested minimum area
6/10/20/30 and morphological opening 0/3. Larger area thresholds or opening
reduced fragments in that scene; **none were promoted to tracking preferences**.
The diagnostic includes `static_min_area10` and `static_min_area20` for explicit
comparison. These depend on image resolution, distance and apparent LED size.
The existing normal/sensitive detectors remain in the report for comparison.

The subsequent 20-second probe confirmed **five candidates in each eye for
1,074/1,074 pairs** with `static_min_area20`. All 1,074 frames per eye passed
the local reference association gate; RMS image displacement from the initial
reference was **0.123 px left / 0.091 px right**. No capture errors, read timeouts
or pending cleanup occurred. This is encouraging for this one fixed pose, not
validation at other distances, in motion, or of stereo identities/metric accuracy.
The comparison threshold remains diagnostic-only; tracking defaults are unchanged.

Full initial records:

- `logs/ps4_camera/ps4-led-20261023T223359952187Z.json` and matching `.jpg`
- `logs/ps4_camera/ps4-led-20261023T223538044869Z.json` and matching `.jpg`
- `logs/ps4_camera/ps4-led-20261023T223628788687Z.json` and matching `.jpg`
- `logs/ps4_camera/ps4-static-threshold-comparison.json`

Logs/images are local diagnostics, not bundled source assets.

## Implemented diagnostic

`tools/psvr_camera/ps4_led_probe.py` aggregates fixed-size per-eye counters and
only retains the last image pair. Five-candidate frames may be associated
**temporally within the same eye**, using unique nearest neighbours, a 4 px
gate and rejection of near ties. Sorting blobs is never used to assign LED IDs.
Dropouts, merges, extra blobs and motion outside the gate are counted but
excluded from that reference-motion statistic. No left/right matching occurs.

The Suite Camera → PS4 Camera panel's **Check LEDs (10 s)** action runs the
diagnostic in a child process, saves the full report and last annotated image,
and shows a short summary including errors and cleanup status. A large report
must not be truncated in a way that hides those fields.

Verification: **261 tests passed, 4 skipped**, plus 158 subtests in the targeted
camera/Suite regressions. A real hidden Tk panel invoked the LED action, started
its child process, captured **473 pairs at 58.999 host-callback FPS**, and
displayed a valid summary pointing to the full report and snapshot; no errors
or pending cleanup. That run includes detection and concurrent test-suite load,
not a sensor throughput benchmark. Large-report summary regression tests retain
capture errors/status even when the full candidate report exceeds 16,000 chars.

## What still needs the user (storico — calibrazione in GUIDED_CALIBRATION + 6DoF statico FRONT5)

1. Verify physical eye order rather than inferring it from firmware labels.
2. Collect a measured chessboard at varied poses, field positions and distances.
3. Estimate and validate **two PS4 intrinsics plus stereo R/T in metres**,
   using held-out paired images. PS3 Eye intrinsics cannot be reused.
4. Only then rectify/match LEDs, triangulate with uncertainty, validate rig
   scale, and integrate with the existing IMU/driver bridge.

A stationary unmeasured headset non può stabilire calibrazione stereo, scala,
sincronizzazione dinamica, latenza tracking o 6DoF affidabile.
