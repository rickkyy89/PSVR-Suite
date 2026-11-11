# SPDX-License-Identifier: MIT
"""Offline tests for the EDID trial comparison matrix.

Nothing is applied or fetched: the accepted baseline is the versioned sanitized
EDID plan fixture (tests/fixtures/edid_plan.json) plus its 120 Hz candidate,
while the conflict/unsupported-baseline fixtures are real-format plan logs
written into a temporary directory. Expected values are derived from the loaded
evidence and from the reused generators, never copied from a hardcoded synthetic
EDID.
"""
import copy
import hashlib
import importlib.util
import io
import json
import re
import sys
import tempfile
import unittest
import uuid
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


matrix = load('edid_trial_matrix')
proposal = load('edid_120_proposal')
blocks = load('edid_blocks')
hmd_edid = load('hmd_edid')

PLAN_PATH = HERE / 'tests' / 'fixtures' / 'edid_plan.json'
CAND_PATH = HERE / 'configurations' / 'candidates' / 'hdmi_native_120hz.json'
PLAN = json.loads(PLAN_PATH.read_text(encoding='utf-8-sig'))
CANDIDATE = json.loads(CAND_PATH.read_text(encoding='utf-8-sig'))
ORIGINAL = bytes.fromhex(PLAN['raw_edid_hex'])
PATCHED_FULL = ORIGINAL[:128] + bytes.fromhex(PLAN['patched_block_hex'])
CONTAINER_GUID = uuid.UUID(PLAN['container_guid'])
USAGE07 = hmd_edid.add_hmd_block(ORIGINAL, CONTAINER_GUID)
USAGE27 = hmd_edid.add_hmd_block(ORIGINAL, CONTAINER_GUID, True)
CELL_IDS = [f'{variant}/{column}' for variant in matrix.VARIANTS for column in matrix.COLUMNS]
INSERT_SHIFT = 22          # byte length of the Microsoft HMD VSDB block inserted by hmd_edid


def fix_checksums(data):
    """Fixture helper: make the block checksum(s) of a 128- or 256-byte fixture valid again."""
    blob = bytearray(data)
    for i in range(0, len(blob), 128):
        blob[i + 127] = (-sum(blob[i:i + 127])) & 0xFF
    return bytes(blob)


def mutate_hex(hex_text, index, value):
    """One byte changed in a hex blob, with valid checksums (a real-format fixture edit)."""
    blob = bytearray(bytes.fromhex(hex_text))
    blob[index] = value
    return fix_checksums(bytes(blob)).hex()


def corrupt_hex(hex_text, index, value):
    """One byte changed with the checksum left broken, to exercise the checksum gate."""
    blob = bytearray(bytes.fromhex(hex_text))
    blob[index] = value
    return bytes(blob).hex()


def plan_with(**overrides):
    """A real-format plan log derived from the saved one with the given fields replaced."""
    plan = copy.deepcopy(PLAN)
    for key, value in overrides.items():
        if value is None:
            plan.pop(key, None)
        else:
            plan[key] = value
    return plan


def candidate_with(timing_changes=None, validation_changes=None):
    candidate = copy.deepcopy(CANDIDATE)
    for key, value in (timing_changes or {}).items():
        candidate['nvidia_manual_timing_reference'][key] = value
    for key, value in (validation_changes or {}).items():
        candidate['validation'][key] = value
    return candidate


def write_json(directory, name, data):
    path = Path(directory) / name
    path.write_text(json.dumps(data), encoding='utf-8')
    return str(path)


def blob_of(cell):
    return bytes.fromhex(cell['edid_hex'])


def offsets(rows):
    return sorted(row['offset'] for row in rows)


