# SPDX-License-Identifier: MIT
"""Synthetic offline tests for the 1080p120 EDID proposal.

Nothing is applied or fetched: inputs come from the versioned sanitized
fixture (tests/fixtures/edid_plan.json) and candidate files, everything
else is a byte fixture in memory.
"""
import copy
import hashlib
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


proposal = load('edid_120_proposal')
blocks = load('edid_blocks')

FIXTURE_PLAN = HERE / 'tests' / 'fixtures' / 'edid_plan.json'
ORIGINAL, CANDIDATE = proposal.load_evidence(FIXTURE_PLAN,
                                             HERE / proposal.DEFAULT_CANDIDATE)
PATCHED_FULL = ORIGINAL[:128] + bytes.fromhex(
    json.loads(FIXTURE_PLAN.read_text(encoding='utf-8-sig'))['patched_block_hex'])
LOG_PATH = str(FIXTURE_PLAN)
CAND_PATH = str(HERE / proposal.DEFAULT_CANDIDATE)
# Intended diff. Base block: exactly the three FD rate bytes plus its
# recomputed checksum. CTA: the semantic edits are byte 2 (end of blocks),
# the Video Data Block header, the appended SVD at 145, the VSDB Max TMDS
# byte at 161 and the recomputed checksum at 255; the rest of the CTA delta
# is only the pure right-shift of the block tail inside the fixed block.
EXPECTED_BASE_DIFF = [114, 116, 117, 127]
SHIFT_START = 145          # video SVD lands here; original tail bytes move right
SEMANTIC_CTA_DIFF = [130, 132, SHIFT_START, 161, 255]


def fix_checksums(data):
    """Return the blob with both block checksums valid again (fixture helper)."""
    d = bytearray(data)
    for i in (0, 128):
        d[i + 127] = (-sum(d[i:i + 127])) & 0xFF
    return bytes(d)


def candidate():
    return copy.deepcopy(CANDIDATE)


def mutate(index, value):
    d = bytearray(ORIGINAL)
    d[index] = value
    return fix_checksums(bytes(d))


def assert_byte_diff_real(test, blob_a, blob_b, rows):
    """byte_diff rows are pure (offset, old, new) physical pairs; compare all three."""
    for row in rows:
        test.assertEqual(int(row['old'], 16), blob_a[row['offset']], f"old at {row['offset']}")
        test.assertEqual(int(row['new'], 16), blob_b[row['offset']], f"new at {row['offset']}")


def assert_semantic_pairs(test, blob_a, blob_b, entries):
    """edit: same index; insert: old=None; field_shift: original vs new offsets."""
    for row in entries:
        if row['kind'] == 'insert':
            test.assertIsNone(row['old'])
            test.assertEqual(int(row['new'], 16), blob_b[row['offset']])
        elif row['kind'] == 'field_shift':
            test.assertEqual(int(row['old'], 16), blob_a[row['original_offset']])
            test.assertEqual(int(row['new'], 16), blob_b[row['new_offset']])
        else:
            test.assertEqual(int(row['old'], 16), blob_a[row['offset']])
            test.assertEqual(int(row['new'], 16), blob_b[row['offset']])


class ProposalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proposed, cls.report = proposal.generate_proposal(ORIGINAL, candidate())

    def test_valid_output_and_checksums(self):
        self.assertEqual(len(self.proposed), 256)
        blocks.require_complete(self.proposed)
        self.assertEqual([sum(self.proposed[i:i + 128]) % 256 for i in (0, 128)], [0, 0])
        self.assertEqual(self.report['checksums'], [0, 0])

    def test_diff_allowlist_only_expected_bytes(self):
        audit = blocks.audit(ORIGINAL, self.proposed)
        self.assertEqual(audit['changed_base'], EXPECTED_BASE_DIFF)
        # Nothing in the CTA block changed outside byte 2, the VDB header,
        # and the right-shifted tail window (plus the recomputed checksum).
        self.assertTrue(set(audit['changed_cta']) <= {130, 132, 255} | set(range(SHIFT_START, 255)))
        # The tail shift is content-preserving, not a rewrite (the single
        # VSDB edit at 161 is asserted separately in test_cta_blocks...).
        self.assertEqual(self.proposed[SHIFT_START + 1:161], ORIGINAL[SHIFT_START:160])
        self.assertEqual(self.proposed[162:255], ORIGINAL[161:254])
        self.assertEqual(self.proposed[SHIFT_START], 0x3F)
        self.assertEqual(self.proposed[161], 0x3C)                  # 300 MHz
        self.assertEqual(self.proposed[132], 0x4D)                  # tag 2, length 13
        self.assertEqual(self.proposed[130], ORIGINAL[130] + 1)
        self.assertEqual(sorted((row['offset'] if 'offset' in row else row['new_offset'])
                                for row in self.report['diff_reasons']),
                         EXPECTED_BASE_DIFF + SEMANTIC_CTA_DIFF)
        self.assertTrue(audit['checksums_valid'])

    def test_byte_diff_rows_are_real_physical_pairs(self):
        rows = self.report['byte_diff']
        audit = blocks.audit(ORIGINAL, self.proposed)
        self.assertEqual([row['offset'] for row in rows], audit['changed_offsets'])
        assert_byte_diff_real(self, ORIGINAL, self.proposed, rows)
        untouched = set(range(256)) - {row['offset'] for row in rows}
        for i in sorted(untouched):
            self.assertEqual(ORIGINAL[i], self.proposed[i], f'offset {i} should not differ')
        # The index where the SVD lands previously held the audio header, not padding.
        self.assertEqual(ORIGINAL[SHIFT_START], 0x23)
        self.assertEqual(self.proposed[SHIFT_START], 0x3F)

    def test_semantic_kinds_honest_insertion_and_field_shift(self):
        entries = self.report['diff_reasons']
        assert_semantic_pairs(self, ORIGINAL, self.proposed, entries)
        inserts = [e for e in entries if e['kind'] == 'insert']
        self.assertEqual(len(inserts), 1)
        self.assertIsNone(inserts[0]['old'])                        # no fake '00' for the SVD
        self.assertEqual(inserts[0]['offset'], SHIFT_START)
        shifted = [e for e in entries if e['kind'] == 'field_shift']
        self.assertEqual(len(shifted), 1)
        self.assertEqual(shifted[0]['original_offset'], 160)        # logical TMDS field before
        self.assertEqual(shifted[0]['new_offset'], 161)             # ... after the right-shift
        self.assertEqual(int(shifted[0]['old'], 16), ORIGINAL[160])  # 0x1e = measured 150 MHz
        self.assertEqual(int(shifted[0]['new'], 16), self.proposed[161])  # 0x3c = 300 MHz
        self.assertEqual(ORIGINAL[161], 0x0F)                       # unrelated physical byte there
        shifts = self.report['cta_shift']
        self.assertEqual(shifts['original_tail'], [SHIFT_START, 254])
        self.assertEqual(shifts['shifted_into'], [SHIFT_START + 1, 255])

    def test_identity_serial_dtds_and_modes_preserved(self):
        self.assertEqual(self.proposed[:114], ORIGINAL[:114])       # header, Sony D94D:6A04, serial,
                                                                    # size, modes, both 60 Hz DTDs,
                                                                    # name, FD minima
        self.assertEqual(self.proposed[115], ORIGINAL[115])         # FD min horizontal
        self.assertEqual(self.proposed[118:127], ORIGINAL[118:127]) # FD tail and extension count
        self.assertEqual(self.proposed[54:90], ORIGINAL[54:90])     # explicit DTD re-check
        self.assertEqual(self.proposed[90:108], ORIGINAL[90:108])   # "SIE HMD *08"
        self.assertEqual(self.proposed[127], (-sum(self.proposed[:127])) & 0xFF)  # valid re-check

    def test_fd_rate_maxima_raise_to_cover_vic63(self):
        d = self.proposed[108:126]
        self.assertEqual((d[5], d[7]), (23, 26))                    # minima preserved
        self.assertEqual((d[6], d[8], d[9] * 10), (120, 135, 300))  # measured 120 Hz / 135 kHz / 297->300 MHz

    def test_cta_blocks_preserved_and_shifted(self):
        old = list(blocks.walk_cta_blocks(ORIGINAL[128:256]))
        new = list(blocks.walk_cta_blocks(self.proposed[128:256]))
        self.assertEqual(len(old), len(new))
        self.assertEqual(self.proposed[130], ORIGINAL[130] + 1)     # byte 2: end of blocks +1
        for (o_pos, o_tag, o_pay), (n_pos, n_tag, n_pay) in zip(old, new):
            self.assertEqual(o_tag, n_tag)                          # same tags in same order
            self.assertIn(n_pos - o_pos, (0, 1))                    # only shifted, never rewritten
            if o_tag == 2:
                self.assertEqual(n_pay, o_pay + bytes([63]))        # VIC 63 appended last
            elif o_tag == 3 and o_pay[:3] == bytes.fromhex('030c00'):
                self.assertEqual(n_pay[:6], o_pay[:6])              # OUI, address, flags intact
                self.assertEqual(n_pay[6], 60)                      # 150 -> 300 MHz
                self.assertEqual(n_pay[7], o_pay[7])                # following byte intact
            else:
                self.assertEqual(n_pay, o_pay)                      # audio/speaker/extended verbatim

    def test_no_extension_dtds_and_padding_only_appended(self):
        self.assertEqual(self.proposed[131], ORIGINAL[131])         # flags nibble: zero DTDs
        self.assertEqual(self.proposed[145], 0x3F)                  # SVD VIC 63, non-native
        self.assertEqual(self.proposed[166:255], b'\x00' * 89)      # DTD area stays pure padding

    def test_vic63_and_candidate_evidence_in_report(self):
        vic = self.report['advertisement']['vic']
        self.assertEqual(vic['number'], 63)
        self.assertEqual(vic['label'], '1920x1080p120')
        t = vic['timing']
        self.assertEqual((t['h_total'], t['v_total'], t['expected_pixel_clock_hz']),
                         (2200, 1125, 297000000))
        self.assertEqual((t['h_front_porch'], t['h_sync_width'], t['h_back_porch']), (88, 44, 148))
        self.assertEqual((t['v_front_porch'], t['v_sync_width'], t['v_back_porch']), (4, 5, 36))
        self.assertEqual((t['h_sync_polarity'], t['v_sync_polarity']), ('positive', 'positive'))
        self.assertEqual(self.report['advertisement']['before'], dict(proposal.BASELINE))
        self.assertEqual(self.report['generated_from']['original_sha256'],
                         hashlib.sha256(ORIGINAL).hexdigest())

    def test_not_apply_ready_and_scope_constraints(self):
        text = json.dumps(self.report).lower()
        self.assertIn('not hardware-apply-ready', text)
        self.assertIn('long_term_stability_verified is false', text)
        self.assertIn('90 hz is out of scope', text)                # only ever mentioned as excluded
        for reason in self.report['diff_reasons']:
            self.assertNotIn('90', str(reason))

    def test_deterministic_and_inputs_immutable(self):
        again, report2 = proposal.generate_proposal(ORIGINAL, candidate())
        self.assertEqual(again, self.proposed)
        self.assertEqual(report2['diff_reasons'], self.report['diff_reasons'])
        fresh_original, fresh_candidate = proposal.load_evidence(LOG_PATH, CAND_PATH)
        self.assertEqual(fresh_original, ORIGINAL)                  # fixture untouched
        self.assertEqual(fresh_candidate, CANDIDATE)                # plans untouched

    def test_pure_offline_no_forbidden_imports(self):
        source = (TOOLS / 'edid_120_proposal.py').read_text(encoding='utf-8')
        for forbidden in ('winreg', 'ctypes', 'subprocess', 'socket', 'urllib',
                          'requests', 'RegOpenKey', 'EnumDisplay', 'cuda', 'pip'):
            self.assertNotIn(forbidden, source)


