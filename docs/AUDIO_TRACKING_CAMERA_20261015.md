# Audio, camera selection and long-session tracking — 2026-10-15

Copyright (c) 2026 rickkyy. MIT.

This report records the user's symptoms, the software changes and the remaining
hardware checks for the next release. The audio issue and the observed tracking
degradation have not yet been reproduced in a controlled live session.

## 1. Audio: observed behaviour and diagnostic limits

Paths involved (PSVR1 processor unit, "PU"):

- **Cinema mode — validated (user session, SIE HMD, headset headphone
  jack).** Host HDMI stream carries audio to the PU and the headphones
  work. This is the reference audio path today.
- **VR mode / SteamVR — observed silent (same user session).** The suite
  VR sequence (`tools/psvr_suite/runtime.py: start_vr`) sends the PU
  `--vr-mode` via `psvr_diag`; in VR the PU re-routes the headset
   internally and the headphone output can go silent. PU hardware/firmware
   routing is a possible explanation, not a confirmed diagnosis of this
   session; SteamVR endpoint selection and application routing also need
   checking. The documented control protocol
  (`tools/psvr_diag/control_protocol.hpp`: power 0x17, mode 0x23, LEDs
  0x15) has **no headphone/audio command**, and inventing USB payloads is
   outside the implemented protocol. **No VR headset-jack audio fix has been
   implemented or validated.** The new diagnostics CLI reports state (§3).
- **USB MI_01 "PS VR Audio" (usbaudio) ≠ proprietary 3D audio.** The
  composite exposes MI_01 as a standard USB audio interface
  (`docs/MILESTONE_1_REPORT.md`, binding untouched per M2 rules in
  `docs/WINUSB_CONTROL.md`). Selecting it as a Windows playback device is
  at most a plain stereo path, still **unvalidated** in this repo; it
  does not enable Sony's 3D audio pipeline and does not substitute for
  the HDMI audio carried in cinema mode.
- **PnP inventory does not establish the default render endpoint.** This CLI
  lists endpoints and their PnP state, not the Windows default or per-app route.
  Windows MMDevice can query defaults read-only, but is not implemented here;
  check SteamVR audio settings and `mmsys.cpl` / volume mixer separately.
- **Practical workaround (not a fix): a PC headset.** Because the PU
  headphone jack can go silent in VR and no host-side command exists to
   change PU routing, the recommended workaround to test is a separate PC
  headset (USB or 3.5 mm headphones wired to the PC) selected as SteamVR's
  audio device, so audio routes through the PC and not the PU. That is an
  operator workaround, not a repo change: it does not restore the PU
  headphone path and does not claim its VR-mode audio quality as
  hardware-validated here.
- Nothing here changes SteamVR settings, driver bindings or any external
  component. Audio interfaces of the composite stay untouched (M1 rule:
  they are diagnosed separately, never opened by the sensor/control
  tooling).

## 2. Open hardware validation (explicitly NOT done)

- Test matrix for the VR-mode headphone silence (same PU, cable, HDMI
  endpoint, application, cinema vs VR back-to-back).
- MI_01 USB audio capability (playback? mic? sample formats?).
- HDMI render endpoint behaviour across cinema/VR transitions.
- Camera-source registry and clock tracking under live hardware (§4/§5
  software contracts are offline-tested only).

## 3. Added today: read-only audio diagnostics CLI

New package `tools/psvr_audio_diag` (standalone; no suite file was
modified by this change):

```
python -m tools.psvr_audio_diag              # live, read-only
python -m tools.psvr_audio_diag --offline    # facts/guidance only, no queries
python -m tools.psvr_audio_diag --json       # machine-readable
```

Guarantees (contract-tested in `tests/test_audio_diag.py`):

- PU mode comes only from the existing passive observation
  `psvr_diag --status --json-stream` (F0 `control_status`; USB IN reads,
  no query/write commands). No power/mode/LED flags are ever assembled —
  a guard test asserts the forbidden set never reaches argv.
- Windows inventory uses read-only `Get-PnpDevice` / `Get-CimInstance`
  queries; a guard test rejects PowerShell verbs that write.
- F0 absent or unparseable ⇒ `pu_mode: "unknown"`, never guessed.
- Failing sections degrade to `availability: "unavailable"` with the raw
  reason; the command exits 0. Stdlib only, works with the camera venv.

Report content: PU mode (off/cinema/vr/transition/unknown), PSVR1
composite presence + MI_01 rows, render-endpoint inventory with
headset-name token matches, sound devices, plus fixed guidance strings
and the §2 validation-pending list shipped in every report.

## 4. Camera source registry / dropdown

2026-10-28: contratto dell'aggiunta PS4 stereo e limiti di validazione in
[`PS4_SUITE_SOURCE_20261028.md`](PS4_SUITE_SOURCE_20261028.md). Il resoconto
datato sotto descrive la baseline precedente all'aggiunta.

