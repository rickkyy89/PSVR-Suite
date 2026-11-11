# Application readiness inventory — M7 / M8 / M10 (2026-10-05)

Status: **read-only inventory only. Not a compatibility result and not a
milestone claim.** The M5 display/compositor gate (recurring fullscreen `-202`)
and the M6 optical-acceptance gate are open, so this document and the tool it
describes never report an end-to-end "ready".

This work is autonomous and additive. It does not touch SteamVR, the driver,
the OpenXR runtime or the Steam configuration. It does not launch a game,
install or download anything, write a file or change a registry value.

## 1. What this is for

Before M7 (VR video / YouTube), M8 (Assetto Corsa) and M10 (MSFS) can be
attempted, the prerequisites have to be *known* rather than assumed:

* **M7** — a VR video player plus `yt-dlp`/`ffmpeg` for a YouTube flow.
* **M8** — Assetto Corsa installed and its known package present.
* **M10** — MSFS (2020 or 2024) installed and **SteamVR selected as the active
  OpenXR runtime**.

`tools/psvr_app_readiness/` answers exactly that, offline and read-only, from a
small amount of well-defined data.

## 2. Read-only guarantees

| Concern | Behaviour |
|---|---|
| Process launch | Never. No game, player, `yt-dlp`, `ffmpeg` or installer is executed. |
| Filesystem | Only reads `libraryfolders.vdf`, `steamapps/*.acf` and a fixed list of known relative paths. Game directories are never traversed. |
| Registry | `winreg.OpenKey(..., KEY_READ)` only. No `CreateKey`, no `SetValue`. |
| Network | None. `yt-dlp`/`ffmpeg` presence is checked, never run or downloaded. |
| Runtime | SteamVR / OpenXR are not initialised; the OpenXR runtime is only read from the registry. |

The unit suite enforces the first and third points by scanning the package
source for launch/write calls and by snapshotting a synthetic Steam tree before
and after a report.

## 3. Data sources

* **Steam root** — explicit `--steam-root`, else the registry `SteamPath`
  (`HKCU\Software\Valve\Steam`) / `InstallPath`, else the standard
  `C:\Program Files (x86)\Steam`.
* **Steam libraries** — `steamapps/libraryfolders.vdf`, parsed with a strict
  KeyValues reader that decodes escaped Windows paths
  (`"D:\\SteamLibrary"` → `D:\SteamLibrary`), quoted paths with spaces, `//`
  comments and a UTF-8 BOM. Unreadable or malformed manifests are reported as
  errors, never fatal.
* **Installed games** — only `steamapps/*.acf` (small files), matched by
  `appid`; a fixed list of relative executables/directories is checked with
  `is_file`/`is_dir`. No disk walk.
* **Microsoft Store MSFS** — read-only `%LOCALAPPDATA%\Packages\...` existence
  check.
* **OpenXR** — read-only `ActiveRuntime` from
  `HKLM\SOFTWARE\Khronos\OpenXR\1` (64-bit and WOW6432Node). The value is
  reported; it is never changed.

## 4. Usage

```powershell
$env:PYTHONPATH = "tools"
python -B -m psvr_app_readiness inventory
python -B -m psvr_app_readiness inventory --steam-root "D:\Steam" --library "E:\SteamLibrary"
python -B -m psvr_app_readiness inventory --bin-dir "C:\tools" --require ffmpeg --strict
```

Flags:

* `--steam-root PATH` — explicit Steam root.
* `--library PATH` — extra library root (repeatable).
* `--library-folders VDF` — explicit `libraryfolders.vdf` path.
* `--bin-dir PATH` — directory searched for tools before `PATH` (repeatable).
* `--require TOOL` — tool id that must be present (repeatable). An id outside
  the catalog is reported as `unknown`, never assumed present.
* `--local-appdata PATH` — override `%LOCALAPPDATA%` for Store probing.
* `--generated-utc TS` — deterministic timestamp.
* `--strict` — exit `3` if a required tool is missing or unknown.

Exit codes: `0` report produced, `1` internal error, `2` usage error, `3`
`--strict` failure.

## 5. Report shape (abridged)

