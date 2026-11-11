# Changelog

Release version `0.3.0-alpha` (pre-release, not stable), aligned with
`CMakeLists.txt` (`psvr_pc 0.3.0`) and the `VERSION` file.
Live status: `STATUS.md`. Session reports in `docs/` are linked from
`README.md` and kept as historical detail.

## [Unreleased] — planned for the next pre-release

### Included

- Constellation recognition `constellation_led_v1` (live feedback 2026-10-16):
  many fewer gray screens, small residual hitches possibly at constellation
  changes (user hypothesis, not proven cause); current baseline kept,
  transition optimization deferred. Replay/stability caveats and 336+219
  test verification in `docs/CONSTELLATION_RECOGNITION_20261016.md`.
  No perfect-solution claim.

- Read-only audio diagnostics CLI `tools/psvr_audio_diag`
  (`python -m tools.psvr_audio_diag`, `--offline`, `--json`): PU mode
  comes only from the existing passive `psvr_diag --status` F0
  observation (never power/mode/LED commands), plus read-only USB
  composite/MI_01 and Windows render-endpoint inventory. Unknown stays
  unknown; offline guard tests in `tests/test_audio_diag.py`. No SteamVR
   setting or driver binding is modified by the diagnostics.
- Camera-tab dropdown with persisted `camera_source`, human-readable PS3 Eye
  and OpenCV backend labels, worker reopening on source change, and an
  extensible source registry for future cameras.
- Long-session IMU timestamp mapping: replace the lifetime minimum with a
  bounded 2 s window, preserve published history and cap upward offset changes.
  Offline regressions cover uninterrupted 20-minute sessions, drift, jitter,
  stale transport and session/recenter boundaries.
- `docs/AUDIO_TRACKING_CAMERA_20261015.md`: cinema-vs-VR-mode audio
   observations (PU routing is a possible cause of VR-mode headphone silence;
   **no headset-jack audio fix is implemented or validated**; MI_01 "PS VR Audio" is
  plain USB audio, **not** the proprietary 3D audio pipeline), the new
  camera-source registry behind the suite dropdown (selecting a source
  does **not** replace calibration), and the IMU/capture clock tracking
  (2 s sliding-minimum window, immutable published timestamps). The
   practical audio workaround — a separate PC headset routed through
   SteamVR — is documented as an operator workaround, not a fix.
- B1-01: driver-settings rollback now compares against the same byte snapshot
  it decoded, rejecting detected concurrent edits without overwriting unrelated
  application settings. Regression tests cover conflicting edits and preserving
  unrelated changes made before the snapshot; this remains an optimistic check.
- B1-02: driver-settings writes now stop when the runtime-process query fails
  or returns an unknown state. Successful empty queries remain valid; failures
  at the second guard leave the settings unchanged and the journal unapplied.
  Tests cover query errors and the real PowerShell script with mocked inventory.
- B1-03: initial preferences now read the driver file via an explicit
  `DEFAULT_VRSETTINGS` path, keeping `--preferences` > user > driver >
  defaults order and leaving corrupt files untouched. Covered by 10 new
  headless tests with real temp files.
- B1-07: `check_paths` checks the file only in `profile` mode;
  `identity`/`current` or missing key need no file, camera files still
  checked. Covered by 7 new read-only path tests.
- B1-04: saved/UI camera FPS must be positive integers (75.0 is represented
  exactly as 75); fractional rates fail before saving. Diagnostic IMU exports
  reject gaps above 20000 us before creating a file, while preserving the
  driver's separate 50000 us limit. Native parser interoperability checks
  cover exported timing boundaries; the global baseline remains read-only.
- B1-05: `tracking_config_path` now reaches `psvr_diag --config` for both VR
  startup commands and cinema transitions; relative paths use the suite root,
  with no fallback for an invalid explicit path. UI labels distinguish this
  diagnostic file from driver settings and camera-worker configuration.
- B1-06: importing a partial driver section now reports 3DoF when
  `positionEnabled` is absent, matching native defaults. Only explicit true
  imports 6DoF; malformed flag types are rejected. Fresh suite defaults remain
  6DoF. These three fixes add 12 Python regressions plus native parser checks.