Implemented in `tools/psvr_suite/cameras.py`, exposed in the Camera tab and wired
through settings and `tools/psvr_suite/tracking.py`:

- A pure registry of acquisition sources actually available today,
  exposed as the UI dropdown with human-readable labels: `pseye` (PS3 Eye,
  native backend, default,
  requires `psvr_pseye.dll` + existing libusb-win32 binding),
  `opencv-dshow`, `opencv-msmf`, `opencv-any` (generic OpenCV stack).
  No fictional entries; unlisted OpenCV aliases are not Windows 6DoF
  paths.
- `camera_source` (stable id) is the canonical suite preference and
  resolves to `(backend, index)` for `open_camera_source`. Legacy
  `backend`/`index` pairs are accepted only when they match a registered
  id — mismatched or unknown combinations fail closed in
  `normalize_prefs` (`IMMUTABLE_CAM_KEYS` includes `camera_source`;
  changing the source reopens the camera within the same tracking log).
- No implicit hardware enumeration; extension is a single
   `register_camera_source({"id": ..., "backend": ..., "index": ...})` call with a real
  implementation, and the dropdown + validation follow automatically.
- **Selecting a source does not replace calibration.** `camera_source`
  is host-side only (which camera the tracking worker opens; never
  written to `steamvr.vrsettings` — `tools/psvr_suite/settings.py`).
  Intrinsics load separately from `intrinsicsPath`; the worker refuses
  frames whose resolution differs from the calibrated image size
  (`tools/psvr_suite/tracking.py`: `"live camera resolution differs from
  calibration"`), and the only calibrated baseline shipped today is the
  PS3 Eye (`configurations/pseye_blue_640x480_calibrated.json`). Each new
  source needs its own calibration before it can be trusted for 6DoF.
- Status: offline contracts exist (`tests/test_suite_camera_source.py`,
  `tests/test_suite_tracking.py`); **live capture through each source
  needs the hardware validation of §2.**

## 5. Long-session clock tracking

Two distinct clock layers, both already in the codebase:

1. **IMU transport clock mapping** — `tools/psvr_camera/imu_clock.py`
   (`ImuClockMapper`): one-way mapping of driver timestamps into the
   local receive clock using a 2 s sliding-**minimum** transport floor.
   Deliberate limits, quoted from the module: it "is not clock
   synchronisation: constant latency is unobservable"; published
   timestamps are immutable and monotone within one clock origin; equal source
   sample times stay equal, including a stalled sensor. Offset increases are
   limited to 1000 ppm to avoid disguising queued stale packets as fresh after
   a transport stall. A session or diagnostic elapsed-time reset clears the
   mapping; recenter clears pose history without changing the clock origin.
2. **Suite tracking clock** — `tools/psvr_suite/tracking.py`: capture
   timestamps are `time.perf_counter()` seconds (`monotonic_s`), session
   header records "host_time_ns is wall clock; no synthetic pose". IMU
   freshness is measured as `captured_at - imu_sample.monotonic_s`
   bounded by `MAX_AGE_S` from the driver IMU source; per-frame
   telemetry exposes `age_ms`, `session_id`, `recenter_epoch`, `seq`;
   the runtime probe path additionally forwards `imu_sample_age_ms`.
   Driver rebinds (`IMU_REBIND_EVERY_S`) and filter resets log
   `position_reset` events with `monotonic_s`, keeping the clock basis
   auditable in the JSONL.

The previous lifetime minimum could accumulate lag under relative clock drift
and remapped previously published history. The new bounded window removes that
behaviour. This is a robustness fix, not proof that clock drift caused the
reported ten-minute degradation: Windows MSVC steady_clock and Python
perf_counter normally share QPC. Real camera timing, optical rejection and IMU
freshness must be examined if the symptom persists.

Testing: `tests/test_imu_clock.py` covers the 2 s sliding-minimum window
and the immutable/monotone published-timestamp contract, including
offline **synthetic ≥20-minute (1200 s) driver and diagnostic soak feeds**
   including uninterrupted sessions, jitter, stalls and resets. A **live hardware soak of roughly
20–30 minutes** under real IMU/camera streams is still required and has
not been run.

Status: these are code-level facts validated offline; behaviour of both
clocks on live IMU/camera streams is part of §2, not claimed done.

## 6. Live acceptance check for the next release

1. In Camera, keep PS3 Eye for the existing calibrated setup, apply the source
   and save preferences; confirm the choice survives a suite restart.
2. Run a 20–30 minute 6DoF session with repeated head motion and short optical
   occlusions. Compare the first and last minute of the tracking JSONL:
   IMU `age_ms`, camera frame intervals, rejection/loss reasons and recovery.
3. If tracking degrades, preserve the session JSONL before restarting. Passing
   synthetic clock tests alone does not close the reported hardware issue.
4. Compare the same audio application and endpoint in cinema and VR, then test
   separate PC headphones as SteamVR output. Record the CLI report in each mode.
