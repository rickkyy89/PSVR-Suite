# Real OpenVR pose probe

Windows x64. Reads poses via the real OpenVR client API without opening USB.
Requires an already running vrserver in the same Windows session and an explicit
absolute path to the runtime's openvr_api.dll. Does not bundle that client DLL.

```powershell
.\psvr_runtime_probe.exe --help
.\psvr_runtime_probe.exe --openvr-dll "C:\Program Files (x86)\Steam\steamapps\common\SteamVR\bin\win64\openvr_api.dll" --duration-ms 1000
```

Utility initialization, raw-and-uncalibrated tracking universe, zero prediction.
Every accepted sample checks HMD class and exact serial PSVRPC-HMD-0. JSON lines
include connected/valid/result, matrix, position, WXYZ quaternion and YXZ Euler;
summary exit 0 means valid target poses collected, not compositor/display proof.
Exit 2 CLI error, 3 loader/API/init failure, 4 no valid target pose.

Optional `--request recenter` or `--request recalibrate` dispatches exactly once
through IVRDebug to that verified HMD, then continues sampling. A queued response
is not proof of application; inspect pose changes and driver logs. Recalibration
requires a stationary headset on a table. No quit/other requests are accepted.
Default invocation is read-only. No auto-start, registration or settings changes.

Diagnostic `--request simulate-worn` queues a **synthetic presence** lease in the
driver: the documented `/proximity` boolean becomes true for 180 seconds, with
`Prop_ContainsProximitySensor_Bool` temporarily enabled. `simulate-unworn` ends
the lease early. RunFrame applies the request; verify the driver log, not just
the queued response. Expiry/deactivation publishes false and restores the
previous proximity property. No persistent settings or IMU poses are changed.
This exercises SteamVR's worn/standby behavior; it cannot operate a physical
PSVR proximity sensor or certify that the panel is illuminated. These requests,
like recenter, cannot combine with `--compositor-status`; use a subsequent probe.

Optional `--compositor-status` observes fullscreen, CanRenderScene and scene-focus
PID through an explicitly selected Background client (Utility cannot access
IVRCompositor). `--bring-to-front` implies status and dispatches one documented
window-front request after exact-HMD verification. It does not take Scene focus
or guarantee fullscreen. No silent application-type fallback, settings change
or combination with `--request`. Observations before/after/final are reported;
exit 0 still means valid poses, not that fullscreen was activated.

Optional `--disable-standby-pause` is an explicit diagnostic setting mutation:
only `power.pauseCompositorOnStandby=false`, after exact-HMD verification,
with before/after readback and status observations. It cannot combine with
`--request` or `--bring-to-front`. This does not claim a fullscreen fix or
fake headset-worn data. To restore default behavior remove only this user
override (or restore the previously recorded value), preserving other settings.

CLI tests use nonexistent DLLs and never initialize the real runtime. Real pose
verification was performed separately during the hardware session documented
in docs/RUNTIME_CHECKS_20261004.md in the source repository.

## Summarize unattended captures

From the repository root:

```powershell
python -B tools/psvr_runtime_probe/summarize_capture.py logs/presentation_20261006_100222
```

Stdlib-only, read-only: summarizes `capture_presentation_trial.ps1` output as
JSON. Checks requested duration, completion/sample counts, pose validity,
fullscreen/render availability and the captured Sony SNY6A04 display states.
Exit 0: unattended sampled checks passed; 1: failed/incomplete checks; 2: invalid
input. Never asserts physical lens acceptance or diagnoses the cause of a
tracking loss. A completion file alone does not prove the requested duration
was reached. Historical compositor/driver logs should be inspected separately.

## Record the stereo output and compare refresh trials

`scripts/record_headset_view.ps1` records only the exact live Headset Window,
after checking it against the driver-selected Sony display. Default backend
Windows Graphics Capture requires ffmpeg with `gfxcapture` (8.1.2 on this PC).
GDI is available as an explicit diagnostic fallback. Records H.264 1280×720 at
approximately 30fps, no audio; renews a temporary synthetic presence lease and
releases it in finally. Saves metadata and full runtime logs alongside the video.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/record_headset_view.ps1 -OutputFile logs/new_trial.mp4 -DurationSeconds 180
.venv-camera\Scripts\python.exe -B tools/psvr_runtime_probe/analyze_recording.py logs/new_trial.mp4 --out logs/new_trial.analysis.json
```

Brightness analysis requires NumPy. Detects candidate broad luma rises, not
their cause, and can miss short/localized/subtle effects. Never certifies the
physical OLED output or frame skipping. `--out` requires a new file.

`scripts/set_trial_refresh.ps1 -Hz 120 -TestOnly` validates the mode without
applying it. Apply requires SteamVR closed, exact driver-selected Sony,
1920×1080, unchanged bounds, Windows mode acceptance and verified path/signal
refresh. `-PersistForTrial` saves the existing mode through Windows; restore
90Hz with the same switch after the trial, and reverify active/stored mode.
Creates no EDID overrides or custom timings. Settings changes are backed up
and merged; a failed apply/verification attempts display rollback.

Recording results and limitations: `docs/VR_VIEW_90_120_20261007.md`.

## Continuous flash telemetry (MSVC)

`psvr_runtime_telemetry.exe` loads the explicitly selected runtime DLL once as
a Background client and records raw/render pose observations, fade/grid alpha,
dashboard/fullscreen/focus, its event queue, and deduplicated frame timing
history. No USB access, no Scene init, no auto-start. Default read-only;
`--synthetic-presence` explicitly renews/releases a lease through IVRDebug.
CLI output/ready/stop paths must be new absolute paths in existing directories.

```powershell
.\out\runtime_probe\psvr_runtime_telemetry.exe --openvr-dll "C:\Program Files (x86)\Steam\steamapps\common\SteamVR\bin\win64\openvr_api.dll" --output "C:\Users\rickk\Desktop\PSVR\logs\new_telemetry.jsonl" --duration-ms 180000 --sample-ms 10
```

The video recorder now uses a single continuous client by default. Add
`-Dashboard shown` to require/observe the dashboard; `-Dashboard hidden` requires
it already disabled at startup and refuses to claim success if visible.
For the controlled hidden trial, use `set_trial_refresh.ps1 -DisableDashboard`
with SteamVR closed, then restore the single policy from its saved settings
backup using `-RestoreDashboardFrom <backup>` after closing SteamVR.
`-NoTelemetry` explicitly selects the earlier periodic diagnostic mode.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/record_headset_view.ps1 -OutputFile logs/new_flash_trial.mp4 -DurationSeconds 180 -Dashboard shown
.venv-camera\Scripts\python.exe -B tools/psvr_runtime_probe/correlate_flashes.py logs/new_flash_trial.mp4 --out logs/new_flash_trial.correlation.json
```

Collector sampling is nominal, with actual poll gaps recorded. Frame history
contains app render poses and received timestamps, not proof of driver pose at
that instant. Video times have capture startup uncertainty. Correlation is not
claimed to be exact GPU-frame synchronization. Root-cause evidence, GetPose
fix and before/after results: `docs/FLASH_DIAGNOSIS_FIX_20261007.md`.
