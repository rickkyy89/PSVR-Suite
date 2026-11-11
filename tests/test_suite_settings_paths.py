"""B1-07: check_paths optics aliases; files only, no hardware."""
import unittest
from pathlib import Path
import tempfile

from tools.psvr_suite import settings as S


def _touch(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    return path


def _prefs_with_cameras(root):
    """Defaults with the three camera files present under root (isolates optics)."""
    prefs = S.default_preferences()
    for key in ("intrinsicsPath", "rigPath", "imu_rig"):
        _touch(Path(root) / prefs[key])
    return prefs


class CheckPathsOpticsTests(unittest.TestCase):
    def test_identity_needs_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs = _prefs_with_cameras(tmp)
            prefs["optics_profile"] = "identity"
            self.assertEqual(S.check_paths(prefs, root=Path(tmp)), [])

    def test_current_and_missing_key_need_no_file(self):
        for label, mutate in (
            ("current", lambda p: p.update(optics_profile="current")),
            ("missing_key", lambda p: p.pop("optics_profile", None)),
        ):
            with self.subTest(case=label):
                with tempfile.TemporaryDirectory() as tmp:
                    prefs = _prefs_with_cameras(tmp)
                    mutate(prefs)
                    self.assertEqual(S.check_paths(prefs, root=Path(tmp)), [])

    def test_alias_present_and_missing(self):
        alias = next(iter(S.OPTICS_PROFILES))
        for label, present in (("present", True), ("missing", False)):
            with self.subTest(alias=alias, case=label):
                with tempfile.TemporaryDirectory() as tmp:
                    prefs = _prefs_with_cameras(tmp)
                    prefs["optics_profile"] = alias
                    if present:
                        _, resolved = S.resolve_optics_profile(alias, root=Path(tmp))
                        _touch(resolved)
                    missing = S.check_paths(prefs, root=Path(tmp))
                    if present:
                        self.assertEqual(missing, [])
                    else:
                        self.assertEqual(missing, ["optics_profile"])

    def test_custom_relative_present_missing_and_directory(self):
        cases = ("present", "missing", "directory")
        for case in cases:
            with self.subTest(kind="relative", case=case):
                with tempfile.TemporaryDirectory() as tmp:
                    prefs = _prefs_with_cameras(tmp)
                    prefs["optics_profile"] = "custom_optics.json"
                    target = Path(tmp) / "custom_optics.json"
                    if case == "present":
                        _touch(target)
                    elif case == "directory":
                        target.mkdir(parents=True, exist_ok=True)
                    missing = S.check_paths(prefs, root=Path(tmp))
                    if case == "present":
                        self.assertEqual(missing, [])
                    else:
                        self.assertEqual(missing, ["optics_profile"])

    def test_custom_absolute_present_and_missing(self):
        for label, present in (("present", True), ("missing", False)):
            with self.subTest(kind="absolute", case=label):
                with tempfile.TemporaryDirectory() as tmp:
                    prefs = _prefs_with_cameras(tmp)
                    target = Path(tmp) / "abs_optics.json"
                    if present:
                        _touch(target)
                    prefs["optics_profile"] = str(target)
                    missing = S.check_paths(prefs, root=Path(tmp))
                    if present:
                        self.assertEqual(missing, [])
                    else:
                        self.assertEqual(missing, ["optics_profile"])

    def test_camera_files_missing(self):
        for key in ("intrinsicsPath", "rigPath", "imu_rig"):
            with self.subTest(key=key):
                with tempfile.TemporaryDirectory() as tmp:
                    prefs = S.default_preferences()
                    prefs["optics_profile"] = "identity"
                    for other in ("intrinsicsPath", "rigPath", "imu_rig"):
                        if other != key:
                            _touch(Path(tmp) / prefs[other])
                    self.assertEqual(S.check_paths(prefs, root=Path(tmp)), [key])

    def test_check_paths_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs = _prefs_with_cameras(tmp)
            prefs["optics_profile"] = "identity"
            before = sorted(p.name for p in Path(tmp).rglob("*"))
            missing = S.check_paths(prefs, root=Path(tmp))
            after = sorted(p.name for p in Path(tmp).rglob("*"))
            self.assertEqual(missing, [])
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
