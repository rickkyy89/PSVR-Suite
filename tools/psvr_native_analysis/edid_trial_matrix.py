# SPDX-License-Identifier: MIT
"""Offline EDID trial comparison matrix: original, HMD VSDB variants, 120 Hz proposal.

Pure offline analysis over the project's own evidence files. This module reads the saved
EDID plan log and the measured 120 Hz candidate, re-derives every candidate blob in
memory with the existing fail-closed generators, and prints (or writes, via --out, into a
NEW file only) a JSON comparison matrix. It never touches the registry, GPU, UAC, runtime
or hardware, never applies anything, and never claims that a video mode is usable: an
EDID is an advertisement, not a measurement.

Matrix (three variants x the optional measured-120 proposal column):

  original                 captured raw EDID cached by the plan log (pre-override state)
  hmd_v2_usage07           Microsoft HMD VSDB v2, primary use case 0x07 (VR)
  hmd_v2_usage27           same block with the experimental compatibility use case 0x27
  <variant>/measured120    optional: edid_120_proposal applied on top of that variant

Reused generators (their gates are not relaxed or reimplemented):
  tools/psvr_native_analysis/hmd_edid.py          add_hmd_block (usage 0x07 / 0x27)
  tools/psvr_native_analysis/edid_120_proposal.py validate_identity, validate_candidate,
      verify_base_advertisement, fd_descriptor, generate_proposal
  tools/psvr_native_analysis/edid_blocks.py       require_complete, walk_cta_blocks, audit

The baseline is never hardcoded or synthesized: the plan log must be self-consistent
(regenerating usage 0x07 offline reproduces its recorded patched_block_hex) and must
carry the measured Sony identity, otherwise nothing is emitted. A cell whose generator
refuses its input is reported as blocked, quoting the generator's own reason, with no
bytes and no invented success; if the candidate lacks a verified timing the 120 column
simply stays blocked, so no exact physical timing is ever made up here.

Every cell carries its own delta against the baseline plus SHA-256, block checksums,
identity check and capability delta. In each delta every differing byte is classified
exactly once as:
  byte_shift          content preserved, only its position moved inside the fixed
                      128-byte extension (verified as new[i] == old[i - shift])
  logical             a real content change (inserted block byte, edited field,
                      CTA byte-2 or data-block-length bookkeeping), labelled by role
  checksum_recompute  the mandatory checksum byte of a block whose content changed
  unexplained         nothing matched: surfaced loudly, never absorbed silently
A usage-only pair therefore comes out as "primary use case byte plus checksum" only if
the bytes really say so; any additional differing byte is labelled with its role rather
than excused.

Causal steps are computed from the bytes, not asserted: classification (HMD VSDB
presence / primary use case) versus timing advertisement (VIC 63 plus range-limits and
HDMI VSDB maxima). The report keeps the single-step pairs apart from the pairs that mix
steps, because a local test must isolate one cause at a time.

Field layouts come from the sources already cited by edid_120_proposal.py: VESA E-EDID base
block (established timings at bytes 35..37, the eight standard timing identifiers at 38..53,
four descriptors at 54..125 with type 0xFC monitor name, 0xFD range limits, 0xFF display
product serial number string; inside the range-limits descriptor relative byte 4 is reserved
in EDID 1.3 and carries the range offset flags in EDID 1.4, never a minimum pixel clock, and
byte 11 is the secondary timing information byte), ANSI/CTA-861-G data block collection
(extension byte 0 = block tag 0x02, byte 1 = CTA revision, byte 2 = end of the data block
collection), HDMI 1.3a VSDB payload (Maximum TMDS = MHz/5) and the Microsoft HMD VSDB (IEEE
OUI CA125C, version 2, primary use case byte). Byte-range labels are only labels; no value is
inferred and nothing is claimed beyond what the reused generators validated.
"""
import argparse
import difflib
import hashlib
import json
import re
import uuid
from pathlib import Path

try:
    from . import edid_blocks, hmd_edid
    from . import edid_120_proposal as proposal
except ImportError:  # loaded file-relative, as tests/ does for tools
    import edid_blocks
    import hmd_edid
    import edid_120_proposal as proposal

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PLAN = 'logs/hmd_edid_override_20261005.json'
DEFAULT_CANDIDATE = 'configurations/candidates/hdmi_native_120hz.json'
MATRIX_NAME = 'edid_trial_matrix_20261005.json'

VARIANTS = ('original', 'hmd_v2_usage07', 'hmd_v2_usage27')
COLUMNS = ('base', 'measured120')

HDMI_OUI = bytes.fromhex('030c00')            # HDMI Licensing, little-endian in the payload
MICROSOFT_OUI = bytes.fromhex('5c12ca')       # Microsoft HMD VSDB OUI CA125C
OUI_NAMES = {HDMI_OUI: 'HDMI VSDB (IEEE OUI 000C03)',
             MICROSOFT_OUI: 'Microsoft HMD VSDB (IEEE OUI CA125C)'}
MICROSOFT_HMD_NAME = OUI_NAMES[MICROSOFT_OUI]
# CTA-861 data block tag field (bits 7:5): 1 audio, 2 video, 3 VSDB, 4 speaker allocation,
# 7 extended tag, as already used by edid_120_proposal.verify_cta and hmd_edid.
TAG_NAMES = {1: 'Audio Data Block', 2: 'Video Data Block', 3: 'VSDB',
             4: 'Speaker Allocation Data Block', 7: 'Extended Tag Data Block'}

# Capability projections compared cell by cell. The CTA projection excludes block offsets,
# so a pure insertion right-shift never counts as a capability change.
CAPABILITY_FIELDS = ('identity_hex', 'serial_hex', 'edid_version', 'screen_size_cm',
                     'chromaticity_hex', 'established_timings_hex', 'standard_timing_ids_hex',
                     'base_descriptor_kinds', 'fd_range_limits', 'extension_count',
                     'cta_block_count', 'cta_block_order', 'video_vics', 'video_native_vics',
                     'hdmi_vsdb_max_tmds_mhz', 'ms_hmd_vsdb')
TIMING_FIELDS = ('fd_range_limits', 'video_vics', 'video_native_vics', 'hdmi_vsdb_max_tmds_mhz',
                 'base_descriptor_kinds', 'extension_count', 'established_timings_hex',
                 'standard_timing_ids_hex', 'screen_size_cm', 'chromaticity_hex', 'edid_version')

# E-EDID base block byte roles: 35..37 carry the established timings, 38..53 the eight
# standard timing identifiers, 54..125 the four descriptor blocks.
BASE_ROLES = ((0, 8, 'EDID header'), (8, 12, 'manufacturer/product identity'),
              (12, 16, 'serial number'), (16, 18, 'manufacture week/year'),
              (18, 20, 'EDID version/release'), (20, 21, 'video input definition'),
              (21, 23, 'screen image size (cm)'), (23, 24, 'display gamma'),
              (24, 25, 'features/support'), (25, 35, 'chromaticity coordinates'),
              (35, 38, 'established timings'), (38, 54, 'standard timing identifiers'),
              (126, 127, 'extension count'))
CTA_HEADER_ROLES = {128: 'CTA-861 extension block tag (0x02 identifies this CTA extension)',
                    129: 'CTA extension revision',
                    130: 'CTA byte 2 (first DTD byte / end of data block collection)',
                    131: 'CTA byte 3 (DTD count / native flags)'}
