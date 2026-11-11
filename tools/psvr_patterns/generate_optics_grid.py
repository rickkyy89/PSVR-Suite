# SPDX-License-Identifier: MIT
"""Generate a flat, square-line PNG reference with no drawing dependencies.

Fixed 1024x1024 output (B10-08): the grid is consumed as-is by the optics
trials (`visual_trial.py --grid`, `runtime.py show_grid`) and the runtime
probe overlay, which all take an existing PNG file path, never a width.
The CLI therefore exposes only `--out`; there is intentionally no `--width`.
"""
import argparse
import struct
import zlib
from pathlib import Path

WIDTH = 1024


def png():
    width = WIDTH
    rows = bytearray()
    for y in range(width):
        rows.append(0)
        for x in range(width):
            color = (12, 15, 20)
            if x % 64 < 2 or y % 64 < 2:
                color = (180, 190, 200)
            if min(x, y, width - 1 - x, width - 1 - y) < 4:
                color = (255, 170, 40)
            if abs(x - width // 2) < 2:
                color = (230, 70, 70)
            # Al centro la riga verde (orizzontale) prevale sulla rossa (verticale).
            if abs(y - width // 2) < 2:
                color = (70, 230, 100)
            rows.extend(color)
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, width, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows, 6)) + chunk(b'IEND', b''))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        parser.error('output file already exists (no overwrite; choose a new path)')
    if not args.out.parent.is_dir():
        parser.error('parent directory is missing (create it first, no auto-mkdir)')
    with args.out.open('xb') as output:
        output.write(png())
    print(args.out.resolve())