- B2-01: read-only PnP/HID inventory now completes with multiple MI_05 nodes;
  ambiguous capture/control returns exit 3 before ownership, open or write.
- M2 contract: descriptor/status/power/mode/LED actions require only a healthy,
  unambiguous MI_05 WinUSB control interface; MI_04 remains mandatory for
  passive/IMU capture. Existing multi-device ambiguity guards are preserved.
- Block 2 offline coverage is now in CTest: 41 real-consumer scenarios,
  53 real-backend fault/contract scenarios with Windows device APIs replaced
  at test call sites, and 4 real ownership cases on isolated mutex names.
  Current-source CMake/MSVC build and 17 selected CTest checks pass; physical
  unplug/replug during capture remains a separate hardware validation.
- B3-01: standalone IMU capture checks the previous progress timestamp before
  accepting buffered reports and after control polling. Configured timeouts
  stay inclusive and unchanged; ordinary jitter, isolated malformed/duplicate
  reports and brief empty reads within the window remain tolerated.
- B3-02: prediction angular velocity now uses the physical sensor-to-world
  rotation, without the neutral head offset. Recenter transports EMA history
  through the world-reference change only. Geometric regression coverage
  includes tilted neutral poses, full/yaw, smoothing and repeated recenters.
- B3-03: CLI axis-test instructions and startup log identify the baseline at
  calibration completion and disabled manual recenter; the worn guided trial
  uses ordinary capture/monitor with recenter before measurement.
- Block 3 regressions registered in CTest: 21 real-capture scenarios with a
  deterministic monotonic clock and synthetic HID, and 16 velocity trajectories.
  Current CMake/MSVC build and 20 selected CTest checks pass, including driver
  pose, export and camera interoperability. Physical yaw/thermal checks remain
  deferred in `docs/MANUAL_VALIDATION_QUEUE.md`.

### Block 4 software fixes
- B4-01: patched native completion retires failed re-submissions once, prevents
  submission after shutdown cancellation and keeps the owner alive through the
  callback's final access. Eight real-body transfer regressions run in CTest.
- B4-02: finite OpenCV probes/captures own open/read/close on one daemon thread
  with bounded caller waits; unsupported timeout properties remain nonfatal.
  A stuck call is not closed concurrently: reports expose `cleanup_pending`,
  and its owning thread releases it when the call returns. Capture callbacks
  remain on the caller's thread and are cooperative.
- B4-03/04: native handles stay under local ownership until adapter construction
  completes; buffer/thread-start failures close once. Reader exceptions surface
  through error/stop/wakeup, while ordinary transient grab failures retain the
  existing retry tolerance. Release wakes consumers.
- B4-05: latest-frame drop counts exclude frames already consumed.
- B4-06: native count/open refresh the device cache while idle; live handles
  keep their index snapshot. The shim checks patch level 3 before init and the
  package includes the complete patch series. MSVC exports the upstream C API.
- Block 4 validation: 130 Python tests and 63 subtests pass (7 expected skips),
  three native CTest contracts pass, including pre-init rejection of a legacy DLL;
  current shim and full upstream DLL built/linked with MSVC. Physical camera
  unplug/replug and source checks remain deferred in the manual queue.

### Block 5 software fixes (7/7: 6 P2 + 1 P3, 2026-10-16)

- B5-07: constellation outer update rejects explicit depth ambiguity as
  invalid and restores the motion prior; valid optical correction kept,
  strong-front fast path, no-pattern fallback, LED minimum and alignment kept.
- B5-01/02/03: conservative common-K Zhang scoring, offline collector pinned
  to first accepted board, full-K structure/invertibility checks; accepted
  blue profile only loaded, never reclassified.
- B5-04: generic-always IPPE planar solver (`planar_solver.py`, Kinv rays,
  full-K pixel score, both-front distinct-only gate); pure-NumPy fallback
  marked `physical_ambiguity_checked=False`; direct Intrinsics K validated
  in estimator. Original review pixel fixture now invalid/ambiguous.
- B5-05/06: stable Rodrigues branch near pi; white-core detector dilates the
  union once (review 500-core fixture: 2 full-frame dilations, same 500
  candidates, identical centroids/areas/thresholds).
