# SPDX-License-Identifier: MIT
"""Offline unittests for the prepared 90 Hz trial manifest and its assessment CLI.

Nothing is applied or measured: the manifest fixture is the repo's own
configurations/candidates/hdmi_native_90hz_trial.json, the inventory fixtures are
synthetic rows built exactly in the serialized form of
tools/psvr_display/display_inventory.cpp (inventory_json), and the history cases
reuse the versioned sanitized inventory fixture plus the 120 Hz candidate's
recorded numbers to prove that no 120 Hz (or 60 Hz) evidence is ever
re-labelled as 90 Hz.
"""
import copy
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
TOOLS = HERE / 'tools' / 'psvr_native_analysis'
sys.path.insert(0, str(TOOLS))


def load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


trial = load('refresh90_trial')
readiness = load('readiness')

MANIFEST_PATH = HERE / 'configurations' / 'candidates' / 'hdmi_native_90hz_trial.json'
CANDIDATE_120_PATH = HERE / 'configurations' / 'candidates' / 'hdmi_native_120hz.json'
HIDDEN_INVENTORY_PATH = HERE / 'tests' / 'fixtures' / 'hidden_inventory.json'
MANIFEST = json.loads(MANIFEST_PATH.read_text(encoding='utf-8-sig'))
CANDIDATE_120 = json.loads(CANDIDATE_120_PATH.read_text(encoding='utf-8-sig'))
SONY_PATH = CANDIDATE_120['steamvr_overrides']['driver_psvrpc']['displayMonitorPath']


def make_rate(numerator, denominator, hz=None, available=True):
    """One serialized refresh rational exactly as inventory_json emits it."""
    if hz is None:
        hz = (numerator / denominator) if isinstance(denominator, (int, float)) and denominator else None
    return {'numerator': numerator, 'denominator': denominator, 'available': available, 'hz': hz}


def make_row(**changes):
    """A Sony SNY6A04 desktop row at measured 90/1, serialized like display_inventory.cpp.

    Keyword changes replace or (with None) remove top-level fields, so malformed shapes
    are exercised as real serializer output would be, not as invented Python objects.
    """
    row = {
        'active': True,
        'source_adapter': {'low': 76797, 'high': 0},
        'target_adapter': {'low': 76797, 'high': 0},
        'source_id': 1,
        'target_id': 4352,
        'source_gdi_name': '\\\\.\\DISPLAY2',
        'monitor_device_path': SONY_PATH,
        'friendly_name': 'SIE HMD *08',
        'adapter_name': '\\\\?\\PCI#adapter',
        'source_adapter_name': '\\\\?\\PCI#adapter',
        'edid_ids': {'manufacturer': readiness.SONY_VENDOR, 'product': readiness.SONY_PRODUCT,
                     'valid': True},
        'edid_ids_available': True,
        'edid_bytes': None,
        'edid_bytes_available': False,
        'source': {'x': 1920, 'y': 492, 'width': 1920, 'height': 1080},
        'target': {'width': 1920, 'height': 1080,
                   'signal_refresh': make_rate(90, 1), 'scanline_ordering': 1},
        'refresh': make_rate(90, 1),
        'rotation': 1,
        'scanline_ordering': 1,
        'scaling': 1,
        'output_technology': 4,
        'target_available': True,
        'candidate_hint': '1920x1080 scanout only; not proof of PSVR identity or suitability',
        'available_modes': [],
        'warnings': ['Raw EDID bytes not queried; IDs are Windows EDID-reported values'],
    }
    for key, value in changes.items():
        if value is None:
            row.pop(key, None)
        else:
            row[key] = value
    return row


def make_inventory(displays=None, **changes):
    inventory = {'schema_version': 1, 'read_only': True, 'scope': 'active_paths',
                 'automatic_selection': False,
                 'displays': [make_row()] if displays is None else displays}
    for key, value in changes.items():
        if value is None:
            inventory.pop(key, None)
        else:
            inventory[key] = value
    return inventory


