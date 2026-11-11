# SPDX-License-Identifier: MIT
"""Offline 90 Hz trial assessment: prepared manifest + saved psvr_display inventory.

This module judges ONLY whether a previously saved read-only inventory artifact (the JSON
printed by `psvr_display --json` and stored by the operator) shows the exact Sony SNY6A04
monitor path on a desktop active path that already carries a measured 90/1 Hz rational.
It is pure offline analysis: no subprocess, no live enumeration, no registry/GPU/runtime
or hardware access, nothing is created or applied, and the rounded `hz` helper field is
never trusted.

Honesty rules carried by the trial manifest (status "prepared_unverified_90"):
- 90 Hz has never been measured on this unit. The manifest must not claim otherwise: every
  current_physical_validation leaf is null or false, and the historical SNYB403 timing
  provenance covers timing values only, never identity, so the 120 Hz candidate's
  measurements are never copied as 90 Hz evidence.
- refresh timing and EDID are separate controls: this trial plans manual custom timings
  only, requests no EDID override or 90 Hz EDID generator, and invents no VIC (CEA-861 has
  no 1080p90 VIC).
- runtime expectation is exactly two SteamVR override keys
  (driver_psvrpc.displayRefreshNumerator=90, displayRefreshDenominator=1); power and
  config mode never change automatically, and restore uses the two actually-saved pre-trial
  values recorded in a fresh backup at trial start, not assumed 120 or 60.
- an absent Sony row blocks the DESKTOP branch only; it does not prove that a Direct Mode
  90 Hz route would fail (that needs other backend evidence and measured timing), and no
  outcome here ever claims Direct Mode, panel cadence or SteamVR working state.
- a positive gate assesses the GPU-reported 90/1 rationals inside the SAVED artifact; it
  never rewrites this prepared manifest into a "measured" candidate, and the operator
  ready-step is backup + fresh re-check + the two refresh keys, not re-applying timings.

The inventory identity contract (manufacturer 0xD94D / product 0x6A04 for SNY6A04 as
serialized by tools/psvr_display/display_inventory.cpp) is reused from readiness.py, the
project's accepted-identity contract, instead of re-deriving a representation here.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

try:
    from . import readiness
except ImportError:  # loaded file-relative, as tests/ does for tools
    import readiness

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = 'configurations/candidates/hdmi_native_90hz_trial.json'

MANIFEST_SCHEMA_VERSION = 1
MANIFEST_KIND = 'refresh90_trial_manifest'
MANIFEST_STATUS = 'prepared_unverified_90'
TARGET_NUMERATOR = 90
TARGET_DENOMINATOR = 1
PANEL_WIDTH = 1920
PANEL_HEIGHT = 1080

# Pinned to the documented historical 90 Hz column of
# docs/PSVR_HIGH_REFRESH_TEST.md: an internally consistent NEW timing under the
# historical provenance label would be an invented mode, so the checker accepts only
# these exact values (the arithmetic guards below stay as a second line of defense).
DOCUMENTED_90HZ_TIMING = {
    'h_front_porch': 88, 'h_sync_width': 44, 'h_back_porch': 148, 'h_total': 2200,
    'v_front_porch': 4, 'v_sync_width': 5, 'v_back_porch': 36, 'v_total': 1125,
    'expected_pixel_clock_hz': 222750000, 'expected_horizontal_frequency_hz': 101250,
}
DOCUMENTED_90HZ_POLARITY = {'h_sync_polarity': 'positive', 'v_sync_polarity': 'positive',
                            'scan': 'progressive'}
DOCUMENTED_REFERENCE_URL = 'https://github.com/gusmanb/PSVRFramework/files/605425/psvr_120.zip'
DOCUMENTED_ARCHIVE_SHA256 = '54574aae8e507ad7f3c5fe05437d003ce4ee0b426a0fa15300c48209cb740a08'

OUTCOME_READY = 'desktop_90_ready_for_runtime_configuration'
OUTCOME_NOT_READY = 'not_ready_desktop_90_mismatch'
OUTCOME_BLOCKED_DISPLAY_NOT_OBSERVED = 'blocked_display_not_observed_desktop_branch_only'
OUTCOME_INVENTORY_UNKNOWN = 'blocked_inventory_unknown_fail_closed'
OUTCOME_MANIFEST_INVALID = 'blocked_manifest_invalid'

NO_SONY_NOTE = (
    'The exact Sony SNY6A04 monitor path is not on any active desktop path in this saved '
    'inventory. This blocks the DESKTOP branch of the 90 Hz trial only: a direct route may '
    'legitimately hide the HMD from the OS, so absence is not proof that Direct Mode at '
    '90 Hz would fail. Evaluating direct mode needs other backend evidence (private '
    'enumeration or Display Core modes plus measured timing); EDID or property values '
    'alone never prove either direction.')

SHA256_RE = re.compile(r'^[0-9a-f]{64}$')


class TrialInputError(RuntimeError):
    """Raised when an input file cannot be read or parsed at all; nothing is assessed."""


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def is_int(value):
    """True only for real ints: bool is an int subclass and must never sneak through."""
    return isinstance(value, int) and not isinstance(value, bool)


def resolve_input(path):
    """Absolute paths pass through; repo-relative defaults also work from any CWD."""
    candidate = Path(path)
    if candidate.is_absolute() or candidate.exists():
        return candidate
    return REPO_ROOT / candidate


def load_json_object(path, label):
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise TrialInputError(f'{label} cannot be read: {error}')
    try:
        data = json.loads(raw.decode('utf-8-sig'))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TrialInputError(f'{label} is not readable JSON: {error}')
    if not isinstance(data, dict):
        raise TrialInputError(f'{label} must be a JSON object (got {type(data).__name__})')
    return data, _sha(raw)


# --------------------------------------------------------------------------- manifest

def _missing(errors, section, field, container, expected=None):
    """Append a missing-field error; unknown is fail-closed, never a silent pass."""
    if field not in container or container[field] is None and expected is not None:
        errors.append(f'{section}.{field} is missing or null: cannot verify it, failing closed')
        return True
    return False


def _section(manifest, name, errors):
    value = manifest.get(name)
    if not isinstance(value, dict):
        errors.append(f'manifest section {name} must be an object '
                      f'(got {type(value).__name__ if value is not None else "missing"})')
        return None
    return value


def validate_manifest(manifest):
    """Return every validation error for the prepared trial manifest ([] means accepted).

    The manifest describes a trial, so any claim that 90 Hz was already measured, any
    hardcoded pre-trial assumption, any mixing of the refresh and EDID controls, or any
    arithmetic slip in the documented timings is an error: an unverified trial must never
    masquerade as a measured candidate.
    """
    errors = []
    if not isinstance(manifest, dict):
        return ['manifest must be a JSON object']
    if manifest.get('schema_version') != MANIFEST_SCHEMA_VERSION or not is_int(manifest.get('schema_version')):
        return [f'unknown manifest schema_version {manifest.get("schema_version")!r}: this '
                f'checker understands revision {MANIFEST_SCHEMA_VERSION} only (fail closed)']
    if manifest.get('kind') != MANIFEST_KIND:
        errors.append(f'manifest kind must be {MANIFEST_KIND!r}')
    if manifest.get('status') != MANIFEST_STATUS:
        errors.append(f'trial manifest status must be {MANIFEST_STATUS!r}: a measured or '
                      'certified label on an untested 90 Hz trial is a false claim')
    if not isinstance(manifest.get('name'), str) or not manifest.get('name').strip():
        errors.append('manifest name must be a non-empty string')

    identity = _section(manifest, 'trial_identity', errors)
    if identity is not None:
        path = identity.get('monitor_device_path')
        if not isinstance(path, str) or not path or '\0' in path:
            errors.append('trial_identity.monitor_device_path must be a non-empty exact string')
        ids = identity.get('edid_ids')
        if not isinstance(ids, dict):
            errors.append('trial_identity.edid_ids must be an object')
        else:
            vendor, product = ids.get('manufacturer'), ids.get('product')
            for label, value in (('manufacturer', vendor), ('product', product)):
                if not is_int(value) or not 1 <= value <= 0xFFFF:
                    errors.append(f'trial_identity.edid_ids.{label} must be an integer 1..65535')
            if is_int(vendor) and is_int(product) \
                    and not readiness.is_sony_edid_ids({'manufacturer': vendor, 'product': product}):
                errors.append('trial_identity.edid_ids conflicts with the serialized inventory '
                              'identity contract (readiness.is_sony_edid_ids): refusing to '
                              'redefine the Sony representation here')

    desired = _section(manifest, 'desired_mode', errors)
    if desired is not None:
        if desired.get('active_width') != PANEL_WIDTH or desired.get('active_height') != PANEL_HEIGHT \
                or not is_int(desired.get('active_width')) or not is_int(desired.get('active_height')):
            errors.append(f'desired_mode must be exactly {PANEL_WIDTH}x{PANEL_HEIGHT}')
        if desired.get('scan') != 'progressive':
            errors.append('desired_mode.scan must be "progressive"')
        refresh = desired.get('refresh')
        if not isinstance(refresh, dict) or refresh.get('numerator') != TARGET_NUMERATOR \
                or refresh.get('denominator') != TARGET_DENOMINATOR \
                or not is_int(refresh.get('numerator')) or not is_int(refresh.get('denominator')):
            errors.append(f'desired_mode.refresh must be exactly the rational {TARGET_NUMERATOR}/'
                          f'{TARGET_DENOMINATOR}')

    timing = _section(manifest, 'planned_timing', errors)
    if timing is not None:
        fields = tuple(DOCUMENTED_90HZ_TIMING)
        values = {}
        for field in fields:
            value = timing.get(field)
            if not is_int(value) or value <= 0:
                errors.append(f'planned_timing.{field} must be a positive integer '
                              f'(got {value!r})')
            else:
                values[field] = value
        # Pin first: the trial's provenance is the documented historical table, so any
        # deviation (even a self-consistent one) is an invented timing and is refused.
        for field, documented in DOCUMENTED_90HZ_TIMING.items():
            if field in values and values[field] != documented:
                errors.append(f'planned_timing.{field}={values[field]} deviates from the '
                              f'documented historical 90 Hz timing ({documented}): this '
                              'trial is pinned to docs/PSVR_HIGH_REFRESH_TEST.md')
        for field, documented in DOCUMENTED_90HZ_POLARITY.items():
            if timing.get(field) != documented:
                errors.append(f'planned_timing.{field} must be {documented!r} as documented '
                              f'(got {timing.get(field)!r})')
        # Arithmetic consistency guards, fail closed on every missing input.
        porch_h = [values.get(k, 0) for k in ('h_front_porch', 'h_sync_width', 'h_back_porch')]
        if 'h_total' in values and values['h_total'] != sum(porch_h) + PANEL_WIDTH:
            errors.append('planned_timing.h_total does not equal active + front + sync + back')
        porch_v = [values.get(k, 0) for k in ('v_front_porch', 'v_sync_width', 'v_back_porch')]
        if 'v_total' in values and values['v_total'] != sum(porch_v) + PANEL_HEIGHT:
            errors.append('planned_timing.v_total does not equal active + front + sync + back')
        if all(k in values for k in ('expected_pixel_clock_hz', 'h_total', 'v_total')):
            if values['expected_pixel_clock_hz'] * TARGET_DENOMINATOR \
                    != TARGET_NUMERATOR * values['h_total'] * values['v_total']:
                errors.append(f'planned_timing pixel clock is not exactly {TARGET_NUMERATOR}/'
                              f'{TARGET_DENOMINATOR} Hz derived from h_total*v_total')
        if all(k in values for k in ('expected_pixel_clock_hz', 'h_total',
                                     'expected_horizontal_frequency_hz')):
            if values['expected_horizontal_frequency_hz'] * values['h_total'] \
                    != values['expected_pixel_clock_hz']:
                errors.append('planned_timing horizontal frequency does not equal pixel_clock/h_total')

    provenance = _section(manifest, 'timing_provenance', errors)
    if provenance is not None:
        if provenance.get('reference_url') != DOCUMENTED_REFERENCE_URL:
            errors.append(f'timing_provenance.reference_url must be the documented historical '
                          f'archive {DOCUMENTED_REFERENCE_URL}: the trial is pinned to that source')
        sha = provenance.get('reference_archive_sha256')
        if not isinstance(sha, str) or not SHA256_RE.match(sha):
            errors.append('timing_provenance.reference_archive_sha256 must be a lowercase 64-hex digest')
        elif sha != DOCUMENTED_ARCHIVE_SHA256:
            errors.append('timing_provenance.reference_archive_sha256 does not match the documented '
                          'historical archive digest')
        if provenance.get('historical_profile_id') != 'SNYB403':
            errors.append('timing_provenance.historical_profile_id must be the documented '
                          'SNYB403 source profile')
        if provenance.get('scope') != 'timing_only_not_identity':
            errors.append('timing_provenance.scope must be "timing_only_not_identity"')
        if provenance.get('identity_adopted') is not False:
            errors.append('the historical SNYB403 identity must never be adopted over SNY6A04')
        if provenance.get('tested_on_this_unit') is not False:
            errors.append('provenance must not claim 90 Hz was tested on this unit')

    refresh_control = _section(manifest, 'refresh_control', errors)
    if refresh_control is not None and refresh_control.get('applied') is not False:
        errors.append('refresh_control.applied must be false: a manifest applies nothing')
    edid_control = _section(manifest, 'edid_control', errors)
    if edid_control is not None:
        if edid_control.get('edid_override_requested') is not False \
                or edid_control.get('edid_90_generator_requested') is not False:
            errors.append('EDID is a separate control and must not be requested by this trial')
        if edid_control.get('vic_1080p90') is not None:
            errors.append('no CEA-861 VIC defines 1920x1080p90 and none may be invented')

    validation = _section(manifest, 'current_physical_validation', errors)
    if validation is not None:
        for field, value in validation.items():
            if field == 'note':
                continue
            if value is not None and value is not False:
                errors.append(f'current_physical_validation.{field} must be null or false: the '
                              'trial is prepared, never measured (a copied 120 Hz measurement '
                              'would be a false 90 Hz claim)')

    runtime = _section(manifest, 'expected_runtime_changes', errors)
    if runtime is not None:
        if runtime.get('manual_operator_steps_only') is not True:
            errors.append('expected_runtime_changes.manual_operator_steps_only must be true')
        if runtime.get('steamvr_override_keys') != {'driver_psvrpc.displayRefreshNumerator': 90,
                                                    'driver_psvrpc.displayRefreshDenominator': 1}:
            errors.append('expected runtime changes are exactly the two displayRefresh rational '
                          'keys set to 90 and 1')
        never = runtime.get('never_change_automatically')
        if not isinstance(never, list) or not never or not all(isinstance(item, str) for item in never):
            errors.append('expected_runtime_changes.never_change_automatically must be a non-empty '
                          'list of strings')
        elif not any('pauseCompositorOnStandby' in item for item in never) \
                or not any('power' in item.lower() or 'config' in item.lower() for item in never):
            errors.append('never_change_automatically must cover the power/standby key and the '
                          'power/config mode')

    restore = _section(manifest, 'restore_pretrial', errors)
    if restore is not None:
        if restore.get('keys') != ['driver_psvrpc.displayRefreshNumerator',
                                   'driver_psvrpc.displayRefreshDenominator']:
            errors.append('restore_pretrial.keys must be exactly the two refresh-rational keys')
        if restore.get('assumed_value_used') is not False:
            errors.append('restore must target the actually saved pre-trial values, never an assumption')
        hardcoded = restore.get('do_not_hardcode')
        if not isinstance(hardcoded, list) or len(hardcoded) < 2 \
                or not all(isinstance(item, str) for item in hardcoded):
            errors.append('restore_pretrial.do_not_hardcode must list at least the 120 and 60 assumptions')

    limitations = manifest.get('limitations')
    if not isinstance(limitations, list) or not limitations \
            or not all(isinstance(item, str) and item.strip() for item in limitations):
        errors.append('manifest limitations must be a non-empty list of strings')
    return errors


# ------------------------------------------------------------------------- assessment

def exact_rational(rate_obj, numerator, denominator):
    """('pass'|'fail'|'unknown', detail): exact cross-multiplication, never rounded hz.

    A rational must carry positive-integer numerator/denominator and, when the serializer's
    `available` flag is present, it must say true. Equivalent rationals (180/2 == 90/1)
    pass through cross-multiplication; bools and floats are shape violations, unknown.
    """
    if not isinstance(rate_obj, dict):
        return 'unknown', f'refresh object missing or not an object (got {type(rate_obj).__name__})'
    num, den = rate_obj.get('numerator'), rate_obj.get('denominator')
    if not is_int(num) or not is_int(den) or num <= 0 or den <= 0:
        return 'unknown', f'invalid refresh rational {num!r}/{den!r} (positive ints required)'
    if 'available' in rate_obj and rate_obj['available'] is not True:
        return 'unknown', f'serializer marks {num}/{den} as not available ' \
                          f'(available={rate_obj["available"]!r})'
    if not (num * denominator == numerator * den):
        return 'fail', f'{num}/{den} != {numerator}/{denominator}'
    return 'pass', f'{num}/{den} is exactly {numerator}/{denominator}'


def _gate(gate_id, status, detail):
    return {'id': gate_id, 'status': status, 'detail': detail}


def assess(manifest, inventory):
    """Judge a saved inventory against the validated trial manifest. Pure; writes nothing.

    Returns (outcome, gates, extra_notes). Outcomes:
    - OUTCOME_READY: every measured-field gate passes (desktop branch evidence only),
    - OUTCOME_NOT_READY: the display row is present but a measured field contradicts 90 Hz,
    - OUTCOME_BLOCKED_DISPLAY_NOT_OBSERVED: exact path absent from the saved inventory,
    - OUTCOME_INVENTORY_UNKNOWN: schema or fields missing/malformed: fail closed.
    """
    gates = []
    notes = []

    if inventory.get('schema_version') != MANIFEST_SCHEMA_VERSION or not is_int(inventory.get('schema_version')):
        gates.append(_gate('inventory_schema', 'unknown',
                           f'schema_version {inventory.get("schema_version")!r} is not 1: unknown, not a pass'))
        return OUTCOME_INVENTORY_UNKNOWN, gates, ['The saved inventory contract is not recognizable; nothing is inferred.']
    if inventory.get('read_only') is not True:
        gates.append(_gate('inventory_schema', 'unknown',
                           f'read_only flag is {inventory.get("read_only")!r}, expected true'))
        return OUTCOME_INVENTORY_UNKNOWN, gates, []
    if inventory.get('scope') != 'active_paths':
        gates.append(_gate('inventory_schema', 'unknown',
                           f'scope is {inventory.get("scope")!r}, expected "active_paths"'))
        return OUTCOME_INVENTORY_UNKNOWN, gates, []
    displays = inventory.get('displays')
    if not isinstance(displays, list):
        gates.append(_gate('inventory_schema', 'unknown',
                           f'displays must be a list (got {type(displays).__name__})'))
        return OUTCOME_INVENTORY_UNKNOWN, gates, []
    bad_rows = [i for i, entry in enumerate(displays) if not isinstance(entry, dict)]
    if bad_rows:
        indexes = ', '.join(str(i) for i in bad_rows)
        gates.append(_gate('inventory_schema', 'unknown',
                           f'displays entries at index/indices {indexes} are not objects: '
                           'malformed rows are a broken artifact, not evidence of an absent Sony'))
        return OUTCOME_INVENTORY_UNKNOWN, gates, []
    if 'automatic_selection' not in inventory or inventory['automatic_selection'] is not False:
        gates.append(_gate('inventory_schema', 'unknown',
                           'automatic_selection must be present and exactly false: the read-only '
                           'contract is asserted explicitly, never defaulted'))
        return OUTCOME_INVENTORY_UNKNOWN, gates, []
    gates.append(_gate('inventory_schema', 'pass',
                       f'schema_version=1, read_only=true, scope=active_paths, '
                       f'automatic_selection=false, {len(displays)} row(s)'))

    expected_path = manifest['trial_identity']['monitor_device_path']
    # Ordinal exactness is the identity contract; never -like/substring matching.
    matches = [row for row in displays
               if isinstance(row, dict) and row.get('monitor_device_path') == expected_path]
    if not matches:
        gates.append(_gate('display_uniquely_selected', 'unknown',
                           'no active-path row carries the exact SNY6A04 monitor path in this saved inventory'))
        return OUTCOME_BLOCKED_DISPLAY_NOT_OBSERVED, gates, [NO_SONY_NOTE]
    if len(matches) > 1:
        gates.append(_gate('display_uniquely_selected', 'fail',
                           f'{len(matches)} rows share the exact monitor path: duplicate is ambiguous, nothing selected'))
        return OUTCOME_NOT_READY, gates, []
    row = matches[0]
    gates.append(_gate('display_uniquely_selected', 'pass',
                       'exactly one row carries the exact monitor path (ordinal, case-sensitive)'))

    dependent = []  # collected so a present row is judged on every measured field at once

    active, target_available = row.get('active'), row.get('target_available')
    if active is True and target_available is True:
        dependent.append(_gate('active_and_target_available', 'pass', 'active=true, target_available=true'))
    elif not isinstance(active, bool) or not isinstance(target_available, bool):
        dependent.append(_gate('active_and_target_available', 'unknown',
                               f'active={active!r}, target_available={target_available!r}: not booleans'))
    else:
        dependent.append(_gate('active_and_target_available', 'fail',
                               f'active={active}, target_available={target_available}'))

    if row.get('edid_ids_available') is not True or not isinstance(row.get('edid_ids'), dict) \
            or row['edid_ids'].get('valid') is not True \
            or not is_int(row['edid_ids'].get('manufacturer')) or not is_int(row['edid_ids'].get('product')):
        dependent.append(_gate('edid_identity', 'unknown',
                               f'edid ids missing/invalid on the row: availability is not a pass (edid_ids={row.get("edid_ids")!r})'))
    elif readiness.is_sony_edid_ids(row['edid_ids']):
        dependent.append(_gate('edid_identity', 'pass',
                               'edid_ids valid and equal the SNY6A04 serialized contract '
                               f'({row["edid_ids"]["manufacturer"]}/{row["edid_ids"]["product"]})'))
    else:
        dependent.append(_gate('edid_identity', 'fail',
                               'wrong EDID identity on the exact path; SNY6A04 is never re-inferred '
                               f'from a substring or size hint (edid_ids={row["edid_ids"]!r})'))

    def dims(geom):
        if not isinstance(geom, dict):
            return None, 'null'
        return geom, f'{geom.get("width")}x{geom.get("height")}'

    source, target = row.get('source'), row.get('target')
    src_ok = isinstance(source, dict) and source.get('width') == PANEL_WIDTH and source.get('height') == PANEL_HEIGHT \
        and is_int(source.get('width')) and is_int(source.get('height'))
    tgt_ok = isinstance(target, dict) and target.get('width') == PANEL_WIDTH and target.get('height') == PANEL_HEIGHT \
        and is_int(target.get('width')) and is_int(target.get('height'))
    if not (isinstance(source, dict) and isinstance(target, dict)):
        dependent.append(_gate('source_target_geometry', 'unknown',
                               f'source={dims(source)[1]}, target={dims(target)[1]}: geometry unavailable'))
    elif src_ok and tgt_ok:
        dependent.append(_gate('source_target_geometry', 'pass',
                               'source and target both exactly 1920x1080'))
    else:
        dependent.append(_gate('source_target_geometry', 'fail',
                               f'source {dims(source)[1]}, target {dims(target)[1]}: both must be exactly 1920x1080'))

    rotation, scaling = row.get('rotation'), row.get('scaling')
    if not is_int(rotation) or not is_int(scaling):
        dependent.append(_gate('identity_transforms', 'unknown',
                               f'rotation={rotation!r}, scaling={scaling!r}: not numeric'))
    elif rotation == 1 and scaling == 1:
        dependent.append(_gate('identity_transforms', 'pass', 'rotation=1 and scaling=1 (numeric identity)'))
    else:
        dependent.append(_gate('identity_transforms', 'fail',
                               f'rotation={rotation}, scaling={scaling}: identity transforms required'))

    path_scan, target_scan = row.get('scanline_ordering'), target.get('scanline_ordering') if isinstance(target, dict) else None
    if not is_int(path_scan) or not is_int(target_scan):
        dependent.append(_gate('progressive_scan', 'unknown',
                               f'scanline orderings path={path_scan!r} target={target_scan!r}'))
    elif path_scan == 1 and target_scan == 1:
        dependent.append(_gate('progressive_scan', 'pass', 'path and target scanline_ordering=1 (progressive)'))
    else:
        dependent.append(_gate('progressive_scan', 'fail',
                               f'path scanline={path_scan}, target scanline={target_scan}: interlaced or unknown'))

    def _source_identity(item):
        """(low, high, source_id) when the row carries a well-formed source identity, else None.

        low/high must be real ints (bool excluded, like every other numeric contract)
        and source_id a non-negative int; anything else is malformed, not "no clone".
        """
        adapter = item.get('source_adapter')
        if not isinstance(adapter, dict) or not is_int(adapter.get('low')) \
                or not is_int(adapter.get('high')):
            return None
        if not is_int(item.get('source_id')) or item['source_id'] < 0:
            return None
        return adapter['low'], adapter['high'], item['source_id']

    selected_identity = _source_identity(row)
    if selected_identity is None:
        dependent.append(_gate('non_clone_source', 'unknown',
                               'selected row has a missing or malformed source identity '
                               '(source_adapter low/high ints and non-negative int source_id '
                               'required): a clone cannot be ruled out'))
    else:
        clone = None
        malformed_active = 0
        for other in displays:
            if other is row or other.get('active') is not True:
                continue
            other_identity = _source_identity(other)
            if other_identity is None:
                malformed_active += 1
                continue
            if other_identity == selected_identity:
                clone = other
                break
        if clone is not None:
            dependent.append(_gate('non_clone_source', 'fail',
                                   'another active path shares this source: a cloned source '
                                   'feeds other displays'))
        elif malformed_active:
            dependent.append(_gate('non_clone_source', 'unknown',
                                   f'{malformed_active} active row(s) carry a malformed source '
                                   'identity and could hide a clone: fail closed'))
        else:
            dependent.append(_gate('non_clone_source', 'pass',
                                   'no other active row shares a well-formed source_adapter+source_id'))

    path_status, path_detail = exact_rational(row.get('refresh'), TARGET_NUMERATOR, TARGET_DENOMINATOR)
    signal_status, signal_detail = exact_rational(
        target.get('signal_refresh') if isinstance(target, dict) else None,
        TARGET_NUMERATOR, TARGET_DENOMINATOR)
    if 'unknown' in (path_status, signal_status):
        status, detail = 'unknown', f'path refresh {path_detail}; target signal {signal_detail}'
    elif path_status == 'fail' or signal_status == 'fail':
        status = 'fail'
        detail = (f'path refresh {path_detail}; target signal {signal_detail}: a measured 120/1 or '
                  '60/1 is not evidence of 90 Hz and is never re-rounded into a 90 Hz claim')
    else:
        # The C++ gate pairs path with target signal by cross-multiplication too.
        p, s = row.get('refresh'), target.get('signal_refresh')
        if p.get('numerator') * s.get('denominator') != s.get('numerator') * p.get('denominator'):
            status, detail = 'fail', f'path {p["numerator"]}/{p["denominator"]} != signal ' \
                                     f'{s["numerator"]}/{s["denominator"]}: path/signal timing mismatch'
        else:
            status, detail = 'pass', f'path refresh and target signal vSyncFreq both exactly 90/1 ' \
                                     f'({path_detail}; {signal_detail}; rounded hz fields ignored)'
    gates.append(_gate('refresh_rational_90', status, detail))

    gates.extend(dependent)
    failures = [g for g in gates if g['status'] == 'fail']
    unknowns = [g for g in gates if g['status'] == 'unknown']
    if failures:
        outcome = OUTCOME_NOT_READY
    elif unknowns:
        outcome = OUTCOME_INVENTORY_UNKNOWN
        notes.append('One or more measured fields are missing or malformed in the saved inventory; '
                     'unknown is fail-closed, never a fake pass.')
    else:
        outcome = OUTCOME_READY
        notes.append('Every desktop-branch gate passes on the saved measured fields. This is the '
                     'assessment outcome name, not a Direct Mode, panel-cadence or SteamVR claim.')
    return outcome, gates, notes


def build_report(manifest_path, manifest_raw, manifest_sha, inventory_path, inventory_raw,
                 inventory_sha, manifest_errors, outcome, gates, notes):
    """Assemble the printed JSON report. Claims and safety are explicit and always present."""
    gate_by_id = {gate['id']: gate['status'] for gate in gates}
    all_pass = outcome == OUTCOME_READY
    identity = manifest_raw.get('trial_identity')
    expected_path = identity.get('monitor_device_path') if isinstance(identity, dict) else None
    rows = inventory_raw.get('displays')
    return {
        'schema_version': 1,
        'kind': 'refresh90_trial_assessment',
        'tool': 'tools/psvr_native_analysis/refresh90_trial.py',
        'purpose': 'offline assessment of a saved psvr_display inventory against the prepared '
                   '90 Hz trial manifest; the trial stays prepared_unverified_90 until a '
                   'supervised runtime test measures the panel',
        'inputs': {
            'manifest': {'path': str(manifest_path), 'sha256': manifest_sha,
                         'status': manifest_raw.get('status'),
                         'accepted': not manifest_errors,
                         'validation_errors': manifest_errors},
            'inventory': {'path': str(inventory_path), 'sha256': inventory_sha,
                          'display_rows': len(rows) if isinstance(rows, list) else None,
                          'displays_shape_ok': isinstance(rows, list),
                          'saved_artifact': True,
                          'note': 'a previously saved `psvr_display --json` output; this tool never '
                                  'runs psvr_display, never enumerates live displays and never '
                                  'uses subprocess'},
        },
        'target': {'monitor_device_path': expected_path,
                   'desired_refresh': {'numerator': TARGET_NUMERATOR, 'denominator': TARGET_DENOMINATOR},
                   'dimensions': [PANEL_WIDTH, PANEL_HEIGHT],
                   'scan': 'progressive'},
        'gates': gates,
        'gate_status': gate_by_id,
        'outcome': outcome,
        'outcome_notes': notes,
        'claims': {
            'desktop_90_ready_claimed': all_pass,
            'gpu_reported_90_in_saved_artifact_assessed': all_pass,
            'physical_panel_90_claimed': False,
            'direct_mode_90_proven': False,
            'direct_mode_90_failure_claimed': False,
            'panel_cadence_measured': False,
            'steamvr_runtime_working_claimed': False,
            'runtime_configuration_applied': False,
            'prepared_manifest_updated': False,
            'edid_override_applied': False,
        },
        'safety': {'offline_only': True, 'hardware_access': False,
                   'registry_or_gpu_or_runtime_access': False, 'uac_elevation_used': False,
                   'subprocess_used': False, 'live_display_enumeration': False,
                   'nothing_written_or_applied': True,
                   'manifest_or_settings_modified': False},
        'prohibited_claims': [
            'that the physical panel runs 90 Hz, is stable at 90 Hz, or that a SteamVR runtime '
            'works at 90 Hz: even a positive gate only assesses the GPU-reported 90/1 path and '
            'signal rationals inside the SAVED inventory artifact at capture time; the manifest '
            'stays prepared_unverified_90 and this tool never updates it with measurements',
            'that the Direct Mode path fails (or works): a missing Sony row blocks the DESKTOP branch '
            'only and direct evaluation needs other backend modes plus measured timing evidence, not '
            'EDID or property values alone',
            'that a 120 Hz or 60 Hz observation transfers to 90 Hz, or that rounded hz fields prove '
            'a rational refresh',
            'that the EDID control was touched: refresh timing and EDID are separate controls and this '
            'trial requests no EDID override, no EDID 90 generator and invents no 1080p90 VIC',
            'that pre-trial SteamVR settings may be restored to an assumed 120/1 or 60/1: restore uses '
            'the two values actually saved at trial start, recorded in a fresh backup',
        ],
        'limitations': [
            'assessment only: applying the trial (NVIDIA custom timing, SteamVR refresh keys) remains a '
            'manual supervised operator step governed by docs/PSVR_HIGH_REFRESH_TEST.md',
            'a saved inventory reflects the moment it was captured; topology can change afterwards',
            'even a passing desktop gate measures the GPU path/signal rational, not the panel cadence: '
            'scanout claims require runtime evidence this tool never produces',
        ],
        'next_step': {
            OUTCOME_READY: 'The saved artifact already measures 90/1 on the exact Sony path, so do '
                           'not re-apply the manual NVIDIA timing step: first back up the two '
                           'ACTUAL live SteamVR refresh values, then re-capture a fresh inventory '
                           'to re-confirm 90/1 still holds immediately before the change, and only '
                           'then edit the two refresh keys. No power or config-mode change is '
                           'automatic, and this assessment (and the manifest) are not updated by '
                           'it.',
            OUTCOME_NOT_READY: 'Do not proceed on this artifact: re-capture the inventory after the '
                               'NVIDIA timing step, or restore the baseline per restore_pretrial.',
            OUTCOME_BLOCKED_DISPLAY_NOT_OBSERVED: 'Desktop branch cannot be judged from this artifact; '
                                                  'the HMD is currently hidden from active desktop paths '
                                                  '(see the saved readiness bundles). Re-capture while '
                                                  'the HMD is visible on the desktop, or evaluate '
                                                  'direct mode through its own backends and evidence.',
            OUTCOME_INVENTORY_UNKNOWN: 'Fix the inventory artifact first: fields are missing or '
                                       'malformed and the checker fails closed instead of guessing.',
            OUTCOME_MANIFEST_INVALID: 'The trial manifest itself is refused; no inventory judgement '
                                      'is made.',
        }.get(outcome, 'unknown outcome label: treat as fail closed'),
    }


def assess_files(manifest_path=DEFAULT_MANIFEST, inventory_path=None):
    """Load, validate, assess. Raises TrialInputError on unreadable inputs; returns the report."""
    manifest_file = resolve_input(manifest_path)
    manifest, manifest_sha = load_json_object(manifest_file, f'manifest {manifest_file}')
    if inventory_path is None:
        raise TrialInputError('an inventory JSON path is required (--inventory); nothing is '
                              'enumerated live as a fallback')
    inventory_file = resolve_input(inventory_path)
    inventory, inventory_sha = load_json_object(inventory_file, f'inventory {inventory_file}')

    manifest_errors = validate_manifest(manifest)
    if manifest_errors:
        return build_report(manifest_file, manifest, manifest_sha, inventory_file, inventory,
                            inventory_sha, manifest_errors, OUTCOME_MANIFEST_INVALID,
                            [_gate('manifest_validation', 'fail', error) for error in manifest_errors],
                            ['The prepared trial manifest was refused fail-closed, so the saved '
                             'inventory was not judged: a broken trial definition cannot certify '
                             'anything.'])
    outcome, gates, notes = assess(manifest, inventory)
    return build_report(manifest_file, manifest, manifest_sha, inventory_file, inventory,
                        inventory_sha, [], outcome, gates, notes)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='refresh90_trial',
        description='Offline assessment of the prepared PSVR 90 Hz desktop trial against a saved '
                    'psvr_display --json inventory artifact. Pure analysis: no subprocess, no live '
                    'hardware, nothing applied.',
        epilog='Exit codes: 0 assessment produced (see "outcome" in the JSON), 1 input refused or '
               'unreadable (fail closed), 2 CLI argument error.')
    parser.add_argument('--manifest', default=DEFAULT_MANIFEST,
                        help='prepared 90 Hz trial manifest JSON (default: the repo candidate)')
    parser.add_argument('--inventory', required=True,
                        help='path to a saved `psvr_display --json` inventory artifact JSON')
    parser.add_argument('--out', help='optional NEW assessment JSON path; the parent directory must '
                                      'already exist and the file must not exist yet, so evidence is '
                                      'never overwritten')
    args = parser.parse_args(argv)
    try:
        report = assess_files(args.manifest, args.inventory)
    except TrialInputError as error:
        print(json.dumps({'schema_version': 1, 'kind': 'refresh90_trial_assessment',
                          'status': 'blocked', 'outcome': OUTCOME_INVENTORY_UNKNOWN,
                          'reason': str(error),
                          'note': 'an input was refused or unreadable: nothing was assessed, '
                                  'invented or written'}, indent=2))
        return 1
    print(json.dumps(report, indent=2))
    if args.out:
        destination = Path(args.out)
        if not destination.parent.is_dir():
            parser.error(f'{destination.parent} does not exist; this tool never creates directories')
        try:
            with destination.open('x', encoding='utf-8') as handle:
                json.dump(report, handle, indent=2)
                handle.write('\n')
        except FileExistsError:
            parser.error(f'{destination} already exists; only a NEW assessment file is written')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
