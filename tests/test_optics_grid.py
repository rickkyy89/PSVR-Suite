"""Regressione B10-07: copertura della PNG flat-grid usata dai trial ottici.

Solo stdlib: determinismo byte-identico, IHDR 1024x1024, spot-check pixel
decodificando IDAT con zlib, no-overwrite e parent-mancante via CLI (exit 2).
Nessuna HMD, nessun path live: solo temp dir e subprocess offline.

B10-08: output 1024 fisso documentato, nessun `--width` in CLI/firma.
"""
import inspect
import struct
import subprocess
import sys
import unittest
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory

import tools.psvr_patterns.generate_optics_grid as grid_module
from tools.psvr_patterns.generate_optics_grid import png

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'tools' / 'psvr_patterns' / 'generate_optics_grid.py'
WIDTH = 1024

EXPECTED = {
    'background': (12, 15, 20),
    'grid': (180, 190, 200),
    'border': (255, 170, 40),
    'center_vertical': (230, 70, 70),
    'center_horizontal': (70, 230, 100),
}

# (x, y) -> colore atteso, vedi ordine di override in generate_optics_grid.png.
SPOTS = {
    (10, 10): 'background',
    (64, 10): 'grid',
    (10, 64): 'grid',
    (1, 500): 'border',
    (512, 100): 'center_vertical',
    (100, 512): 'center_horizontal',
    (512, 512): 'center_horizontal',
}


def _parse_png(raw):
    assert raw[:8] == b'\x89PNG\r\n\x1a\n', 'bad PNG signature'
    pos = 8
    ihdr = None
    idat = bytearray()
    while pos < len(raw):
        (length,) = struct.unpack('>I', raw[pos:pos + 4])
        kind = raw[pos + 4:pos + 8]
        data = raw[pos + 8:pos + 8 + length]
        crc = struct.unpack('>I', raw[pos + 8 + length:pos + 12 + length])[0]
        assert crc == (zlib.crc32(kind + data) & 0xffffffff), 'bad CRC in %r' % kind
        if kind == b'IHDR':
            ihdr = struct.unpack('>IIBBBBB', data)
        elif kind == b'IDAT':
            idat += data
        elif kind == b'IEND':
            break
        pos += 12 + length
    assert ihdr is not None, 'missing IHDR'
    return ihdr, bytes(idat)


def _pixel(raw, x, y):
    _, idat = _parse_png(raw)
    rows = zlib.decompress(idat)
    stride = 1 + WIDTH * 3
    assert len(rows) == WIDTH * stride
    base = y * stride
    assert rows[base] == 0, 'unexpected filter byte'
    off = base + 1 + x * 3
    return tuple(rows[off:off + 3])


def _run_cli(*args):
    return subprocess.run([sys.executable, '-B', str(SCRIPT), *args],
                          capture_output=True, text=True, timeout=60)


class OpticsGridTests(unittest.TestCase):
    def test_deterministic_byte_identical(self):
        self.assertEqual(png(), png())

    def test_ihdr_is_1024x1024(self):
        ihdr, _ = _parse_png(png())
        width, height, depth, color, comp, filt, inter = ihdr
        self.assertEqual((width, height), (WIDTH, WIDTH))
        self.assertEqual((depth, color, comp, filt, inter), (8, 2, 0, 0, 0))

    def test_spot_check_pixels(self):
        raw = png()
        for (x, y), name in SPOTS.items():
            with self.subTest(x=x, y=y, color=name):
                self.assertEqual(_pixel(raw, x, y), EXPECTED[name])

    def test_cli_writes_deterministic_file(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / 'grid.png'
            proc = _run_cli('--out', str(out))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(out.read_bytes(), png())

    def test_cli_no_overwrite_exit_2(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / 'grid.png'
            out.write_bytes(b'sentinel')
            proc = _run_cli('--out', str(out))
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(out.read_bytes(), b'sentinel')

    def test_cli_missing_parent_exit_2(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / 'no-such-dir' / 'grid.png'
            proc = _run_cli('--out', str(out))
            self.assertEqual(proc.returncode, 2)
            self.assertFalse(out.exists())

    def test_width_fixed_1024_no_parameter(self):
        # B10-08: 1024 fisso, parametro width rimosso (nessun caller usa width custom).
        self.assertEqual(grid_module.WIDTH, 1024)
        self.assertEqual(list(inspect.signature(png).parameters), [])
        with self.assertRaises(TypeError):
            png(512)

    def test_cli_has_no_width_option(self):
        # B10-08: la CLI espone solo --out; --width rifiutato (exit 2).
        help_proc = _run_cli('--help')
        self.assertEqual(help_proc.returncode, 0)
        self.assertIn('--out OUT', help_proc.stdout)
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / 'grid.png'
            proc = _run_cli('--out', str(out), '--width', '512')
            self.assertEqual(proc.returncode, 2)
            self.assertIn('--width', proc.stderr)
            self.assertFalse(out.exists())


if __name__ == '__main__':
    unittest.main()