# FD byte roles, mirroring the layout edid_120_proposal.fd_descriptor verifies: relative byte 4
# is reserved in EDID 1.3 (range offset flags in EDID 1.4), never a minimum pixel clock; byte 10
# is the internal-spacing/reserved byte the baseline keeps at 0x00; byte 11 is the secondary
# timing information byte (0x0A in the measured baseline).
FD_ROLES = {4: 'FD reserved byte (EDID 1.4 horizontal/vertical range offset flags)',
            5: 'FD min vertical rate (Hz)', 6: 'FD max vertical rate (Hz)',
            7: 'FD min horizontal rate (kHz)', 8: 'FD max horizontal rate (kHz)',
            9: 'FD max pixel clock (MHz / 10)',
            10: 'FD internal spacing / reserved byte (0x00 in the measured baseline)',
            11: 'FD secondary timing information byte (0x0A in the measured baseline)',
            12: 'FD trailing padding byte'}


class TrialMatrixBlockedError(RuntimeError):
    """Raised when the baseline or an input is refused; the matrix is then not produced."""


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _hex(value):
    return f'{value:02x}'


def resolve_input(path):
    """Absolute paths pass through; repo-relative defaults also work from any CWD."""
    candidate = Path(path)
    if candidate.is_absolute() or candidate.exists():
        return candidate
    return REPO_ROOT / candidate


def read_plan(plan_path):
    """Read the saved EDID plan log and require the fields this offline matrix needs."""
    path = resolve_input(plan_path)
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise TrialMatrixBlockedError(f'plan log cannot be read: {error}')
    try:
        plan = json.loads(raw.decode('utf-8-sig'))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TrialMatrixBlockedError(f'plan log is not readable JSON: {error}')
    if not isinstance(plan, dict):
        raise TrialMatrixBlockedError('plan log must be a JSON object')
    for field in ('raw_edid_hex', 'patched_block_hex', 'container_guid'):
        value = plan.get(field)
        if not isinstance(value, str) or not value:
            raise TrialMatrixBlockedError(f'plan log field {field} must be a non-empty string: '
                                          f'unsupported baseline (got {type(value).__name__})')
    hex_text = plan['raw_edid_hex']
    if not re.fullmatch(r'(?:[0-9a-fA-F]{2}){256}', hex_text):
        raise TrialMatrixBlockedError('raw_edid_hex is not a complete 256-byte EDID; '
                                      'the matrix interprets the measured two-block plan only')
    original = bytes.fromhex(hex_text)
    try:
        edid_blocks.require_complete(original)
    except ValueError as error:
        raise TrialMatrixBlockedError(f'captured raw EDID is not complete: {error}')
    effective = plan.get('effective_edid_hex')
    if not (effective is None or isinstance(effective, str)):
        raise TrialMatrixBlockedError('plan log effective_edid_hex must be a string or null: '
                                      'unsupported baseline')
    if effective not in (None, hex_text):
        raise TrialMatrixBlockedError('plan log records an effective EDID that differs from the captured '
                                      'raw EDID: the comparison baseline would be ambiguous')
    other = plan.get('other_overrides')
    if not (other is None or isinstance(other, dict)):
        raise TrialMatrixBlockedError('plan log other_overrides must be an object or null: '
                                      'unsupported baseline')
    if other:
        raise TrialMatrixBlockedError('plan log records other EDID overrides present: the baseline would '
                                      'not be a single clean capture')
    return plan, original, path, _sha(raw)


def read_candidate(candidate_path):
    """Read the measured 120 Hz candidate and check the shapes this report touches."""
    path = resolve_input(candidate_path)
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise TrialMatrixBlockedError(f'measured-120 candidate cannot be read: {error}')
    try:
        candidate = json.loads(raw.decode('utf-8-sig'))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TrialMatrixBlockedError(f'measured-120 candidate is not readable JSON: {error}')
    if not isinstance(candidate, dict):
        raise TrialMatrixBlockedError('measured-120 candidate must be a JSON object: '
                                      f'unsupported evidence (got {type(candidate).__name__})')
    validation = candidate.get('validation')
    if not (validation is None or isinstance(validation, dict)):
        raise TrialMatrixBlockedError('measured-120 candidate validation must be an object or null: '
                                      f'unsupported evidence (got {type(validation).__name__})')
    dimensions = (validation or {}).get('dimensions')
    if dimensions is not None and not isinstance(dimensions, (list, tuple)):
        raise TrialMatrixBlockedError('measured-120 candidate validation dimensions must be a list or '
                                      f'null (got {type(dimensions).__name__})')
    return candidate, path, _sha(raw)


def block_name(tag, payload):
    if tag == 3:
        return OUI_NAMES.get(payload[:3], f'VSDB (payload OUI {payload[:3].hex()})')
    return TAG_NAMES.get(tag, f'Data Block tag {tag}')


def cta_blocks(data):
    """CTA data blocks with absolute offsets: offset, tag, length, payload, name, key."""
    return [dict(offset=128 + pos, tag=tag, length=len(payload), payload=payload,
                 name=block_name(tag, payload), key=(tag, payload.hex()))
            for pos, tag, payload in edid_blocks.walk_cta_blocks(data[128:256])]


def parse_descriptors(data):
    out = []
    for offset in (54, 72, 90, 108):
        d = data[offset:offset + 18]
        if d[:3] == b'\x00\x00\x00':
            kind = {0xFC: 'monitor name descriptor', 0xFD: 'range limits (FD) descriptor',
                    0xFE: 'unspecified ASCII data descriptor',
                    0xFF: 'display product serial number string descriptor'}.get(
                        d[3], f'standard descriptor type 0x{d[3]:02x}')
        else:
            kind = 'detailed timing descriptor'
        out.append(dict(offset=offset, kind=kind, hex=d.hex()))
    return out


def _fd_range_limits(data):
    """Parsed range-limit maxima, or None when the descriptor is absent/unexpected (fail closed)."""
    try:
        offset, d = proposal.fd_descriptor(data)
    except (proposal.ProposalBlockedError, ValueError):
        return None
    return dict(offset=offset, reserved_byte4=d[4], min_vertical_hz=d[5],
                max_vertical_hz=d[6], min_horizontal_khz=d[7], max_horizontal_khz=d[8],
                max_pixel_clock_mhz=d[9] * 10, secondary_timing_byte=d[11])


def format_guid(raw16):
    """Windows GUID form of a 16-byte container identifier, or None if it is not one."""
    try:
        return '{' + str(uuid.UUID(bytes_le=bytes(raw16))).upper() + '}'
    except ValueError:
        return None


def ms_hmd_projection(ms):
    """Microsoft HMD VSDB fields only (no offsets), so a pure byte shift is never a change."""
    if ms is None:
        return None
    return dict(version=ms['version'], usage_hex=ms['usage_hex'], container_guid=ms['container_guid'])


