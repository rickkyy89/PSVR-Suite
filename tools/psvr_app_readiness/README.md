# psvr_app_readiness — read-only M7/M8/M10 inventory

Answers one narrow, offline question: which of the tools and installed games
that **M7** (VR video / YouTube), **M8** (Assetto Corsa) and **M10** (MSFS)
depend on are present on this machine. It is **not** a compatibility claim.

The M5 display gate (fullscreen `-202`) and the M6 optical-acceptance gate are
open, so the report always returns `end_to_end_ready: false`.

## Read-only

No process is launched, no file is written, no registry value is created or
changed, nothing is installed or downloaded. Steam libraries are found through
`libraryfolders.vdf`; only the small `steamapps/*.acf` manifests and a fixed
list of known relative paths are inspected, so game directories are never
traversed. The active OpenXR runtime is only read from the registry.

## Usage

```powershell
$env:PYTHONPATH = "tools"
python -B -m psvr_app_readiness inventory
python -B -m psvr_app_readiness inventory --steam-root "D:\Steam" --library "E:\SteamLibrary"
python -B -m psvr_app_readiness inventory --bin-dir "C:\tools" --require ffmpeg --strict
```

Flags: `--steam-root`, `--library` (repeatable), `--library-folders`,
`--bin-dir` (repeatable), `--require TOOL` (repeatable; unknown ids are
reported, never assumed), `--local-appdata`, `--generated-utc`, `--strict`.

Exit codes: `0` report, `1` internal error, `2` usage, `3` `--strict` failure.

## Layout

* `vdf.py` — strict KeyValues reader for `libraryfolders.vdf` / `*.acf`.
* `registry.py` — read-only Steam install path and OpenXR `ActiveRuntime`.
* `steam.py` — library discovery and appmanifest inventory.
* `catalog.py` — conservative known-tool / known-game catalog.
* `inventory.py` — builds the report.
* `cli.py` / `__main__.py` — `python -m psvr_app_readiness`.

See `docs/APPLICATION_READINESS_20261005.md` for the full design, the actual
2026-10-05 machine findings and the CMake test registration.