class MatrixOnRealEvidenceTests(unittest.TestCase):
    """The matrix must accept the actual saved plan and describe it honestly."""

    @classmethod
    def setUpClass(cls):
        cls.report = matrix.build_matrix(str(PLAN_PATH), str(CAND_PATH))
        cls.cells = {c['cell_id']: c for c in cls.report['matrix']}
        cls.pairs = {tuple(p['pair']): p for p in cls.report['pairwise_causal_diffs']}

    def test_all_six_cells_generated_and_valid(self):
        self.assertEqual(self.report['matrix_status'], 'complete')
        self.assertEqual([c['cell_id'] for c in self.report['matrix']], CELL_IDS)
        for cell in self.report['matrix']:
            self.assertEqual(cell['status'], 'generated', cell['cell_id'])
            self.assertEqual(cell['length'], 256)
            self.assertEqual(cell['checksums'], [0, 0])
            self.assertTrue(cell['checksums_valid'])
            blob = blob_of(cell)
            blocks.require_complete(blob)
            self.assertEqual(blob[:128], ORIGINAL[:128] if cell['column'] == 'base' else blob[:128])
        self.assertEqual(len({c['edid_sha256'] for c in self.report['matrix']}), 6)

    def test_baseline_is_the_versioned_fixture_not_hardcoded(self):
        check = self.report['baseline_check']
        self.assertTrue(check['accepted'])
        self.assertTrue(check['offline_regenerated_usage07_cta_matches_recorded_patched_block'])
        self.assertTrue(check['container_guid_matches_plan'])
        self.assertEqual(check['plan_container_guid'], PLAN['container_guid'])
        self.assertEqual(check['regenerated_block_container_guid'], PLAN['container_guid'])
        self.assertEqual(self.report['inputs']['plan_log']['raw_edid_sha256'],
                         hashlib.sha256(ORIGINAL).hexdigest())
        self.assertEqual(self.report['inputs']['plan_log']['monitor_path'], PLAN.get('monitor_path'))
        self.assertEqual(self.cells['original/base']['edid_sha256'], hashlib.sha256(ORIGINAL).hexdigest())
        self.assertEqual(blob_of(self.cells['original/base']), ORIGINAL)
        self.assertEqual(blob_of(self.cells['hmd_v2_usage07/base']), USAGE07)
        self.assertEqual(blob_of(self.cells['hmd_v2_usage27/base']), USAGE27)
        self.assertEqual(self.cells['original/base']['capabilities']['ms_hmd_vsdb'], None)
        self.assertEqual(self.cells['hmd_v2_usage07/base']['capabilities']['ms_hmd_vsdb']['usage_hex'], '07')
        self.assertEqual(self.cells['hmd_v2_usage27/base']['capabilities']['ms_hmd_vsdb']['usage_hex'], '27')

    def test_a_different_plan_guid_is_honoured_so_nothing_is_hardcoded(self):
        other_guid = uuid.UUID('0F1E2D3C-4B5A-6978-1234-56789ABCDEF0')
        regenerated = hmd_edid.add_hmd_block(ORIGINAL, other_guid)
        with tempfile.TemporaryDirectory() as tmp:
            plan = plan_with(container_guid='{%s}' % str(other_guid).upper(),
                             patched_block_hex=regenerated[128:256].hex())
            report = matrix.build_matrix(write_json(tmp, 'plan.json', plan), str(CAND_PATH))
        cells = {c['cell_id']: c for c in report['matrix']}
        check = report['baseline_check']
        self.assertTrue(check['offline_regenerated_usage07_cta_matches_recorded_patched_block'])
        self.assertEqual(cells['hmd_v2_usage07/base']['capabilities']['ms_hmd_vsdb']['container_guid'],
                         '{0F1E2D3C-4B5A-6978-1234-56789ABCDEF0}')
        self.assertNotEqual(cells['hmd_v2_usage07/base']['edid_sha256'],
                            self.cells['hmd_v2_usage07/base']['edid_sha256'])
        self.assertEqual(cells['hmd_v2_usage07/base']['edid_sha256'],
                         hashlib.sha256(regenerated).hexdigest())
        # The capture itself and the identity are untouched by the new GUID.
        self.assertEqual(cells['original/base']['edid_sha256'], self.cells['original/base']['edid_sha256'])
        self.assertEqual(cells['hmd_v2_usage27/base']['capabilities']['ms_hmd_vsdb']['usage_hex'], '27')

    def test_identity_dtds_name_and_capabilities_preserved_cell_by_cell(self):
        original_caps = matrix.capabilities(ORIGINAL)
        for cell in self.report['matrix']:
            caps, identity = cell['capabilities'], cell['identity']
            self.assertEqual(caps['identity_hex'], ORIGINAL[8:12].hex(), cell['cell_id'])
            self.assertEqual(caps['serial_hex'], ORIGINAL[12:16].hex(), cell['cell_id'])
            self.assertTrue(identity['identity_and_serial_match_baseline'], cell['cell_id'])
            self.assertTrue(identity['name_descriptor_match_baseline'], cell['cell_id'])
            self.assertTrue(identity['base_block_below_fd_maxima_identical'], cell['cell_id'])
            for descriptor, before in zip(caps['base_descriptors'], original_caps['base_descriptors']):
                if descriptor['kind'].startswith('range limits') and cell['column'] == 'measured120':
                    differing = [k for k in range(18)
                                 if descriptor['hex'][2 * k:2 * k + 2] != before['hex'][2 * k:2 * k + 2]]
                    self.assertEqual(differing, [6, 8, 9], cell['cell_id'])   # the three rate maxima only
                else:
                    self.assertEqual(descriptor['hex'], before['hex'],
                                     f"{cell['cell_id']} {descriptor['kind']}")
            delta = {d['field'] for d in cell['capability_delta_vs_baseline']}
            self.assertNotIn('identity_hex', delta)
            self.assertNotIn('serial_hex', delta)
            for field in ('screen_size_cm', 'chromaticity_hex', 'edid_version', 'established_timings_hex',
                          'standard_timing_ids_hex', 'base_descriptor_kinds', 'extension_count'):
                self.assertIn(field, cell['capability_preserved_fields'], f"{cell['cell_id']} {field}")
            structure = {'cta_block_count', 'cta_block_order'}
            timing = {'fd_range_limits', 'video_vics', 'hdmi_vsdb_max_tmds_mhz'}
            if cell['column'] == 'base':
                self.assertEqual(delta, set() if cell['variant'] == 'original'
                                 else structure | {'ms_hmd_vsdb'})
            elif cell['variant'] == 'original':
                self.assertEqual(delta, timing)
            else:
                # Against the captured baseline a 120 cell shows both steps: the classification
                # block (count/order/VSDB) plus the timing advertisement fields.
                self.assertEqual(delta, structure | {'ms_hmd_vsdb'} | timing)
                base_caps = matrix.capabilities(blob_of(self.cells[f"{cell['variant']}/base"]))
                # Within its own variant the timing step preserves the CTA structure and usage.
                self.assertEqual(caps['cta_block_order'], base_caps['cta_block_order'])
                self.assertEqual(caps['cta_block_count'], base_caps['cta_block_count'])
                self.assertEqual(caps['ms_hmd_vsdb']['usage_hex'], base_caps['ms_hmd_vsdb']['usage_hex'])
                self.assertEqual(caps['ms_hmd_vsdb']['container_guid'],
                                 base_caps['ms_hmd_vsdb']['container_guid'])

    def test_every_cell_carries_its_own_delta_with_real_byte_pairs(self):
        self.assertIsNone(self.cells['original/base']['delta_vs_baseline'])
        distinct = set()
        for cell in self.report['matrix']:
            delta = cell['delta_vs_baseline']
            if delta is None:
                continue
            blob = blob_of(cell)
            audit = blocks.audit(ORIGINAL, blob)
            self.assertEqual(delta['changed_offsets'], audit['changed_offsets'], cell['cell_id'])
            self.assertTrue(delta['all_changed_bytes_explained'], cell['cell_id'])
            self.assertEqual(delta['unexplained'], [])
            rows = delta['logical'] + delta['byte_shift'] + delta['checksum_recompute']
            self.assertEqual(offsets(rows), audit['changed_offsets'], cell['cell_id'])
            self.assertEqual(len(rows), delta['changed_count'])
            for row in rows:
                self.assertEqual(int(row['old'], 16), ORIGINAL[row['offset']], f"{cell['cell_id']} {row}")
                self.assertEqual(int(row['new'], 16), blob[row['offset']], f"{cell['cell_id']} {row}")
            for row in delta['byte_shift']:
                self.assertEqual(blob[row['offset']], ORIGINAL[row['from_offset']])
                self.assertEqual(row['offset'] - row['from_offset'], row['block_shift'])
            distinct.add(tuple((row['offset'], row['old'], row['new']) for row in rows))
        # Five non-baseline cells, five separate deltas. The two usage variants can share the same
        # differing offsets, so the comparison uses the actual old/new byte pairs, not offsets only.
        self.assertEqual(len(distinct), 5)

    def test_usage_only_pairs_change_usage_and_checksum_only(self):
        for name in [('hmd_v2_usage07/base', 'hmd_v2_usage27/base'),
                     ('hmd_v2_usage07/measured120', 'hmd_v2_usage27/measured120')]:
            pair = self.pairs[name]
            self.assertEqual(pair['causal_steps'], ['classification_hmd_vsdb_field_changed'], name)
            self.assertEqual(len(pair['changed_offsets']), 2, name)
            self.assertEqual([row['role'].rsplit(': ', 1)[-1] for row in pair['logical']],
                             ['primary use case byte'], name)
            self.assertEqual([pair['logical'][0]['old'], pair['logical'][0]['new']], ['07', '27'], name)
            self.assertEqual(offsets(pair['checksum_recompute']), [255], name)
            self.assertEqual(pair['byte_shift'], [], name)
            self.assertEqual(pair['unexplained'], [], name)
            self.assertEqual(pair['changed_base'], [], name)
            self.assertIn('no timing byte', pair['verdict'], name)

    def test_insertion_pair_keeps_logical_bytes_apart_from_byte_shift(self):
        pair = self.pairs[('original/base', 'hmd_v2_usage07/base')]
        self.assertEqual(pair['causal_steps'], ['classification_hmd_vsdb_added'])
        self.assertEqual(pair['changed_base'], [])                    # no base-block byte moves
        self.assertEqual(pair['blocks_inserted'], [matrix.MICROSOFT_HMD_NAME])
        self.assertEqual(pair['blocks_removed'], [])
        self.assertEqual(offsets(pair['checksum_recompute']), [255])
        bookkeeping = [row for row in pair['logical'] if row['offset'] == 130]
        self.assertEqual(len(bookkeeping), 1)
        self.assertIn('end of data block collection', bookkeeping[0]['role'])
        inserted_rows = [row for row in pair['logical'] if row['offset'] != 130]
        span = list(range(inserted_rows[0]['offset'], inserted_rows[-1]['offset'] + 1))
        self.assertEqual(offsets(inserted_rows), [i for i in span if ORIGINAL[i] != USAGE07[i]])
        self.assertEqual({row['role'] for row in inserted_rows},
                         {f'{matrix.MICROSOFT_HMD_NAME}: inserted data block byte'})
        self.assertEqual(len(span), INSERT_SHIFT)
        self.assertEqual(USAGE07[span[0]], 0x75)                      # CTA VSDB header tag+length
        self.assertEqual(USAGE07[span[0] + 1:span[0] + 4], matrix.MICROSOFT_OUI)
        self.assertEqual(USAGE07[span[0] + 4], 2)                     # VSDB version 2
        self.assertEqual(USAGE07[span[0] + 5], 0x07)                  # primary use case
        self.assertTrue(pair['byte_shift'])
        for row in pair['byte_shift']:
            self.assertEqual(row['block_shift'], INSERT_SHIFT)
            self.assertEqual(USAGE07[row['offset']], ORIGINAL[row['offset'] - INSERT_SHIFT])
            self.assertIn('content preserved', row['role'])
        self.assertIn('no timing, identity or advertised-capability byte changed', pair['verdict'])

    def test_timing_pairs_add_only_the_measured_advertisement(self):
        role_sets = {}
        for variant in matrix.VARIANTS:
            pair = self.pairs[(f'{variant}/base', f'{variant}/measured120')]
            self.assertEqual(pair['causal_steps'], ['timing_advertisement'], variant)
            self.assertTrue(pair['all_changed_bytes_explained'], variant)
            self.assertEqual(pair['unexplained'], [], variant)
            joined = ' | '.join(row['role'] for row in pair['logical'])
            for expected in ('FD max vertical rate (Hz)', 'FD max horizontal rate (kHz)',
                             'FD max pixel clock (MHz / 10)', 'Short Video Descriptor',
                             'Maximum TMDS frequency byte', 'data block header (tag 2, length 13)'):
                self.assertIn(expected, joined, f'{variant}: {expected}')
            self.assertEqual(len(pair['checksum_recompute']), 2, variant)
            self.assertEqual(offsets(pair['checksum_recompute']), [127, 255], variant)
            for row in pair['logical']:
                self.assertFalse(row['role'].startswith(matrix.MICROSOFT_HMD_NAME), variant)
            for row in pair['byte_shift']:
                self.assertGreater(row['block_shift'], 0, variant)
            self.assertEqual(pair['blocks_inserted'], [], variant)
            self.assertEqual(pair['blocks_removed'], [], variant)
            role_sets[variant] = sorted(row['role'] for row in pair['logical'])
        self.assertEqual(role_sets['hmd_v2_usage07'], role_sets['hmd_v2_usage27'])
        self.assertIn('HMD VSDB classification unchanged',
                      self.pairs[('original/base', 'original/measured120')]['verdict'])

    def test_proposal_pair_matches_the_generator_semantic_view(self):
        """The base -> measured120 pair must agree with the reused generator's own semantics."""
        for variant in matrix.VARIANTS:
            cell = self.cells[f'{variant}/measured120']
            base = self.cells[f'{variant}/base']
            pair = self.pairs[(f'{variant}/base', f'{variant}/measured120')]
            semantics = cell['proposal120_generator_semantics']
            generator_offsets = sorted(row['offset'] if 'offset' in row else row['new_offset']
                                       for row in semantics['diff_reasons'])
            mine = sorted(offsets(pair['logical']) + offsets(pair['checksum_recompute']))
            self.assertEqual(generator_offsets, mine, variant)
            audit = blocks.audit(blob_of(base), blob_of(cell))
            shifts = offsets(pair['byte_shift'])
            self.assertEqual(sorted(set(audit['changed_offsets']) - set(mine)), shifts, variant)
            self.assertEqual(pair['unexplained'], [], variant)
            self.assertEqual(semantics['advertisement']['vic']['label'], '1920x1080p120', variant)
            self.assertEqual(semantics['advertisement']['after']['max_vertical_hz'], 120, variant)
            self.assertIn('NOT hardware-apply-ready', ' '.join(semantics['constraints']), variant)

    def test_fd_maxima_and_vic63_only_where_the_proposal_was_generated(self):
        original_caps = matrix.capabilities(ORIGINAL)
        for cell in self.report['matrix']:
            caps = cell['capabilities']
            if cell['column'] == 'base':
                self.assertEqual(caps['fd_range_limits']['max_vertical_hz'],
                                 proposal.BASELINE['max_vertical_hz'], cell['cell_id'])
                self.assertEqual(caps['fd_range_limits']['max_horizontal_khz'],
                                 proposal.BASELINE['max_horizontal_khz'], cell['cell_id'])
                self.assertEqual(caps['hdmi_vsdb_max_tmds_mhz'],
                                 proposal.BASELINE['vsdb_max_tmds_byte'] * 5, cell['cell_id'])
                self.assertFalse(caps['vic63_listed'], cell['cell_id'])
                self.assertEqual(caps['video_vics'], original_caps['video_vics'], cell['cell_id'])
            else:
                for key in ('max_vertical_hz', 'max_horizontal_khz', 'max_pixel_clock_mhz'):
                    self.assertEqual(caps['fd_range_limits'][key], proposal.PROPOSED[
                        {'max_vertical_hz': 'max_vertical_hz', 'max_horizontal_khz': 'max_horizontal_khz',
                         'max_pixel_clock_mhz': 'max_pixel_clock_mhz'}[key]], cell['cell_id'])
                self.assertEqual(caps['hdmi_vsdb_max_tmds_byte'],
                                 proposal.PROPOSED['vsdb_max_tmds_byte'], cell['cell_id'])
                self.assertEqual(caps['video_vics'][:-1], original_caps['video_vics'], cell['cell_id'])
                self.assertEqual(caps['video_vics'][-1], 63, cell['cell_id'])
                self.assertNotIn(63, caps['video_native_vics'], cell['cell_id'])
                self.assertEqual(caps['fd_range_limits']['min_vertical_hz'],
                                 original_caps['fd_range_limits']['min_vertical_hz'], cell['cell_id'])
                self.assertEqual(caps['fd_range_limits']['min_horizontal_khz'],
                                 original_caps['fd_range_limits']['min_horizontal_khz'], cell['cell_id'])

    def test_cta_blocks_survive_the_proposal_shifted_not_rewritten(self):
        for variant in matrix.VARIANTS:
            before = list(blocks.walk_cta_blocks(blob_of(self.cells[f'{variant}/base'])[128:256]))
            after = list(blocks.walk_cta_blocks(blob_of(self.cells[f'{variant}/measured120'])[128:256]))
            self.assertEqual(len(before), len(after), variant)
            for (old_pos, old_tag, old_payload), (new_pos, new_tag, new_payload) in zip(before, after):
                self.assertEqual(old_tag, new_tag, variant)
                self.assertIn(new_pos - old_pos, (0, 1), variant)
                if old_tag == 2:
                    self.assertEqual(new_payload, old_payload + bytes([63]), variant)
                elif new_payload[:3] == bytes.fromhex('030c00'):
                    self.assertEqual(new_payload[6], proposal.PROPOSED['vsdb_max_tmds_byte'], variant)
                    self.assertEqual(new_payload[:6], old_payload[:6], variant)
                else:
                    self.assertEqual(new_payload, old_payload, variant)

    def test_causal_step_separation_is_computed_and_disjoint(self):
        separation = self.report['causal_step_separation']
        presence = [tuple(pair[0]) for pair in separation['classification_hmd_vsdb_presence_pairs']]
        usage = [tuple(pair[0]) for pair in separation['classification_usage_only_pairs']]
        timing = [tuple(pair[0]) for pair in separation['timing_advertisement_only_pairs']]
        self.assertEqual(len(presence), 2)
        self.assertEqual(len(usage), 2)
        self.assertEqual(len(timing), 3)
        self.assertFalse(set(presence) & set(usage))
        self.assertFalse(set(timing) & (set(usage) | set(presence)))
        self.assertEqual(separation['pairs_mixing_steps'], [])
        self.assertEqual({c['cell_id'] for c in separation['cells_mixing_steps']},
                         {'hmd_v2_usage07/measured120', 'hmd_v2_usage27/measured120'})
        self.assertIn('different causal steps', separation['rule'])

    def test_semantic_report_denies_apply_readiness_and_mode_proof(self):
        for cell in self.report['matrix']:
            semantic = cell['semantic']
            self.assertFalse(semantic['apply_ready'], cell['cell_id'])
            self.assertFalse(semantic['mode_usability_proven'], cell['cell_id'])
            self.assertFalse(semantic['written_by_this_tool'], cell['cell_id'])
            text = ' '.join(semantic['human_lines']).lower()
            self.assertIn('not apply-ready', text, cell['cell_id'])
            self.assertIn('no proof of mode usability', text, cell['cell_id'])
            if cell['cell_id'] == 'original/base':
                self.assertEqual(semantic['candidate_kind'], 'captured_baseline')
            elif cell['cell_id'] == 'hmd_v2_usage07/base' and PLAN.get('applied'):
                self.assertEqual(semantic['candidate_kind'],
                                 'recorded_current_state_of_the_captured_instance')
                self.assertTrue(semantic['plan_log_records_applied'])
                self.assertFalse(semantic['proposal_only'])
                self.assertIn('live registry state is not read', text)
            else:
                self.assertEqual(semantic['candidate_kind'], 'proposal_only', cell['cell_id'])
                self.assertTrue(semantic['proposal_only'], cell['cell_id'])
                if cell['cell_id'] == 'hmd_v2_usage07/base':
                    self.assertFalse(semantic['plan_log_records_applied'])
        safety = self.report['safety']
        self.assertTrue(safety['offline_analysis_only'])
        self.assertTrue(safety['inputs_read_only'])
        for flag in ('any_candidate_written_or_applied', 'apply_ready', 'mode_usability_proven',
                     'registry_or_gpu_or_runtime_access', 'uac_elevation_used', 'hardware_access',
                     'power_or_display_mode_changed', 'ownership_or_mode_commands_issued'):
            self.assertFalse(safety[flag], flag)
        self.assertFalse(self.report['inputs']['plan_log']['live_registry_state_read'])
        claims = ' '.join(self.report['prohibited_claims']).lower()
        self.assertIn('120 hz', claims)
        self.assertIn('90 hz', claims)
        self.assertIn('apply-ready', claims)

    def test_measured_timing_is_quoted_only_from_the_validated_candidate(self):
        cell = self.cells['hmd_v2_usage07/measured120']
        vic = cell['proposal120_generator_semantics']['advertisement']['vic']
        reference = CANDIDATE['nvidia_manual_timing_reference']
        self.assertEqual(vic['timing']['h_total'], reference['h_total'])
        self.assertEqual(vic['timing']['v_total'], reference['v_total'])
        self.assertEqual(vic['timing']['expected_pixel_clock_hz'], reference['expected_pixel_clock_hz'])
        text = json.dumps(cell['semantic']).lower()
        self.assertIn('long_term_stability_verified=false', text)
        self.assertIs(self.report['inputs']['measured120_candidate']['long_term_stability_verified'], False)
        self.assertIn('unverified', ' '.join(self.report['limitations']).lower())

    def test_all_reported_invariants_hold_for_the_real_evidence(self):
        self.assertEqual(self.report['matrix_status'], 'complete')
        for name, value in self.report['invariants'].items():
            self.assertTrue(value, name)

    def test_deterministic_and_inputs_untouched(self):
        before = (PLAN_PATH.read_bytes(), CAND_PATH.read_bytes())
        again = matrix.build_matrix(str(PLAN_PATH), str(CAND_PATH))
        self.assertEqual(json.dumps(again, sort_keys=True), json.dumps(self.report, sort_keys=True))
        self.assertEqual((PLAN_PATH.read_bytes(), CAND_PATH.read_bytes()), before)

    def test_unwalkable_cta_is_reported_not_absorbed(self):
        broken = bytearray(PATCHED_FULL)
        broken[130] = 100                      # declared end of blocks overshoots the collection
        broken[170] ^= 0xFF                    # a byte inside the Microsoft HMD VSDB payload
        result = matrix.analyze_pair(PATCHED_FULL, fix_checksums(bytes(broken)))
        self.assertTrue(result['cta_walk_note'])
        self.assertFalse(result['all_changed_bytes_explained'])
        self.assertIn(170, offsets(result['unexplained']))
        self.assertIn('not explained', ' '.join(row['role'] for row in result['unexplained']))
        self.assertIn('review before', result['verdict'])

    def test_pure_offline_and_no_hardcoded_edid_literals(self):
        source = (TOOLS / 'edid_trial_matrix.py').read_text(encoding='utf-8')
        for forbidden in ('winreg', 'ctypes', 'subprocess', 'socket', 'urllib', 'requests', 'RegOpenKey',
                          'EnumDisplay', 'ChangeDisplaySettings', 'QueryDisplayConfig', 'SetupDi', 'pip'):
            self.assertNotIn(forbidden, source)
        self.assertIsNone(re.search(r'[0-9a-fA-F]{32,}', source))    # no embedded synthetic baseline