def lg_row():
    """The real LG desktop row shape observed in the saved 2026-10-05 bundles."""
    return make_row(monitor_device_path='\\\\?\\DISPLAY#GSM0001#5&625d12c&1&UID4354#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}',
                    friendly_name='LG TV', source_id=0, target_id=4354,
                    edid_ids={'manufacturer': 27934, 'product': 1, 'valid': True},
                    source={'x': 0, 'y': 0, 'width': 1920, 'height': 1080})


def assess(inventory, manifest=None):
    outcome, gates, notes = trial.assess(manifest or MANIFEST, inventory)
    return outcome, {g['id']: g for g in gates}, notes


class ManifestValidationTests(unittest.TestCase):
    """The repo's trial manifest must validate and must stay a trial, never a claim."""

    def test_real_manifest_validates_clean(self):
        self.assertEqual(trial.validate_manifest(MANIFEST), [])

    def test_identity_is_the_same_sony_path_as_the_120_manifest(self):
        self.assertEqual(MANIFEST['trial_identity']['monitor_device_path'], SONY_PATH)

    def test_desired_timing_matches_the_historical_90hz_column(self):
        timing = MANIFEST['planned_timing']
        self.assertEqual(timing['h_front_porch'], 88)
        self.assertEqual(timing['h_sync_width'], 44)
        self.assertEqual(timing['h_back_porch'], 148)
        self.assertEqual(timing['v_front_porch'], 4)
        self.assertEqual(timing['v_sync_width'], 5)
        self.assertEqual(timing['v_back_porch'], 36)
        self.assertEqual(timing['h_total'], 2200)
        self.assertEqual(timing['v_total'], 1125)
        self.assertEqual(timing['expected_pixel_clock_hz'], 222750000)
        self.assertEqual(timing['expected_horizontal_frequency_hz'], 101250)
        self.assertEqual(222750000, 90 * 2200 * 1125)
        self.assertEqual(timing['h_sync_polarity'], 'positive')
        self.assertEqual(timing['v_sync_polarity'], 'positive')

    def test_no_120_evidence_was_copied_and_nothing_is_claimed_measured(self):
        self.assertEqual(MANIFEST['status'], 'prepared_unverified_90')
        for field, value in MANIFEST['current_physical_validation'].items():
            if field == 'note':
                continue
            self.assertIn(value, (None, False), field)
        for forbidden in ('artifacts', 'steamvr_overrides', 'validation'):
            self.assertNotIn(forbidden, MANIFEST)

    def test_edid_control_is_separate_and_refresh_only(self):
        self.assertFalse(MANIFEST['edid_control']['edid_override_requested'])
        self.assertFalse(MANIFEST['edid_control']['edid_90_generator_requested'])
        self.assertIsNone(MANIFEST['edid_control']['vic_1080p90'])
        self.assertFalse(MANIFEST['refresh_control']['applied'])

    def test_runtime_expectation_and_restore_are_the_two_refresh_keys(self):
        runtime = MANIFEST['expected_runtime_changes']
        self.assertEqual(runtime['steamvr_override_keys'],
                         {'driver_psvrpc.displayRefreshNumerator': 90,
                          'driver_psvrpc.displayRefreshDenominator': 1})
        self.assertTrue(any('pauseCompositorOnStandby' in item
                            for item in runtime['never_change_automatically']))
        self.assertEqual(MANIFEST['restore_pretrial']['keys'],
                         ['driver_psvrpc.displayRefreshNumerator',
                          'driver_psvrpc.displayRefreshDenominator'])
        self.assertFalse(MANIFEST['restore_pretrial']['assumed_value_used'])
        self.assertEqual(len(MANIFEST['restore_pretrial']['do_not_hardcode']), 2)
        nvidia = MANIFEST['restore_pretrial']['nvidia_custom_mode'].lower()
        self.assertIn('actually present', nvidia)
        self.assertIn('fallback only', nvidia)
        self.assertNotIn('keep the 60 hz baseline', nvidia)

    def test_unknown_schema_version_and_bad_identifiers_fail_closed(self):
        bad = copy.deepcopy(MANIFEST)
        bad['schema_version'] = 2
        self.assertTrue(trial.validate_manifest(bad))
        bad = copy.deepcopy(MANIFEST)
        del bad['schema_version']
        self.assertTrue(any('schema_version' in error for error in trial.validate_manifest(bad)))
        for section in ('trial_identity', 'planned_timing', 'restore_pretrial'):
            bad = copy.deepcopy(MANIFEST)
            bad.pop(section)
            self.assertTrue(any(section in error for error in trial.validate_manifest(bad)), section)

    def _mutate(self, section, key, value):
        bad = copy.deepcopy(MANIFEST)
        target = bad if section is None else bad[section]
        target[key] = value
        return trial.validate_manifest(bad)

    def _mutate_fields(self, section, changes):
        bad = copy.deepcopy(MANIFEST)
        target = bad if section is None else bad[section]
        target.update(changes)
        return trial.validate_manifest(bad)

    def test_timing_is_pinned_to_the_documented_table_not_just_self_consistent(self):
        # Porch swaps that preserve totals and every arithmetic identity stay internally
        # consistent, yet they are undocumented timings: under the historical SNYB403
        # provenance label they are invented modes and must be refused.
        for changes in ({'h_front_porch': 90, 'h_back_porch': 146},
                        {'h_sync_width': 40, 'h_front_porch': 92},
                        {'v_front_porch': 5, 'v_back_porch': 35},
                        {'v_sync_width': 4, 'v_front_porch': 5}):
            self.assertTrue(self._mutate_fields('planned_timing', changes), changes)
        # The provenance pointer is pinned to the documented archive, not any valid URL.
        self.assertTrue(self._mutate('timing_provenance', 'reference_url',
                                     'https://example.org/other_psvr_120.zip'))
        self.assertTrue(self._mutate('timing_provenance', 'reference_archive_sha256',
                                     'a' * 64))

    def test_timing_arithmetic_tampering_is_rejected(self):
        for key, value in (('h_total', 2199), ('v_total', 1124),
                           ('expected_pixel_clock_hz', 297000000),
                           ('expected_horizontal_frequency_hz', 135000),
                           ('h_back_porch', 147.5), ('v_sync_width', True)):
            errors = self._mutate('planned_timing', key, value)
            self.assertTrue(errors, f'{key}={value!r} must be refused')

    def test_measurement_claims_are_rejected(self):
        self.assertTrue(self._mutate('current_physical_validation', 'path_refresh_measured', 90))
        self.assertTrue(self._mutate('current_physical_validation', 'panel_cadence_verified', True))
        self.assertTrue(self._mutate(None, 'status', 'candidate_measured_90'))
        self.assertTrue(self._mutate('timing_provenance', 'tested_on_this_unit', True))
        self.assertTrue(self._mutate('timing_provenance', 'identity_adopted', True))

    def test_control_mixing_assumptions_and_bad_rationals_are_rejected(self):
        self.assertTrue(self._mutate('edid_control', 'edid_90_generator_requested', True))
        self.assertTrue(self._mutate('edid_control', 'vic_1080p90', 99))
        self.assertTrue(self._mutate('restore_pretrial', 'assumed_value_used', True))
        self.assertTrue(self._mutate('expected_runtime_changes', 'steamvr_override_keys',
                                     {'driver_psvrpc.displayRefreshNumerator': 90}))
        self.assertTrue(self._mutate('desired_mode', 'refresh', {'numerator': 180, 'denominator': 2}))
        self.assertTrue(self._mutate('trial_identity', 'edid_ids',
                                     {'manufacturer': 27934, 'product': 1}))

    def test_sha256_and_provenance_shape_are_enforced(self):
        self.assertTrue(self._mutate('timing_provenance', 'reference_archive_sha256', '54574aae'))
        self.assertTrue(self._mutate('timing_provenance', 'historical_profile_id', 'SNY6A04'))