class MicrosoftHmdVsdPreservationTests(unittest.TestCase):
    """When the applied MS HMD VSDB is present, it must survive untouched."""

    def test_ms_block_shifted_but_identical(self):
        proposed, report = proposal.generate_proposal(PATCHED_FULL, candidate())
        assert_byte_diff_real(self, PATCHED_FULL, proposed, report['byte_diff'])
        assert_semantic_pairs(self, PATCHED_FULL, proposed, report['diff_reasons'])
        shifted = [e for e in report['diff_reasons'] if e['kind'] == 'field_shift']
        self.assertEqual(len(shifted), 1)
        self.assertEqual(shifted[0]['original_offset'], 160)
        self.assertEqual(shifted[0]['new_offset'], 161)
        inserts = [e for e in report['diff_reasons'] if e['kind'] == 'insert']
        self.assertEqual(len(inserts), 1)
        self.assertIsNone(inserts[0]['old'])
        old = list(blocks.walk_cta_blocks(PATCHED_FULL[128:256]))
        new = list(blocks.walk_cta_blocks(proposed[128:256]))
        self.assertEqual(len(old), len(new))
        self.assertEqual(len(new), 6)
        def ms(blocks_):
            found = [p[2] for p in blocks_ if p[1] == 3 and p[2][:3] == bytes.fromhex('5c12ca')]
            self.assertEqual(len(found), 1)
            return found[0]
        self.assertEqual(ms(new), ms(old))                          # GUID/usage bytes intact
        audit = blocks.audit(PATCHED_FULL, proposed)
        self.assertEqual(audit['changed_base'], EXPECTED_BASE_DIFF)
        # Same semantic edits; everything else is only the tail right-shift.
        self.assertTrue(set(audit['changed_cta']) <= {130, 132, 255} | set(range(SHIFT_START, 255)))
        self.assertEqual(proposed[SHIFT_START + 1:161], PATCHED_FULL[SHIFT_START:160])
        self.assertEqual(proposed[162:255], PATCHED_FULL[161:254])
        self.assertEqual(proposed[161], 0x3C)
        self.assertEqual(audit['changed_cta'][0], 130)              # byte 2 bump present
        self.assertEqual([sum(proposed[i:i + 128]) % 256 for i in (0, 128)], [0, 0])


