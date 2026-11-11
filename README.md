# PSVR Suite (PSVR-PC)

Copyright (c) 2026 rickkyy. MIT License — see [LICENSE](LICENSE).

**Version:** `0.3.0-alpha` (pre-release, **not stable**).
Release version, aligned with `CMakeLists.txt` (`psvr_pc 0.3.0`) and the
`VERSION` file. No stability, performance, or calibration claims.
See [CHANGELOG.md](CHANGELOG.md) and [docs/RELEASE.md](docs/RELEASE.md).
Local build, clean-source verification, and packaging results are recorded in
[docs/RELEASE_VERIFICATION.md](docs/RELEASE_VERIFICATION.md).

Open-source Suite for **PSVR1 (PS4 headset) on Windows 10/11 x64**,
with an optional **PS3 Eye** camera path for 6DoF position.
**PS4 Camera stereo acquisition** is also available from the Camera tab
(external UVC firmware setup required; stereo acquisition verified 2026-10-24, static 6DoF experimentally verified 2026-11-01 (629/954); motion robustness not certified).
See [docs/PS4_CAMERA.md](docs/PS4_CAMERA.md).
Project name: **PSVR Suite / PSVR-PC**.

> Canonical language of this file is English. A best-effort Italian mirror
> is in [README.it.md](README.it.md); in case of divergence this file wins.
> Historical detail reports remain linked below and are preserved as-is.

## What this is

- **Stage 1 (verified on real hardware):** passive USB/HID diagnostics,
  ~1000 sensor reports/s, power on/off and VR/cinema mode with device
  state confirmation. Current binding: **sensors = HID (interface 4),
  control = WinUSB (interface 5)**.
- **Stage 2 (in progress, real-hardware checked):** 3DoF IMU tracking +
  Tk monitor GUI, OpenVR HMD-only driver (`out/driver_psvrpc/psvrpc`,
  disabled by default), extended-desktop display component (opt-in),
  optics loader/validator, PS3 Eye metric tracking + UDP/IMU/OpenVR bridge.
- **Actual play (real sessions, not synthetic):**
  - **MSFS 2024:** two real VR sessions on 2026-10-14, both logged.
    Session 1 (17:50–18:19 local) started well then degraded with marked
    stutters and heavy 3DoF fallback; session 2 (18:21–18:32 local, after
    restart with fallback **disabled** and distortion **identity**, no other
    changes) was much better with few stutters. Identity was the user's
    preferred visual choice in-game. Details in
    [MSFS2024_FIRST_FLIGHT_20261014.md](docs/MSFS2024_FIRST_FLIGHT_20261014.md).
  - **Assetto Corsa:** user feedback that cars/objects (e.g. wheels) look
    narrow-tall motivated the diagnostic projection trial below.
    No lap-time or fidelity claims.
  - **No native 90 fps guarantee.** Both MSFS sessions show ~31%
    reprojection with 0 dropped frames; compositor/CPU/GPU timings overlap
    and must not be summed into a framerate claim. See
    [docs/MSFS2024_FIRST_FLIGHT_20261014.md](docs/MSFS2024_FIRST_FLIGHT_20261014.md).

## Accepted choices (do not auto-revert)

These are explicit user-tested choices. Code and docs must not silently
change them back:

- **Distortion: `identity` accepted.** `peripheral` and `stronger` are
  **visually rejected** (background feels stretched vs foreground).
  `peripheral` was applied as a diagnostic counter-warp but never visually
  accepted (`lens_acceptance_verified=false`). See
  [OPTICS_RADIAL_TRIAL_20261014.md](docs/OPTICS_RADIAL_TRIAL_20261014.md)
  and [SUITE_GUI_20261014.md](docs/SUITE_GUI_20261014.md).
- **Position filter: One Euro `2.5 Hz / beta 8` is the default baseline.**
  Kalman (initial/optimized) is selectable but **not accepted in motion**:
  live comparison showed 1,856 valid optical poses held by Kalman (7.19%)
  vs 0 extra holds by One Euro on valid optics. Continuity first. See
  [MOVEMENT_TRIAL_FEEDBACK_20261014.md](docs/MOVEMENT_TRIAL_FEEDBACK_20261014.md).
- **3DoF fallback: optional, default `false`.** The driver omits the key
  unless the user opts in; there is no global loose default. The second
  MSFS session ran with fallback disabled (no `3dof_fallback` /
  `6dof_reacquiring` transitions). The hardware smoke test
  (`6dof → 3dof_fallback → 6dof`, PASS) covers fallback only when
  explicitly enabled, with recovery blend `150 ms / 1.5 m/s`. See
  [SUITE_GUI_20261014.md](docs/SUITE_GUI_20261014.md).
- **Projection/FOV: `H 78.2670786731° / V 84.9392708260561°`, diagnostic
  personal setting, not universal calibration.** Preserves the projection
  aspect `tan(H/2)/tan(V/2) = 8/9` with ~1.09247 uniform magnification.
  Baseline was `H90/V90` (±1 tangents); intermediate diagnostic was
  `H83.2671/V90`. These numbers change apparent angular size only; they
  are **not physically calibrated and not product claims**. See
  [PROJECTION_TRIAL.md](docs/PROJECTION_TRIAL.md).
