# Source-specific Suite camera settings

`tools.psvr_suite.camera_settings` is a pure configuration/UI module boundary. Each module
owns `controls`, edit capability, `view(root, requested, status)`,
`validate_controls(...)`, `validate_saved_configuration(...)`, and
`render(view, language)`. `effective_config` means
resolved acquisition configuration, **not observed device state**. Registry IDs
remain unchanged; unknown IDs never resolve to a PS3/PS4 fallback.

- **PS3 Eye:** existing Tk variables remain its unsaved edit buffer. The module
  owns exposure/gain sensor ranges, validation and the authoritative unchanged
  legacy baseline (`default_configuration()`). UI handlers get loaded values;
  no duplicated camera targets are initialized in Tk. Existing explicit
  apply/save actions and flat legacy preference fields remain in place.
- **PS4 stereo:** a separate projection uses the acquisition profile
  loader, including calibration/rig provenance validation. Format, paths, eye
  order, brightness, exposure and gain targets cannot overwrite PS3 variables.
  Targets are refreshed on selection; status polls update observations without
  rehashing artifacts. Reselect PS4 to reload an externally changed profile.
  Exposure/gain/brightness editors have independent literal drafts. They enable
  only when the current backend explicitly acknowledges the carrier/apply mode
  and publishes measured native range/step/Manual capabilities. Saved targets
  are validated against source profile policy; that policy is NOT a claim that
  every PS4 has the same device range. Applying edited values additionally
  requires the current actual range. Blank control entries inherit the source
  profile. No control is written on selection, typing or Save.
- **Generic cameras:** no borrowed PS3 sliders/calibration. Dedicated format,
  calibration and rig configuration is required, otherwise selected-source
  validation fails closed. A configuration editor enables only after the
  backend advertises `camera_profile_contract = 1`. Native control capabilities
  remain unknown; no numeric exposure/gain targets are invented.

## Backend integration contract (separate owner)

The optional `camera_profiles` key is accepted by settings and AppModel. It is
absent from legacy defaults; no migration or write occurs on load. Example:

```python
{
    "ps4-stereo": {"controls": {"exposure": -7, "gain": 8, "brightness": 1}},
    "opencv-dshow": {"width": 800, "height": 600, "fps": 30,
                     "intrinsicsPath": "my-calibration.json", "rigPath": "my-rig.json",
                     "imu_rig": "my-imu-rig.json"},
}
```

PS3 remains in the existing flat fields; duplicate `pseye` overrides are rejected.
The generic example requires real, matching user artifacts; none are generated
or implicitly validated by the UI. Backend must validate them before device open.

- `validate_camera_profiles(mapping)` validates/detaches optional saved targets.
- `resolve_camera_preferences(root, requested) -> (effective, source_profile)`
  resolves the selected source, without persistence/device calls. Call after
  canonical validation, before open. Do not pass native PS4 effective exposure
  back through the PS3 flat preference validator. PS4's resolved profile includes
  `controls`, plus `control_overrides`/`profile_control_targets` when overridden;
  artifact/profile hashes still identify the original accepted files. Generic
  effective exposure/gain/brightness are `None`, never PS3 targets.
- Backend should advertise `TrackingWorker.camera_profile_contract = 1` only
  after consuming this resolver and supporting explicit source-owned apply.
- Current running status must publish matching `camera_source`, `prefs.camera_source`,
  and the capability dictionary below. Partial/missing/mismatched capabilities
  leave affected editors disabled. A dirty draft with lost capability blocks
  Apply/Save instead of silently ignoring edits.

```python
camera_settings_capabilities = {
    "source": "ps4-stereo", "preference_key": "camera_profiles",
    "apply_mode": "reopen",  # or "live" ONLY if the owner safely implements it
    "controls": {
        "exposure": {"minimum": measured_min, "maximum": measured_max,
                     "step": measured_step, "capabilities": measured_flags},
        # gain/brightness: their own measured ranges, not borrowed from exposure
    },
}
```

The existing explicit Apply/Attach/Start payload carries the selected valid draft;
the backend owner must apply it only on that explicit action (safe owner update
or reopen), revalidate against the device actually opened, and publish actual
`camera_controls` readback/confirmation. Old backends without acknowledgment keep
new native editors disabled. This UI patch alone does not certify that lifecycle.

## Compatibility and truthfulness

There is no on-disk migration. Legacy canonical preferences remain a PS3 shadow
configuration even when PS4 is selected. The literal PS3 Tk draft is separate
from its last validated loaded/saved snapshot. A PS3 -> PS4 -> PS3 roundtrip never
rewrites or normalizes that draft, including empty/invalid text. While PS3 is
selected its visible draft is parsed and validated. For other sources hidden PS3
fields are not read or validated: the unchanged flat backend schema receives the
validated snapshot instead. PS4's owner still overrides it with its own profile.
An apply/collection does not persist any camera's edits; no guessed defaults are used if
the snapshot is missing. PS4's existing separate profile is only read, never
implicitly saved. Invalid/missing profiles hide legacy controls and show an
explicit error, without fallback targets.

### Explicit Save policy

- With **PS3 selected**, Save validates the visible PS3 draft and commits it only
  after successful persistence. Invalid drafts block Save and remain untouched.
- With **PS4/generic selected**, Save never persists the hidden PS3 draft, even
  when valid. It preserves the destination preferences file's valid PS3 values
  (re-read at this explicit save), or the validated loaded snapshot if no user
  file exists. Other common preferences/source selection may be saved normally.
  The success message explicitly says the hidden PS3 draft was not saved.
- The optional carrier persists only the selected source's validated draft;
  hidden source entries are preserved from the destination, not from their
  unsaved widgets. Draft buffers (including invalid text) survive switching and
  applying. Save is explicit and never applies hardware.
  This merge also replaces the candidate carrier when PS3 is selected. An
  existing destination's removed/empty carrier is authoritative: old loaded
  source profiles must not be resurrected. A destination reread is not a
  cross-process transaction; writes occurring after that reread still require
  external writer coordination.
- An unreadable/invalid existing destination or missing validated snapshot
  blocks Save with an explicit error; no fallback overwrite is attempted.
  To persist PS3 edits, select PS3 and explicitly save valid values.

Readback can come only from `camera_controls` in the current **running** tracking
status with an explicit matching current `camera_source` and matching prefs
(requested prefs alone do not prove the acquiring camera). Profile targets and historical pose/session
logs are never substituted. The current worker may not publish that status
field; the honest result is “unavailable / Manual not confirmed”. Backend
confirmation describes its acquisition target, not a subsequently edited profile.

This owner changed no algorithms, tracking/capture files, rig/calibration files,
driver settings or live processes. Backend integration is separately owned.
Loading this fix requires a GUI restart at a user-approved
time; do not restart the active session automatically.

## Offline verification (not executed by the implementation agent)

```powershell
python -m pytest -q tests/test_suite_camera_settings_ui.py tests/test_suite_camera_source.py tests/test_suite_app_model.py tests/test_suite_settings.py tests/test_suite_settings_consumers.py tests/test_suite_i18n.py tests/test_suite_b12_08_actions.py tests/test_suite_ps4_panel.py
```

New tests use FakeTk and temporary profile copies: actual combobox event wiring,
unsaved roundtrips, bidirectional module isolation, distinct profiles, legacy
projection, fail-closed errors, known-range validation, status provenance, and
IT/EN switching. They do not certify hardware behavior or total system isolation.