class BlockedInputTests(unittest.TestCase):
    """Every corrupted input must fail closed with a clear reason."""

    def blocked(self, data, cand=None, needle=''):
        with self.assertRaises(proposal.ProposalBlockedError) as caught:
            proposal.generate_proposal(data, cand or candidate())
        if needle:
            self.assertIn(needle, str(caught.exception).lower())

    def test_checksum_identity_and_truncation(self):
        bad = bytearray(ORIGINAL)
        bad[20] ^= 1                                                # checksum broken on purpose
        self.blocked(bytes(bad), needle='checksum')
        self.blocked(mutate(10, 0x00), needle='product')
        self.blocked(mutate(12, 0x02), needle='serial')
        self.blocked(ORIGINAL[:255], needle='complete')

    def test_unexpected_advertisement_and_layout(self):
        self.blocked(mutate(108 + 6, 34), needle='61')              # FD max vertical moved
        self.blocked(mutate(108 + 4, 0x01), needle='unexpected fd layout')
        self.blocked(mutate(108 + 3, 0xFE), needle='no fd range')   # descriptor type gone
        self.blocked(mutate(54, 0x03), needle='60 hz dtds')         # corrupt 1080p60 DTD

    def test_cta_truncation_padding_and_duplicates(self):
        self.blocked(mutate(130, 100), needle='malformed')          # end beyond walkable blocks
        self.blocked(mutate(130, 0), needle='truncated')
        self.blocked(mutate(250, 0x01), needle='truncat')           # non-zero padding
        self.blocked(mutate(131, 0xF1), needle='detailed timings')  # extension DTDs in use
        self.blocked(mutate(153, 0x48), needle='video data block')  # second video block
        self.blocked(mutate(153, 0xE8), needle='hdmi vsdb')         # HDMI VSDB gone
        self.blocked(mutate(160, 0x3C), needle='not the measured 30')  # VSDB byte already moved
        self.blocked(mutate(133, 0xBF), needle='already present')   # VIC 63 listed next to 61 Hz cap

    def test_candidate_must_match_vic63_evidence(self):
        for key, value in [('h_total', 2201), ('expected_pixel_clock_hz', 296999999),
                           ('h_sync_polarity', 'negative'), ('v_front_porch', 5)]:
            c = candidate()
            c['nvidia_manual_timing_reference'][key] = value
            self.blocked(ORIGINAL, c, needle='does not match')
        c = candidate()
        c['validation'] = dict(c['validation'], gpu_path_and_signal_refresh_hz=60)
        self.blocked(ORIGINAL, c, needle='verified 1920x1080')
        c = candidate()
        del c['nvidia_manual_timing_reference']
        self.blocked(ORIGINAL, c, needle='lacks')


class CliTests(unittest.TestCase):
    """CLI is read-only by default; --out writes a NEW file into an EXISTING parent."""

    def run_cli(self, argv):
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = proposal.main(argv)
        return code, stream.getvalue()

    def test_default_writes_nothing(self):
        before = hashlib.sha256(Path(LOG_PATH).read_bytes()).hexdigest()
        code, out = self.run_cli(['--log', LOG_PATH, '--candidate', CAND_PATH])
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(len(bytes.fromhex(report['proposed_edid_hex'])), 256)
        self.assertNotIn('written', report)                         # stdout only
        self.assertEqual(hashlib.sha256(Path(LOG_PATH).read_bytes()).hexdigest(), before)

    def test_out_requires_new_file_and_existing_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / proposal.PROPOSAL_NAME
            code, _ = self.run_cli(['--log', LOG_PATH, '--candidate', CAND_PATH, '--out', str(target)])
            self.assertEqual(code, 0)
            self.assertTrue(target.is_file())
            report = json.loads(target.read_text(encoding='utf-8'))
            self.assertEqual(len(bytes.fromhex(report['proposed_edid_hex'])), 256)
            with self.assertRaises(SystemExit):                     # refuses to overwrite
                self.run_cli(['--log', LOG_PATH, '--candidate', CAND_PATH, '--out', str(target)])
            with self.assertRaises(SystemExit):                     # never creates directories
                self.run_cli(['--log', LOG_PATH, '--candidate', CAND_PATH,
                              '--out', str(Path(tmp) / 'missing' / 'p.json')])

    def test_blocked_cli_reports_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'bad.json'
            log.write_text(json.dumps({'raw_edid_hex': ORIGINAL[:255].hex()}), encoding='utf-8')
            code, out = self.run_cli(['--log', str(log), '--candidate', CAND_PATH])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(out)['status'], 'blocked')


if __name__ == '__main__':
    unittest.main()