class EvidenceShapeAndLabelTests(unittest.TestCase):
    """Byte-role labels, column-aware semantics and strict input shapes."""

    @classmethod
    def setUpClass(cls):
        cls.report = matrix.build_matrix(str(PLAN_PATH), str(CAND_PATH))
        cls.lines = {c['cell_id']: ' '.join(c['semantic']['human_lines']).lower()
                     for c in cls.report['matrix']}

    def label(self, index):
        return matrix.base_role(ORIGINAL, index)

    def test_base_block_byte_roles_match_the_measured_layout(self):
        self.assertIn('established timings', self.label(35))
        self.assertIn('established timings', self.label(37))
        self.assertNotIn('reserved', self.label(35))
        self.assertIn('standard timing identifiers', self.label(38))
        self.assertIn('standard timing identifiers', self.label(53))
        self.assertIn('chromaticity', self.label(34))
        caps = matrix.capabilities(ORIGINAL)
        self.assertEqual(caps['established_timings_hex'], ORIGINAL[35:38].hex())
        self.assertEqual(caps['standard_timing_ids_hex'], ORIGINAL[38:54].hex())
        self.assertNotEqual(caps['standard_timing_ids_hex'], ORIGINAL[36:54].hex())
        self.assertIn('established_timings_hex', matrix.CAPABILITY_FIELDS)
        self.assertIn('established_timings_hex', matrix.TIMING_FIELDS)

    def test_cta_header_and_fd_byte_roles(self):
        self.assertIn('extension block tag', matrix.CTA_HEADER_ROLES[128])
        self.assertIn('revision', matrix.CTA_HEADER_ROLES[129])
        self.assertNotIn('DTD start', matrix.CTA_HEADER_ROLES[129])
        pair = matrix.analyze_pair(ORIGINAL, bytes.fromhex(mutate_hex(PLAN['raw_edid_hex'], 129, 0x04)))
        row = next(row for row in pair['logical'] if row['offset'] == 129)
        self.assertEqual(row['role'], matrix.CTA_HEADER_ROLES[129])
        self.assertEqual(row['new'], '04')
        fd = matrix.capabilities(ORIGINAL)['fd_range_limits']
        self.assertEqual(fd['reserved_byte4'], ORIGINAL[112])
        self.assertEqual(fd['secondary_timing_byte'], ORIGINAL[119])
        self.assertNotIn('min pixel clock', self.label(112))
        self.assertIn('reserved', self.label(112))
        self.assertIn('secondary timing information', self.label(119))
        self.assertIn('max pixel clock', self.label(117))

    def test_ff_descriptor_is_the_serial_number_string(self):
        fixture = bytearray(ORIGINAL)
        fixture[108:126] = bytes([0, 0, 0, 0xFF]) + b'SERIAL12345678'
        kinds = [d['kind'] for d in matrix.parse_descriptors(fix_checksums(bytes(fixture)))]
        self.assertIn('display product serial number string descriptor', kinds)
        self.assertNotIn('reserved descriptor', kinds)

    def test_semantic_lines_are_column_aware(self):
        self.assertIn('comparison baseline itself', self.lines['original/base'])
        self.assertIn('not a candidate to write anywhere', self.lines['original/base'])
        self.assertIn('byte-identical to the capture', self.lines['hmd_v2_usage07/base'])
        for cell_id in ('original/measured120', 'hmd_v2_usage07/measured120', 'hmd_v2_usage27/measured120'):
            text = self.lines[cell_id]
            self.assertNotIn('comparison baseline', text, cell_id)
            self.assertNotIn('byte-identical to the capture', text, cell_id)
            self.assertIn('range-limits maxima and its recomputed checksum', text, cell_id)
        self.assertIn('classification step is absent', self.lines['original/measured120'])
        self.assertIn('not the plan log baseline', self.lines['original/measured120'])

    def test_verdict_is_not_called_identical_when_bytes_differ(self):
        """A byte outside every projection is still a real byte edit, never a no-op."""
        edited = bytes.fromhex(mutate_hex(PLAN['raw_edid_hex'], 24, ORIGINAL[24] ^ 0x01))
        pair = matrix.analyze_pair(ORIGINAL, edited)
        self.assertEqual(pair['causal_steps'], ['no_semantic_change'])
        self.assertEqual(pair['changed_offsets'], [24, 127])
        self.assertTrue(pair['identity_and_serial_unchanged'])
        self.assertIn('raw byte edits', pair['verdict'])
        self.assertNotIn('identical bytes', pair['verdict'])
        self.assertIn('identical bytes', matrix.analyze_pair(ORIGINAL, ORIGINAL)['verdict'])

    def test_equivalent_guid_spelling_is_accepted_and_a_real_change_still_fails(self):
        sloppy = str(CONTAINER_GUID)                       # lowercase, brace-free, same UUID
        self.assertNotEqual(sloppy, PLAN['container_guid'])
        with tempfile.TemporaryDirectory() as tmp:
            report = matrix.build_matrix(write_json(tmp, 'plan.json', plan_with(container_guid=sloppy)),
                                         str(CAND_PATH))
        check = report['baseline_check']
        self.assertTrue(check['container_guid_matches_plan'])
        self.assertEqual(check['plan_container_guid'], sloppy)
        self.assertEqual(check['plan_container_guid_canonical'], PLAN['container_guid'])
        self.assertEqual(check['regenerated_block_container_guid'], PLAN['container_guid'])
        self.assertEqual(report['matrix_status'], 'complete')
        other = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(matrix.TrialMatrixBlockedError) as caught:
                matrix.build_matrix(write_json(tmp, 'plan.json', plan_with(container_guid=other)),
                                    str(CAND_PATH))
        self.assertIn('does not reproduce', str(caught.exception).lower())


