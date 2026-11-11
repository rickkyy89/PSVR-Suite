# Driver ↔ camera bridge contract

Implementation contract for the opt-in 6DoF path. SteamVR driver is the only
PSVR HID owner. Camera process consumes exported IMU and contributes position.
Existing diagnostic/front tracking and position-v1 consumers remain supported.

## IMU driver → camera (UDP 127.0.0.1:4272)

JSON protocol `psvr.imu`, version 1, maximum 2048 bytes:

```json
{
  "protocol": "psvr.imu", "version": 1,
  "frame": "psvr_imu_unrecentered", "clock": "driver_steady_seconds",
  "session_id": "unique-per-hmd-activation", "seq": 1, "recenter_epoch": 1,
  "sample_time_s": 123.0, "sent_time_s": 123.001,
  "connected": true, "valid": true, "state": "tracking",
  "quaternion_sensor_wxyz": [1, 0, 0, 0],
  "world_rotation_wxyz": [1, 0, 0, 0],
  "corrected_gyro_rad_s": [0, 0, 0]
}
```

- Sensor quaternion is **absolute physical sensor orientation**, before both
  `head_offset_` AND left `reference_` recenter. Compute from the same fusion
  state as `world_rotation` and epoch; equivalently
  `world_rotation.conjugate() * fusion.sensor_orientation()`.
- `world_rotation` maps the gravity-aligned unrecentered IMU world to the
  currently published driver world. Position bridge applies it exactly once.
- Finite normalized quaternions (norm tolerance 1e-3); finite vector/time
  values; integer nonnegative sequence/epoch; bounded nonempty session string.
  `sample_time_s <= sent_time_s`; `valid` requires connected tracking and fresh
  sensor data, independent of whether optical position is currently valid.
- Export opt-in (`imuExportEnabled=false` default), configurable port and Hz
  (`imuExportPort=4272`, `imuExportHz=100`). Nonblocking sends, bounded cadence,
  serialization/sends outside the tracker mutex. Stop after joining owner
  threads. Transport errors must not invalidate orientation-only operation.
- Python maps driver clock to its high-resolution receive clock using minimum
  observed transport offset. Drop duplicates, retired sessions and earlier
  epochs. Never interpolate across session/epoch boundaries. A new session
  forces optical alignment; recenter within a session preserves the stable
  absolute camera/IMU alignment but discards older buffered samples.

Python public API: `parse_driver_imu_packet(payload)` and
`validate_driver_imu_packet(dict)` raise `ValueError` on malformed data;
`DriverImuSource(port=4272)` has `local_port`, `latest()`,
`at(captured_at, wait_s=.025)`, `close()`. Port 0 permitted for offline tests.
Samples retain wire metadata, expose mapped `monotonic_s`, `session_id`,
`recenter_epoch`, and alias `recenter_count` for legacy tracker compatibility.
Interpolated samples retain corresponding driver-domain sample time.

## Position camera → driver (UDP 127.0.0.1:4271)

Existing `psvr.position` v1 remains unchanged. Bound v2 adds:

```json
{
  "protocol": "psvr.position", "version": 2,
  "frame": "psvr_imu_unrecentered", "units": "m",
  "session_id": "same-as-imu", "recenter_epoch": 1,
  "imu_seq": 1, "imu_sample_time_s": 123.0,
  "seq": 1, "monotonic_s": 999.0, "host_time_ns": 1,
  "valid": true, "state": "tracking", "source": "blob_led",
  "position_m": [0, 0, 1]
}
```

- All v1 validation remains; v2 metadata mandatory, finite/bounded and typed.
- Driver rejects wrong activation session, old/future epoch and (valid packets)
  old/future driver-domain IMU sample timestamps. Source mismatch must not
  refresh sequence/freshness or establish origin. On identity/epoch changes,
  invalidate old samples; origin must await a matching fresh valid frame.
- When driver export and opt-in position are both enabled, require bound v2.
  Legacy v1 remains usable with legacy position mode/export disabled.
- v2 position is already in **unrecentered gravity-aligned IMU coordinates**.
  Do NOT apply `positionCameraYawOffsetDeg` to it; bridge subtracts seated
  origin and applies `world_rotation` exactly once. Legacy v1 yaw behaviour is
  unchanged. Local receive watchdog remains in force. Bound `valid:false`
  clears tracking even if its last IMU timestamp is old.

## Camera transform and modes

Let `A = camera_from_unrecentered_imu_world`, `R = camera_from_model` from
the IMU-guided tracker, `t = camera_from_front_centre_led` translation.

```
head_camera_m = position_scale * t + R * head_offset_model_m
position_unrecentered_imu_m = A.transpose() * head_camera_m
```

No ad-hoc OpenCV axis flip or recenter yaw after this transform. Offset is in
physical metres in model axes, independent of nominal-model scale adjustment.
Default scale 1, head offset zero (LED origin until physically calibrated).
Reject non-finite or nonpositive scale, invalid rotation and malformed offset.

`led-track --driver-imu` uses UDP receiver only, never launches diagnostic or
claims HID. Mutually exclusive with standalone `--imu`. Position transmission
is opt-in `--send-position`, requires driver mode, defaults loopback port 4271.
Driver settings stay disabled until explicit future trial; no automatic
SteamVR startup/registration. Preserve preset 60/6/13/0 and accepted rig.

Python sender module `driver_position.py` exposes
`build_driver_position_packet(pose, imu_sample, alignment, seq,
head_offset_model_m=(0,0,0), scale=1.0, now=None)` returning dict or None when
there is no usable driver identity; invalid/lost poses send v2 `valid:false`.
`DriverPositionSender` sends on loopback, emits terminating invalid when possible,
and handles socket errors without holding stale valid tracking.

## Verification split

Camera optimization 2026-10-13 preserves this wire contract. With the optional
`prediction_output=driver_hold` policy, internal predicted estimates are sent as
`valid:false,state:lost,position_m:null` with unchanged binding metadata. They
cannot refresh the driver's last-good clock; only accepted optical corrections
publish valid positions. The existing driver grace/watchdogs and orientation-only
behaviour are unchanged. Original camera-prediction publication remains available
as a baseline; it can still stack its prediction horizon with driver grace.
Details and limitations: [KALMAN_OPTIMIZATION_20261013.md](KALMAN_OPTIMIZATION_20261013.md).

Automated: codec, loopback IPC, session/epoch changes, stale/future input,
partial tracking, coordinate/head-offset math, v1 compatibility, driver poses,
resource shutdown, MSVC build and mock runtime. No headset/user required.

Deferred operator tests: physical scale, anatomical head offset/pivot,
tracking near/far/full rotation in SteamVR, positional direction/recenter,
latency, lens fluidity and presentation stability. Optical 180-degree acceptance
does not by itself validate those quantities.