def capabilities(data):
    """Structural capability/identity fingerprint of a 256-byte EDID blob."""
    caps = dict(length=len(data), sha256=_sha(data),
                checksums=[sum(data[i:i + 128]) % 256 for i in (0, 128)])
    caps['checksums_valid'] = not any(caps['checksums'])
    caps['identity_hex'] = data[8:12].hex()
    caps['serial_hex'] = data[12:16].hex()
    caps['edid_version'] = f'{data[18]}.{data[19]}'
    caps['screen_size_cm'] = [data[21], data[22]]
    caps['chromaticity_hex'] = data[25:35].hex()
    caps['established_timings_hex'] = data[35:38].hex()
    caps['standard_timing_ids_hex'] = data[38:54].hex()
    caps['base_descriptors'] = parse_descriptors(data)
    caps['base_descriptor_kinds'] = [d['kind'] for d in caps['base_descriptors']]
    caps['fd_range_limits'] = _fd_range_limits(data)
    caps['extension_count'] = data[126]
    blocks, video, hdmi, ms = [], None, None, None
    try:
        blocks = cta_blocks(data)
    except ValueError as error:
        caps['cta_parse_error'] = str(error)
    for b in blocks:
        if b['tag'] == 2 and video is None:
            video = b
        elif b['tag'] == 3 and b['payload'][:3] == HDMI_OUI:
            hdmi = b
        elif b['tag'] == 3 and b['payload'][:3] == MICROSOFT_OUI:
            ms = b
    caps['cta_block_count'] = len(blocks)
    caps['cta_block_order'] = [b['name'] for b in blocks]
    caps['cta_block_offsets'] = [b['offset'] for b in blocks]
    payload = video['payload'] if video else b''
    caps['video_block_length'] = len(payload)
    caps['video_vics'] = [x & 0x7F for x in payload]
    caps['video_native_vics'] = sorted(x & 0x7F for x in payload if x & 0x80)
    caps['hdmi_vsdb_max_tmds_byte'] = hdmi['payload'][6] if hdmi and hdmi['length'] > 6 else None
    caps['hdmi_vsdb_max_tmds_mhz'] = (caps['hdmi_vsdb_max_tmds_byte'] * 5
                                      if caps['hdmi_vsdb_max_tmds_byte'] is not None else None)
    caps['ms_hmd_vsdb'] = None
    if ms is not None:
        payload = ms['payload']
        caps['ms_hmd_vsdb'] = dict(offset=ms['offset'], version=payload[3] if len(payload) > 3 else None,
                                   usage=payload[4] if len(payload) > 4 else None,
                                   usage_hex=_hex(payload[4]) if len(payload) > 4 else None,
                                   container_guid=format_guid(payload[5:21]) if len(payload) >= 21 else None,
                                   payload_hex=payload.hex())
    caps['vic63_listed'] = 63 in caps['video_vics']
    caps['identity_and_serial'] = data[8:16].hex()
    caps['name_descriptor_hex'] = next((d['hex'] for d in caps['base_descriptors']
                                        if d['kind'] == 'monitor name descriptor'), None)
    return caps


def base_role(data, index):
    """Label for a differing base-block byte, following the layout edid_120_proposal verified."""
    if index == 127:
        return 'base block checksum'
    for position, offset in enumerate((54, 72, 90, 108)):
        if offset <= index < offset + 18:
            kind = parse_descriptors(data)[position]['kind']
            relative = index - offset
            if kind == 'range limits (FD) descriptor':
                return f'{kind} at {offset}: {FD_ROLES.get(relative, f"byte {relative} (padding/reserved)")}'
            if kind == 'monitor name descriptor':
                return f'{kind} at {offset}: text byte {relative}'
            return f'{kind} at {offset}: byte {relative}'
    for start, stop, label in BASE_ROLES:
        if start <= index < stop:
            return f'base block {label} (byte {index})'
    return f'base block byte {index}'


def payload_role(block, j):
    """Label for a CTA data block payload byte, using only the layouts already verified offline."""
    tag, payload = block['tag'], block['payload']
    if tag == 2 and j < len(payload):
        value = payload[j]
        return (f'{block["name"]}: Short Video Descriptor #{j + 1} '
                f'(VIC {value & 0x7F}{" native" if value & 0x80 else ""})')
    if tag == 3 and payload[:3] in OUI_NAMES:
        name = OUI_NAMES[payload[:3]]
        if name.startswith('HDMI') and j == 6:
            return f'{name}: Maximum TMDS frequency byte (MHz / 5)'
        if name.startswith('Microsoft'):
            if j == 3:
                return f'{name}: VSDB version byte'
            if j == 4:
                return f'{name}: primary use case byte'
            if 5 <= j < 21:
                return f'{name}: device container GUID byte'
            if j < 3:
                return f'{name}: IEEE OUI byte'
        return f'{name}: payload byte {j}'
    return f'{block["name"]}: payload byte {j}'


def classify_matched_block(old, new, o, n, changed, claimed, add, semantic_edit):
    """Classify one matched CTA block pair; shift-verified content is not reported as an edit."""
    shift = n['offset'] - o['offset']
    for index in range(n['offset'], min(255, n['offset'] + 1 + n['length'])):
        if index not in changed or index in claimed:
            continue
        if shift and 128 <= index - shift < 255 and new[index] == old[index - shift]:
            add('byte_shift', index, f'{n["name"]}: content preserved, position moved '
                                     f'{o["offset"]} -> {n["offset"]}',
                {'from_offset': index - shift, 'block_shift': shift})
        elif index == n['offset']:
            add('logical', index, f'{n["name"]}: data block header (tag {n["tag"]}, '
                                  f'length {n["length"]})', {'block_shift': shift})
        else:
            add('logical', index, payload_role(n, index - n['offset'] - 1), {'block_shift': shift})
    return dict(name=n['name'], tag=n['tag'], old_offset=o['offset'], new_offset=n['offset'],
                shift=shift, payload_identical=o['payload'] == n['payload'],
                semantic_edit=semantic_edit,
                changed_bytes=[i for i in range(n['offset'], n['offset'] + 1 + n['length'])
                               if i in changed])


