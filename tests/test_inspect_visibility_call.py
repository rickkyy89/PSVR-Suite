# SPDX-License-Identifier: MIT
"""Regression tests for B13-08: bounded PE parsing in inspect_visibility_call.

Truncated binaries, a missing log string and oversize inputs must fail with a
clean exit-2 message (no traceback); small valid binaries behave as before.
Capstone is stubbed: disassembly bytes are out of scope, parsing is not.
"""
import contextlib
import importlib.util
import io
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

TOOL = Path(__file__).resolve().parents[1] / "tools/psvr_native_analysis/inspect_visibility_call.py"
LOG_STRING = b"[Display] Successfully set display visibility."

spec = importlib.util.spec_from_file_location("inspect_visibility_call", TOOL)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def _fake_capstone():
    fake = types.ModuleType("capstone")
    fake.CS_ARCH_X86 = 0
    fake.CS_MODE_64 = 0

    class _Cs:
        def __init__(self, *args, **kwargs):
            pass

        def disasm_lite(self, *args, **kwargs):
            return iter(())

    fake.Cs = _Cs
    return fake


def _run(argv):
    """Run main() with stubbed capstone; return (exit_code, stdout, stderr)."""
    stdout, stderr = io.StringIO(), io.StringIO()
    with mock.patch.dict(sys.modules, {"capstone": _fake_capstone()}):
        with mock.patch.object(sys, "argv", argv):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    module.main()
                except SystemExit as exc:
                    code = exc.code
                else:
                    code = 0
    return code, stdout.getvalue(), stderr.getvalue()


def _build_pe(path, code=b"\x90" * 16, extra=b"", funcs=((0x1000, 0x1020),)):
    """Write a minimal synthetic PE32+ binary; return its raw bytes."""
    pe_off = 0x80
    opt_size = 0xF0
    headers_len = 0x200
    blob = bytearray(headers_len)
    blob[0:2] = b"MZ"
    struct.pack_into("<I", blob, 0x3C, pe_off)
    blob[pe_off:pe_off + 4] = b"PE\0\0"
    struct.pack_into("<H", blob, pe_off + 6, 1)
    struct.pack_into("<H", blob, pe_off + 20, opt_size)
    struct.pack_into("<H", blob, pe_off + 24, 0x20B)
    opt = pe_off + 24
    struct.pack_into("<Q", blob, opt + 24, 0x140000000)
    section_va = 0x1000
    records = b"".join(struct.pack("<III", begin, end, 0) for begin, end in funcs)
    section_data = records + bytes(code) + bytes(extra)
    struct.pack_into("<II", blob, opt + 136, section_va, len(records))
    section_raw = headers_len
    o = opt + opt_size
    blob[o:o + 8] = b".text\0\0\0"
    struct.pack_into("<I", blob, o + 12, section_va)
    struct.pack_into("<I", blob, o + 16, len(section_data))
    struct.pack_into("<I", blob, o + 20, section_raw)
    struct.pack_into("<I", blob, o + 36, 0x60000020)
    raw = bytes(blob) + section_data
    Path(path).write_bytes(raw)
    return raw


class InspectVisibilityCallB1308Tests(unittest.TestCase):
    def test_explicit_rva_valid_binary_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "valid.exe"
            _build_pe(binary, funcs=((0x1000, 0x1010),))
            code, out, err = _run(["inspect_visibility_call.py", str(binary), "--rva", "0x1000"])
        self.assertEqual(code, 0, err)
        self.assertIn("binary_sha256=", out)
        self.assertIn("image_base=0x140000000", out)
        self.assertIn("xref=0x1000 function=0x1000..0x1010", out)

    def test_default_string_path_valid_binary_unchanged(self):
        # LEA at section offset 12 targets the log string at offset 28.
        code = b"\x48\x8d\x05" + struct.pack("<i", 9) + b"\x90" * 9
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "valid.exe"
            _build_pe(binary, code=code, extra=LOG_STRING + b"\0" * 16)
            code_rc, out, err = _run(["inspect_visibility_call.py", str(binary)])
        self.assertEqual(code_rc, 0, err)
        self.assertIn("log_string_rva=0x101c", out)
        self.assertIn("xref=0x100c function=0x1000..0x1020", out)

    def test_tiny_truncated_binary_is_clean_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "tiny.exe"
            binary.write_bytes(b"\x00" * 10)
            code, out, err = _run(["inspect_visibility_call.py", str(binary)])
        self.assertEqual(code, 2, out)
        self.assertIn("Visibility inspection failed", err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("Traceback", out)

    def test_truncated_headers_are_clean_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "cut.exe"
            raw = _build_pe(binary)
            binary.write_bytes(raw[:100])
            code, out, err = _run(["inspect_visibility_call.py", str(binary)])
        self.assertEqual(code, 2, out)
        self.assertIn("Visibility inspection failed", err)
        self.assertNotIn("Traceback", err)

    def test_truncated_section_data_is_clean_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "cut.exe"
            raw = _build_pe(binary)
            binary.write_bytes(raw[:0x200])
            code, out, err = _run(["inspect_visibility_call.py", str(binary), "--rva", "0x1000"])
        self.assertEqual(code, 2, out)
        self.assertIn("Visibility inspection failed", err)
        self.assertNotIn("Traceback", err)

    def test_non_pe_binary_is_clean_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "flat.bin"
            binary.write_bytes(b"\x00" * 0x200)
            code, out, err = _run(["inspect_visibility_call.py", str(binary)])
        self.assertEqual(code, 2, out)
        self.assertIn("Expected PE32+ binary", err)
        self.assertNotIn("Traceback", err)

    def test_missing_string_is_clean_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "nostring.exe"
            _build_pe(binary, extra=b"hello")
            code, out, err = _run(["inspect_visibility_call.py", str(binary)])
        self.assertEqual(code, 2, out)
        self.assertIn("Log string not found", err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("Traceback", out)

    def test_string_outside_sections_is_clean_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "outside.exe"
            raw = _build_pe(binary, extra=b"hello")
            binary.write_bytes(raw + LOG_STRING)
            code, out, err = _run(["inspect_visibility_call.py", str(binary)])
        self.assertEqual(code, 2, out)
        self.assertIn("outside known sections", err)
        self.assertNotIn("Traceback", err)

    def test_oversize_binary_is_clean_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "valid.exe"
            _build_pe(binary)
            with mock.patch.object(module, "_MAX_BINARY_BYTES", 16):
                code, out, err = _run(["inspect_visibility_call.py", str(binary)])
        self.assertEqual(code, 2, out)
        self.assertIn("too large", err)
        self.assertNotIn("Traceback", err)

    def test_missing_file_is_clean_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = _run(["inspect_visibility_call.py", str(Path(tmp) / "absent.exe")])
        self.assertEqual(code, 2, out)
        self.assertIn("Visibility inspection failed", err)
        self.assertNotIn("Traceback", err)

    def test_check_range_bounds(self):
        data = b"\x00" * 64
        module._check_range(data, 60, 4, "edge")
        with self.assertRaises(ValueError):
            module._check_range(data, 61, 4, "overflow")
        with self.assertRaises(ValueError):
            module._check_range(data, -1, 4, "negative")


if __name__ == "__main__":
    unittest.main()