class UnsupportedBaselineTests(unittest.TestCase):
    """A plan log that is not the measured, self-consistent capture refuses everything."""

    def blocked(self, plan, needle, candidate=None):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_json(tmp, 'plan.json', plan)
            cand = write_json(tmp, 'cand.json', candidate or CANDIDATE)
            with self.assertRaises(matrix.TrialMatrixBlockedError) as caught:
                matrix.build_matrix(path, cand)
        if needle:
            self.assertIn(needle, str(caught.exception).lower())

    def test_missing_fields_truncated_capture_and_bad_guid(self):
        self.blocked(plan_with(raw_edid_hex=None), 'non-empty string')
        self.blocked(plan_with(patched_block_hex=None), 'non-empty string')
        self.blocked(plan_with(container_guid=None), 'non-empty string')
        self.blocked(plan_with(raw_edid_hex=ORIGINAL[:255].hex()), '256-byte')
        self.blocked(plan_with(raw_edid_hex='zz' * 256), '256-byte')
        self.blocked(plan_with(patched_block_hex=ORIGINAL[128:200].hex()), '128-byte extension')
        self.blocked(plan_with(container_guid='not-a-guid'), 'not a uuid')
        self.blocked(plan_with(container_guid='{00000000-0000-0000-0000-000000000000}'), 'empty')

    def test_identity_serial_checksum_and_advertisement_are_still_checked(self):
        moved_identity = mutate_hex(PLAN['raw_edid_hex'], 10, 0x00)
        self.blocked(plan_with(raw_edid_hex=moved_identity, effective_edid_hex=moved_identity), 'sony')
        moved_serial = mutate_hex(PLAN['raw_edid_hex'], 12, 0x02)
        self.blocked(plan_with(raw_edid_hex=moved_serial, effective_edid_hex=moved_serial), 'serial')
        moved_fd = mutate_hex(PLAN['raw_edid_hex'], 114, 34)         # FD max vertical no longer 61 Hz
        self.blocked(plan_with(raw_edid_hex=moved_fd, effective_edid_hex=moved_fd),
                     'fd descriptor advertises')
        broken_dtd = mutate_hex(PLAN['raw_edid_hex'], 54, 0x03)
        self.blocked(plan_with(raw_edid_hex=broken_dtd, effective_edid_hex=broken_dtd), '60 hz dtds')
        unchecksummed = corrupt_hex(PLAN['raw_edid_hex'], 20, ORIGINAL[20] ^ 0x01)
        self.blocked(plan_with(raw_edid_hex=unchecksummed, effective_edid_hex=unchecksummed), 'checksum')

    def test_plan_log_conflicts(self):
        self.blocked(plan_with(patched_block_hex=mutate_hex(PLAN['patched_block_hex'], 40, 0xEE)),
                     'does not reproduce')
        self.blocked(plan_with(effective_edid_hex=mutate_hex(PLAN['effective_edid_hex'], 20, 0x11)),
                     'ambiguous')
        self.blocked(plan_with(other_overrides={'OtherMonitor': 'deadbeef'}), 'other edid overrides')

    def test_capture_that_already_has_the_block_refuses_a_second_insertion(self):
        patched = PATCHED_FULL.hex()
        with tempfile.TemporaryDirectory() as tmp:
            path = write_json(tmp, 'plan.json',
                              plan_with(raw_edid_hex=patched, effective_edid_hex=patched))
            with self.assertRaises(matrix.TrialMatrixBlockedError) as caught:
                matrix.build_matrix(path, str(CAND_PATH))
        self.assertIn('already present', str(caught.exception).lower())

    def test_unreadable_or_malformed_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(matrix.TrialMatrixBlockedError) as caught:
                matrix.build_matrix(str(Path(tmp) / 'absent.json'), str(CAND_PATH))
            self.assertIn('cannot be read', str(caught.exception))
            broken = Path(tmp) / 'broken.json'
            broken.write_text('{ not json', encoding='utf-8')
            with self.assertRaises(matrix.TrialMatrixBlockedError) as caught:
                matrix.build_matrix(str(broken), str(CAND_PATH))
            self.assertIn('not readable json', str(caught.exception).lower())
            not_an_object = write_json(tmp, 'list.json', [1, 2, 3])
            with self.assertRaises(matrix.TrialMatrixBlockedError) as caught:
                matrix.build_matrix(not_an_object, str(CAND_PATH))
            self.assertIn('json object', str(caught.exception).lower())

    def blocked_candidate(self, payload, needle):
        with tempfile.TemporaryDirectory() as tmp:
            plan = write_json(tmp, 'plan.json', plan_with())
            cand = Path(tmp) / 'cand.json'
            cand.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding='utf-8')
            with self.assertRaises(matrix.TrialMatrixBlockedError) as caught:
                matrix.build_matrix(plan, str(cand))
        self.assertIn(needle, str(caught.exception).lower())

    def test_wrong_json_field_types_are_blocked_not_crashing(self):
        self.blocked(plan_with(raw_edid_hex=12345), 'non-empty string')
        self.blocked(plan_with(patched_block_hex=[ORIGINAL[128:256].hex()]), 'non-empty string')
        self.blocked(plan_with(container_guid=CONTAINER_GUID.int), 'non-empty string')
        self.blocked(plan_with(effective_edid_hex=12), 'string or null')
        self.blocked(plan_with(other_overrides='yes'), 'object or null')

    def test_candidate_shapes_are_checked_before_the_gates(self):
        self.blocked_candidate('[1, 2, 3]', 'json object')
        self.blocked_candidate('"just a string"', 'json object')
        broken = copy.deepcopy(CANDIDATE)
        broken['validation'] = 'verified once'
        self.blocked_candidate(broken, 'validation must be an object')
        broken = copy.deepcopy(CANDIDATE)
        broken['validation']['dimensions'] = 1920
        self.blocked_candidate(broken, 'dimensions must be a list')