def analyze_pair(old, new):
    """Causal structural comparison of two 256-byte EDIDs: logical vs byte-shift vs checksum.

    Every differing index is classified exactly once; anything left over is reported as
    unexplained instead of being folded into an explanation.
    """
    if len(old) != len(new):
        raise TrialMatrixBlockedError('cannot compare EDIDs of differing length')
    if len(old) != 256:
        raise TrialMatrixBlockedError('the matrix only interprets 256-byte (two-block) EDIDs')
    audit = edid_blocks.audit(old, new)
    changed = set(audit['changed_offsets'])
    rows = dict(logical=[], byte_shift=[], checksum_recompute=[], unexplained=[])
    claimed = set()

    def add(kind, index, role, extra=None):
        row = dict(kind=kind, offset=index, old=_hex(old[index]), new=_hex(new[index]), role=role)
        if extra:
            row.update(extra)
        rows[kind].append(row)
        claimed.add(index)

    for index in sorted(changed):
        if index < 128:
            add('checksum_recompute' if index == 127 else 'logical', index,
                'base block checksum recomputed' if index == 127 else base_role(old, index))
        elif 128 <= index <= 131:
            add('logical', index, CTA_HEADER_ROLES.get(index, f'CTA header byte {index - 128}'))
        elif index == 255:
            add('checksum_recompute', index, 'CTA block checksum recomputed')

    matched, inserted, removed, walk_note = [], [], [], None
    try:
        old_blocks, new_blocks = cta_blocks(old), cta_blocks(new)
    except ValueError as error:
        walk_note = f'CTA data block collection is not walkable on one side: {error}'
        old_blocks = new_blocks = None
    if old_blocks is not None:
        ops = difflib.SequenceMatcher(None, [b['key'] for b in old_blocks],
                                     [b['key'] for b in new_blocks], autojunk=False).get_opcodes()
        for op, i1, i2, j1, j2 in ops:
            if op == 'equal' or (op == 'replace' and i2 - i1 == j2 - j1):
                for k in range(i2 - i1):
                    matched.append(classify_matched_block(old, new, old_blocks[i1 + k],
                                                          new_blocks[j1 + k], changed, claimed, add,
                                                          semantic_edit=(op == 'replace')))
            else:
                removed.extend(old_blocks[i1:i2])
                inserted.extend(new_blocks[j1:j2])
        for b in inserted:
            for index in range(b['offset'], min(255, b['offset'] + 1 + b['length'])):
                if index in changed and index not in claimed:
                    add('logical', index, f'{b["name"]}: inserted data block byte', {'block_shift': None})
        for b in removed:
            for index in range(b['offset'], min(255, b['offset'] + 1 + b['length'])):
                if index in changed and index not in claimed:
                    add('unexplained', index, f'no matched counterpart for removed {b["name"]}')
    for index in sorted(changed):
        if index not in claimed:
            add('unexplained', index, 'not explained by any matched block or verified shift'
                + (f' ({walk_note})' if walk_note else ''))

    old_caps, new_caps = capabilities(old), capabilities(new)
    steps = causal_steps(old_caps, new_caps)
    identity_kept = old_caps['identity_and_serial'] == new_caps['identity_and_serial']
    result = dict(changed_offsets=audit['changed_offsets'], changed_base=audit['changed_base'],
                  changed_cta=audit['changed_cta'], changed_count=len(audit['changed_offsets']),
                  logical=rows['logical'], byte_shift=rows['byte_shift'],
                  checksum_recompute=rows['checksum_recompute'], unexplained=rows['unexplained'],
                  cta_blocks=matched, blocks_inserted=[b['name'] for b in inserted],
                  blocks_removed=[b['name'] for b in removed],
                  cta_walk_note=walk_note,
                  all_changed_bytes_explained=not rows['unexplained'],
                  checksums_valid_after=audit['checksums_valid'],
                  identity_and_serial_unchanged=identity_kept, causal_steps=steps)
    result['verdict'] = verdict_for(result)
    return result


def causal_steps(old_caps, new_caps):
    """Which causal steps the bytes actually show, computed from the capability projections."""
    steps = []
    old_ms = ms_hmd_projection(old_caps.get('ms_hmd_vsdb'))
    new_ms = ms_hmd_projection(new_caps.get('ms_hmd_vsdb'))
    if old_ms is None and new_ms is not None:
        steps.append('classification_hmd_vsdb_added')
    elif old_ms is not None and new_ms is None:
        steps.append('classification_hmd_vsdb_removed')
    elif old_ms != new_ms:
        steps.append('classification_hmd_vsdb_field_changed')
    if any(old_caps.get(k) != new_caps.get(k) for k in TIMING_FIELDS):
        steps.append('timing_advertisement')
    if old_caps.get('identity_and_serial') != new_caps.get('identity_and_serial'):
        steps.append('identity_changed')
    if any(old_caps.get(k) != new_caps.get(k) for k in ('cta_block_count', 'cta_block_order')):
        # The Microsoft HMD VSDB block is itself the classification payload: adding or removing
        # exactly that block is already reported as a classification step, not as extra noise.
        explained = any(step in ('classification_hmd_vsdb_added', 'classification_hmd_vsdb_removed',
                                 'classification_hmd_vsdb_field_changed') for step in steps)
        delta = old_caps['cta_block_count'] - new_caps['cta_block_count']
        other = (_drop_one(old_caps['cta_block_order'], MICROSOFT_HMD_NAME)
                 == _drop_one(new_caps['cta_block_order'], MICROSOFT_HMD_NAME))
        if not (explained and abs(delta) <= 1 and other):
            steps.append('cta_block_structure_changed')
    return steps or ['no_semantic_change']


def _drop_one(sequence, item):
    """Sequence without the first occurrence of item (None-safe, for structure comparison)."""
    sequence = list(sequence or [])
    if item in sequence:
        sequence.remove(item)
    return sequence


def verdict_for(pair):
    """One honest sentence about what a pair's bytes do and do not show."""
    steps = pair['causal_steps']
    counts = dict(logical=len(pair['logical']), byte_shift=len(pair['byte_shift']),
                  checksum=len(pair['checksum_recompute']), unexplained=len(pair['unexplained']))
    if steps == ['no_semantic_change']:
        if not pair['changed_offsets']:
            return 'identical bytes: no causal step at all.'
        return (f'no projected capability or identity field changed, but {pair["changed_count"]} byte(s) '
                f'differ ({counts["logical"]} logical, {counts["byte_shift"]} shift, '
                f'{counts["checksum"]} checksum, {counts["unexplained"]} unexplained): the compared '
                'projections do not cover these offsets, so they are reported as raw byte edits needing '
                'review, not as a no-op.')
    if counts['unexplained'] or pair['cta_walk_note']:
        return (f'{steps}: {counts["unexplained"]} differing byte(s) are not explained by a matched block '
                f'or a verified shift ({pair["cta_walk_note"] or "see unexplained rows"}); review before '
                'drawing any conclusion.')
    if steps == ['classification_hmd_vsdb_field_changed']:
        roles = ', '.join(row['role'].rsplit(': ', 1)[-1] for row in pair['logical']) or 'none'
        return (f'isolated classification change: only {roles} plus the mandatory checksum differ '
                f'({counts["checksum"]} byte(s)); {counts["byte_shift"]} byte(s) shifted; no timing byte '
                'and no advertised capability changed.')
    if steps == ['timing_advertisement']:
        return (f'timing advertisement step only: {counts["logical"]} logical byte(s), '
                f'{counts["byte_shift"]} byte(s) right-shifted by the insertion, '
                f'{counts["checksum"]} checksum(s); HMD VSDB classification unchanged.')
    if steps == ['classification_hmd_vsdb_added']:
        roles = ', '.join(sorted({row['role'] for row in pair['logical']}))
        return (f'isolated classification change: the {MICROSOFT_HMD_NAME} block is inserted; the logical '
                f'bytes are [{roles}]; {counts["byte_shift"]} pre-existing byte(s) only right-shift inside '
                f'the fixed 128-byte extension and {counts["checksum"]} checksum(s) are recomputed; no '
                'timing, identity or advertised-capability byte changed.')
    if steps == ['classification_hmd_vsdb_removed']:
        return (f'isolated classification change: the {MICROSOFT_HMD_NAME} block is removed, '
                f'{counts["byte_shift"]} byte(s) only shift back, {counts["checksum"]} checksum(s); '
                'no timing byte changed.')
    if all(step.startswith('classification') for step in steps):
        return (f'classification step(s) {steps}: {counts["logical"]} logical byte(s), '
                f'{counts["byte_shift"]} shifted, {counts["checksum"]} checksum(s); no timing '
                'advertisement byte changed.')
    return (f'mixed causal steps {steps}: {counts} - this is not a single-step comparison, so a local '
            'test on this pair would not isolate one cause.')


