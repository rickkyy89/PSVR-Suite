# SPDX-License-Identifier: MIT
"""Tests for the read-only application readiness inventory (M7/M8/M10).

Everything runs offline against a synthetic Steam tree. No game is launched,
nothing is installed and the real registry is only read (never written).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from psvr_app_readiness import catalog  # noqa: E402
from psvr_app_readiness import inventory  # noqa: E402
from psvr_app_readiness import registry  # noqa: E402
from psvr_app_readiness import steam  # noqa: E402
from psvr_app_readiness import vdf  # noqa: E402

FIXED_TIME = "2026-10-05T00:00:00Z"


def vdf_escape(path) -> str:
    return str(path).replace("\\", "\\\\")


def build_fake_steam(root: Path):
    """Create a two-library Steam tree with escaped, space-bearing paths."""
    steam_root = root / "Steam"
    steamapps = steam_root / "steamapps"
    steamapps.mkdir(parents=True)
    lib2 = root / "SteamLibrary Extra"
    lib2_steamapps = lib2 / "steamapps"
    lib2_steamapps.mkdir(parents=True)

    assetto = steamapps / "common" / "assettocorsa"
    assetto.mkdir(parents=True)
    (assetto / "acs.exe").write_bytes(b"MZ")
    (assetto / "AssettoCorsa.exe").write_bytes(b"MZ")
    (assetto / "content").mkdir()
    (assetto / "system").mkdir()

    msfs = lib2_steamapps / "common" / "MicrosoftFlightSimulator"
    msfs.mkdir(parents=True)
    (msfs / "FlightSimulator.exe").write_bytes(b"MZ")

    (steamapps / "appmanifest_244210.acf").write_text(
        '"AppState"\n{\n'
        '    "appid" "244210"\n'
        '    "name" "Assetto Corsa"\n'
        '    "installdir" "assettocorsa"\n'
        '    "StateFlags" "4"\n'
        '    "buildid" "1234"\n'
        '    "SizeOnDisk" "100"\n'
        "}\n",
        encoding="utf-8",
    )
    (lib2_steamapps / "appmanifest_1250410.acf").write_text(
        '"AppState"\n{\n'
        '    "appid" "1250410"\n'
        '    "name" "Microsoft Flight Simulator"\n'
        '    "installdir" "MicrosoftFlightSimulator"\n'
        '    "StateFlags" "4"\n'
        "}\n",
        encoding="utf-8",
    )
    (steamapps / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n'
        '    "0"\n    {\n'
        f'        "path" "{vdf_escape(steam_root)}"\n'
        '        "apps"\n        {\n            "244210" "1"\n        }\n'
        "    }\n"
        '    "1"\n    {\n'
        f'        "path" "{vdf_escape(lib2)}"\n'
        '        "apps"\n        {\n            "1250410" "1"\n        }\n'
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    return steam_root, lib2


def run_cli(*args):
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


class VdfParserTest(unittest.TestCase):
    def test_nested_block_and_escaped_windows_path(self):
        text = '"libraryfolders"\n{\n "0" { "path" "D:\\\\SteamLibrary\\\\Games" }\n}\n'
        data = vdf.loads(text)
        self.assertEqual(data["libraryfolders"]["0"]["path"], r"D:\SteamLibrary\Games")

    def test_bom_comment_and_escaped_quote(self):
        text = '\ufeff// leading comment\n"a" { "b" "x\\"y" } // trailing\n'
        self.assertEqual(vdf.loads(text)["a"]["b"], 'x"y')

    def test_unclosed_block_is_malformed(self):
        with self.assertRaises(vdf.VdfError):
            vdf.loads('"a" { "b" "c"')

    def test_missing_key_value_is_malformed(self):
        with self.assertRaises(vdf.VdfError):
            vdf.loads('"AppState" { "appid"')

    def test_missing_file_is_vdf_error(self):
        with self.assertRaises(vdf.VdfError):
            vdf.loads_file(Path("definitely-missing") / "libraryfolders.vdf")

    def test_directory_is_vdf_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(vdf.VdfError):
                vdf.loads_file(Path(tmp))

    def test_oversize_file_is_rejected_before_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "libraryfolders.vdf"
            target.write_bytes(b"x" * (vdf.MAX_VDF_FILE_BYTES + 1))
            with self.assertRaisesRegex(vdf.VdfError, "too large"):
                vdf.loads_file(target)

    def test_deep_nesting_is_vdf_error_not_recursion_error(self):
        depth = 5000
        text = "".join(f'"k{i}" {{\n' for i in range(depth))
        text += '"leaf" "value"\n' + "}" * depth + "\n"
        try:
            vdf.loads(text)
        except RecursionError:
            self.fail("deep nesting raised RecursionError instead of VdfError")
        except vdf.VdfError as exc:
            self.assertIn("nesting too deep", str(exc))
        else:
            self.fail("deep nesting parsed without error")

    def test_deep_nesting_file_is_vdf_error(self):
        depth = 5000
        text = "".join(f'"k{i}" {{\n' for i in range(depth))
        text += '"leaf" "value"\n' + "}" * depth + "\n"
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested.vdf"
            target.write_text(text, encoding="utf-8")
            with self.assertRaises(vdf.VdfError):
                vdf.loads_file(target)

    def test_legit_manifest_file_still_parses(self):
        text = (
            '"AppState"\n{\n'
            '    "appid" "244210"\n'
            '    "name" "Assetto Corsa"\n'
            '    "installdir" "assettocorsa"\n'
            "}\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "appmanifest_244210.acf"
            target.write_text(text, encoding="utf-8")
            data = vdf.loads_file(target)
        self.assertEqual(data["AppState"]["appid"], "244210")
        self.assertEqual(data["AppState"]["installdir"], "assettocorsa")


class RegistryTest(unittest.TestCase):
    def test_openxr_runtime_shape_is_read_only(self):
        report = registry.read_openxr_runtime()
        self.assertIs(report["read_only"], True)
        for key in ("available", "active_runtime", "source", "steamvr", "error"):
            self.assertIn(key, report)
        if report["available"]:
            self.assertIsInstance(report["active_runtime"], str)
            self.assertIsInstance(report["steamvr"], bool)

    def test_steam_install_path_shape(self):
        value, source = registry.read_steam_install_path()
        self.assertTrue(value is None or isinstance(value, str))
        self.assertTrue(source is None or isinstance(source, str))


class SteamLibraryTest(unittest.TestCase):
    def test_multiple_libraries_and_escaped_space_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            steam_root, lib2 = build_fake_steam(Path(tmp))
            folders = steam.parse_library_folders(
                steam_root / "steamapps" / "libraryfolders.vdf"
            )
            self.assertEqual(folders, [str(steam_root), str(lib2)])
            self.assertIn(" ", str(lib2))

            record = steam.load_library(lib2, "libraryfolders.vdf")
            self.assertTrue(record["steamapps_exists"])
            self.assertEqual(len(record["appmanifests"]), 1)
            self.assertEqual(record["appmanifests"][0]["appid"], "1250410")

    def test_missing_library_is_recorded_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "NoSuchLibrary"
            record = steam.load_library(missing, "explicit")
            self.assertFalse(record["exists"])
            self.assertFalse(record["steamapps_exists"])
            self.assertEqual(record["appmanifests"], [])


class InventoryTest(unittest.TestCase):
    def test_finds_assetto_and_msfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            steam_root, _ = build_fake_steam(Path(tmp))
            report = inventory.build_report(
                steam_root=str(steam_root),
                local_appdata=str(Path(tmp) / "localappdata"),
                generated_utc=FIXED_TIME,
            )
            self.assertFalse(report["readiness"]["end_to_end_ready"])
            self.assertEqual(report["readiness"]["blocked_by"], ["M5", "M6"])

            components = report["components"]
            self.assertEqual(
                components["m8_assetto_corsa"]["inventory_status"], "present"
            )
            m10 = components["m10_msfs"]
            self.assertEqual(m10["inventory_status"], "partial")
            products = {product["appid"]: product for product in m10["products"]}
            self.assertTrue(products["1250410"]["found"])
            self.assertEqual(products["1250410"]["inventory_status"], "present")
            self.assertFalse(products["2537590"]["found"])

            for component in components.values():
                self.assertEqual(component["readiness"], "not_ready")
                self.assertNotEqual(component["inventory_status"], "ready")

    def test_store_package_is_detected_without_steam(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local = root / "localappdata"
            package = (
                local / "Packages" / "Microsoft.FlightSimulator_8wekyb3d8bbwe"
            )
            package.mkdir(parents=True)
            report = inventory.build_report(
                steam_root=str(root / "none"),
                local_appdata=str(local),
                generated_utc=FIXED_TIME,
            )
            products = {
                product["appid"]: product
                for product in report["components"]["m10_msfs"]["products"]
            }
            msfs_2020 = products["1250410"]
            self.assertFalse(msfs_2020["found"])
            self.assertTrue(msfs_2020["store_installed"])
            self.assertEqual(msfs_2020["install_source"], "store")
            self.assertEqual(msfs_2020["inventory_status"], "present")

    def test_extra_library_is_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            steam_root, lib2 = build_fake_steam(Path(tmp))
            report = inventory.build_report(
                steam_root=str(steam_root),
                extra_libraries=[str(lib2), str(lib2)],
                generated_utc=FIXED_TIME,
            )
            paths = [library["path"] for library in report["steam"]["libraries"]]
            self.assertIn(str(lib2), paths)
            self.assertEqual(paths.count(str(lib2)), 1)

    def test_broken_and_unreadable_manifests_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            steam_root, _ = build_fake_steam(Path(tmp))
            steamapps = steam_root / "steamapps"
            # A directory named *.acf is unreadable as a manifest file.
            (steamapps / "appmanifest_broken.acf").mkdir()
            # A malformed manifest.
            (steamapps / "appmanifest_bad.acf").write_text(
                '"AppState" { "appid"', encoding="utf-8"
            )
            report = inventory.build_report(
                steam_root=str(steam_root), generated_utc=FIXED_TIME
            )
            root_library = next(
                library
                for library in report["steam"]["libraries"]
                if library["path"] == str(steam_root)
            )
            names = {Path(entry["file"]).name for entry in root_library["errors"]}
            self.assertIn("appmanifest_broken.acf", names)
            self.assertIn("appmanifest_bad.acf", names)

    def test_permission_error_on_manifest_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            steam_root, _ = build_fake_steam(Path(tmp))
            target = steam_root / "steamapps" / "appmanifest_244210.acf"
            real_read_text = Path.read_text

            def guarded(self, *args, **kwargs):
                if self == target:
                    raise PermissionError(13, "Access is denied", str(self))
                return real_read_text(self, *args, **kwargs)

            with unittest.mock.patch.object(Path, "read_text", guarded):
                report = inventory.build_report(
                    steam_root=str(steam_root), generated_utc=FIXED_TIME
                )
            root_library = next(
                library
                for library in report["steam"]["libraries"]
                if library["path"] == str(steam_root)
            )
            self.assertTrue(
                any("permission" in entry["error"].lower() for entry in root_library["errors"])
            )
            self.assertFalse(
                report["components"]["m8_assetto_corsa"]["products"][0]["found"]
            )

    def test_known_tool_is_found_in_explicit_bin_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bins"
            bin_dir.mkdir()
            (bin_dir / "yt-dlp.exe").write_bytes(b"MZ")
            report = inventory.build_report(
                steam_root=str(Path(tmp) / "none"),
                bin_dirs=[str(bin_dir)],
                generated_utc=FIXED_TIME,
            )
            yt_dlp = next(
                tool
                for tool in report["components"]["m7_video"]["tools"]
                if tool["id"] == "yt-dlp"
            )
            self.assertTrue(yt_dlp["found"])
            self.assertEqual(yt_dlp["location"], str(bin_dir / "yt-dlp.exe"))

    def test_unknown_required_tool_is_never_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = inventory.build_report(
                steam_root=str(Path(tmp) / "none"),
                required=["mystery-player"],
                generated_utc=FIXED_TIME,
            )
            component = report["components"]["m7_video"]
            self.assertEqual(component["required_unknown"], ["mystery-player"])
            self.assertEqual(component["readiness"], "not_ready")
            self.assertIn("mystery-player", report["readiness"]["strict_failures"])
            catalog_ids = {tool["id"] for tool in catalog.TOOLS}
            self.assertNotIn("mystery-player", catalog_ids)

    def test_known_required_tool_missing_is_a_strict_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(
                inventory, "find_executable", return_value=None
            ):
                report = inventory.build_report(
                    steam_root=str(Path(tmp) / "none"),
                    required=["ffmpeg"],
                    generated_utc=FIXED_TIME,
                )
            component = report["components"]["m7_video"]
            self.assertEqual(component["required_missing"], ["ffmpeg"])
            self.assertIn("ffmpeg", report["readiness"]["strict_failures"])


class CliTest(unittest.TestCase):
    def test_help_exits_zero(self):
        proc = run_cli("--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("read-only", proc.stdout.lower())

    def test_unknown_command_is_usage_error(self):
        self.assertEqual(run_cli("wobble").returncode, 2)

    def test_inventory_json_on_fake_steam(self):
        with tempfile.TemporaryDirectory() as tmp:
            steam_root, _ = build_fake_steam(Path(tmp))
            proc = run_cli(
                "inventory",
                "--steam-root",
                str(steam_root),
                "--generated-utc",
                FIXED_TIME,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertEqual(report["schema"], "psvr.app_readiness")
            self.assertFalse(report["readiness"]["end_to_end_ready"])
            self.assertEqual(report["generated_utc"], FIXED_TIME)

    def test_strict_exit_for_unknown_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run_cli(
                "inventory",
                "--steam-root",
                str(Path(tmp) / "none"),
                "--require",
                "not-a-real-tool",
                "--strict",
                "--generated-utc",
                FIXED_TIME,
            )
            self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)


class ReadOnlyGuaranteeTest(unittest.TestCase):
    FORBIDDEN = (
        "subprocess",
        "os.system",
        "os.startfile",
        "winreg.SetValue",
        "winreg.CreateKey",
        "shutil.rmtree",
        "os.remove",
        "os.unlink",
    )

    def test_package_has_no_launch_or_write_calls(self):
        for path in (TOOLS / "psvr_app_readiness").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for token in self.FORBIDDEN:
                self.assertNotIn(token, text, f"{path.name} contains {token!r}")

    def test_inventory_does_not_modify_the_steam_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            steam_root, _ = build_fake_steam(root)

            def snapshot():
                return sorted(
                    (
                        str(item.relative_to(root)),
                        item.stat().st_size,
                        item.stat().st_mtime_ns,
                    )
                    for item in root.rglob("*")
                )

            before = snapshot()
            inventory.build_report(
                steam_root=str(steam_root), generated_utc=FIXED_TIME
            )
            self.assertEqual(before, snapshot())


if __name__ == "__main__":
    unittest.main()
