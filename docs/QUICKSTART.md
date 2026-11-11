# PSVR Suite Quick Start (docs/QUICKSTART.md)

## Web language selection

- The **Lingua / Language** selector switches between **Italiano** and **English**
  immediately; the choice persists without restarting tracking. Italian is the
  initial language. Driver preferences and UI language are stored separately and
  are excluded from source releases.

- **PSVR Suite launcher** (`tools/psvr_suite/install_psvr_suite_shortcut.ps1`)
  installs the PSVR Suite shortcut from the repo root. Run from the root with
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

## How to run and test

- **Virtual environment:** create a local Python environment with
  `python -m venv .venv-camera` and install the requirements from
  `requirements-suite.txt` (numpy, opencv-contrib-python-headless).
- **Build + offline tests:** run
  `powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1`
  (defaults: VS2022 / RelWithDebInfo).
- **Headless suite check:** run
  `powershell -NoProfile -ExecutionPolicy Bypass -File .\run_suite.ps1 -Check`
  for headless checks without Tk/hardware.
- **Camera tests:** run `pytest tests/test_camera*.py` (pytest or unittest).
  Camera tests are registered under `test_camera*` and are skipped when pytest
  is not registered.

## Security checks

- No push to remote GitHub (the remote is not configured for this session).
- No credentials or personal data are written to the repo.
- Tests are run locally; no network calls are made from the repo.

## Missing / open items

- **Motion robustness:** not certified; the 6DoF fallback is opt-in and
  motion robustness is not guaranteed.
- **Opt-in fallback:** 3DoF fallback is optional and disabled by default;
  enabling it changes motion feel.
- **`-202` resize issue:** resizing the desktop shown in the SteamVR
  dashboard reproducibly triggers the `-202` headset error
  (`fullscreen=false`). Restarting the compositor recovered
  `fullscreen=true` (10/10) in the observed session, but this is a
  **compositor-only workaround, not a definitive fix**.
- **GPL/LGPL binary redistribution:** the runtime DLL/backend (PS3 Eye
  backend tables, libusb runtime) has unresolved GPL/LGPL obligations.
  Do not redistribute binaries until that is resolved.

## Aggiungere la propria webcam via agente

La suite permette di aggiungere una webcam personale registrando un nuovo
backend e profilo tramite l'agente. L'agente aggiunge il backend della
telecamera in `tools/psvr_camera/camera_backend.py` e
`tools/psvr_camera/camera_source.py`, registra il profilo sotto
`configurations/` ed espone i controlli in `tools/psvr_suite/cameras.py`
e la directory `camera_settings/`.

Esempio di prompt per l'agente (vincoli solo su questi file, niente
credenziali, nessun push a GitHub remoto):

    Aggiungi un nuovo profilo webcam per una telecamera USB/UVC locale.
    Registra il backend in `tools/psvr_camera/camera_backend.py` e
    `tools/psvr_camera/camera_source.py`, crea un profilo JSON sotto
    `configurations/` ed espone i controlli di cattura/calibrazione in
    `tools/psvr_suite/cameras.py` e `camera_settings/`. Esegui il
    controllo della suite con `pytest tests/test_camera_capture.py
    tests/test_suite_camera_source.py` e verifica la preview della tab
    della telecamera e la routine di calibrazione da `cli.py`.

Passaggi di verifica: esegui il flag `-Check` della suite, visualizza la
preview della tab della telecamera e esegui la calibrazione da
`cli.py`. Il profilo è locale; `psvr_suite_user.json` non viene
committato su GitHub.

## Documentation map

- Start here: `docs/QUICKSTART.md` ·
  `docs/RELEASE.md` · `CHANGELOG.md` · `docs/DONATIONS.md`
- Live project status: `STATUS.md`
- Architecture / build / wiring / troubleshooting:
  `ARCHITECTURE.md` · `BUILD.md` ·
  `TESTING.md` · `TROUBLESHOOTING.md` ·
  `third_party/README.md` ·
  `docs/CONFIGURATION.md` ·
  `docs/WINUSB_CONTROL.md`
- Milestones: `M1` · `M2` · `M3` · `M4` · `M5` · `M6 software` ·
  `M9 software`
- Session evidence (2026-10-14):
  `SUITE_GUI_20261014.md` · `MSFS2024_FIRST_FLIGHT_20261014.md` ·
  `MOVEMENT_TRIAL_FEEDBACK_20261014.md` · `DASHBOARD_RESIZE_FULLSCREEN_20261014.md` ·
  `OPTICS_RADIAL_TRIAL_20261014.md` · `PROJECTION_TRIAL.md` ·
  `PSVR_SUITE_LAUNCHER.md`
- Physical checks / manual queue:
  `M3_PHYSICAL_CHECKS_20261004.md` · `MANUAL_VALIDATION_QUEUE.md` ·
  `KALMAN_OPTIMIZATION_20261013.md`