def derive_variant_blobs(plan, original):
    """Re-derive the HMD VSDB variants offline and check the saved plan's self-consistency."""
    try:
        guid = uuid.UUID(plan['container_guid'])
    except ValueError as error:
        raise TrialMatrixBlockedError(f'plan log container_guid is not a UUID: {error}')
    if not guid.int:
        raise TrialMatrixBlockedError('plan log container_guid is empty: hmd_edid refuses it')
    try:
        usage07 = hmd_edid.add_hmd_block(original, guid)
        usage27 = hmd_edid.add_hmd_block(original, guid, True)
    except ValueError as error:
        raise TrialMatrixBlockedError(f'HMD VSDB derivation refused the plan baseline: {error}')
    try:
        recorded = bytes.fromhex(plan['patched_block_hex'])
    except ValueError as error:
        raise TrialMatrixBlockedError(f'plan log patched_block_hex is not hex: {error}')
    if len(recorded) != 128:
        raise TrialMatrixBlockedError('plan log patched_block_hex is not one 128-byte extension block')
    match = usage07[128:256] == recorded
    # Compare the container identifier as a parsed UUID, so an equivalent lowercase or brace-free
    # spelling of the same GUID is accepted instead of failing on string formatting alone.
    regenerated_guid = _ms_guid_uuid(usage07)
    baseline_check = dict(
        accepted=match,
        offline_regenerated_usage07_cta_matches_recorded_patched_block=match,
        recorded_patched_block_sha256=_sha(recorded),
        regenerated_usage07_cta_sha256=_sha(usage07[128:256]),
        recorded_cta_tag_and_revision=[recorded[0], recorded[1]],
        regenerated_cta_tag_and_revision=[usage07[128], usage07[129]],
        plan_container_guid=plan['container_guid'],
        plan_container_guid_canonical=format_guid(guid.bytes_le),
        regenerated_block_container_guid=(format_guid(regenerated_guid.bytes_le)
                                          if regenerated_guid else None),
        container_guid_matches_plan=regenerated_guid == guid,
        plan_log_records_applied=bool(plan.get('applied')),
        live_registry_state_read=False,
        identity_hex=original[8:12].hex(), serial_hex=original[12:16].hex(),
        note='The baseline is the plan log capture itself, re-validated by edid_120_proposal gates: no '
             'EDID bytes are hardcoded here. If the recorded patched_block_hex is not reproducible from '
             'the captured raw EDID plus its own container GUID, or the identity is not the measured '
             'Sony, nothing is emitted.')
    if not match:
        raise TrialMatrixBlockedError('regenerating the Microsoft HMD VSDB from the captured raw EDID and '
                                      'the plan log container GUID does not reproduce the recorded '
                                      'patched_block_hex: the saved plan is not a baseline this matrix '
                                      'may describe')
    if not baseline_check['container_guid_matches_plan']:
        raise TrialMatrixBlockedError('the regenerated HMD VSDB container GUID does not match the plan '
                                      'log field: refusing to describe the identity of this override')
    return dict(original=original, hmd_v2_usage07=usage07, hmd_v2_usage27=usage27), baseline_check


def _ms_guid_uuid(blob):
    """Parsed container UUID inside the Microsoft HMD VSDB payload, or None if absent/short."""
    for block in cta_blocks(blob):
        if block['tag'] == 3 and block['payload'][:3] == MICROSOFT_OUI and len(block['payload']) >= 21:
            try:
                return uuid.UUID(bytes_le=bytes(block['payload'][5:21]))
            except ValueError:
                return None
    return None


def capability_delta(baseline_caps, caps):
    """Per-field before/after projection between the baseline cell and a candidate cell."""
    changed, preserved = [], []
    for field in CAPABILITY_FIELDS:
        before, after = baseline_caps.get(field), caps.get(field)
        if field == 'ms_hmd_vsdb':
            before, after = ms_hmd_projection(before), ms_hmd_projection(after)
        if field == 'base_descriptor_kinds':
            before, after = list(before), list(after)
        if before == after:
            preserved.append(field)
        else:
            changed.append(dict(field=field, before=before, after=after))
    return changed, preserved


def semantic_lines(variant, column, plan, caps):
    """Human-readable status lines: what a blob is, and what may not be claimed about it.

    The variant sentences describe the classification state; the base-block sentences are
    column-aware because the measured-120 column does edit the base block (range-limits
    maxima plus its checksum) and is never the plan log baseline.
    """
    lines = []
    base_identical = 'the base block stays byte-identical to the capture'
    base_edited = ('the base block differs from the capture only in the three range-limits maxima '
                   'and its recomputed checksum')
    base_note = base_identical if column == 'base' else base_edited
    if variant == 'original':
        if column == 'base':
            lines.append('captured original EDID cached by the plan log (pre-override state): the '
                         'comparison baseline itself, byte-identical to the recorded capture and not a '
                         'candidate to write anywhere.')
        else:
            lines.append('measured-120 proposal built on the captured original alone: no Microsoft HMD '
                         f'VSDB is present, so the classification step is absent; {base_note}. This cell '
                         'is not the plan log baseline.')
    elif variant == 'hmd_v2_usage07':
        lines.append(f'Microsoft HMD VSDB v2 (IEEE OUI CA125C) with primary use case 0x07 (VR) inserted '
                     f'in the first CTA extension; {base_note}.')
        if column == 'base' and plan.get('applied'):
            lines.append('the plan log records this override as applied for the instance it captured, but '
                         'this tool only re-derives the bytes offline: live registry state is NOT read and '
                         'NOT re-verified, and nothing is authorized by that record.')
    else:
        lines.append('the same Microsoft HMD VSDB v2 with the experimental compatibility primary use case '
                     '0x27 (bit 5 set, which the current Microsoft v2 specification reserves): proposal '
                     f'only, never applied by this tool, still pending its own local compatibility proof; '
                     f'{base_note}.')
    if column == 'measured120':
        lines.append('measured-120 proposal column: the offline generator added Short Video Descriptor '
                     'VIC 63 (1920x1080p120) and raised the range-limits maxima plus the HDMI VSDB '
                     'Maximum TMDS byte strictly from the measured candidate numbers. The candidate '
                     'records a known fullscreen recurrence and long_term_stability_verified=false, so '
                     'this is review material, not a configuration.')
        if caps['vic63_listed']:
            lines.append('the only timing numbers quoted for this cell are the ones the candidate file '
                         'documents and the generator validated; no exact 120 Hz physical timing is '
                         'invented and none is proven to run on the panel.')
    lines.append('not apply-ready: no registry, GPU, UAC, runtime, ownership or hardware action is '
                 'performed or implied by this report.')
    lines.append('no proof of mode usability: an EDID byte only advertises; nothing here shows that the '
                 'panel, the GPU driver or any runtime accepts, enumerates or displays a mode.')
    if caps['ms_hmd_vsdb'] and caps['vic63_listed']:
        lines.append('this cell carries the HMD classification block AND the 120 Hz advertisement, so '
                     'testing it alone would mix two causal steps; use the single-step pairs listed under '
                     'causal_step_separation.')
    return lines