- Validation: broad `camera or suite or lateral_handoff` 788 passed, 7 skipped,
  652 subtests; targeted 42 + 376 subtests; 21/21 native CTest + 3/3 PS Eye
  PASS; headless suite PASS; 120 s replay 8534 frames / 6335 valid. No live
  thresholds, accepted profiles, rig or world frame changed; no hardware
  claims; residual transition hitches still deferred to B5-HW-01.

### Block 6 software fixes (9/9: 2 P2 + 7 P3, 2026-10-16)

- B6-01: `load_position_config`/`PositionPipeline.__init__` raise `ValueError`
  (never a `TypeError` leak) for non-dict sections and unknown keys; the
  suite's tolerant Kalman table now falls back to defaults as designed.
- B6-02/05: explicit `context_valid is False` forces the One Euro branch
  invalid; `context_valid=None` defaults to `raw is not None` (no longer
  inferred from `prediction_allowed`). Production callers pass an explicit
  boolean, so recorded-path valid counts are unchanged.
- B6-04: `invalid()` updates `_last` on the One Euro branch and documents the
  intended asymmetry (Kalman full reset ~80 ms vs One Euro brief hold
  ~100 ms); no reset/prediction policy changed.
- B6-07: missing covariance with a present raw measurement reports
  `accepted/reliable=False` (`missing_covariance`); `raw_world_position_m`
  is copied, not aliased.
- B6-08: incomplete packets raise `ValueError` instead of `KeyError`
  (pipeline and One Euro filter).
- B6-03: successful Kalman reseed returns `rejection_reason=None` without
  double-counting the frame (diagnostics only; gates/Q/speed untouched).
- B6-06/09: `inflate_optical_covariance` validates shape/finiteness/symmetry
  and `det(alignment)==1`; non-numeric `scale` → `invalid_input`, string
  labels → `invalid_correspondences` (NumPy scalars still accepted, bool
  rejected). Thresholds, formulas and reliable/accepted policy unchanged.
- Validation: filter area **288 passed + 41 subtests** (filter, pipeline,
  position_pipeline[+b6], replay, uncertainty[+b6], estimator[+b6],
  kalman_*); new `test_camera_position_pipeline_b6` (15 + 5 subtests),
  `test_camera_pose_estimator_b6` (2), `test_camera_pose_uncertainty_b6`
  (15). Two synthetic tests + one budget test aligned to the production
  pattern (explicit `context_valid=True` where context was intended); GLS
  reseed shares the fixed return path but has no dedicated test. No
  threshold, accepted-profile or tracking-tolerance change; no physical
  proof required.

### Block 7 software fixes (9/9: 1 P0 + 1 P1 + 3 P2 + 4 P3/notes, 2026-10-16)

- B7-01/02/03 (`imu_source.py`): `DiagnosticImuSource.latest()` added
  (deepcopy, `None` when empty; invalid/anti-stale binding only, never
  promotes poses); quaternion validation in `_accept` (finiteness +
  norm ±1e-3, rejected rows dropped); `at()` argument validation as in the
  driver. New `tests/test_camera_imu_diagnostic_b7.py` (9 + 15 subtests).
- B7-04 (`driver_imu_source.py`): interpolation only within the same
  session/epoch; otherwise returns past without `interpolated`. New
  `tests/test_camera_driver_imu_b7.py` (3).
- B7-05/08 (`position_protocol.hpp`): identity change always discards origin
  and state, including legacy v1 without grace; unsigned `inliers` accepted
  without exception flow (rejection stays fail-closed).
- B7-06/07/09: dead except removed in `validate_driver_position_packet`
  (specific `ProtocolError` preserved); v1-only probe declared in
  `main.cpp` comment + README; version/yaw-offset comments; three B7-09
  notes as comments only.
- Validation: bridge area **89 passed + 119 subtests** (driver_python,
  driver_link, position_replay, driver_imu_b7, imu_diagnostic_b7,
  suite_tracking, protocol); native harness 14/14 and recompiled
  `position_tests.cpp` PASS (temp dir only). B7-01 P0 confined to the
  diagnostic trial in driver mode (live suite immune: driver mode and
  `--imu` are mutually exclusive, suite uses only `DriverImuSource`);
  huge uint64 `inliers` accepted as diagnostics-only. No threshold,
  profile or tracking-tolerance change; no physical proof required.