- **`-202` resize issue: reproducible trigger, session-only recovery.**
  Resizing the desktop shown in the SteamVR dashboard reproducibly
  triggers headset error `-202` (`fullscreen=false`). Restarting only the
  compositor recovered `fullscreen=true` (10/10) in the observed session,
  but this is a **compositor-only workaround, not a definitive fix**.
  The `6500 µs vs 5000 µs` tick-gap threshold is unchanged. See
  [DASHBOARD_RESIZE_FULLSCREEN_20261014.md](docs/DASHBOARD_RESIZE_FULLSCREEN_20261014.md).

## Requirements

- Windows 10/11 x64, PSVR1 headset + USB.
- PS3 Eye camera only for the 6DoF optical path (optional).
- WinUSB on control interface MI_05 is a **separate manual user step**
  with Zadig (external GPLv3 tool, **not bundled, not installed by the
  build**). Procedure: [docs/WINUSB_CONTROL.md](docs/WINUSB_CONTROL.md).
- Build: Visual Studio 2022 C++ workload (preferred) or portable
  w64devkit MinGW; CMake ≥ 3.24; Git in PATH; Python 3.11 for the suite
  GUI and offline tests. Full steps: [BUILD.md](BUILD.md),
  [docs/QUICKSTART.md](docs/QUICKSTART.md).

## Quick start (local)

```powershell
# 0. Create the local Python environment once:
py -3.11 -m venv .venv-camera
.\.venv-camera\Scripts\python.exe -m pip install -r requirements-dev.txt

# 1. Build + offline tests (defaults: -Toolchain VS2022 -Configuration RelWithDebInfo):
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1

# 2a. Start Menu launcher (no console window):
# Install once, then open "PSVR Suite" from Start:
powershell -NoProfile -ExecutionPolicy Bypass -File tools\psvr_suite\install_psvr_suite_shortcut.ps1

# 2b. Or start directly from the repo root:
powershell -NoProfile -ExecutionPolicy Bypass -File .\run_suite.ps1
# Optional: .\run_suite.ps1 -StartVR   # VR after init + guided calibration
#           .\run_suite.ps1 -Attach    # track toward already-running VR
#           .\run_suite.ps1 -Check     # headless check, no Tk/hardware
```

The header **Lingua / Language** selector switches between **Italiano**
and **English** immediately; the choice persists without restarting tracking.
Italian is the initial language. Driver preferences and UI language are
stored separately and are excluded from source releases.

Keep the headset still throughout calibration (8 s plus startup margin),
then wear + Recenter. Building alone does not register/configure a SteamVR
driver: follow the driver and PS3 Eye setup links in the quickstart.
Suite behaviour and tabs: [docs/PSVR_SUITE_LAUNCHER.md](docs/PSVR_SUITE_LAUNCHER.md),
[docs/MONITOR_APP.md](docs/MONITOR_APP.md),
[docs/IMU_TESTING.md](docs/IMU_TESTING.md).

Diagnostics binary: `out/diag/psvr_diag.exe`:

```powershell
.\out\diag\psvr_diag.exe --config .\config.json --duration-ms 5000 --raw-packets 3
$LASTEXITCODE
```

## Known limitations

- Pre-release: tracking continuity, optical availability, lens comfort,
  and `-202` recurrence are open. No 90 fps native claim.
- FOV/optics numbers above are personal diagnostics, not calibrated data.
- Fallback and recovery are opt-in; enabling them changes motion feel.
- WinUSB/Zadig step is manual; the build never changes drivers.
- First release artefact is source-only
  (`dist/psvr-suite-0.3.0-alpha-source.zip`). Runtime DLL/backend
  redistribution has unresolved GPL/LGPL obligations (PS3 Eye backend
  tables, libusb runtime) — see [third_party/README.md](third_party/README.md)
  and [docs/RELEASE.md](docs/RELEASE.md). Do not redistribute binaries
  until that is resolved.

## Web language selection and PSVR Suite

- The **Lingua / Language** selector switches between **Italiano** and **English**
  immediately; the choice persists without restarting tracking.
  Italian is the initial language. Driver preferences and UI language are
  stored separately and are excluded from source releases.
- **PSVR Suite launcher** (`tools/psvr_suite/install_psvr_suite_shortcut.ps1`)
  installs the PSVR Suite shortcut from the repo root; run from the root with
  `powershell -NoProfile -ExecutionPolicy Bypass -File .\run_suite.ps1` to start the
  suite directly. `-StartVR` / `-Attach` / `-Check` flags are documented in
  `run_suite.ps1`.
- **PS4 camera stereo acquisition** is available from the Camera tab
  (external UVC firmware setup required; stereo acquisition verified
  2026-10-24, static 6DoF experimentally verified 2026-11-01, 629/954 frames;
  motion robustness not certified).
- **PS3 Eye camera path** is the optional 6DoF optical path for the PSVR Suite.
  The `tools/psvr_camera/` source tree holds the camera driver, position
  estimation, and calibration tools.