class AssessmentPassTests(unittest.TestCase):
    """A saved inventory that genuinely measures 90/1 on the exact path yields the gate."""

    def test_all_measured_fields_pass(self):
        outcome, gates, _ = assess(make_inventory())
        self.assertEqual(outcome, trial.OUTCOME_READY)
        self.assertTrue(all(g['status'] == 'pass' for g in gates.values()),
                        {k: g['status'] for k, g in gates.items()})

    def test_ready_report_makes_only_desktop_claims(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory_file = Path(tmp) / 'inv.json'
            inventory_file.write_text(json.dumps(make_inventory()), encoding='utf-8')
            report = trial.assess_files(str(MANIFEST_PATH), str(inventory_file))
        self.assertEqual(report['outcome'], trial.OUTCOME_READY)
        self.assertTrue(report['claims']['desktop_90_ready_claimed'])
        self.assertTrue(report['claims']['gpu_reported_90_in_saved_artifact_assessed'])
        self.assertFalse(report['claims']['physical_panel_90_claimed'])
        self.assertFalse(report['claims']['prepared_manifest_updated'])
        self.assertFalse(report['claims']['direct_mode_90_proven'])
        self.assertFalse(report['claims']['direct_mode_90_failure_claimed'])
        self.assertFalse(report['claims']['panel_cadence_measured'])
        self.assertFalse(report['claims']['steamvr_runtime_working_claimed'])
        self.assertFalse(report['claims']['runtime_configuration_applied'])
        self.assertTrue(report['safety']['offline_only'])
        # Precise denial: only physical cadence/stability/runtime working are refused,
        # the GPU-reported saved-artifact measurement IS assessed.
        self.assertIn('physical panel', report['prohibited_claims'][0])
        self.assertIn('SAVED inventory artifact', report['prohibited_claims'][0])
        # Ready means the timing is already measured in the artifact: no re-apply step.
        self.assertIn('do not re-apply', report['next_step'])
        self.assertIn('back up the two', report['next_step'])
        self.assertIn('re-capture a fresh inventory', report['next_step'])
        self.assertNotIn('apply manual custom timings', report['next_step'].lower())

    def test_equivalent_rational_180_2_passes_by_cross_multiplication(self):
        row = make_row(refresh=make_rate(180, 2),
                       target={'width': 1920, 'height': 1080,
                               'signal_refresh': make_rate(180, 2), 'scanline_ordering': 1})
        outcome, gates, _ = assess(make_inventory([row]))
        self.assertEqual(outcome, trial.OUTCOME_READY)
        self.assertEqual(gates['refresh_rational_90']['status'], 'pass')


class AssessmentFailClosedTests(unittest.TestCase):
    """Every reject case must stay a reject: unknown is never promoted to pass."""

    def test_measured_120_history_is_rejected_and_never_claimed_as_90(self):
        hz = CANDIDATE_120['validation']['gpu_path_and_signal_refresh_hz']
        self.assertEqual(hz, 120)
        row = make_row(refresh=make_rate(hz, 1),
                       target={'width': 1920, 'height': 1080,
                               'signal_refresh': make_rate(hz, 1), 'scanline_ordering': 1})
        outcome, gates, _ = assess(make_inventory([row]))
        self.assertEqual(outcome, trial.OUTCOME_NOT_READY)
        gate = gates['refresh_rational_90']
        self.assertEqual(gate['status'], 'fail')
        self.assertIn('120/1', gate['detail'])
        self.assertIn('never re-rounded', gate['detail'])

    def test_89_1_with_a_rounded_90_0_hz_field_fails_rational_not_display(self):
        row = make_row(refresh=make_rate(89, 1, hz=90.0))
        outcome, gates, _ = assess(make_inventory([row]))
        self.assertEqual(outcome, trial.OUTCOME_NOT_READY)
        self.assertEqual(gates['refresh_rational_90']['status'], 'fail')

    def test_path_signal_rational_mismatch_is_rejected(self):
        row = make_row(refresh=make_rate(90, 1),
                       target={'width': 1920, 'height': 1080,
                               'signal_refresh': make_rate(60, 1), 'scanline_ordering': 1})
        outcome, gates, _ = assess(make_inventory([row]))
        self.assertEqual(outcome, trial.OUTCOME_NOT_READY)
        self.assertEqual(gates['refresh_rational_90']['status'], 'fail')
        self.assertIn('60/1', gates['refresh_rational_90']['detail'])

    def test_zero_bool_and_float_rationals_are_unknown_not_pass(self):
        for bad in (make_rate(0, 1), make_rate(True, 1), make_rate(90, 0), make_rate(90, False),
                    {'numerator': 90.0, 'denominator': 1, 'available': True, 'hz': 90.0},
                    {'numerator': 90, 'denominator': 1, 'available': False, 'hz': 90.0}):
            row = make_row(refresh=bad)
            outcome, gates, _ = assess(make_inventory([row]))
            self.assertEqual(outcome, trial.OUTCOME_INVENTORY_UNKNOWN, bad)
            self.assertEqual(gates['refresh_rational_90']['status'], 'unknown')

    def test_interlaced_rotated_scaled_and_null_geometry_are_rejected_or_unknown(self):
        scan2 = make_row(scanline_ordering=2)
        outcome, gates, _ = assess(make_inventory([scan2]))
        self.assertEqual((outcome, gates['progressive_scan']['status']),
                         (trial.OUTCOME_NOT_READY, 'fail'))
        rotated = make_row(rotation=2, scaling=2)
        outcome, gates, _ = assess(make_inventory([rotated]))
        self.assertEqual(gates['identity_transforms']['status'], 'fail')
        scaled_bool = make_row(scaling=True)
        outcome, gates, _ = assess(make_inventory([scaled_bool]))
        self.assertEqual(gates['identity_transforms']['status'], 'unknown')
        no_geometry = make_row(source=None, target=None)
        outcome, gates, _ = assess(make_inventory([no_geometry]))
        self.assertEqual(outcome, trial.OUTCOME_INVENTORY_UNKNOWN)
        self.assertEqual(gates['source_target_geometry']['status'], 'unknown')
        self.assertEqual(gates['progressive_scan']['status'], 'unknown')

    def test_wrong_identity_duplicate_inactive_and_clone_are_rejected(self):
        wrong = make_row(edid_ids={'manufacturer': 27934, 'product': 1, 'valid': True})
        outcome, gates, _ = assess(make_inventory([wrong]))
        self.assertEqual((outcome, gates['edid_identity']['status']),
                         (trial.OUTCOME_NOT_READY, 'fail'))
        no_valid = make_row(edid_ids={'manufacturer': readiness.SONY_VENDOR,
                                      'product': readiness.SONY_PRODUCT, 'valid': False})
        outcome, gates, _ = assess(make_inventory([no_valid]))
        self.assertEqual(gates['edid_identity']['status'], 'unknown')
        outcome, gates, _ = assess(make_inventory([make_row(), make_row(target_id=9999)]))
        self.assertEqual((outcome, gates['display_uniquely_selected']['status']),
                         (trial.OUTCOME_NOT_READY, 'fail'))
        outcome, gates, _ = assess(make_inventory([make_row(active=False, target_available=False)]))
        self.assertEqual(gates['active_and_target_available']['status'], 'fail')
        other = lg_row()
        other = dict(other, source_adapter={'low': 76797, 'high': 0}, source_id=1)
        outcome, gates, _ = assess(make_inventory([make_row(source_id=1), other]))
        self.assertEqual((outcome, gates['non_clone_source']['status']),
                         (trial.OUTCOME_NOT_READY, 'fail'))

    def test_missing_schema_fields_are_unknown_not_fake_pass(self):
        for broken in ({'displays': [make_row()]},
                       make_inventory(read_only=None),
                       make_inventory(scope=None),
                       make_inventory(schema_version=2),
                       make_inventory(displays={'not': 'a list'}),
                       make_inventory(automatic_selection=None),
                       make_inventory(automatic_selection=True)):
            outcome, gates, _ = assess(broken)
            self.assertEqual(outcome, trial.OUTCOME_INVENTORY_UNKNOWN)
            self.assertEqual(gates['inventory_schema']['status'], 'unknown')

    def test_malformed_display_entries_are_schema_unknown_not_absent_sony(self):
        # A non-dict row is a broken artifact: it must never be read as "no Sony here".
        outcome, gates, _ = assess(make_inventory(displays=['a string is not a row']))
        self.assertEqual(outcome, trial.OUTCOME_INVENTORY_UNKNOWN)
        self.assertEqual(gates['inventory_schema']['status'], 'unknown')
        self.assertNotIn('display_uniquely_selected', gates)
        mixed = make_inventory(displays=[make_row(), None])
        outcome, gates, _ = assess(mixed)
        self.assertEqual(outcome, trial.OUTCOME_INVENTORY_UNKNOWN)

    def test_clone_gate_requires_wellformed_source_identities(self):
        missing_adapter = make_row(source_adapter=None)
        outcome, gates, _ = assess(make_inventory([missing_adapter]))
        self.assertEqual((outcome, gates['non_clone_source']['status']),
                         (trial.OUTCOME_INVENTORY_UNKNOWN, 'unknown'))
        bool_source = make_row(source_id=True)
        outcome, gates, _ = assess(make_inventory([bool_source]))
        self.assertEqual(gates['non_clone_source']['status'], 'unknown')
        negative_source = make_row(source_id=-1)
        outcome, gates, _ = assess(make_inventory([negative_source]))
        self.assertEqual(gates['non_clone_source']['status'], 'unknown')
        # A malformed ACTIVE other row could hide a clone: unknown, not a silent pass.
        shadow = dict(lg_row(), source_adapter=None)
        outcome, gates, _ = assess(make_inventory([make_row(), shadow]))
        self.assertEqual((outcome, gates['non_clone_source']['status']),
                         (trial.OUTCOME_INVENTORY_UNKNOWN, 'unknown'))
        self.assertIn('hide a clone', gates['non_clone_source']['detail'])
        # A malformed INACTIVE other row cannot be a clone on an active path: still pass.
        inactive_shadow = dict(shadow, active=False)
        outcome, gates, _ = assess(make_inventory([make_row(), inactive_shadow]))
        self.assertEqual((outcome, gates['non_clone_source']['status']),
                         (trial.OUTCOME_READY, 'pass'))

    def test_assess_files_survives_malformed_manifest_and_inventory_shapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            inventory_file = tmp / 'inv.json'
            inventory_file.write_text(json.dumps(make_inventory()), encoding='utf-8')
            # trial_identity present but not an object must not crash report building.
            bad_manifest = copy.deepcopy(MANIFEST)
            bad_manifest['trial_identity'] = ['not', 'an', 'object']
            manifest_file = tmp / 'm.json'
            manifest_file.write_text(json.dumps(bad_manifest), encoding='utf-8')
            report = trial.assess_files(str(manifest_file), str(inventory_file))
            self.assertEqual(report['outcome'], trial.OUTCOME_MANIFEST_INVALID)
            self.assertIsNone(report['target']['monitor_device_path'])
            self.assertFalse(any(report['claims'].values()))
            # displays null / int must not crash len() in the report either.
            for broken_displays in (None, 5):
                broken_inventory = {'schema_version': 1, 'read_only': True,
                                    'scope': 'active_paths', 'automatic_selection': False,
                                    'displays': broken_displays}
                inventory_file.write_text(json.dumps(broken_inventory), encoding='utf-8')
                report = trial.assess_files(str(MANIFEST_PATH), str(inventory_file))
                self.assertEqual(report['outcome'], trial.OUTCOME_INVENTORY_UNKNOWN)
                self.assertIsNone(report['inputs']['inventory']['display_rows'])
                self.assertFalse(report['inputs']['inventory']['displays_shape_ok'])
                self.assertFalse(any(report['claims'].values()))

    def test_hidden_inventory_fixture_blocks_the_desktop_branch_only(self):
        inventory = json.loads(HIDDEN_INVENTORY_PATH.read_text(encoding='utf-8-sig'))
        self.assertEqual([d['monitor_device_path'] for d in inventory['displays']],
                         [lg_row()['monitor_device_path']])
        outcome, gates, notes = assess(inventory)
        self.assertEqual(outcome, trial.OUTCOME_BLOCKED_DISPLAY_NOT_OBSERVED)
        self.assertEqual(gates['display_uniquely_selected']['status'], 'unknown')
        joined = ' '.join(notes)
        self.assertIn('not proof that Direct Mode at 90 Hz would fail', joined)
        report = trial.build_report(MANIFEST_PATH, MANIFEST, '', HIDDEN_INVENTORY_PATH, inventory, '',
                                    [], outcome, list(gates.values()), notes)
        self.assertFalse(report['claims']['direct_mode_90_failure_claimed'])
        self.assertFalse(report['claims']['desktop_90_ready_claimed'])

    def test_invalid_manifest_short_circuits_before_inventory_judgement(self):
        bad = copy.deepcopy(MANIFEST)
        bad['status'] = 'candidate_measured_90'
        # assess() assumes a validated manifest; the enforced path is assess_files:
        with tempfile.TemporaryDirectory() as tmp:
            manifest_file = Path(tmp) / 'm.json'
            manifest_file.write_text(json.dumps(bad), encoding='utf-8')
            inventory_file = Path(tmp) / 'i.json'
            inventory_file.write_text(json.dumps(make_inventory()), encoding='utf-8')
            report = trial.assess_files(str(manifest_file), str(inventory_file))
        self.assertEqual(report['outcome'], trial.OUTCOME_MANIFEST_INVALID)
        self.assertEqual(report['gates'][0]['id'], 'manifest_validation')
        self.assertFalse(report['inputs']['manifest']['accepted'])
        self.assertIn('prepared_unverified_90',
                      report['inputs']['manifest']['validation_errors'][0])


class CliTests(unittest.TestCase):
    """The CLI only reads two saved files and prints JSON; --out may only create new files."""

    def run_cli(self, argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = trial.main(argv)
        return code, buffer.getvalue()

    def test_assessment_prints_json_and_writes_nothing_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory_file = Path(tmp) / 'inv.json'
            inventory_file.write_text(json.dumps(make_inventory()), encoding='utf-8')
            before = sorted(p.name for p in Path(tmp).iterdir())
            code, out = self.run_cli(['--manifest', str(MANIFEST_PATH),
                                      '--inventory', str(inventory_file)])
            self.assertEqual(code, 0)
            report = json.loads(out)
            self.assertEqual(report['outcome'], trial.OUTCOME_READY)
            self.assertEqual(report['kind'], 'refresh90_trial_assessment')
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), before)

    def test_out_writes_exactly_one_new_file_in_an_existing_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory_file = Path(tmp) / 'inv.json'
            inventory_file.write_text(json.dumps(make_inventory()), encoding='utf-8')
            destination = Path(tmp) / 'assessment.json'
            code, _ = self.run_cli(['--manifest', str(MANIFEST_PATH),
                                    '--inventory', str(inventory_file), '--out', str(destination)])
            self.assertEqual(code, 0)
            self.assertTrue(destination.exists())
            self.assertEqual(json.loads(destination.read_text(encoding='utf-8'))['outcome'],
                             trial.OUTCOME_READY)

    def test_out_refuses_existing_files_missing_parents_and_creates_no_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory_file = Path(tmp) / 'inv.json'
            inventory_file.write_text(json.dumps(make_inventory()), encoding='utf-8')
            existing = Path(tmp) / 'exists.json'
            existing.write_text('{}', encoding='utf-8')
            with self.assertRaises(SystemExit) as ctx:
                self.run_cli(['--inventory', str(inventory_file), '--out', str(existing)])
            self.assertEqual(ctx.exception.code, 2)
            self.assertEqual(existing.read_text(encoding='utf-8'), '{}')
            with self.assertRaises(SystemExit):
                self.run_cli(['--inventory', str(inventory_file),
                              '--out', str(Path(tmp) / 'nope' / 'deep' / 'a.json')])
            self.assertFalse((Path(tmp) / 'nope').exists())

    def test_missing_or_malformed_inputs_exit_blocked_without_inventing_results(self):
        code, out = self.run_cli(['--inventory', str(HERE / 'no' / 'such-file.json')])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)['status'], 'blocked')
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / 'broken.json'
            broken.write_text('{"displays": [', encoding='utf-8')
            code, out = self.run_cli(['--inventory', str(broken)])
            self.assertEqual(code, 1)
            self.assertIn('not readable JSON', json.loads(out)['reason'])
            broken.write_text('["a list is not an inventory"]', encoding='utf-8')
            code, out = self.run_cli(['--inventory', str(broken)])
            self.assertEqual(code, 1)
            self.assertIn('must be a JSON object', json.loads(out)['reason'])

    def test_inventory_is_required_and_unknown_arguments_are_errors(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli([])
        self.assertEqual(ctx.exception.code, 2)
        with self.assertRaises(SystemExit):
            self.run_cli(['--inventory', 'x.json', '--live-enumerate'])


if __name__ == '__main__':
    unittest.main()
