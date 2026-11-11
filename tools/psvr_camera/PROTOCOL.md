# PSVR camera position protocol (v1)

This is the contract between the M9 webcam-side tools and the driver agent.
`protocol.json` is the machine-readable copy; `protocol.py` is the
implementation and a test asserts the two agree. Do not change one without the
other, and do not enable the stream until the driver agent confirms the endpoint
and the axes.

## Transport

* UDP, one UTF-8 JSON object per datagram, compact and key-sorted.
* Default endpoint `127.0.0.1:4271` (configurable). This avoids AITrack `4242`
  and the monitor HTTP API.
* A receiver must reject a packet whose `version` it does not implement. No
  best-effort field guessing.

## Coordinate frame

Right-handed, metres. The frame is the driver world frame used by
`driver/pose_bridge.hpp` and `docs/AXIS_CONVENTIONS.md`:

| Axis | Direction |
|---|---|
| +X | right |
| +Y | up |
| +Z | backward, so **-Z is forward** |

The camera observes the marker in the OpenCV optical frame (+X right, +Y down,
+Z forward). The sender converts every point with
`(x, y, z) -> (x, -y, -z)` before emission, so the receiver never has to know
the camera convention.

`orientation_wxyz` is `world_from_body` (maps body vectors into the world
frame), WXYZ order, unit norm. It is optional because the driver already owns
orientation from the IMU.

## Packet fields

| Field | Type | Notes |
|---|---|---|
| `protocol` | string | constant `psvr.position` |
| `version` | int | `1` |
| `seq` | int | non-negative, increments per sent packet |
| `host_time_ns` | int | sender wall clock, ns since epoch |
| `monotonic_s` | number | sender monotonic seconds at capture |
| `valid` | bool | `false` => pose not usable this frame |
| `state` | string | `tracking`, `lost`, `calibrating`, `ambiguous`, `error` |
| `source` | string | `aruco`, `blob_led`, `none` |
| `frame` | string | constant `psvr_world` |
| `units` | string | constant `m` |
| `position_m` | `[x,y,z]` or null | metres in frame |
| `orientation_wxyz` | `[w,x,y,z]` or null | unit quaternion |
| `reprojection_px` | number or null | RMS reprojection error |
| `inliers` | int or null | accepted correspondences |
| `ambiguity_ratio` | number or null | planar PnP ambiguity metric |
| `note` | string or null | short reason, e.g. `reprojection 3.4px > 2.0px` |

## Validity rules

* `valid:true` requires a finite `position_m`.
* When `orientation_wxyz` is present it must be a unit quaternion within `1e-3`.
* `lost`/`error`/`ambiguous` frames are sent with `valid:false` so the receiver
  can freeze or fade instead of holding a stale pose. A receiver should also
  apply its own `monotonic_s` staleness timeout.

## What this protocol does **not** claim

* It does not claim the PSVR built-in LED ring geometry is known or calibrated.
  Geometry is only sent when the operator measured it (`user_measured`) or took
  it from a datasheet for an external marker. `psvr_builtin` geometry is refused.
* It does not claim the intrinsics are calibrated. The estimator refuses
  uncalibrated intrinsics unless explicitly overridden for diagnostics.
* It does not define rotation. IMU orientation is a separate source.

## Driver agent coordination checklist

1. Confirm the UDP bind endpoint (host/port) and whether loopback is enough.
2. Confirm `psvr_world` axes match the driver pose frame.
3. Confirm the staleness timeout to apply on `monotonic_s`.
4. Confirm whether `orientation_wxyz` should be ignored (recommended for M9).
5. Record the agreed values in this repository (parent-owned file) and in the
   driver's position receiver configuration.