## Known limitations

- Pre-release: tracking continuity, optical availability, lens comfort,
  and `-202` recurrence are open. No 90 fps native claim.
- FOV/optics numbers above are personal diagnostics, not calibrated data.
- Fallback and recovery are opt-in; enabling them changes motion feel.
- WinUSB/Zadig step is manual; the build never changes drivers.
- First release artefact is source-only
  (`dist/psvr-suite-0.3.0-alpha-source.zip`). Runtime DLL/backend
  redistribution has unresolved GPL/LGPL obligations (PS3 Eye backend
  tables, libusb runtime) — see [third_party/README.md](third_party/README.md)
  and [docs/RELEASE.md](docs/RELEASE.md). Do not redistribute binaries
  until that is resolved.

## Documentation map

- Start here: [docs/QUICKSTART.md](docs/QUICKSTART.md) ·
  [docs/RELEASE.md](docs/RELEASE.md) · [CHANGELOG.md](CHANGELOG.md) ·
  [docs/DONATIONS.md](docs/DONATIONS.md)
- Live project status: [STATUS.md](STATUS.md)
- Architecture / build / wiring / troubleshooting:
  [ARCHITECTURE.md](ARCHITECTURE.md) · [BUILD.md](BUILD.md) ·
  [TESTING.md](TESTING.md) · [TROUBLESHOOTING.md](TROUBLESHOOTING.md) ·
  [third_party/README.md](third_party/README.md) ·
  [docs/CONFIGURATION.md](docs/CONFIGURATION.md) ·
  [docs/WINUSB_CONTROL.md](docs/WINUSB_CONTROL.md)
- Milestones: [M1](docs/MILESTONE_1_REPORT.md) ·
  [M2](docs/MILESTONE_2_REPORT.md) · [M3](docs/MILESTONE_3_REPORT.md) ·
  [M4](docs/MILESTONE_4_REPORT.md) · [M5](docs/MILESTONE_5_REPORT.md) ·
  [M6 software](docs/MILESTONE_6_SOFTWARE.md) ·
  [M9 software](docs/MILESTONE_9_SOFTWARE.md)
- Session evidence (2026-10-14):
  [SUITE_GUI_20261014.md](docs/SUITE_GUI_20261014.md) ·
  [MSFS2024_FIRST_FLIGHT_20261014.md](docs/MSFS2024_FIRST_FLIGHT_20261014.md) ·
  [MOVEMENT_TRIAL_FEEDBACK_20261014.md](docs/MOVEMENT_TRIAL_FEEDBACK_20261014.md) ·
  [DASHBOARD_RESIZE_FULLSCREEN_20261014.md](docs/DASHBOARD_RESIZE_FULLSCREEN_20261014.md) ·
  [OPTICS_RADIAL_TRIAL_20261014.md](docs/OPTICS_RADIAL_TRIAL_20261014.md) ·
  [PROJECTION_TRIAL.md](docs/PROJECTION_TRIAL.md) ·
  [PSVR_SUITE_LAUNCHER.md](docs/PSVR_SUITE_LAUNCHER.md)
- Physical checks / manual queue:
  [M3_PHYSICAL_CHECKS_20261004.md](docs/M3_PHYSICAL_CHECKS_20261004.md) ·
  [MANUAL_VALIDATION_QUEUE.md](docs/MANUAL_VALIDATION_QUEUE.md) ·
  [KALMAN_OPTIMIZATION_20261013.md](docs/KALMAN_OPTIMIZATION_20261013.md)

## License and donations

MIT — free functions for everyone, same build whether or not you donate.

## Add your own webcam via agent

The suite lets you add a personal webcam by registering a new backend and
profile through the agent. The agent adds the camera backend in
`tools/psvr_camera/camera_backend.py` and `tools/psvr_camera/camera_source.py`,
registers the profile under `configurations/`, and exposes the controls in
`tools/psvr_suite/cameras.py` and the `camera_settings/` directory.

Example prompt for the agent (constraints only on these files, no
credentials, no push to remote GitHub):

    Add a new webcam profile for a local USB/UVC camera. Register the
    backend in `tools/psvr_camera/camera_backend.py` and
    `tools/psvr_camera/camera_source.py`, create a profile JSON under
    `configurations/`, and expose the capture/calibration controls in
    `tools/psvr_suite/cameras.py` and `camera_settings/`. Run the
    headless suite check with `pytest tests/test_camera_capture.py
    tests/test_suite_camera_source.py` and verify the camera tab preview
    and the calibration CLI (`tools/psvr_camera/cli.py`).

Verification steps: run the suite `-Check` flag, preview the camera tab,
and run the calibration routine from `cli.py`. The profile is local only;
`psvr_suite_user.json` is not committed to GitHub.
Donations are voluntary and optional via PayPal; no PayPal link is
published yet. See [docs/DONATIONS.md](docs/DONATIONS.md).
Dependency licences (BSL-1.0 decoder, HIDAPI BSD, JSON MIT, OpenVR
BSD, PS3 Eye backend GPL notes): [third_party/README.md](third_party/README.md).