def build_matrix(plan_path=DEFAULT_PLAN, candidate_path=DEFAULT_CANDIDATE, with_120=True,
                 include_hex=True):
    """Assemble the offline report. Raises TrialMatrixBlockedError when the baseline is refused."""
    plan, original, plan_path_resolved, plan_sha = read_plan(plan_path)
    try:
        proposal.validate_identity(original)
        proposal.verify_base_advertisement(original)
    except proposal.ProposalBlockedError as error:
        raise TrialMatrixBlockedError(f'captured baseline refused by the reused gates: {error}') from error
    blobs, baseline_check = derive_variant_blobs(plan, original)
    baseline_caps = capabilities(original)

    candidate = candidate_resolved = candidate_sha = None
    candidate_rejected = None
    if with_120:
        candidate, candidate_resolved, candidate_sha = read_candidate(candidate_path)
        try:
            proposal.validate_candidate(candidate)
        except proposal.ProposalBlockedError as error:
            # A candidate whose content the fail-closed gate refuses blocks the whole
            # measured-120 column; the classification rows stay valid and no timing is quoted.
            candidate_rejected = str(error)

    cells, generator_semantics, generated_blobs = [], {}, {}
    for variant in VARIANTS:
        for column in COLUMNS:
            cell_id = f'{variant}/{column}'
            if column == 'base':
                blob, chain = blobs[variant], _chain(variant, False)
            elif candidate_rejected is not None:
                chain = _chain(variant, True) + ['edid_120_proposal.validate_candidate refused the '
                                                 'candidate, so generate_proposal never ran for it']
                cells.append(_blocked_cell(cell_id, variant, column, chain,
                                           f'measured-120 candidate refused before generation: '
                                           f'{candidate_rejected}'))
                continue
            elif not with_120:
                cells.append(dict(cell_id=cell_id, variant=variant, column=column, status='excluded',
                                  blocked_reason='measured-120 column excluded by --skip-120: no bytes '
                                                 'generated and no judgement made about it',
                                  generator_chain=[], measured120_proposal_included=False,
                                  delta_vs_baseline=None, capabilities=None,
                                  semantic=dict(candidate_kind='excluded_on_request', proposal_only=True,
                                                apply_ready=False, mode_usability_proven=False,
                                                written_by_this_tool=False,
                                                not_apply_ready_reason='not generated in this run',
                                                human_lines=['column excluded on request: this run makes '
                                                             'no statement at all about a 120 Hz proposal '
                                                             'for this variant.'])))
                continue
            else:
                chain = _chain(variant, True)
                try:
                    blob, report = proposal.generate_proposal(blobs[variant], candidate)
                except proposal.ProposalBlockedError as error:
                    cells.append(_blocked_cell(cell_id, variant, column, chain, str(error)))
                    continue
                generator_semantics[cell_id] = report
            caps = capabilities(blob)
            generated_blobs[cell_id] = blob
            cell = dict(cell_id=cell_id, variant=variant, column=column, status='generated',
                        blocked_reason=None, generator_chain=chain, capabilities=caps,
                        measured120_proposal_included=(column == 'measured120'),
                        composition=('hmd_edid insertion first (on the captured original), then '
                                     'edid_120_proposal on the resulting blob; no other generator runs'
                                     if column == 'measured120' else
                                     'single generator only: this cell keeps the two causal steps apart'),
                        edid_sha256=caps['sha256'], length=caps['length'], checksums=caps['checksums'],
                        checksums_valid=caps['checksums_valid'],
                        hmd_vsdb_usage=caps['ms_hmd_vsdb']['usage_hex'] if caps['ms_hmd_vsdb'] else None)
            if include_hex:
                cell['edid_hex'] = blob.hex()
            delta = None if cell_id == 'original/base' else analyze_pair(original, blob)
            fd = baseline_caps['fd_range_limits']
            only_fd = fd and delta is not None and sorted(delta['changed_base']) == sorted(
                [fd['offset'] + 6, fd['offset'] + 8, fd['offset'] + 9, 127])
            cell['identity'] = dict(identity_hex=caps['identity_hex'], serial_hex=caps['serial_hex'],
                                    name_descriptor_hex=caps['name_descriptor_hex'],
                                    identity_and_serial_match_baseline=caps['identity_and_serial']
                                    == baseline_caps['identity_and_serial'],
                                    name_descriptor_match_baseline=caps['name_descriptor_hex']
                                    == baseline_caps['name_descriptor_hex'],
                                    base_block_identical_to_baseline=blob[:128] == original[:128],
                                    base_block_changed_offsets=delta['changed_base'] if delta else [],
                                    base_block_below_fd_maxima_identical=blob[:fd['offset'] + 6]
                                    == original[:fd['offset'] + 6] if fd else None,
                                    base_block_change_is_fd_maxima_and_checksum_only=bool(only_fd))
            changed, preserved = capability_delta(baseline_caps, caps)
            cell['capability_delta_vs_baseline'] = changed
            cell['capability_preserved_fields'] = preserved
            cell['capability_preserved'] = not changed
            cell['delta_vs_baseline'] = dict(purpose='candidate bytes vs the captured original baseline',
                                             **delta) if delta else None
            if cell_id in generator_semantics:
                rep = generator_semantics[cell_id]
                cell['proposal120_generator_semantics'] = dict(
                    advertisement=rep['advertisement'], diff_reasons=rep['diff_reasons'],
                    cta_shift=rep['cta_shift'], preserved=rep['preserved'], constraints=rep['constraints'])
            if variant == 'original' and column == 'base':
                kind = 'captured_baseline'
            elif variant == 'hmd_v2_usage07' and column == 'base' and plan.get('applied'):
                kind = 'recorded_current_state_of_the_captured_instance'
            else:
                kind = 'proposal_only'
            cell['semantic'] = dict(
                candidate_kind=kind, proposal_only=kind == 'proposal_only',
                apply_ready=False, mode_usability_proven=False, written_by_this_tool=False,
                plan_log_records_applied=(bool(plan.get('applied'))
                                          if variant == 'hmd_v2_usage07' and column == 'base' else None),
                not_apply_ready_reason='offline byte analysis only: no registry/GPU/runtime write, no mode '
                                       'enumeration, no stability or usability claim',
                human_lines=semantic_lines(variant, column, plan, caps))
            cells.append(cell)

    pairs = _pairwise(cells, generated_blobs, with_120)
    generated = [c for c in cells if c['status'] == 'generated']
    blocked = [c for c in cells if c['status'] == 'blocked']
    if generated and len(generated) == len(cells):
        matrix_status = 'complete'
    elif generated and not blocked:
        matrix_status = 'complete_with_excluded_measured120_column'
    elif generated:
        matrix_status = 'partial'
    else:
        matrix_status = 'blocked'
    return finalize_report(plan, plan_path_resolved, plan_sha, candidate, candidate_resolved,
                           candidate_sha, baseline_check, cells, pairs, matrix_status, with_120,
                           candidate_rejected)


def _chain(variant, with_120):
    chain = ['plan log raw_edid_hex (captured original, read only)']
    if variant == 'hmd_v2_usage07':
        chain.append('hmd_edid.add_hmd_block(container_guid, compat_non_microsoft_bit=False) -> usage 0x07')
    elif variant == 'hmd_v2_usage27':
        chain.append('hmd_edid.add_hmd_block(container_guid, compat_non_microsoft_bit=True) -> usage 0x27')
    if with_120:
        chain.append('edid_120_proposal.generate_proposal(<variant blob>, measured 120 Hz candidate)')
    return chain