### Block 8 software fixes (6/6: 2 conditional P1 + 3 P2 + 1 cumulative P3, 2026-10-16)

- B8-01/02 (`driver_psvrpc.cpp`): explicit lifecycle state machine
  (Inactive/Activating/Active/Stopping) with serialized transitions and
  in-flight gates on Hmd publish and Provider frames; `Deactivate`/`Cleanup`
  drain before join/destroy; documented host-callback contract (no
  re-entry/wait on lifecycle). Sequential behavior unchanged.
- B8-03: simulated-worn mailbox drained after Stopping + drain, direct
  proximity/property restore, enqueue only while Active (180 s lease kept).
- B8-04: topology latch invalidated only on actual display-verification
  failure; other errors clean up with retry still possible.
- B8-05: allocation-free `ScopeExit` rollback before diagnostics, catch-all
  at SDK boundaries, contained teardown/callbacks; pose path byte-identical.
- B8-06: `stop_` under `display_wait_mutex_` + notify (join order kept);
  3 package asserts; `IsOnDesktop` extended-desktop left to owner decision
  (comment only).
- Validation: MSVC Release 0 errors; repo CTest **69/69**; mock DLL 19 cases
  + 192 sessions; harness 7/7, 8/8, 15/15 + B8-02/B8-04 regressions;
  stress up to 25,600 sessions. The two P1s are conditional (incorrect
  interleaving in code, no proof SteamVR overlaps those callbacks); no
  additional SteamVR guarantee claimed; no physical proof required.

### Block 9 software fixes (9/9 decided/fixed: 2 design P1 + 6 P2 + 1 cumulative P3, 2026-10-16)

- B9-01 (design decision): native gate unchanged (a simple AND cannot
  guarantee fail-closed with the unreliable producer); separate
  `native_desktop_absence_verified` helper + contract docs + producer
  warning + tests (selection 31, inventory 76, component 103 invalid).
  Gate integration pending a trustworthy producer.
- B9-02: device-removed reason logging verified pre-existing in tree
  (best-effort protected read, always-terminal fail, no retry); static
  21/21, mock-device stub 8/8, build + `ctest -R presenter` 1/1. In-place
  re-init remains a future architecture decision.
- B9-03/04 (`driver_psvrpc.cpp` Redirect): activation/publication gate +
  notification lease drained by Deactivate (oracle 1→0 poses); index
  published only after all properties succeed (oracle 1→0); repo CTest
  69/69 re-run by primary.
- B9-05/06/07 (scripts): single-read snapshot for parse+SHA256, re-read
  with fingerprint refuse + authorized-keys-only merge, explicit-boolean
  stale with `unknown` reason; new PS1 regressions 7/7, 10/10, 7/7
  (all offline on temp fixtures); existing projection 14/14,
  driver_scripts 21/21, display_core_probe PASS.
- B9-08/09: comments only (PS1 parse OK) + disarmed guard in boundary test
  (3 temp executables 3/3 PASS).
- Final on-HMD validation for native direct mode and device-lost deferred
  to a future hardware session (non-blocking); everything else offline.

### Block 10 software fixes (9/9: 2 P1 + 6 P2 + 1 cumulative P3, 2026-10-16)

- B10-01/02/05/06 (`trial.py`, `visual_trial.py`): typed restore validation
  (MODES always enforced), readiness requires `valid_count>0` fail-closed,
  grid `communicate` + kill + `grid_timeout` + guarded close, atomic
  `write_journal` + reused `require_section`. New `test_optics_trial_types`
  (5), `test_optics_visual_trial` (8 + 5 subtests),
  `test_optics_trial_journal_b10` (5).
- B10-03/04 (`distortion_math.hpp`): forward-certified acceptance with
  constrained square search (strict path bit-identical); certified
  strict-only edge extension from the conditioning bound. Repro 8 ULP/16x
  → 0 over-budget, no-over-reject 2646/2646 per mapper, sweep 2997/2997
  zero spurious rejects; distortion suite 61.865 then 89.676 checks (-O2/-O0),
  regressions FAIL on pre-fix headers; four shipped profiles unchanged.
