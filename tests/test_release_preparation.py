"""Release exports preserve sources and exclude live/private/generated state."""
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from tools.prepare_release import build_source_package
from tools.collect_licenses import check_manifest, sha256_of
from tools.verify_release import verify_release


class ReleasePreparationTests(unittest.TestCase):
    def test_export_includes_untracked_source_and_icon_but_not_local_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "VERSION").write_text("0.3.0-alpha\n")
            fixtures = {"tools/code.py": b"print('release')\n",
                        "tools/psvr_suite/assets/icon.ico": b"\0\0\1\0icon",
                        "configurations/baseline.json": b"{}",
                        "driver/package/driver.vrdrivermanifest": b'{"name":"psvrpc"}',
                        "driver/package/resources/settings/default.vrsettings": b'{"driver_psvrpc":{}}',
                        "configurations/psvr_suite_user.json": b"private",
                        "configurations/psvr_suite_ui.json": b"private",
                        "tools/.tmp.driveupload/secret.py": b"private",
                        "out/binary.exe": b"private", "logs/report.json": b"private",
                        "tools/__pycache__/code.py": b"private", "tools/.deps/source.py": b"private"}
            for relative, content in fixtures.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            result = build_source_package(root)
            with zipfile.ZipFile(result["archive"]) as archive:
                prefix = "psvr-suite-0.3.0-alpha-source/"
                self.assertEqual(archive.read(prefix + "tools/code.py"), fixtures["tools/code.py"])
                self.assertEqual(archive.read(prefix + "tools/psvr_suite/assets/icon.ico"), fixtures["tools/psvr_suite/assets/icon.ico"])
                self.assertFalse(any(archive.read(name) == b"private" for name in archive.namelist()))
                manifest = json.loads(archive.read(prefix + "RELEASE_MANIFEST.json"))
                self.assertFalse(manifest["published"])
                self.assertIn(prefix + "driver/package/driver.vrdrivermanifest", archive.namelist())
                self.assertIn(prefix + "driver/package/resources/settings/default.vrsettings", archive.namelist())
                self.assertEqual(result["files"], 6)
            with self.assertRaises(FileExistsError):
                build_source_package(root)

    def test_license_check_needs_no_build_checkout_and_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "third_party/licenses"
            directory.mkdir(parents=True)
            content = b"Original license notice\n"
            (directory / "notice.txt").write_bytes(content)
            manifest = {"licenses": [{"path": "third_party/licenses/notice.txt",
                                       "bytes": len(content), "sha256": sha256_of(content)}]}
            (directory / "manifest.json").write_text(json.dumps(manifest))
            self.assertEqual(len(check_manifest(root)["licenses"]), 1)
            (directory / "notice.txt").write_bytes(b"tampered")
            with self.assertRaises(ValueError):
                check_manifest(root)

    def test_license_paths_cannot_escape_license_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "third_party/licenses"
            directory.mkdir(parents=True)
            (directory / "manifest.json").write_text(json.dumps({"licenses": [
                {"path": "third_party/licenses/../../secret.txt", "bytes": 0, "sha256": ""}]}))
            with self.assertRaises(ValueError):
                check_manifest(root)

    def test_archive_verifier_rejects_path_escape_before_running_any_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("../escaped.txt", "do not extract")
            with self.assertRaises(ValueError):
                verify_release(archive, temporary, False)
            self.assertFalse((Path(temporary) / "escaped.txt").exists())

    def test_archive_verifier_rejects_tampered_content_before_running_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "tampered.zip"
            manifest = {"version": "0.3.0-alpha", "files": [
                {"path": "source.py", "bytes": 8, "sha256": sha256_of(b"original")}]}
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("release/RELEASE_MANIFEST.json", json.dumps(manifest))
                package.writestr("release/source.py", b"modified")
            with self.assertRaisesRegex(ValueError, "checksum"):
                verify_release(archive, temporary, False)


if __name__ == "__main__":
    unittest.main()
