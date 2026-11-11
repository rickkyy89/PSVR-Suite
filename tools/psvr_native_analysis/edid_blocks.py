# SPDX-License-Identifier: MIT
"""Pure helpers to structurally compare two EDID blobs. No hardware access."""


def require_complete(data):
    """A complete E-EDID: 128-byte base block plus declared 128-byte blocks."""
    if len(data) < 256 or len(data) % 128 or len(data) != (data[126] + 1) * 128:
        raise ValueError('complete EDID with extensions required')
    if data[:8] != bytes.fromhex('00ffffffffffff00'):
        raise ValueError('invalid EDID header')


def walk_cta_blocks(b):
    """Yield (offset, tag, payload) for the CTA-861 data block collection.

    Truncated or self-inconsistent collections raise ValueError instead of
    yielding partial structure.
    """
    if b[0] != 2:
        raise ValueError('not a CTA-861 extension block')
    end = b[2]
    if not 4 <= end <= 127:
        raise ValueError('truncated or out-of-range CTA data block collection')
    pos = 4
    while pos < end:
        tag, length = b[pos] >> 5, b[pos] & 0x1F
        if length == 0 or pos + 1 + length > end:
            raise ValueError(f'malformed or truncated CTA data block at byte {pos}')
        yield pos, tag, bytes(b[pos + 1:pos + 1 + length])
        pos += 1 + length
    if pos != end:
        raise ValueError('CTA data block collection does not end exactly at byte 2')


def audit(old, new):
    """Compare two full EDID blobs; classify base vs CTA block changes."""
    if len(old) != len(new):
        raise ValueError('cannot audit EDIDs of differing length')
    changed = [i for i, (a, b) in enumerate(zip(old, new)) if a != b]
    return dict(changed_offsets=changed,
                changed_base=sorted(i for i in changed if i < 128),
                changed_cta=sorted(i for i in changed if 128 <= i < 256),
                changed_later=[i for i in changed if i >= 256],
                base_untouched=not any(i < 128 for i in changed),
                checksums_valid=not any(sum(new[i:i + 128]) % 256 for i in range(0, len(new), 128)))