- B10-07/08/09 (patterns/tests): isolated apply/restore cycle test,
  `test_optics_grid.py` (determinism, IHDR, spot-checks, no-overwrite),
  fixed 1024 documented, `read(LIMIT+1)` boundary, peripheral-doc, distinct
  messages, SPDX, `strip()` in `generate()`. Final area 77 + 12 subtests PASS.
- On-HMD optical validation remains queued (§12 B9); nothing else needs hardware.

### Block 14 software fixes (9/9: 1 P1 + 4 P2 + 4 P3, 2026-10-17)

- B14-01: 4 orphan PS1 wired into CTest with the existing pattern
  (`ctest -N` 73 tests, 4/4 Passed; temp configure, never in-tree build).
- B14-02/03: new `pseye-offline` and `build-ps1-offline` CI jobs
  (requirements verified on runners; existing jobs intact; first runs to
  be observed, no `continue-on-error`).
- B14-04: `build.ps1` configures via `--preset` + `-D` overrides (package
  literals removed, mingw preset self-sufficient; effective values
  identical, 30/30 parity; stale-cache caveat documented).
- B14-05: dict↔pin coherence test via AST (11 PASS + 36-license check OK).
- B14-06..09: explicit SKIPs, `--timeout 300`, cmake≥3.24 check (dedup
  declared not-done: needed for `BUILD_TESTING=OFF`), authoritative-pin
  note, count without number, `--generator` with unchanged default,
  whitespace filter (21/21 scripts PASS), flow doc line.
- Never install/package/deploy/push; no hardware proof required.

### Block 12 software fixes (9/9: 2 P1 + 1 P0/P1 coverage + 4 P2 + 1 cumulative P3, 2026-10-17)

- B12-01/02 (`app.py`): non-blocking `put_nowait` in the serial wrapper
  (dedicated `Full` branch, no Tk logging from the worker); close destroys
  on the main thread via completion event, bg only shuts down serial.
  New `test_suite_run_serial_nonblocking` (4) and
  `test_suite_on_close_thread` (2, main-only destroy pinned).
- B12-03/08 (tests): posix `creationflags` pin (regression written first,
  failed pre-fix) + fix with the shared `getattr` pattern; 13 + 40-subtest
  action/matrix suite, zero production changes, no real bug found.
- B12-04/05 (`app.py`): protected `set` + `after` in `finally` (drain never
  dies); last-frame coalescing inside the 32-batch (non-frame order kept).
  New `test_suite_drain_reschedule` (4) and `test_suite_frame_coalescing` (5).
- B12-06/07/09: instance timestamp removed (IT+EN); EN→IT tab aliases
  (order kept, new test 6 + 27 subtests); resize-after cancel, `MARKERS_UNKNOWN`
  casefold, `--root=` recognized, shared tab constant, deploy message +
  root docs, cameras docs-only (`Queue.join` correctly left: no timeout param).
- Validation: suite selection **305 + 217 subtests PASS** (primary re-run).
  Real Tk only a future manual check (non-blocking).

### Block 13 software fixes (9/9: 2 P2 + 7 P3, 2026-10-17)

- B13-01 (`summarize_transition.py`): line-by-line streaming with per-row
  try/`.get` defaults, skip + `skipped` count (valid-input totals
  identical); 4 new robustness tests.
- B13-02 (`replay_position_log.py`): operational memory limit in the module
  docstring/`--help`; format unchanged.
- B13-03/04 (probe/monitor): clean exit-2 errors in both CLIs (new 6-test
  file); `--root` resolved + `is_dir` with exit 2 (2 new tests).
- B13-05/06 (readiness): 1 MiB VDF cap + depth 64 → `VdfError`; installdir
  traversal/blank/utc validation (new 4 + 12 tests).
- B13-07/08/09 (analysis/benchmark): atomic `open('x')` in both tools;
  200 MiB PE cap + unpack bounds + clean exit (new 11 tests); 100k frames
  cap. No hardware proof required (all offline with mocks).