def _blocked_cell(cell_id, variant, column, chain, reason):
    return dict(cell_id=cell_id, variant=variant, column=column, status='blocked', blocked_reason=reason,
                generator_chain=chain, measured120_proposal_included=False, edid_sha256=None,
                edid_hex=None, length=None, checksums=None, checksums_valid=None,
                delta_vs_baseline=None, capabilities=None, identity=None,
                capability_delta_vs_baseline=None, capability_preserved_fields=None,
                capability_preserved=None,
                semantic=dict(candidate_kind='proposal_only_blocked', proposal_only=True,
                              apply_ready=False, mode_usability_proven=False, written_by_this_tool=False,
                              not_apply_ready_reason='the fail-closed generator refused this input: no '
                                                     'bytes are emitted and nothing is invented',
                              human_lines=['blocked: the existing 120 Hz proposal generator rejected this '
                                           'combination, so no 120 Hz bytes exist for it, no capability '
                                           'delta is claimed and no timing is quoted. Its own reason is '
                                           'reproduced verbatim in blocked_reason.']))


def _pairwise(cells, generated_blobs, with_120):
    """Single-causal-step pairs, plus the usage-only control on top of the proposal column."""
    specs = [('original/base', 'hmd_v2_usage07/base',
              'classification only: insert the Microsoft HMD VSDB v2 with usage 0x07 over the capture'),
             ('original/base', 'hmd_v2_usage27/base',
              'classification only: insert the Microsoft HMD VSDB v2 with the compatibility usage 0x27'),
             ('hmd_v2_usage07/base', 'hmd_v2_usage27/base',
              'classification only, no timing at all: the two HMD VSDB primary use case bytes differ')]
    for variant in VARIANTS:
        specs.append((f'{variant}/base', f'{variant}/measured120',
                      f'timing only, no classification change: measured-120 advertisement on {variant}'))
    specs.append(('hmd_v2_usage07/measured120', 'hmd_v2_usage27/measured120',
                  'classification only, on top of the timing proposal: usage 0x07 vs 0x27'))
    pairs = []
    for left_id, right_id, purpose in specs:
        if not with_120 and ('measured120' in left_id or 'measured120' in right_id):
            continue
        pair = dict(pair=[left_id, right_id], purpose=purpose)
        left, right = generated_blobs.get(left_id), generated_blobs.get(right_id)
        if left is None or right is None:
            statuses = {c['cell_id']: c['status'] for c in cells}
            pairs.append(dict(pair, status='unavailable',
                              reason=(f'one side has no generated bytes ({statuses.get(left_id)}/'
                                      f'{statuses.get(right_id)}): no diff is invented, no claim is made')))
            continue
        pairs.append(dict(pair, status='generated', **analyze_pair(left, right)))
    return pairs


def finalize_report(plan, plan_path, plan_sha, candidate, candidate_path, candidate_sha,
                    baseline_check, cells, pairs, matrix_status, with_120, candidate_rejected=None):
    generated = [c for c in cells if c['status'] == 'generated']
    analysed = [p for p in pairs if p.get('status') == 'generated']
    usage_only = [p for p in analysed if p['causal_steps'] == ['classification_hmd_vsdb_field_changed']]
    presence_only = [p for p in analysed if p['causal_steps'] == ['classification_hmd_vsdb_added']]
    timing_only = [p for p in analysed if p['causal_steps'] == ['timing_advertisement']]
    mixed = [p for p in analysed if p not in usage_only + presence_only + timing_only]
    mixing_cells = [c for c in generated if c['measured120_proposal_included']
                    and c['capabilities']['ms_hmd_vsdb']]
    def holds(items, predicate):
        """True when every item passes, None when there is nothing to check: never a fake False."""
        return None if not items else all(predicate(item) for item in items)

    invariants = dict(
        every_generated_cell_is_256_bytes=holds(generated, lambda c: c['length'] == 256),
        every_generated_cell_has_valid_block_checksums=holds(generated, lambda c: c['checksums_valid']),
        identity_and_serial_identical_in_every_cell=holds(
            generated, lambda c: c['identity']['identity_and_serial_match_baseline']),
        monitor_name_identical_in_every_cell=holds(
            generated, lambda c: c['identity']['name_descriptor_match_baseline']),
        base_block_changes_are_fd_maxima_and_checksum_only_or_nothing=holds(
            generated, lambda c: c['identity']['base_block_identical_to_baseline']
            or c['identity']['base_block_change_is_fd_maxima_and_checksum_only']),
        all_generated_cell_shas_distinct=holds(generated, lambda c: True) and (
            len({c['edid_sha256'] for c in generated}) == len(generated)),
        every_changed_byte_explained_in_every_pair=holds(
            analysed, lambda p: p['all_changed_bytes_explained']),
        usage_only_pairs_change_only_usage_and_checksum=holds(usage_only, lambda p: (
            [row['role'].rsplit(': ', 1)[-1] for row in p['logical']] == ['primary use case byte']
            and len(p['checksum_recompute']) == 1 and not p['byte_shift'])),
        classification_only_pairs_change_no_timing_byte=holds(usage_only + presence_only, lambda p: (
            not any(row['offset'] < 128 for row in p['logical']))),
        timing_only_pairs_keep_hmd_classification=holds(timing_only, lambda p: (
            p['causal_steps'] == ['timing_advertisement']
            and not any(row['role'].startswith(MICROSOFT_HMD_NAME) for row in p['logical']))),
        vic63_listed_only_in_measured120_cells=holds(generated, lambda c: (
            c['capabilities']['vic63_listed'] == (c['column'] == 'measured120'))),
        no_cell_is_marked_apply_ready=all(c['semantic']['apply_ready'] is False for c in cells),
        no_cell_claims_mode_usability=all(c['semantic']['mode_usability_proven'] is False for c in cells),
        no_cell_claims_to_have_written_anything=all(
            c['semantic']['written_by_this_tool'] is False for c in cells),
        baseline_reproduced_from_the_plan_log=baseline_check['accepted'],
    )
    report = dict(
        schema_version=1, kind='edid_trial_matrix', matrix_status=matrix_status,
        purpose='offline comparison matrix of the captured original EDID, the two Microsoft HMD VSDB v2 '
                'usage variants and the optional measured-120 proposal, with per-candidate deltas that '
                'separate real byte edits from insertion byte-shifts',
        safety=dict(offline_analysis_only=True, inputs_read_only=True, hardware_access=False,
                    registry_or_gpu_or_runtime_access=False, uac_elevation_used=False,
                    ownership_or_mode_commands_issued=False, power_or_display_mode_changed=False,
                    any_candidate_written_or_applied=False, apply_ready=False, mode_usability_proven=False,
                    note='Nothing in this report is an instruction or an authorization to write anything: '
                         'every blob is an offline re-derivation of the bytes already recorded in the plan '
                         'log, produced in memory for review only.'),
        inputs=dict(
            plan_log=dict(path=str(plan_path), sha256=plan_sha, monitor_path=plan.get('monitor_path'),
                          instance_id=plan.get('instance_id'), registry_path=plan.get('registry_path'),
                          container_guid=plan.get('container_guid'),
                          raw_edid_sha256=_sha(bytes.fromhex(plan['raw_edid_hex'])),
                          effective_edid_hex_present='effective_edid_hex' in plan,
                          effective_edid_matches_raw=True,
                          other_overrides_recorded=bool(plan.get('other_overrides')),
                          plan_log_records_applied=bool(plan.get('applied')),
                          plan_log_note=plan.get('note'), live_registry_state_read=False,
                          caveat='raw_edid_hex is a cache of the capture, not a live EEPROM or registry '
                                 'read-out; this tool never re-reads the device'),
            measured120_candidate=dict(
                path=str(candidate_path) if candidate_path else None, sha256=candidate_sha,
                included_in_matrix=with_120, name=candidate.get('name') if candidate else None,
                status=candidate.get('status') if candidate else None,
                evidence_checkpoint_local=(candidate or {}).get('evidence_checkpoint_local'),
                validation=(candidate or {}).get('validation'),
                long_term_stability_verified=((candidate or {}).get('validation') or {})
                .get('long_term_stability_verified'),
                caveat='the candidate records a measured GPU path/signal at 120 Hz with a known '
                       'fullscreen recurrence and unverified long-term stability: it documents a signal '
                       'that was seen, not an EDID that proves the panel advertises or accepts the mode',
                fail_closed_gate_refusal=candidate_rejected,
                column_status=('excluded_by_flag' if not with_120 else
                               'blocked_by_gate_before_generation' if candidate_rejected
                               else 'generated_per_variant'))),
        baseline_check=baseline_check,
        matrix=cells,
        pairwise_causal_diffs=pairs,
        causal_step_separation=dict(
            rule='classification (Microsoft HMD VSDB presence / primary use case byte) and timing '
                 'advertisement (VIC 63 plus range-limits and HDMI VSDB maxima) are different causal '
                 'steps: test one per local run, never both together. The step labels below are computed '
                 'from each pair\'s bytes, not asserted',
            classification_hmd_vsdb_presence_pairs=[[p['pair'], p['verdict']] for p in presence_only],
            classification_usage_only_pairs=[[p['pair'], p['verdict']] for p in usage_only],
            timing_advertisement_only_pairs=[[p['pair'], p['verdict']] for p in timing_only],
            pairs_mixing_steps=[[p['pair'], p['causal_steps'], p['verdict']] for p in mixed],
            cells_mixing_steps=[dict(cell_id=c['cell_id'], hmd_vsdb_usage=c['capabilities']['ms_hmd_vsdb']
                                     ['usage_hex'], vic63_advertised=c['capabilities']['vic63_listed'])
                                for c in mixing_cells]),
        prohibited_claims=[
            'that any cell was applied, is apply-ready, or may be written to a registry, GPU or device',
            'that the panel, the GPU driver or any runtime enumerates, accepts or displays 120 Hz because '
            'these bytes advertise it',
            'that an exact 120 Hz physical timing is proven: only the candidate-documented reference '
            'timing, matching the standard VIC 63 values and accepted by the fail-closed generator, is '
            'quoted, and never as evidence of a running mode',
            'that 90 Hz or any other unverified rate is in scope or supported',
            'that identity, serial number, screen size, monitor name or the existing detailed timings '
            'changed: the per-cell deltas show byte by byte that they did not',
            'that a blocked cell is a failed attempt worth retrying with relaxed gates: it means the '
            'inputs do not support that combination'],
        limitations=[
            'offline byte analysis only: no hardware, registry, GPU, runtime or UAC access, so live state '
            'and the real EDID EEPROM contents stay unverified (the plan log is a cached capture)',
            'proposal-only by design: no cell is apply-ready and none proves that any mode is usable',
            'a byte that coincidentally equals its right-shifted neighbour would be classified as a shift '
            'rather than as an edit; each measured-120 cell keeps the generator\'s own diff_reasons next '
            'to the matrix delta so both views can be cross-checked',
            'the measured two-block (256-byte) layout only: extra extension blocks, CTA detailed timings, '
            'a second video or HDMI VSDB, non-zero padding or unexpected FD/VSDB encodings fail closed in '
            'the reused generators and are reported as blocked cells, never repaired',
            'the plan log records the usage 0x07 override as applied for the instance it captured: a '
            'recorded event, not a re-verified live fact and not an authorization for anything else',
            'no log or artifact is created by default; --out writes exactly one NEW JSON file and never '
            'overwrites existing evidence'],
        invariants=invariants,
        generated_cells=len(generated), blocked_cells=len([c for c in cells if c['status'] == 'blocked']),
        excluded_cells=len([c for c in cells if c['status'] == 'excluded']),
    )
    report['summary'] = summarize(report)
    return report


