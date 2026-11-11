# SPDX-License-Identifier: MIT
"""Regressions for B13-06: installdir sanitize, blank strings, --generated-utc."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from psvr_app_readiness import catalog  # noqa: E402
from psvr_app_readiness import inventory  # noqa: E402
from psvr_app_readiness import steam  # noqa: E402

FIXED_TIME = "2026-10-05T00:00:00Z"


def _spec():
    return [s for s in catalog.GAME_PRODUCTS if s["appid"] == "244210"][0]


class TraversalTest(unittest.TestCase):
    def _tree(self, tmp: Path):
        lib = tmp / "Steam"
        (lib / "steamapps" / "common").mkdir(parents=True)
        return lib

    def test_dotdot_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lib = self._tree(root)
            # Decoy outside the library with matching entries.
            evil = root / "evil"
            (evil).mkdir()
            (evil / "acs.exe").write_bytes(b"MZ")
            (evil / "AssettoCorsa.exe").write_bytes(b"MZ")
            (evil / "content").mkdir()
            (evil / "system").mkdir()
            library = {"path": str(lib)}
            manifest = {
                "file": str(lib / "steamapps" / "appmanifest_244210.acf"),
                "appid": "244210",
                "installdir": "../evil",
            }
            record = inventory._installed_product(
                library, manifest, _spec(), local_appdata=str(root / "none")
            )
            self.assertEqual(record["known_entries"], [])
            self.assertEqual(record["inventory_status"], "missing")
            self.assertIsNone(record["install_source"])
            self.assertIsNone(record["install_path"])
            self.assertTrue(
                any("rejected" in note.lower() for note in record["notes"])
            )

    def test_absolute_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lib = self._tree(root)
            target = root / "abs_target"
            (target).mkdir()
            (target / "acs.exe").write_bytes(b"MZ")
            library = {"path": str(lib)}
            for evil in (str(target), r"C:\Windows", "/etc", "C:/evil"):
                manifest = {
                    "file": "x.acf",
                    "appid": "244210",
                    "installdir": evil,
                }
                record = inventory._installed_product(
                    library, manifest, _spec(), local_appdata=str(root / "none")
                )
                self.assertEqual(
                    record["known_entries"], [], f"installdir={evil!r}"
                )
                self.assertEqual(
                    record["inventory_status"], "missing", f"installdir={evil!r}"
                )

    def test_legit_installdir_still_probed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lib = self._tree(root)
            assetto = lib / "steamapps" / "common" / "assettocorsa"
            (assetto).mkdir(parents=True)
            (assetto / "acs.exe").write_bytes(b"MZ")
            (assetto / "AssettoCorsa.exe").write_bytes(b"MZ")
            (assetto / "content").mkdir()
            (assetto / "system").mkdir()
            library = {"path": str(lib)}
            manifest = {
                "file": "x.acf",
                "appid": "244210",
                "installdir": "assettocorsa",
            }
            record = inventory._installed_product(
                library, manifest, _spec(), local_appdata=str(root / "none")
            )
            self.assertEqual(record["inventory_status"], "present")
            self.assertTrue(len(record["known_entries"]) > 0)


class BlankStringsTest(unittest.TestCase):
    def test_search_dirs_skips_blank(self):
        dirs = inventory.search_dirs(["", "   ", str(Path.cwd())])
        self.assertNotIn("", dirs)
        self.assertFalse(any(not item.strip() for item in dirs))

    def test_store_packages_blank_local_appdata(self):
        spec = {
            "store_packages": ("Microsoft.FlightSimulator_8wekyb3d8bbwe",),
        }
        for blank in ("", "   "):
            results = inventory._store_packages(spec, blank)
            self.assertEqual(len(results), 1)
            self.assertIsNone(results[0]["path"])
            self.assertFalse(results[0]["present"])

    def test_store_packages_skips_blank_package(self):
        spec = {"store_packages": ("", "   ", "Real_Package")}
        results = inventory._store_packages(spec, None)
        self.assertEqual([entry["package"] for entry in results], ["Real_Package"])

    def test_extra_library_blank_does_not_probe_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lib = root / "Steam"
            (lib / "steamapps").mkdir(parents=True)
            report = inventory.build_report(
                steam_root=str(lib),
                extra_libraries=["", "   "],
                local_appdata=str(root / "none"),
                generated_utc=FIXED_TIME,
            )
            paths = [entry["path"] for entry in report["steam"]["libraries"]]
            self.assertNotIn(os.path.abspath(""), paths)
            self.assertNotIn(os.getcwd(), paths)

    def test_steam_helpers_skip_blank(self):
        # Whitespace-only explicit root must not be treated as explicit path.
        path, source = steam.resolve_steam_root("   ")
        self.assertNotEqual(source, "explicit")
        self.assertIsNone(steam.library_folders_path(None, "   "))


class GeneratedUtcCliTest(unittest.TestCase):
    def _run(self, *args):
        import subprocess

        env = dict(os.environ)
        env["PYTHONPATH"] = str(TOOLS) + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run(
            [sys.executable, "-B", "-m", "psvr_app_readiness", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(ROOT),
            env=env,
            timeout=120,
        )

    def test_invalid_utc_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run(
                "inventory",
                "--steam-root",
                str(Path(tmp) / "none"),
                "--generated-utc",
                "not-a-time",
            )
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)

    def test_empty_utc_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run(
                "inventory",
                "--steam-root",
                str(Path(tmp) / "none"),
                "--generated-utc",
                "",
            )
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)

    def test_valid_utc_with_z_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run(
                "inventory",
                "--steam-root",
                str(Path(tmp) / "none"),
                "--generated-utc",
                FIXED_TIME,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertEqual(report["generated_utc"], FIXED_TIME)

    def test_valid_iso_offset_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = "2026-10-05T00:00:00+00:00"
            proc = self._run(
                "inventory",
                "--steam-root",
                str(Path(tmp) / "none"),
                "--generated-utc",
                value,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["generated_utc"], value)


if __name__ == "__main__":
    unittest.main()
