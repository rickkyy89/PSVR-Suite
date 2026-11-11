# Projection trial — independent horizontal/vertical FOV (diagnostic)

Diagnostic only. **Not physical/optical calibration.**

## Why

Assetto/MSFS objects look narrow-tall (wheel vertical ellipse): lateral FOV
is asked independently from vertical. The trial keeps the symmetric tangent
math and signs, only allowing H and V to differ.

## Baseline and suggested diagnostic

- Original baseline: projections `±1` per eye = `H90 V90`
  (`tan(90/2) = tan(45°) = 1`).
- Suggested diagnostic: `H83.2671 V90`.
  - Horizontal tangents `±8/9 ≈ ±0.8889` (`2·atan(8/9) ≈ 83.2671°`).
  - Vertical tangents stay `±1`.
  - `8/9 = 960/1080`, one eye panel slice, not a lens measurement.

### Applied follow-up, 2026-10-14 21:33

User requests a slightly larger/closer-looking scene while preserving the
current horizontal/vertical proportions, approximately ten degrees reduction
counted across both eyes. Interpreted as horizontal reduction of 5 degrees
per eye; binocular fields overlap and are not physically additive.

- Before: H83.2670786731 / V90 degrees.
- After: **H78.2670786731 / V84.9392708260561 degrees**, both eyes.
- Preserve the projection aspect, `tan(H/2) / tan(V/2) = 8/9`, rather
  than preserving the ratio of angle values (which would change proportions).
- Tangents after: horizontal ±0.81365039620535884, vertical
  ±0.9153566957310294; uniform image magnification about 1.09247 (9.25%).
- Identity preserved; SteamVR was stopped. Settings readback verified;
  no game/runtime visual validation claimed. These change apparent angular
  size, not the physical geometry or measured depth of the simulated world.
- Backup before follow-up: `logs/projection_before_20261014_213305.json`.
  Restore this to return to H83.2671/V90; the preceding initial trial backup
  `logs/projection_before_20261014_204956.json` returns to H90/V90.

Signs (unchanged): `Left/Top = -tan(FOV/2)`, `Right/Bottom = +tan(FOV/2)`,
same for both eyes. Legacy `-FovDegrees` sets H and V together.

## Use (SteamVR closed, no restart by the script)

```powershell
# Legacy uniform (preserved)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\set_projection_trial.ps1 `
  -FovDegrees 90

# Independent pair (both required, 70..110, no mixing with -FovDegrees)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\set_projection_trial.ps1 `
  -HorizontalFovDegrees 83.2671 -VerticalFovDegrees 90

# Selective restore of the 8 projection keys only
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\set_projection_trial.ps1 `
  -RestoreFrom logs\projection_before_YYYYMMDD_HHMMSS.json
```

Rules: `distortionMode=identity` required for Set/SetHV; restore touches
only the 8 `projection*` keys and preserves everything else; every run
copies the full settings file to `logs/projection_before_*.json` first
(`-BackupParent`/`-SteamVrSettingsPath` only change the paths for offline
tests/debug, live defaults unchanged). The script refuses to run while
`vrserver`/`vrcompositor` live. No GUI/schema/`appsettings` change.

## Tests (offline, no live settings change)

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_projection_trial.ps1
```

Temp `steamvr.vrsettings` fixtures under `%TEMP%\opencode`; asserts AST
parameter sets, `70..110` bounds, `tan()` math/signs, selective restore,
and identity/missing-key guards.
