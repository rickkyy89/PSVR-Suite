"""Synthetic offline tests; never fetch a reference or modify display state."""
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

spec = importlib.util.spec_from_file_location(
    "reference_edid", Path(__file__).resolve().parents[1] / "tools" / "psvr_display" / "analyze_reference_edid.py")
edid = importlib.util.module_from_spec(spec)
spec.loader.exec_module(edid)


def checksum(block):
    block[127] = (-sum(block[:127])) & 255
    return bytes(block)


def sample(extension=None):
    b = bytearray(128)
    b[:8] = bytes.fromhex("00ffffffffffff00")
    b[8:12] = bytes.fromhex("4dd903b4")
    b[18:20] = b"\x01\x03"
    b[38:54] = b"\x01\x01" * 8
    b[54:72] = bytes.fromhex("0474801871382d40582c4500e00e1100001e")
    b[72:90] = bytes.fromhex("0357801871382d40582c4500e00e1100001e")
    b[90:108] = bytes.fromhex("000000fd00173d1a440f000a202020202020")
    b[126] = int(extension is not None)
    return checksum(b) + (extension or b"")


def archive(members):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return stream.getvalue()


class ReferenceTests(unittest.TestCase):
    def test_exact_timings_and_ranges(self):
        result = edid.decode_edid(sample())
        self.assertEqual(result["identity"], "SNYB403")
        self.assertTrue(result["header_valid"])
        self.assertTrue(result["blocks"][0]["checksum_valid"])
        self.assertEqual([t["refresh_hz"] for t in result["timings"]], [120, 90])
        t = result["timings"][0]
        self.assertEqual((t["htotal"], t["vtotal"], t["pixel_clock_hz"]), (2200, 1125, 297000000))
        self.assertEqual((t["hsync_offset"], t["hsync_width"], t["vsync_offset"], t["vsync_width"]), (88, 44, 4, 5))
        self.assertEqual(result["ranges"][0]["max_pixel_clock_mhz"], 150)
        self.assertTrue(any("exceeds" in w for w in result["warnings"]))

    def test_bad_header_checksum_length_and_count(self):
        b = bytearray(sample())
        b[0] = 5
        b[126] = 1
        result = edid.decode_edid(bytes(b) + b"x")
        self.assertFalse(result["header_valid"])
        self.assertFalse(result["blocks"][0]["checksum_valid"])
        self.assertTrue(any("extension count" in w for w in result["warnings"]))
        self.assertTrue(any("multiple" in w for w in result["warnings"]))
        with self.assertRaises(ValueError):
            edid.decode_edid(b"short")

    def test_cta_video_and_unknown_extension(self):
        b = bytearray(128)
        b[:7] = bytes([2, 3, 7, 0, 66, 63, 127])
        result = edid.decode_edid(sample(checksum(b)))
        modes = result["extensions"][0]["data_blocks"][0]["video_modes"]
        self.assertEqual(modes[0]["label"], "1920x1080p120")
        self.assertTrue(any("VIC 127" in w for w in result["warnings"]))
        b[0] = 112
        self.assertTrue(any("extension tag" in w for w in edid.decode_edid(sample(checksum(b)))["warnings"]))

    def test_malformed_cta(self):
        b = bytearray(128)
        b[:5] = bytes([2, 3, 5, 0, 95])
        self.assertTrue(any("Truncated" in w for w in edid.decode_edid(sample(checksum(b)))["warnings"]))

    def test_inf_matches_binary_and_does_not_extract(self):
        data = sample()
        inf = 'HKR,EDID_OVERRIDE,"0",0x01,' + ",".join(f"0x{x:02X}" for x in data)
        result = edid.inspect_archive(archive({"../test.bin": data, "sample.inf": inf, "installer.exe": b"not executable"}))
        self.assertEqual(result["members"][0]["edid"]["sha256"], result["members"][1]["override_edid"]["sha256"])
        self.assertIn("Skipped", result["members"][2]["inspection"])

    def test_size_limits(self):
        with self.assertRaises(ValueError):
            edid.inspect_archive(b"x" * (edid.LIMIT + 1))
        with self.assertRaises(ValueError):
            edid.inspect_archive(archive({"bomb.bin": b"x" * (edid.LIMIT + 1)}))

    def test_cli_help_and_no_argument_never_fetch(self):
        for argv in (["analysis", "--help"], ["analysis"]):
            with patch("sys.argv", argv), patch.object(edid, "fetch_reference") as fetch:
                with self.assertRaises(SystemExit):
                    edid.main()
                fetch.assert_not_called()

    def test_standard_timings(self):
        b = bytearray(sample())
        b[38:44] = bytes.fromhex("d1c0d1ded1fc")
        result = edid.decode_edid(checksum(b))
        self.assertEqual([t["refresh_hz"] for t in result["standard_timings"]], [60, 90, 120])

    def test_download_bounded_with_mock_no_network(self):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = "https://example.invalid/reference.zip"
        response.headers = {}
        response.read.return_value = b"zip"
        with patch.object(edid.urllib.request, "urlopen", return_value=response):
            self.assertEqual(edid.fetch_reference("https://example.invalid/reference.zip")[0], b"zip")
            response.read.assert_called_once_with(edid.LIMIT + 1)
            response.read.return_value = b"x" * (edid.LIMIT + 1)
            with self.assertRaises(ValueError):
                edid.fetch_reference("https://example.invalid/reference.zip")
        with patch.object(edid.urllib.request, "urlopen") as fetch:
            with self.assertRaises(ValueError):
                edid.fetch_reference("file:///local")
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