### Block 11 software fixes (10/10: 2 P1 + 5 P2 + 2 P3 + 1 P0-doc note, 2026-10-16)

- B11-01/02 (`runtime.py`): `DefaultBackend.shutdown()` reaps terminated
  handles (never kill) + `close()` alias; worker registry (`WeakSet` +
  dedicated lock), bounded-join `shutdown()` reusing backend shutdown,
  cooperative grid cancel; `close()` idempotent. No signal/atexit handlers
  (future UI decision, declared).
- B11-05/06 (`tracking.py`): `_thread` cleared with identity guards on the
  mkdir early-return and in `stop`; `thread.start()` under the same lock.
  New `test_suite_tracking_mkdir_cleanup` (2) and
  `test_suite_tracking_start_stop` (2, fail pre-fix).
- B11-03/04 (`runtime.py`): `abort_requested` event with checkpoints,
  non-blocking `stop_vr` with preemptive quit, fail-closed `aborted`;
  entry-point exception wraps (`_emit` + False), `waiting` in try/finally,
  ownership documented without behavior change.
- B11-07/08/09/00: absolute prefs default, `--vrsettings`/`--dry-run`
  read-only, shortcut guard + `-Force`, producer lock, validated timeout,
  `startswith("vr")`, early optics resolve, smoke fail-fasts + rig label.
  New `test_suite_b11_08_09_00` (13 + 24 subtests).
- Validation: suite selection **269 + 150 subtests PASS** (primary
  re-run). No hardware proof required (smoke stays manual by design).

### Still needs hardware validation (no claims)

- VR-mode headphone silence test matrix (same PU, cable, endpoint, app).
- MI_01 USB-audio capability (playback? mic?) — unvalidated.
- HDMI endpoint behaviour across cinema/VR transitions.
- Live capture through each camera-source entry; per-source calibration
  (only the PS3 Eye baseline is calibrated today).
- Live clock behaviour, including a **~20–30 minute hardware soak**
  (offline synthetic ≥20-minute clock soaks already pass).

## [0.3.0-alpha] — pre-release, not stable

### Included

- English canonical `README.md` (+ Italian mirror `README.it.md`):
  hardware/scope, accepted choices (distortion `identity`,
  One Euro `2.5 Hz / beta 8` default, 3DoF fallback optional default
  `false`, diagnostic personal FOV `H 78.2670786731 / V 84.9392708260561`),
  `-202` resize workaround limits, MSFS 2024 two-session play + Assetto
  Corsa feedback with no native 90 fps claim.
- User guides: `docs/QUICKSTART.md` (build, launcher, first session),
  `docs/RELEASE.md` (version, source-only artefact, local checks),
  `docs/DONATIONS.md` (voluntary PayPal, no link published yet).
- CI: `.github/workflows/ci.yml` (Windows VS2022, CMake preset `vs2022`,
  CTest + full `pytest tests -k suite` + offline `.ps1` test +
  headless suite check + licence check; log files with short tail shown
  only on failure; no live hardware), issue templates,
  `CONTRIBUTING.md`, `FUNDING.yml` (commented future PayPal line only).
- Docs copyright consolidation: `Copyright (c) 2026 rickkyy`, MIT.
- Release artefact: `dist/psvr-suite-0.3.0-alpha-source.zip`
  (source-only; runtime DLL/backend redistribution excluded while
  GPL/LGPL backend obligations are unresolved).

### Prior verified work (referenced)

- M1/M2 USB/HID + WinUSB control on real PSVR1.
- M3 3DoF IMU + monitor GUI; M4 OpenVR HMD-only driver; M5 display
  inventory/component; M6 optics math/loader/validator + patterns.
- M9 PS3 Eye metric tracking + UDP/IMU/OpenVR bridge.
- 2026-10-13 Kalman optimization (offline/synthetic gains, not accepted
  live); 2026-10-14 suite GUI + fallback/recovery smoke PASS,
  peripheral applied but not visually accepted, movement feedback
  (One Euro kept), MSFS 2-session review, `-202` resize diagnosis,
  radial-optics trial, projection FOV trial, suite launcher + Start
  Menu shortcut.