class BlockedProposalColumnTests(unittest.TestCase):
    """A refused candidate blocks only the proposal column; no bytes or timing are invented."""

    def report_for(self, candidate):
        with tempfile.TemporaryDirectory() as tmp:
            return matrix.build_matrix(str(PLAN_PATH), write_json(tmp, 'cand.json', candidate))

    def test_invalid_candidate_timing_blocks_the_column(self):
        for changes in ({'h_total': 2201}, {'expected_pixel_clock_hz': 296999999},
                        {'h_sync_polarity': 'negative'}, {'v_front_porch': 5}):
            report = self.report_for(candidate_with(timing_changes=changes))
            self.assertEqual(report['matrix_status'], 'partial', changes)
            column = report['inputs']['measured120_candidate']
            self.assertEqual(column['column_status'], 'blocked_by_gate_before_generation', changes)
            self.assertIn('does not match', column['fail_closed_gate_refusal'], changes)
            for cell in report['matrix']:
                if cell['column'] == 'base':
                    self.assertEqual(cell['status'], 'generated', changes)
                    continue
                self.assertEqual(cell['status'], 'blocked', changes)
                self.assertIsNone(cell['edid_hex'], changes)
                self.assertIsNone(cell['edid_sha256'], changes)
                self.assertIsNone(cell['delta_vs_baseline'], changes)
                self.assertNotIn('proposal120_generator_semantics', cell, changes)
                text = ' '.join(cell['semantic']['human_lines']).lower()
                self.assertIn('no timing is quoted', text, changes)
            unavailable = [p for p in report['pairwise_causal_diffs'] if p['status'] == 'unavailable']
            self.assertEqual(len(unavailable), 4, changes)
            for pair in unavailable:
                self.assertIn('no generated bytes', pair['reason'])

    def test_unverified_refresh_or_dimensions_block_the_column(self):
        report = self.report_for(candidate_with(validation_changes={'gpu_path_and_signal_refresh_hz': 60}))
        self.assertEqual(report['matrix_status'], 'partial')
        self.assertIn('verified 1920x1080',
                      report['inputs']['measured120_candidate']['fail_closed_gate_refusal'].lower())
        report = self.report_for(candidate_with(validation_changes={'dimensions': [1280, 720]}))
        self.assertEqual(report['matrix_status'], 'partial')

    def test_missing_candidate_file_is_a_hard_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(matrix.TrialMatrixBlockedError) as caught:
                matrix.build_matrix(str(PLAN_PATH), str(Path(tmp) / 'absent.json'))
        self.assertIn('cannot be read', str(caught.exception))

    def test_column_can_be_excluded_on_request(self):
        report = matrix.build_matrix(str(PLAN_PATH), str(CAND_PATH), with_120=False)
        self.assertEqual(report['matrix_status'], 'complete_with_excluded_measured120_column')
        self.assertEqual(report['excluded_cells'], 3)
        for cell in report['matrix']:
            if cell['column'] == 'measured120':
                self.assertEqual(cell['status'], 'excluded')
                self.assertIn('excluded on request', ' '.join(cell['semantic']['human_lines']))
                self.assertIn('no judgement', cell['blocked_reason'])
            else:
                self.assertEqual(cell['status'], 'generated')
        self.assertEqual([p['pair'] for p in report['pairwise_causal_diffs']],
                         [['original/base', 'hmd_v2_usage07/base'],
                          ['original/base', 'hmd_v2_usage27/base'],
                          ['hmd_v2_usage07/base', 'hmd_v2_usage27/base']])

    def test_no_hex_mode_keeps_sha_and_deltas(self):
        report = matrix.build_matrix(str(PLAN_PATH), str(CAND_PATH), include_hex=False)
        for cell in report['matrix']:
            self.assertNotIn('edid_hex', cell)
            if cell['status'] == 'generated':
                self.assertEqual(len(cell['edid_sha256']), 64)
        self.assertTrue(all(p['status'] == 'generated' for p in report['pairwise_causal_diffs']))


