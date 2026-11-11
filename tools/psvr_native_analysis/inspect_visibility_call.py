# SPDX-License-Identifier: MIT
"""Static, read-only PE analysis around SteamVR's display visibility log call.

No process attachment, injection, binary modification or GPU calls.
Uses Capstone installed separately for disassembly; output includes file hash.
"""
import argparse
import bisect
import hashlib
import re
import struct
from pathlib import Path

# Largest binary accepted (200 MiB); larger inputs are rejected before reading.
_MAX_BINARY_BYTES = 200 * 1024 * 1024


def _check_range(data, offset, size, what):
    """Raise ValueError when [offset, offset+size) leaves the file bytes."""
    if offset < 0 or size < 0 or offset + size > len(data):
        raise ValueError(f"Truncated binary: {what} outside file ({len(data)} bytes)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    parser.add_argument("--string", default="[Display] Successfully set display visibility.")
    parser.add_argument("--rva", type=lambda x: int(x, 0))
    parser.add_argument("--span", type=int, help="Bounded bytes from explicit RVA (for split unwind ranges)")
    args = parser.parse_args()
    from capstone import Cs, CS_ARCH_X86, CS_MODE_64

    try:
        declared = args.binary.stat().st_size
    except OSError as exc:
        parser.exit(2, f"Visibility inspection failed: cannot stat binary: {exc}\n")
    if declared > _MAX_BINARY_BYTES:
        parser.exit(2, f"Visibility inspection failed: binary too large ({declared} bytes, limit {_MAX_BINARY_BYTES})\n")
    try:
        data = args.binary.read_bytes()
    except OSError as exc:
        parser.exit(2, f"Visibility inspection failed: cannot read binary: {exc}\n")
    if len(data) > _MAX_BINARY_BYTES:
        parser.exit(2, f"Visibility inspection failed: binary too large ({len(data)} bytes, limit {_MAX_BINARY_BYTES})\n")

    def u16(o):
        _check_range(data, o, 2, f"u16 at offset {o:#x}")
        return struct.unpack_from("<H", data, o)[0]

    def u32(o):
        _check_range(data, o, 4, f"u32 at offset {o:#x}")
        return struct.unpack_from("<I", data, o)[0]

    def file_offset(rva, sections):
        for _, va, raw, size, _ in sections:
            if va <= rva < va+size:
                return raw+rva-va
        raise ValueError(f"Unmapped RVA {rva:#x}")

    try:
        pe = u32(0x3C)
        if pe + 26 > len(data):
            raise ValueError(f"Invalid PE offset {pe:#x} outside file ({len(data)} bytes)")
        if data[pe:pe+4] != b"PE\0\0" or u16(pe+24) != 0x20B:
            raise ValueError("Expected PE32+ binary")
        opt = pe+24
        _check_range(data, opt+24, 8, "image base")
        base = struct.unpack_from("<Q", data, opt+24)[0]
        sections = []
        for n in range(u16(pe+6)):
            o = opt+u16(pe+20)+n*40
            _check_range(data, o, 40, f"section header {n}")
            sections.append((data[o:o+8].rstrip(b"\0"), u32(o+12), u32(o+20), u32(o+16), u32(o+36)))

        _check_range(data, opt+112+3*8, 8, "exception directory")
        exception_rva, exception_size = struct.unpack_from("<II", data, opt+112+3*8)
        if exception_size % 12:
            raise ValueError(f"Invalid exception size {exception_size} (not a multiple of 12)")
        pdata = file_offset(exception_rva, sections)
        _check_range(data, pdata, exception_size, "exception records")
        funcs = [struct.unpack_from("<III", data, o)[:2] for o in range(pdata, pdata+exception_size, 12)]
        starts = [f[0] for f in funcs]
        points = []
        if args.rva is not None:
            points.append(args.rva)
        else:
            try:
                target_raw = data.index(args.string.encode())
            except ValueError:
                raise ValueError(f"Log string not found: {args.string!r}") from None
            target = next((va+target_raw-raw for _, va, raw, size, _ in sections if raw <= target_raw < raw+size), None)
            if target is None:
                raise ValueError("Log string outside known sections")
            print(f"log_string_rva={target:#x}")
            for _, va, raw, size, flags in sections:
                if not flags & 0x20000000:
                    continue
                code = data[raw:raw+size]
                for match in re.finditer(rb"[\x48-\x4f]\x8d[\x05\x0d\x15\x1d\x25\x2d\x35\x3d](.{4})", code, re.DOTALL):
                    address = va+match.start()
                    dest = address+7+struct.unpack("<i", match.group(1))[0]
                    if dest == target:
                        points.append(address)
        print(f"binary_sha256={hashlib.sha256(data).hexdigest()} image_base={base:#x}")
        dis = Cs(CS_ARCH_X86, CS_MODE_64)
        seen = set()
        for point in points:
            idx = bisect.bisect_right(starts, point)-1
            if idx < 0 or not funcs[idx][0] <= point < funcs[idx][1]:
                raise ValueError("No runtime function for xref")
            start, end = funcs[idx]
            if args.span is not None:
                if args.rva is None or not 1 <= args.span <= 4096:
                    raise ValueError("span requires explicit RVA and 1..4096 bytes")
                start, end = point, point+args.span
            if (start,end) in seen:
                continue
            seen.add((start,end))
            if end-start > 16384:
                raise ValueError("Function exceeds output bound; inspect narrower manually")
            print(f"xref={point:#x} function={start:#x}..{end:#x}")
            offset = file_offset(start, sections)
            _check_range(data, offset, end-start, "function bytes")
            for address, _, mnemonic, operands in dis.disasm_lite(data[offset:offset+end-start], base+start):
                print(f"{address-base:#010x}: {mnemonic:8} {operands}")
    except (ValueError, struct.error) as exc:
        parser.exit(2, f"Visibility inspection failed: {exc}\n")


if __name__ == "__main__":
    main()
