# Contributing to projectPSVR Suite

Copyright (c) 2026 rickkyy. MIT.

Pre-release `0.3.0-alpha`: no stability promises.

## Scope

- Docs, offline tests, and diagnostic accuracy are welcome.
- Do **not** change accepted user choices silently: distortion
  `identity`, One Euro `2.5/8` default, 3DoF fallback default `false`,
  diagnostic FOV `H 78.267 / V 84.939` (personal, not calibrated).
- Do not bundle Zadig, drivers, or credentials; WinUSB stays a manual
  user step (`docs/WINUSB_CONTROL.md`).
- Do not redistribute runtime binaries while backend GPL/LGPL
  obligations are unresolved (`third_party/README.md`).

## How to report

- Use the issue templates (bug vs tracking feedback). Paste commands +
  exit codes + bounded log excerpts (max ~60 lines), never full dumps.
- State hardware (PSVR1 model, Windows build, camera or not), settings
  changed, and whether fallback/optics were touched.
- No live-hardware changes from a report alone; maintainers reproduce
  locally first.

## Code style

- Follow the existing style; small, simple changes with clear
  conventional messages.
- C++: MSVC `/W4`, static CRT; Python: stdlib-first, no new
  dependencies without discussion.
- Tests: offline only (`ctest`, `pytest tests -k suite`, offline `.ps1`
  fixtures under `%TEMP%`). Never require headset/SteamVR in tests.
  On failure write a log file and read back only its tail
  (bounded); never pipe test output into a truncating filter —
  it hides the real exit code.