class CliTests(unittest.TestCase):
    """Read-only by default; --out writes one NEW file and never overwrites evidence."""

    def run_cli(self, argv):
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = matrix.main(argv)
        return code, stream.getvalue()

    def base_args(self):
        return ['--plan', str(PLAN_PATH), '--candidate', str(CAND_PATH)]

    def test_default_writes_nothing_and_is_complete(self):
        before = hashlib.sha256(PLAN_PATH.read_bytes()).hexdigest()
        code, out = self.run_cli(self.base_args())
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report['kind'], 'edid_trial_matrix')
        self.assertEqual(len(report['matrix']), 6)
        self.assertNotIn('written', report)
        self.assertEqual(hashlib.sha256(PLAN_PATH.read_bytes()).hexdigest(), before)
        self.assertEqual(hashlib.sha256(CAND_PATH.read_bytes()).hexdigest(),
                         report['inputs']['measured120_candidate']['sha256'])

    def test_out_requires_new_file_and_existing_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / matrix.MATRIX_NAME
            code, _ = self.run_cli(self.base_args() + ['--out', str(target)])
            self.assertEqual(code, 0)
            self.assertTrue(target.is_file())
            report = json.loads(target.read_text(encoding='utf-8'))
            self.assertEqual(len(bytes.fromhex(report['matrix'][0]['edid_hex'])), 256)
            self.assertTrue(report['invariants']['every_changed_byte_explained_in_every_pair'])
            with self.assertRaises(SystemExit):                     # refuses to overwrite evidence
                self.run_cli(self.base_args() + ['--out', str(target)])
            with self.assertRaises(SystemExit):                     # never creates directories
                self.run_cli(self.base_args() + ['--out', str(Path(tmp) / 'missing' / 'm.json')])

    def test_blocked_inputs_report_and_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = plan_with(patched_block_hex=mutate_hex(PLAN['patched_block_hex'], 40, 0xEE))
            code, out = self.run_cli(['--plan', write_json(tmp, 'plan.json', plan),
                                      '--candidate', str(CAND_PATH)])
            self.assertEqual(code, 1)
            blocked = json.loads(out)
            self.assertEqual(blocked['status'], 'blocked')
            self.assertIsNone(blocked['matrix'])
            self.assertIn('does not reproduce', blocked['reason'])
            self.assertNotIn('edid_hex', json.dumps(blocked))

    def test_malformed_input_shapes_exit_blocked(self):
        """User-supplied JSON of the wrong shape is a controlled refusal, never a traceback."""
        with tempfile.TemporaryDirectory() as tmp:
            plan = write_json(tmp, 'plan.json', plan_with(raw_edid_hex=12345))
            code, out = self.run_cli(['--plan', plan, '--candidate', str(CAND_PATH)])
            self.assertEqual(code, 1)
            blocked = json.loads(out)
            self.assertEqual(blocked['status'], 'blocked')
            self.assertIn('non-empty string', blocked['reason'])
            cand = Path(tmp) / 'scalar.json'
            cand.write_text('42', encoding='utf-8')
            code, out = self.run_cli(['--plan', str(PLAN_PATH), '--candidate', str(cand)])
            self.assertEqual(code, 1)
            self.assertIn('json object', json.loads(out)['reason'].lower())

    def test_skip_120_flag_and_summary_lines(self):
        code, out = self.run_cli(self.base_args() + ['--skip-120'])
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report['inputs']['measured120_candidate']['column_status'], 'excluded_by_flag')
        self.assertEqual(report['generated_cells'], 3)
        self.assertTrue(any('excluded' in line for line in report['summary']))
        self.assertTrue(any('no mode usability is proven' in line for line in report['summary']))


if __name__ == '__main__':
    unittest.main()
