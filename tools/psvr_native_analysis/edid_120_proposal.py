# SPDX-License-Identifier: MIT
"""Experimental proposal to advertise the measured HDMI 1080p120 path in EDID.

Read-only, pure offline analysis: this module only parses the two baseline
evidence files, derives a proposed 256-byte EDID in memory and prints or
(optionally, via --out) writes a NEW proposal JSON. It never applies
anything, never touches the registry/GPU/UAC/runtime and is explicitly NOT
hardware-apply-ready: long_term_stability_verified is false in the source
candidate, and 90 Hz is out of scope because it is unverified.

Measured baseline evidence (logs/hmd_edid_override_20261005.json):
raw_edid_hex is the captured raw original Sony EDID, today stored as the
registry override baseline (a cache of the capture, not a live EEPROM
read-out). Identity bytes 8..11 = 4d d9 04 6a (manufacturer field
little-endian 0xd94d, product 0x6a04, "Sony D94D:6A04", monitor path
SNY6A04). It advertises a 1080p60 DTD, a 640x480 DTD, an FD range of max
61 Hz / max 68 kHz / max 150 MHz and an HDMI VSDB Maximum TMDS byte
30 x 5 = 150 MHz, with no VIC 63 listed.

Verified 120 Hz evidence (configurations/candidates/hdmi_native_120hz.json):
GPU path and signal validated at 1920x1080@120 with the documented timing
2200x1125, porches 88/44/148 and 4/5/36, positive/positive, 297 MHz
(nvidia_manual_timing_reference).

Proposed changes (the only permitted diff, every other byte identical):
  1. CTA Video Data Block gains Short Video Descriptor VIC 63 =
     1920x1080p120 (CTA-861 timing identical to the candidate reference).
     Existing SVDs keep their order; audio, speaker allocation, HDMI VSDB,
     the extended-tag block and the Microsoft HMD VSDB (when present) are
     preserved, merely shifted one byte for the insertion.
  2. FD range-limits descriptor raises max vertical 61 -> 120 Hz, max
     horizontal 68 -> 135 kHz and max pixel clock 150 -> 300 MHz, the spec
     rounding of 297 MHz up to the next 10 MHz multiple using the existing
     byte layout (no >255 rate offsets are used).
  3. HDMI VSDB "Maximum TMDS frequency" byte (payload offset 6, value =
     MHz/5, layout verified against the OUI 000C03 block) raises 30 -> 60,
     i.e. 150 -> 300 MHz.
Only the two mandatory block checksums are recomputed: the three FD byte
edits shift the base block sum (+59 +67 +15), so base byte 127 changes too;
nothing else in the base block moves.

Report shape (two honest views of the same change):
  * byte_diff: one row per physically differing index, generated from the
    actual original/result byte pair (offset, old, new).
  * diff_reasons: semantic entries, kind='edit' (same physical index),
    kind='insert' (pure SVD insertion, old=None because no original byte
    belongs at that index) and kind='field_shift' (the HDMI VSDB Maximum
    TMDS logical field: original_offset before the insertion -> new_offset
    after the one-byte right-shift, then edited).
  * cta_shift: documents that the insertion right-shifts the original block
    tail to result positions +1 inside the fixed 128-byte extension; the
    audit enforces that invariant, so no block content is rewritten.

Spec sources consulted for field layouts:
  * VESA E-EDID: Display Range Limits descriptor (bytes 5..9; maximum
    pixel clock rounded up to a 10 MHz multiple; type at byte 3).
  * ANSI/CTA-861-G: extension byte 2 = first DTD byte / end of the data
    block collection; data block tags [7:5]: 1 audio, 2 video (SVDs),
    3 VSDB, 4 speaker allocation, 7 extended tag.
  * HDMI 1.3a VSDB payload: bytes 1-3 IEEE OUI little-endian (000C03 =
    HDMI Licensing), bytes 4-5 source physical address, byte 7 =
    Maximum TMDS frequency / 5 MHz.
  * tools/psvr_native_analysis/hmd_edid.py and
    tools/psvr_display/analyze_reference_edid.py: project-verified layouts
    including the Microsoft HMD VSDB (OUI CA125C) and the label
    "VIC 63 = 1920x1080p120".

Fail-closed: malformed EDID, invalid checksums, wrong identity/serial,
unexpected existing 60 Hz DTD bytes, unexpected baseline advertisement
(61 Hz / 68 kHz / 150 MHz / VSDB 30), truncated or malformed CTA block
collections, non-zero bytes that the insertion would truncate, missing or
duplicated video/HDMI VSDB blocks, extension DTDs in use, an already
present VIC 63 (ambiguous layout), or a candidate timing that does not
match the documented VIC 63 values raise ProposalBlockedError.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

try:
    from . import edid_blocks
except ImportError:  # loaded file-relative, as tests/ does for tools
    import edid_blocks

DEFAULT_LOG = 'logs/hmd_edid_override_20261005.json'
DEFAULT_CANDIDATE = 'configurations/candidates/hdmi_native_120hz.json'
PROPOSAL_NAME = 'edid_120_proposal_20261005.json'

EXPECTED_IDENTITY = bytes.fromhex('4dd9046a')  # Sony D94D:6A04 (bytes 8..11)
EXPECTED_SERIAL = bytes.fromhex('01010101')    # serial of the captured raw original baseline
DTD_1080P60 = bytes.fromhex('023a801871382d40582c4500a05a0000001e')
DTD_640X480 = bytes.fromhex('d60980a020e02d101060a200a05a00000018')

# Documented CTA-861 VIC 63 (1920x1080p120) values. The candidate file must
# match these numbers exactly, or the proposal stays blocked (unverified
# mode -> fail closed, never guess). Keys mirror nvidia_manual_timing_reference.
VIC63 = dict(active_width=1920, active_height=1080, refresh_hz=120,
             scan='progressive', h_front_porch=88, h_sync_width=44, h_back_porch=148,
             h_total=2200, v_front_porch=4, v_sync_width=5, v_back_porch=36, v_total=1125,
             h_sync_polarity='positive', v_sync_polarity='positive',
             expected_pixel_clock_hz=297000000)

BASELINE = dict(max_vertical_hz=61, max_horizontal_khz=68, max_pixel_clock_mhz=150,
                vsdb_max_tmds_byte=30)  # 30 x 5 MHz = 150 MHz
PROPOSED = dict(max_vertical_hz=120, max_horizontal_khz=135, max_pixel_clock_mhz=300,
                vsdb_max_tmds_byte=60)  # 60 x 5 MHz = 300 MHz (297 quantized up)


class ProposalBlockedError(RuntimeError):
    """Raised whenever an input fails validation; the proposal is never guessed."""


def load_evidence(log_path=DEFAULT_LOG, candidate_path=DEFAULT_CANDIDATE):
    """Read the original EDID hex and the 120 Hz candidate offline.

    Returns (original_bytes, candidate_dict). Input plans are only read.
    """
    log = json.loads(Path(log_path).read_text(encoding='utf-8-sig'))
    candidate = json.loads(Path(candidate_path).read_text(encoding='utf-8-sig'))
    hex_text = log.get('raw_edid_hex') or ''
    if not re.fullmatch(r'(?:[0-9a-fA-F]{2}){256}', hex_text):
        raise ProposalBlockedError('raw_edid_hex is missing or not a complete 256-byte EDID')
    return bytes.fromhex(hex_text), candidate


def validate_candidate(candidate):
    """Return the timing dict only if it exactly matches the VIC 63 evidence."""
    if not isinstance(candidate, dict):
        raise ProposalBlockedError('candidate JSON must be an object')
    timing = candidate.get('nvidia_manual_timing_reference')
    if not isinstance(timing, dict):
        raise ProposalBlockedError('candidate lacks nvidia_manual_timing_reference')
    for name, want in VIC63.items():
        got = timing.get(name)
        if got != want:
            raise ProposalBlockedError(f'candidate {name}={got!r} does not match VIC 63 {want!r}')
    # Cross-consistency of the documented numbers (fail closed on typos).
    if timing['h_front_porch'] + timing['h_sync_width'] + timing['h_back_porch'] \
            != timing['h_total'] - timing['active_width']:
        raise ProposalBlockedError('candidate horizontal porches do not fill h_total - active')
    if timing['v_front_porch'] + timing['v_sync_width'] + timing['v_back_porch'] \
            != timing['v_total'] - timing['active_height']:
        raise ProposalBlockedError('candidate vertical porches do not fill v_total - active')
    if timing['expected_pixel_clock_hz'] != timing['h_total'] * timing['v_total'] * timing['refresh_hz']:
        raise ProposalBlockedError('candidate pixel clock is not h_total * v_total * refresh')
    validation = candidate.get('validation') or {}
    if validation.get('gpu_path_and_signal_refresh_hz') != 120 \
            or list(validation.get('dimensions') or ()) != [1920, 1080]:
        raise ProposalBlockedError('candidate does not record a verified 1920x1080 signal at 120 Hz')
    return timing


def validate_identity(data):
    try:
        edid_blocks.require_complete(data)
    except ValueError as error:
        raise ProposalBlockedError(str(error))
    if any(sum(data[i:i + 128]) % 256 for i in range(0, len(data), 128)):
        raise ProposalBlockedError('original EDID block checksums are invalid')
    if data[18:20] != b'\x01\x03':
        raise ProposalBlockedError('only the original EDID 1.3 layout is interpreted')
    if data[8:12] != EXPECTED_IDENTITY:
        raise ProposalBlockedError('manufacturer/product bytes are not the measured Sony D94D:6A04')
    if data[12:16] != EXPECTED_SERIAL:
        raise ProposalBlockedError('serial bytes differ from the captured raw original baseline')


def fd_descriptor(data):
    """Locate the FD range descriptor and verify the documented byte layout."""
    for offset in range(54, 126, 18):
        d = data[offset:offset + 18]
        if d[:3] == b'\x00\x00\x00' and d[3] == 0xFD:
            # byte 4 upper nibble reserved, bytes 4/10 timing-mode bits zero,
            # byte 11 = 0x0A and 12..17 = SP padding as in the original.
            if d[4] & 0xF0 or d[4] & 0x0F or d[10] or d[11] != 0x0A \
                    or bytes(d[12:18]) != b'\x20' * 6:
                raise ProposalBlockedError('unexpected FD layout; rate offsets/timing '
                                           'extensions are not supported')
            return offset, d
    raise ProposalBlockedError('no FD range-limits descriptor found')


def verify_base_advertisement(data):
    """Pin the measured 61 Hz / 68 kHz / 150 MHz baseline and the 60 Hz DTDs."""
    offset, d = fd_descriptor(data)
    got = dict(min_vertical_hz=d[5], max_vertical_hz=d[6], min_horizontal_khz=d[7],
               max_horizontal_khz=d[8], max_pixel_clock_mhz=d[9] * 10)
    want = dict(min_vertical_hz=23, max_vertical_hz=BASELINE['max_vertical_hz'],
                min_horizontal_khz=26, max_horizontal_khz=BASELINE['max_horizontal_khz'],
                max_pixel_clock_mhz=BASELINE['max_pixel_clock_mhz'])
    if got != want:
        raise ProposalBlockedError(f'FD descriptor advertises {got}, measured baseline expected {want}')
    if data[54:72] != DTD_1080P60 or data[72:90] != DTD_640X480:
        raise ProposalBlockedError('existing 60 Hz DTDs are not the measured original bytes')
    return offset


def verify_cta(b):
    """Walk the CTA data block collection; return (blocks, video, hdmi_vsdb)."""
    if b[0] != 2 or b[1] != 3:
        raise ProposalBlockedError('first extension must be a CTA-861 version 3 block')
    end = b[2]
    if not 4 <= end <= 127:
        raise ProposalBlockedError('truncated or out-of-range CTA data block collection')
    if b[3] & 0x0F:
        raise ProposalBlockedError('CTA block contains detailed timings; this layout is not supported')
    blocks, pos = [], 4
    while pos < end:
        tag, length = b[pos] >> 5, b[pos] & 0x1F
        if length == 0 or pos + 1 + length > end:
            raise ProposalBlockedError(f'malformed or truncated CTA data block at byte {pos}')
        blocks.append(dict(offset=pos, tag=tag, length=length,
                           payload=bytes(b[pos + 1:pos + 1 + length])))
        pos += 1 + length
    if pos != end:
        raise ProposalBlockedError('CTA data block collection does not end exactly at byte 2')
    if any(b[i] for i in range(end, 127)):
        raise ProposalBlockedError('non-zero bytes after the blocks would be truncated by the insertion')
    videos = [x for x in blocks if x['tag'] == 2]
    if len(videos) != 1:
        raise ProposalBlockedError(f'exactly one CTA Video Data Block required, found {len(videos)}')
    vsdbs = [x for x in blocks if x['tag'] == 3 and x['payload'][:3] == bytes.fromhex('030c00')]
    if len(vsdbs) != 1:
        raise ProposalBlockedError(f'exactly one HDMI VSDB (OUI 000C03) required, found {len(vsdbs)}')
    return blocks, videos[0], vsdbs[0]


def generate_proposal(original, candidate):
    """Pure function. Returns (proposed_bytes, report) or raises ProposalBlockedError."""
    validate_identity(original)
    fd_offset = verify_base_advertisement(original)
    timing = validate_candidate(candidate)
    blocks, video, vsdb = verify_cta(original[128:256])

    if 63 in [x & 0x7F for x in video['payload']]:
        # VIC 63 already listed: adding it is meaningless while FD/VSDB still
        # cap 61 Hz/150 MHz; that contradictory layout must not be guessed.
        raise ProposalBlockedError('VIC 63 already present next to a 61 Hz/150 MHz advertisement; '
                                   'ambiguous layout, proposal stays blocked')
    if video['length'] > 29:
        raise ProposalBlockedError('Video Data Block SVD count has no room within its 5-bit field')
    if PROPOSED['max_pixel_clock_mhz'] % 10 or PROPOSED['max_pixel_clock_mhz'] // 10 > 255 \
            or PROPOSED['vsdb_max_tmds_byte'] > 255 or PROPOSED['max_horizontal_khz'] > 255 \
            or PROPOSED['max_vertical_hz'] > 255:
        raise ProposalBlockedError('proposed quantized maxima do not fit the existing byte layout')

    proposed = bytearray(original)
    insert_cta = video['offset'] + 1 + video['length']   # cta index where the SVD lands
    insert_at = 128 + insert_cta                         # absolute index of the new byte
    tmds_original_offset = 128 + vsdb['offset'] + 1 + 6  # logical MHz/5 field, original blob
    tmds_index = tmds_original_offset + (1 if vsdb['offset'] >= insert_cta else 0)

    # 1. Video Data Block length 12 -> 13 and appended SVD byte for VIC 63
    #    (not native; the existing native flag on VIC 16 is untouched).
    proposed[128 + video['offset']] = (2 << 5) | (video['length'] + 1)
    proposed.insert(insert_at, 63)
    # Consume one trailing zero-padding byte so the extension stays 128 bytes.
    # verify_cta already proved byte 254 (cta[126]) is zero padding.
    del proposed[255]
    # 2. CTA byte 2 (first DTD byte / end of blocks) moves one byte later.
    proposed[128 + 2] = original[128 + 2] + 1
    # 3. FD maxima raised to cover VIC 63: 120 Hz, 135 kHz, 300 MHz
    #    (= 297 MHz rounded up to the next 10 MHz multiple).
    proposed[fd_offset + 6] = PROPOSED['max_vertical_hz']
    proposed[fd_offset + 8] = PROPOSED['max_horizontal_khz']
    proposed[fd_offset + 9] = PROPOSED['max_pixel_clock_mhz'] // 10
    # 4. HDMI VSDB Maximum TMDS frequency 30 -> 60 (150 -> 300 MHz).
    if proposed[tmds_index] != BASELINE['vsdb_max_tmds_byte']:
        raise ProposalBlockedError('HDMI VSDB Max TMDS byte is not the measured 30 (150 MHz)')
    proposed[tmds_index] = PROPOSED['vsdb_max_tmds_byte']
    # 5. Spec rule: recompute the checksum of every changed block. The FD
    #    edits move the base block sum, so byte 127 is fixed as well.
    proposed[127] = (-sum(proposed[:127])) & 0xFF
    proposed[255] = (-sum(proposed[128:255])) & 0xFF
    result = bytes(proposed)

    # Audit. A mid-block insertion right-shifts every byte of the block tail
    # inside the fixed 128-byte extension, so a per-index allowlist would be
    # misleading: verify the structural invariants instead. Base block must
    # change in exactly the three FD rate bytes plus its checksum byte; every
    # other CTA change must be explained by the header bumps, the appended SVD,
    # the single VSDB edit inside the pure right-shift
    # (result[k] == original[k-1] over the shifted window), and the checksums.
    audit = edid_blocks.audit(original, result)
    expected_base = sorted([fd_offset + 6, fd_offset + 8, fd_offset + 9, 127])
    shift_span = set(range(insert_at, 255))
    allowed_cta = {130, 128 + video['offset'], tmds_index, 255} | shift_span
    if (any(sum(result[i:i + 128]) % 256 for i in (0, 128))
            or audit['changed_base'] != expected_base
            or not set(audit['changed_cta']) <= allowed_cta
            or result[insert_at + 1:tmds_index] != original[insert_at:tmds_index - 1]
            or result[tmds_index + 1:255] != original[tmds_index:254]
            or result[130] != original[130] + 1
            or result[128 + video['offset']] != (2 << 5) | (video['length'] + 1)
            or result[insert_at] != 63
            or result[tmds_index] != PROPOSED['vsdb_max_tmds_byte']):
        raise ProposalBlockedError('audit disagrees with the intended structural diff; '
                                   'refusing to emit')

    def hx(value):
        return f'{value:02x}'

    # Complete raw truth: every physically differing index, as the actual
    # original/result byte pair (e.g. index 145 shows the audio header 23
    # that was overwritten by the inserted SVD, not a fake '00').
    byte_diff = [dict(offset=i, old=hx(original[i]), new=hx(result[i]))
                 for i in range(len(original)) if original[i] != result[i]]
    if [row['offset'] for row in byte_diff] != audit['changed_offsets']:
        raise ProposalBlockedError('byte_diff disagrees with the audit; refusing to emit')

    # Annotated semantic view. kind='edit': same physical index;
    # kind='insert': pure insertion with no original counterpart (old=None);
    # kind='field_shift': logical field moved by the right-shift before being
    # edited (original_offset vs new_offset).
    diff_reasons = sorted([
        dict(kind='edit', offset=fd_offset + 6, old=hx(original[fd_offset + 6]),
             new=hx(result[fd_offset + 6]),
             reason='FD max vertical rate 61 -> 120 Hz to cover the measured 120 Hz signal'),
        dict(kind='edit', offset=fd_offset + 8, old=hx(original[fd_offset + 8]),
             new=hx(result[fd_offset + 8]),
             reason='FD max horizontal rate 68 -> 135 kHz (297 MHz / h_total 2200)'),
        dict(kind='edit', offset=fd_offset + 9, old=hx(original[fd_offset + 9]),
             new=hx(result[fd_offset + 9]),
             reason='FD max pixel clock 150 -> 300 MHz: 297 MHz rounded up to the next 10 MHz '
                    'multiple, as the descriptor encoding requires'),
        dict(kind='edit', offset=127, old=hx(original[127]), new=hx(result[127]),
             reason='mandatory base block checksum recomputation (the FD rate edits '
                    'change the block sum)'),
        dict(kind='edit', offset=130, old=hx(original[130]), new=hx(result[130]),
             reason='CTA byte 2 (first DTD byte / end of data blocks) bumps +1 after the '
                    'insertion'),
        dict(kind='edit', offset=128 + video['offset'], old=hx(original[128 + video['offset']]),
             new=hx(result[128 + video['offset']]),
             reason='CTA Video Data Block length 12 -> 13 for the added SVD'),
        dict(kind='insert', offset=insert_at, old=None, new=hx(result[insert_at]),
             reason='pure Short Video Descriptor insertion (VIC 63 = 1920x1080p120, matching '
                    'the verified 2200x1125 297 MHz candidate timing): no original byte belongs '
                    f'at this index; the byte physically at {insert_at} ({hx(original[insert_at])}, '
                    'the audio block header) and the whole block tail move one byte right, and '
                    'one trailing zero-padding byte is consumed'),
        dict(kind='field_shift', original_offset=tmds_original_offset, new_offset=tmds_index,
             old=hx(original[tmds_original_offset]), new=hx(result[tmds_index]),
             reason='HDMI VSDB Maximum TMDS frequency 30 -> 60 (150 -> 300 MHz): the logical '
                    f'MHz/5 field of original offset {tmds_original_offset} right-shifts to '
                    f'{tmds_index} because of the insertion, then is edited; the original byte '
                    f'physically at {tmds_index} ({hx(original[tmds_index])}) was the following '
                    'VSDB payload byte, unrelated to this edit, and now sits one byte later'),
        dict(kind='edit', offset=255, old=hx(original[255]), new=hx(result[255]),
             reason='mandatory CTA block checksum recomputation'),
    ], key=lambda item: item['offset'] if 'offset' in item else item['new_offset'])

    report = dict(
        generated_from=dict(
            original_sha256=hashlib.sha256(original).hexdigest(),
            original_length=len(original),
            identity='4d d9 04 6a (Sony D94D:6A04, monitor path SNY6A04)',
            candidate_name=candidate.get('name'), candidate_status=candidate.get('status'),
            evidence_checkpoint=candidate.get('evidence_checkpoint_local')),
        advertisement=dict(before=dict(BASELINE), after=dict(PROPOSED),
                           vic=dict(number=63, label='1920x1080p120', timing=dict(timing))),
        diff_reasons=diff_reasons,
        byte_diff=byte_diff,
        cta_shift=dict(
            inserted_at=insert_at,
            original_tail=[insert_at, 254],    # original bytes shifted right
            shifted_into=[insert_at + 1, 255], # result positions holding them
            invariant=f'result[k] == original[k-1] for k in {insert_at + 1}..254 except the '
                      f'edited VSDB field byte at {tmds_index}; one trailing zero-padding byte '
                      'is consumed; the audit enforces this structural equality'),
        preserved=[
            'identity/serial/week-year/version, physical size, chromaticity, gamma',
            'established and standard timings, the 1080p60 and 640x480 DTDs, the name '
            'descriptor "SIE HMD *08"; the base block changes only in the three FD rate '
            'bytes plus its mandatory checksum byte',
            'all pre-existing CTA data blocks in original order: audio, speaker allocation, '
            'HDMI VSDB, extended-tag block and the Microsoft HMD VSDB when present — content '
            'byte-identical, only right-shifted one byte by the insertion (see cta_shift)',
            'CTA DTD count nibble and zero DTD area; padding only used, never overwritten',
        ],
        constraints=[
            'NOT hardware-apply-ready: experimental proposal only; no registry, GPU, UAC or '
            'runtime call is made or implied.',
            'long_term_stability_verified is false in the source candidate; no stability claim.',
            'Only GPU path/signal at 120 Hz was verified; no claim that any driver honours '
            'VIC 63 on this panel.',
            '90 Hz is out of scope: unverified.',
            'FD/VSDB maxima are the quantized 300 MHz encoding of the measured 297 MHz; larger '
            'encodings (e.g. 512 MHz) are deliberately not proposed.',
            'Fail-closed on unexpected layouts; an already-present VIC 63 stays blocked.',
        ],
        checksums=[sum(result[i:i + 128]) % 256 for i in (0, 128)],
        safety='pure offline proposal; original files only read; never applied to hardware',
    )
    return result, report


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline 1080p120 EDID advertisement proposal')
    parser.add_argument('--log', default=DEFAULT_LOG)
    parser.add_argument('--candidate', default=DEFAULT_CANDIDATE)
    parser.add_argument('--out', help='optional NEW proposal JSON path; the parent directory '
                                      'must already exist and the file must not exist yet')
    args = parser.parse_args(argv)
    try:
        original, candidate = load_evidence(args.log, args.candidate)
        proposed, report = generate_proposal(original, candidate)
    except (ProposalBlockedError, json.JSONDecodeError, OSError, ValueError) as error:
        print(json.dumps(dict(status='blocked', reason=str(error)), indent=2))
        return 1
    report.update(proposed_edid_hex=proposed.hex(),
                  proposal_edid_sha256=hashlib.sha256(proposed).hexdigest())
    print(json.dumps(report, indent=2))
    if args.out:
        destination = Path(args.out)
        if not destination.parent.is_dir():
            parser.error(f'{destination.parent} does not exist; this tool never creates directories')
        if destination.exists():
            parser.error(f'{destination} already exists; only a NEW proposal file is written')
        try:
            with destination.open('x', encoding='utf-8') as stream:
                stream.write(json.dumps(report, indent=2) + '\n')
        except FileExistsError:
            parser.error(f'{destination} already exists; only a NEW proposal file is written')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