```json
{
  "schema": "psvr.app_readiness",
  "read_only": true,
  "prerequisites": [
    {"id": "M5", "status": "open", "detail": "fullscreen -202 / Direct Mode not established"},
    {"id": "M6", "status": "open", "detail": "identity profile is diagnostic only"}
  ],
  "steam": {"root": "...", "root_source": "registry:...", "libraries": []},
  "openxr": {"active_runtime": "...", "steamvr": true, "read_only": true},
  "components": {
    "m7_video": {"inventory_status": "partial", "readiness": "not_ready", "blocked_by": ["M5", "M6"]},
    "m8_assetto_corsa": {"inventory_status": "present", "readiness": "not_ready", "blocked_by": ["M5", "M6"]},
    "m10_msfs": {"inventory_status": "partial", "readiness": "not_ready", "blocked_by": ["M5", "M6"]}
  },
  "readiness": {"end_to_end_ready": false, "blocked_by": ["M5", "M6"]}
}
```

`readiness` is always `not_ready` and `end_to_end_ready` is always `false`
while M5/M6 are open. `inventory_status` is `present` / `partial` / `missing`
and describes **presence only**.

## 6. Actual machine run — 2026-10-05 (read-only)

Command: `python -B -m psvr_app_readiness inventory --generated-utc 2026-10-05T00:00:00Z`

Findings (user profile abbreviated to `%USERPROFILE%`):

| Item | Result |
|---|---|
| Steam root | `C:\Program Files (x86)\Steam`, from `HKCU\Software\Valve\Steam\SteamPath` |
| Libraries | 1 (the default); `libraryfolders.vdf` parsed, 0 errors |
| OpenXR active runtime | `...\SteamVR\steamxr_win64.json` → **SteamVR** (M10 prerequisite met) |
| M7 tools | `yt-dlp` present (`%USERPROFILE%\...\Python311\Scripts\yt-dlp.exe`), `ffmpeg` present (WinGet `Gyan.FFmpeg`); no VR video player installed |
| M8 Assetto Corsa | appid `244210`, `assettocorsa`, `acs.exe` + `AssettoCorsa.exe` + `content` + `system` all present |
| M10 MSFS 2020 | appid `1250410` not installed; Store package absent |
| M10 MSFS 2024 | appid `2537590` installed (`MSFS2024`, `FlightSimulator2024.exe` present); Store package `Microsoft.Limitless_8wekyb3d8bbwe` present |
| End-to-end | `end_to_end_ready: false`, `blocked_by: ["M5", "M6"]` |

Missing / not verified: any VR video player for M7; the M5 display and M6
optical gates; the actual game runs (this tool never launches them).

## 7. Tests

`tests/test_app_readiness.py` (24 tests, stdlib `unittest`) covers:

* VDF parsing: nested blocks, escaped Windows paths, quoted paths with spaces,
  BOM, comments, escaped quotes, malformed/missing/unreadable inputs.
* A synthetic Steam tree with **multiple libraries** and escaped paths.
* Parser failures: malformed manifest, a directory named `*.acf`, and a
  simulated `PermissionError` on a manifest read.
* **Unknown tools**: a required id outside the catalog is reported `unknown`
  and never promoted to ready.
* Known tools found via an explicit `--bin-dir`; missing required tools are
  strict failures.
* Registry probes return a read-only-shaped result and never raise.
* CLI: `--help`, usage errors, JSON report, `--strict` exit `3`.
* Read-only proof: no launch/write calls in the package; the synthetic tree is
  byte-for-byte unchanged after a report.

Run directly:

```powershell
python -B -m unittest discover -s tests -p test_app_readiness.py -v
```

## 8. CMake test registration

Add inside the existing `if(BUILD_TESTING)` / `if(Python3_FOUND)` block in
`CMakeLists.txt`:

```cmake
if(Python3_FOUND)
    add_test(NAME app_readiness_python COMMAND ${Python3_EXECUTABLE} -B -m unittest discover
        -s "${CMAKE_SOURCE_DIR}/tests" -p test_app_readiness.py -v)
    set_tests_properties(app_readiness_python PROPERTIES WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})
endif()
```

## 9. Limitations

* Presence is not compatibility. A present player/game/tool is not a verified
  M7/M8/M10 result; the M5 `-202` display gate and the M6 optical-acceptance
  gate remain open.
* The catalog is deliberately conservative; adding a tool or game is a catalog
  edit, not a compatibility claim.
* `%LOCALAPPDATA%` package probing only checks the package folder, not the
  installed build.
* This tool does not read MSFS/AC configuration, does not select the OpenXR
  runtime and does not test SteamVR composition. Those remain operator steps.
