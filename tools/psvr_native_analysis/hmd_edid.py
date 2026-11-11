# SPDX-License-Identifier: MIT
"""Minimal Microsoft HMD VSDB insertion; no identity or timing modifications.

Specification: Microsoft 'EDID Extension for Head-Mounted and Specialized
Monitors', CTA vendor block, OUI CA125C, version 2, primary use case VR (7).
"""
import argparse
import json
import uuid


def add_hmd_block(edid: bytes, container: uuid.UUID, compat_non_microsoft_bit: bool = False) -> bytes:
    if len(edid) < 256 or len(edid) % 128 or len(edid) != (edid[126]+1)*128:
        raise ValueError('Complete EDID with extensions required')
    if edid[:8] != bytes.fromhex('00ffffffffffff00') or any(sum(edid[i:i+128])%256 for i in range(0,len(edid),128)):
        raise ValueError('Invalid EDID header/checksum')
    if int.from_bytes(edid[8:10],'little') != 0xd94d or int.from_bytes(edid[10:12],'little') != 0x6a04:
        raise ValueError('Only measured Sony SNY6A04 is accepted')
    if not container.int:
        raise ValueError('Nonempty device container identifier required')
    cta=bytearray(edid[128:256])
    if cta[0] != 2 or cta[1] != 3 or not 4 <= cta[2] <= 105:
        raise ValueError('Unsupported first CTA extension')
    end=cta[2]
    insert=end
    pos=4
    while pos < end:
        tag,length=cta[pos]>>5,cta[pos]&31
        if not length or pos+1+length > end:
            raise ValueError('Malformed CTA data block collection')
        if tag == 3 and length >= 3 and cta[pos+1:pos+4] == bytes.fromhex('5c12ca'):
            raise ValueError('Microsoft VSDB already present; inspect rather than replace')
        if tag == 7 and insert == end:
            insert=pos
        pos+=length+1
    if any(cta[105:127]):
        raise ValueError('Insufficient zero padding; existing timings must not be truncated')
    # UUID is represented with Windows GUID byte order. No other-device identity.
    # Experimental compatibility variant seen in published PSVR implementations:
    # bit5 is set even for v2. Microsoft's current v2 specification reserves it.
    usage=0x27 if compat_non_microsoft_bit else 0x07
    block=bytes([0x75,0x5c,0x12,0xca,2,usage])+container.bytes_le
    patched=bytearray(cta[:insert]+block+cta[insert:105]+b'\0')
    patched[2]=end+len(block)
    patched[127]=(-sum(patched[:127]))&255
    result=edid[:128]+bytes(patched)+edid[256:]
    assert result[:128] == edid[:128] and result[256:] == edid[256:]
    assert patched[end+22:127] == cta[end:105]
    return result


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--input-hex',required=True)
    parser.add_argument('--container-guid',required=True)
    parser.add_argument('--compat-non-microsoft-bit',action='store_true')
    args=parser.parse_args()
    source=bytes.fromhex(args.input_hex)
    candidate=add_hmd_block(source,uuid.UUID(args.container_guid),args.compat_non_microsoft_bit)
    print(json.dumps(dict(block_number=1,patched_block_hex=candidate[128:256].hex(),
                         original_edid_hex=source.hex(),candidate_edid_hex=candidate.hex(),
                         identity_and_base_timings_unchanged=candidate[:128]==source[:128])))