def summarize(report):
    """Short human-facing digest of the matrix, with no hardware or usability claim."""
    lines = [f"matrix status: {report['matrix_status']}; "
             f"{report['generated_cells']} generated, {report['blocked_cells']} blocked, "
             f"{report['excluded_cells']} excluded; nothing was applied and no mode usability is proven"]
    for cell in report['matrix']:
        if cell['status'] != 'generated':
            lines.append(f"{cell['cell_id']}: {cell['status']} - {cell['blocked_reason']}")
            continue
        delta = cell['delta_vs_baseline']
        lines.append(f"{cell['cell_id']}: sha256 {cell['edid_sha256'][:16]}..., "
                     f"{delta['changed_count'] if delta else 0} differing byte(s) vs baseline, "
                     f"{len(delta['byte_shift']) if delta else 0} of them a verified byte shift, "
                     f"identity preserved: {cell['identity']['identity_and_serial_match_baseline']}, "
                     f"kind: {cell['semantic']['candidate_kind']}, apply ready: "
                     f"{cell['semantic']['apply_ready']}, mode usability proven: "
                     f"{cell['semantic']['mode_usability_proven']}")
    for pair in report['pairwise_causal_diffs']:
        if pair.get('status') == 'generated':
            lines.append(f"pair {pair['pair'][0]} -> {pair['pair'][1]}: {pair['verdict']}")
        else:
            lines.append(f"pair {pair['pair'][0]} -> {pair['pair'][1]}: {pair['reason']}")
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Offline EDID trial comparison matrix (captured original, Microsoft HMD VSDB v2 '
                    'usage 0x07/0x27, optional measured-120 proposal): reads the evidence files and '
                    'writes nothing unless --out names a NEW file')
    parser.add_argument('--plan', default=DEFAULT_PLAN, help='saved EDID plan log JSON')
    parser.add_argument('--candidate', default=DEFAULT_CANDIDATE, help='measured 120 Hz candidate JSON')
    parser.add_argument('--skip-120', action='store_true', help='omit the measured-120 proposal column')
    parser.add_argument('--no-hex', action='store_true',
                        help='omit full EDID hex blobs from the report (SHA-256 and deltas stay)')
    parser.add_argument('--out', help='optional NEW matrix JSON path; the parent directory must already '
                                      'exist and the file must not exist yet, so evidence is never '
                                      'overwritten')
    args = parser.parse_args(argv)
    try:
        report = build_matrix(args.plan, args.candidate, with_120=not args.skip_120,
                              include_hex=not args.no_hex)
    except (TrialMatrixBlockedError, proposal.ProposalBlockedError, OSError, ValueError,
            json.JSONDecodeError) as error:
        print(json.dumps(dict(status='blocked', matrix=None, reason=str(error),
                              note='the baseline or an input was refused: nothing was emitted, '
                                   'invented or written'), indent=2))
        return 1
    print(json.dumps(report, indent=2))
    if args.out:
        destination = Path(args.out)
        if not destination.parent.is_dir():
            parser.error(f'{destination.parent} does not exist; this tool never creates directories')
        if destination.exists():
            parser.error(f'{destination} already exists; only a NEW matrix file is written')
        try:
            with destination.open('x', encoding='utf-8') as handle:
                json.dump(report, handle, indent=2)
                handle.write('\n')
        except FileExistsError:
            parser.error(f'{destination} appeared concurrently; nothing was overwritten')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
