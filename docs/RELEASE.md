# Release process (`0.3.0-alpha` pre-release)

Copyright (c) 2026 rickkyy. MIT.

## Version

- Release version: `0.3.0-alpha` — pre-release, **not stable**.
- Aligned with `CMakeLists.txt` (`project(psvr_pc VERSION 0.3.0 ...)`)
  and the `VERSION` file (`0.3.0-alpha`).
- Docs and the source-zip name use `0.3.0-alpha`.
- The `CHANGELOG.md` `[Unreleased]` items (read-only `tools.psvr_audio_diag`,
  the camera-source registry, the IMU/capture clock mapping) are **not** a
  version bump: `VERSION`, `CMakeLists.txt` and the artefact name stay
  `0.3.0-alpha` until a release is actually cut, and until then no
  hardware-validated claim is made for them.

## Package

- First artefact: **source-only** zip
  `dist/psvr-suite-0.3.0-alpha-source.zip`, built from a clean tree:
  no `build/`, `out/`, `logs/*` outputs, no `.venv-camera/`,
   no `third_party/cache/`, no live user preferences or virtualenv.
- Suggested contents: `VERSION`, `README.md`, `README.it.md`,
  `CHANGELOG.md`, `LICENSE`, `BUILD.md`, `TESTING.md`,
  `TROUBLESHOOTING.md`, `ARCHITECTURE.md`, `CMakeLists.txt`,
  `CMakePresets.json`, `build.ps1`, `run_suite.ps1`, `config.json`,
  `configurations/` (baselines only — exclude `psvr_suite_user.json`
   and `.bak`, and `psvr_suite_ui.json`), `driver/`, `tools/`, `scripts/`, `tests/`, `docs/`,
  `third_party/README.md` + licence texts (no caches/binaries).
- Runtime DLLs / `out/*` binaries are **excluded**: PS3 Eye backend
  tables (GPLv2-derived) and libusb runtime (LGPL) obligations are
  unresolved — see `third_party/README.md`. Do not redistribute
  binaries until the licence path is cleared.

## Local release checks

1. Build + CTest (defaults `-Toolchain VS2022 -Configuration
   RelWithDebInfo`):
   `powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1`
   — must end with CTest PASS.
2. Full offline Python tests in the camera venv (Python 3.11;
   `numpy==2.4.6`, `pytest==9.1.1`,
   `opencv-contrib-python-headless==5.0.0.93`):
   `python -B -m pytest tests -q`. On failure, redirect to a
   log file and read back only the tail:
   `pytest tests -q *> build/pytest-suite.log`, then
   `Get-Content build/pytest-suite.log -Tail 40`.
   No live camera / SteamVR / driver hardware in checks.
3. Offline script test: `tests\test_projection_trial.ps1`
   (temp fixtures under `%TEMP%`).
4. `.\run_suite.ps1 -Check` (headless, no Tk/hardware).
5. Licence check: `python tools/collect_licenses.py --check`.
6. Confirm the accepted choices still hold: distortion `identity`,
   One Euro `2.5/8`, fallback default off, personal FOV
   `H 78.2670786731 / V 84.9392708260561` documented as diagnostic.
7. Confirm the known limitations are still stated in `README.md`:
   no 90 fps native claim, `-202` resize workaround only, fallback
   opt-in, optics not calibrated.
8. Run `python tools/prepare_release.py` to build the source zip and SHA-256
   checksum into `dist/`. The archive contains a per-file manifest. Verify it unzips +
   configures (`cmake --preset vs2022`, with
   `FETCHCONTENT_SOURCE_DIR_*` overrides if offline).
9. Run `python tools/verify_release.py dist/psvr-suite-0.3.0-alpha-source.zip`
   to verify every source hash, extract to a temporary isolated checkout,
   and run license, headless, and Python checks from that checkout. The Python
   interpreter running the verifier must have `requirements-dev.txt` installed.
10. Read-only audio diagnostics, offline contract only (no USB, no
    PowerShell, no SteamVR):
    `python -B -m pytest tests/test_audio_diag.py -q` and
    `python -m tools.psvr_audio_diag --offline --json` (must exit 0;
    `pu_mode` stays `unknown`). Live audio state is operator-checked only
    through `tools.psvr_audio_diag` read-only queries — never by changing
    SteamVR settings, driver bindings or sending audio/unknown USB commands.

## Hardware validation still open before the next release

- Audio: cinema-mode headphone path is user-validated; SteamVR/VR-mode
  headphone silence is a PU hardware routing behaviour with **no
  documented host-side command** — no fix is claimed or promised. The
  test matrix (same PU, cable, HDMI endpoint, application) and the MI_01
  "PS VR Audio" capability check (plain USB audio, **not** the
  proprietary 3D audio pipeline) are still pending on real hardware.
- Camera-source registry (suite dropdown: `pseye`, `opencv-dshow`,
  `opencv-msmf`, `opencv-any`) and IMU/capture clock tracking: offline
  contracts pass (including ≥20-minute synthetic clock soaks); live
  capture through each source, per-source calibration, and a
  **~20–30-minute hardware clock soak** under real streams still need
  hardware validation.
- Details: `docs/AUDIO_TRACKING_CAMERA_20261015.md`.

## CI (reference)

`.github/workflows/ci.yml` mirrors the checks on `windows-2022`
(VS2022, CMake preset `vs2022`, Python 3.11, CTest + full
`pytest tests` + offline `.ps1` test + headless suite check +
licence check). `permissions: contents: read`. Test output goes to
log files; only a short tail is replayed on failure. No publish, no
push, no deployment, no secrets, no user-local paths.
