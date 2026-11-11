"""Suite preference + driver-store contracts; files only, no hardware."""
import copy
import json
import math
import unittest
from pathlib import Path
import tempfile

from tools.psvr_suite import settings as S


class DefaultsTests(unittest.TestCase):
    def test_initial_setup_does_not_restore_rejected_peripheral_profile(self):
        from tools.psvr_suite.setup_trial import argument_parser
        parsed = argument_parser().parse_args(['--backup-dir', 'unused-offline-fixture'])
        self.assertEqual(parsed.optics, 'identity')

    def test_exact_defaults(self):
        prefs = S.default_preferences()
        self.assertEqual(prefs["tracking_mode"], "6dof")
        self.assertIs(prefs["fallback_3dof"], False)
        self.assertEqual(prefs["reacquire_blend_ms"], 150)
        self.assertEqual(prefs["reacquire_speed_m_s"], 1.5)
        self.assertEqual(prefs["initial_offset_m"], [0, 0, 0])
        self.assertEqual(prefs["head_offset_model_m"], [0, 0, 0])
        self.assertEqual(prefs["position_scale"], 1.0)
        self.assertEqual(prefs["position_filter"], "one_euro")
        self.assertEqual(prefs["min_cutoff_hz"], 2.5)
        self.assertEqual(prefs["beta"], 8.0)
        self.assertEqual(prefs["min_area"], 6)
        self.assertEqual(prefs["value_min"], 60)
        self.assertEqual(prefs["saturation_min"], 90)
        self.assertEqual(prefs["opening_size"], 0)
        self.assertEqual(prefs["exposure"], 13)
        self.assertEqual(prefs["gain"], 0)
        self.assertEqual(prefs["fps"], 75)
        self.assertEqual(prefs["width"], 640)
        self.assertEqual(prefs["height"], 480)
        self.assertTrue(prefs["intrinsicsPath"].endswith(".json"))
        self.assertTrue(prefs["rigPath"].endswith(".json"))
        self.assertTrue(prefs["imu_rig"].endswith(".json"))
        self.assertEqual(prefs["optics_profile"], "identity")
        self.assertEqual(prefs["tracking_config_path"], "config.json")
        self.assertEqual(prefs["imu_calibration_ms"], 8000)
        self.assertEqual(prefs["imu_stale_ms"], 100)
        self.assertEqual(prefs["imu_max_gap_us"], 5000)
        self.assertEqual(prefs["camera_source"], "pseye")


class ValidateTests(unittest.TestCase):
    def test_roundtrip_returns_new_dict(self):
        prefs = S.default_preferences()
        cleaned = S.validate_preferences(prefs)
        self.assertEqual(cleaned, prefs)
        self.assertIsNot(cleaned, prefs)
        self.assertIsNot(cleaned["initial_offset_m"], prefs["initial_offset_m"])

    def test_rejects_unknown(self):
        prefs = S.default_preferences()
        prefs["nope"] = 1
        with self.assertRaises(ValueError):
            S.validate_preferences(prefs)

    def test_rejects_missing(self):
        prefs = S.default_preferences()
        del prefs["beta"]
        with self.assertRaises(ValueError):
            S.validate_preferences(prefs)

    def test_no_autocorrect_ranges(self):
        base = S.default_preferences()
        bad = {
            "tracking_mode": "9dof",
            "reacquire_blend_ms": 2001,
            "reacquire_speed_m_s": 0.001,
            "initial_offset_m": [11, 0, 0],
            "position_scale": 0,
            "position_filter": "kalman",
            "min_cutoff_hz": 0,
            "beta": -1,
            "min_area": 0,
            "value_min": 256,
            "saturation_min": -1,
            "opening_size": 7,
            "exposure": 256,
            "gain": 64,
            "fps": 0,
            "width": 0,
            "height": -1,
            "imu_calibration_ms": 499,
            "imu_stale_ms": 19,
            "imu_max_gap_us": 999,
        }
        for key, value in bad.items():
            with self.subTest(key=key):
                prefs = S.default_preferences()
                prefs[key] = value
                with self.assertRaises(ValueError):
                    S.validate_preferences(prefs)
                self.assertEqual(prefs[key], value)

    def test_imu_timing_upper_bounds(self):
        for key, value in (("imu_calibration_ms", 10001), ("imu_stale_ms", 1001),
                           ("imu_max_gap_us", 50001)):
            with self.subTest(key=key):
                prefs = S.default_preferences()
                prefs[key] = value
                with self.assertRaises(ValueError):
                    S.validate_preferences(prefs)

    def test_rejects_nan_and_bool_abuse(self):
        for key, value in (("beta", math.nan), ("reacquire_speed_m_s", math.inf),
                           ("initial_offset_m", [0, math.nan, 0]),
                           ("fallback_3dof", 1), ("reacquire_blend_ms", True),
                           ("min_area", 6.0), ("tracking_mode", "6DOF")):
            with self.subTest(key=key):
                prefs = S.default_preferences()
                prefs[key] = value
                with self.assertRaises(ValueError):
                    S.validate_preferences(prefs)

    def test_rejects_blank_paths(self):
        for key in ("intrinsicsPath", "rigPath", "imu_rig", "tracking_config_path", "optics_profile"):
            with self.subTest(key=key):
                prefs = S.default_preferences()
                prefs[key] = "  "
                with self.assertRaises(ValueError):
                    S.validate_preferences(prefs)


class LoadSaveTests(unittest.TestCase):
    def test_load_missing_yields_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs = S.load_preferences(Path(tmp) / "absent.json")
            self.assertEqual(prefs, S.default_preferences())

    def test_load_preserves_current_known_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prefs.json"
            path.write_text(json.dumps({"beta": 3.0, "exposure": 9}), encoding="utf-8")
            prefs = S.load_preferences(path)
            self.assertEqual(prefs["beta"], 3.0)
            self.assertEqual(prefs["exposure"], 9)
            self.assertEqual(prefs["min_cutoff_hz"], 2.5)

    def test_load_rejects_unknown_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prefs.json"
            path.write_text(json.dumps({"beta": 3.0, "mystery": 1}), encoding="utf-8")
            with self.assertRaises(ValueError):
                S.load_preferences(path)

    def test_save_atomic_with_local_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prefs.json"
            path.write_text(json.dumps(S.default_preferences()), encoding="utf-8")
            prefs = S.default_preferences()
            prefs["beta"] = 4.0
            S.save_preferences(path, prefs)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["beta"], 4.0)
            self.assertTrue(path.with_name("prefs.json.bak").is_file())

    def test_save_rejects_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prefs.json"
            prefs = S.default_preferences()
            prefs["gain"] = 99
            with self.assertRaises(ValueError):
                S.save_preferences(path, prefs)
            self.assertFalse(path.exists())

    def test_save_refuses_global_config(self):
        prefs = S.default_preferences()
        with self.assertRaises(ValueError):
            S.save_preferences(S.ROOT / "config.json", prefs)


def _vrsettings(section_extra=None, other_apps=None):
    section = {
        "positionEnabled": True, "imuExportEnabled": True,
        "displayEnabled": False, "displayWidth": 1920,
        "distortionMode": "identity", "projectionLeftLeft": -1.0,
    }
    section.update(section_extra or {})
    data = {"driver_psvrpc": section}
    data.update(other_apps or {})
    return data


class DriverStoreTests(unittest.TestCase):
    def _store(self, tmp, section_extra=None, other_apps=None, running=(), **kwargs):
        path = Path(tmp) / "steamvr.vrsettings"
        path.write_text(json.dumps(_vrsettings(section_extra, other_apps)), encoding="utf-8")
        store = S.DriverSettingStore(path, Path(tmp) / "backup",
                                     running_checker=lambda: set(running), **kwargs)
        return store, path

    def test_apply_maps_owned_keys_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, path = self._store(tmp, section_extra={"positionFallback3DofEnabled": True},
                                      other_apps={"steamvr": {"x": 1}})
            prefs = S.default_preferences()
            prefs["tracking_mode"] = "3dof"
            prefs["fallback_3dof"] = False
            prefs["initial_offset_m"] = [0.1, -0.2, 0.3]
            journal = store.apply(prefs)
            data = json.loads(path.read_text(encoding="utf-8"))
            section = data["driver_psvrpc"]
            self.assertIs(section["positionEnabled"], False)
            self.assertIs(section["imuExportEnabled"], True)
            self.assertNotIn("positionFallback3DofEnabled", section)
            self.assertEqual(section["positionReacquireBlendMs"], 150)
            self.assertEqual(section["positionReacquireMaxSpeedMps"], 1.5)
            self.assertEqual(section["positionInitialOffsetX"], 0.1)
            self.assertEqual(section["imuCalibrationMs"], 8000)
            self.assertEqual(section["imuStaleTimeoutMs"], 100)
            self.assertEqual(section["imuMaxSampleGapUs"], 5000)
            self.assertEqual(section["displayWidth"], 1920)
            self.assertEqual(section["distortionMode"], "identity")
            self.assertEqual(data["steamvr"], {"x": 1})
            self.assertTrue(journal["applied"])
            self.assertIn("positionEnabled", journal["changed_keys"])
            self.assertIn("positionFallback3DofEnabled", journal["changed_keys"])
            self.assertIn("positionFallback3DofEnabled", journal["after_absent"])

    def test_fallback_true_writes_optional_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, path = self._store(tmp)
            prefs = S.default_preferences()
            prefs["fallback_3dof"] = True
            store.apply(prefs)
            section = json.loads(path.read_text(encoding="utf-8"))["driver_psvrpc"]
            self.assertIs(section["positionFallback3DofEnabled"], True)

    def test_fresh_defaults_fallback_off_and_identity(self):
        prefs = S.default_preferences()
        self.assertIs(prefs["fallback_3dof"], False)
        self.assertEqual(prefs["optics_profile"], "identity")
        with tempfile.TemporaryDirectory() as tmp:
            fresh = S.load_preferences(Path(tmp) / "absent.json")
            self.assertIs(fresh["fallback_3dof"], False)
            self.assertEqual(fresh["optics_profile"], "identity")
            # Fresh apply omette la chiave opzionale (driver default false).
            store, path = self._store(tmp)
            journal = store.apply(S.default_preferences())
            section = json.loads(path.read_text(encoding="utf-8"))["driver_psvrpc"]
            self.assertNotIn("positionFallback3DofEnabled", section)
            self.assertIn("positionFallback3DofEnabled", journal["after_absent"])

    def test_default_backup_root_unique(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "steamvr.vrsettings"
            path.write_text(json.dumps(_vrsettings()), encoding="utf-8")
            first = S.DriverSettingStore(path, running_checker=lambda: set(), root=Path(tmp))
            second = S.DriverSettingStore(path, running_checker=lambda: set(), root=Path(tmp))
            first.apply(S.default_preferences())
            second.apply(S.default_preferences())  # must not collide
            self.assertEqual(len(list((Path(tmp) / "logs" / "suite_backups").iterdir())), 2)

    def test_optics_preserved_unless_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, path = self._store(tmp)
            prefs = S.default_preferences()
            prefs["optics_profile"] = "mild"
            store.apply(prefs)
            section = json.loads(path.read_text(encoding="utf-8"))["driver_psvrpc"]
            self.assertEqual(section["distortionMode"], "identity")
            self.assertNotIn("distortionProfilePath", section)
            calls = []
            store2 = S.DriverSettingStore(path, Path(tmp) / "backup2",
                                          running_checker=lambda: set(),
                                          optics_validator=lambda p: calls.append(str(p)) or {})
            store2.apply(prefs, apply_optics=True)
            section = json.loads(path.read_text(encoding="utf-8"))["driver_psvrpc"]
            self.assertEqual(section["distortionMode"], "profile")
            self.assertTrue(section["distortionProfilePath"].endswith(
                "optics_radial_mild_20261014.json"))
            self.assertEqual(len(calls), 1)

    def test_optics_identity_and_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, path = self._store(tmp)
            prefs = S.default_preferences()
            prefs["optics_profile"] = "identity"
            journal = store.apply(prefs, apply_optics=True)
            section = json.loads(path.read_text(encoding="utf-8"))["driver_psvrpc"]
            self.assertEqual(section["distortionMode"], "identity")
            self.assertEqual(section["distortionProfilePath"], "")
            self.assertIn("distortionMode", journal["owned_keys"])
            store2 = S.DriverSettingStore(path, Path(tmp) / "backup2",
                                          running_checker=lambda: set())
            current_prefs = S.default_preferences()
            current_prefs["optics_profile"] = "current"
            journal2 = store2.apply(current_prefs, apply_optics=True)
            self.assertNotIn("distortionMode", journal2["changed_keys"])

    def test_optics_validator_before_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, path = self._store(tmp)
            prefs = S.default_preferences()
            prefs["optics_profile"] = "mild"

            def reject(_path):
                raise RuntimeError("bad profile")

            store_bad = S.DriverSettingStore(path, Path(tmp) / "backup",
                                             running_checker=lambda: set(),
                                             optics_validator=reject)
            before = path.read_bytes()
            with self.assertRaises(RuntimeError):
                store_bad.apply(prefs, apply_optics=True)
            self.assertEqual(path.read_bytes(), before)

    def test_guard_runtime_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = self._store(tmp, running=("vrserver",))
            with self.assertRaises(RuntimeError):
                store.apply(S.default_preferences())

    def test_rollback_restores_and_absence(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, path = self._store(tmp)
            journal = store.apply(S.default_preferences())
            report = store.rollback(journal)
            section = json.loads(path.read_text(encoding="utf-8"))["driver_psvrpc"]
            self.assertNotIn("positionFallback3DofEnabled", section)
            self.assertIn("positionReacquireBlendMs", report["restored_keys"])

    def test_rollback_absent_vs_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, path = self._store(tmp)
            journal = store.apply(S.default_preferences())
            data = json.loads(path.read_text(encoding="utf-8"))
            # A null value is NOT absence: rollback must refuse, not conflate.
            data["driver_psvrpc"]["positionFallback3DofEnabled"] = None
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                store.rollback(journal)

    def test_rollback_refuses_unrelated_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, path = self._store(tmp)
            journal = store.apply(S.default_preferences())
            data = json.loads(path.read_text(encoding="utf-8"))
            data["driver_psvrpc"]["displayWidth"] = 1280
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                store.rollback(journal)

    def test_rollback_refuses_stale_owned(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, path = self._store(tmp)
            journal = store.apply(S.default_preferences())
            data = json.loads(path.read_text(encoding="utf-8"))
            data["driver_psvrpc"]["positionReacquireBlendMs"] = 999
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                store.rollback(journal)

    def test_rollback_restores_optics(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, path = self._store(tmp)
            prefs = S.default_preferences()
            prefs["optics_profile"] = "identity"
            journal = store.apply(prefs, apply_optics=True)
            store.rollback(journal)
            section = json.loads(path.read_text(encoding="utf-8"))["driver_psvrpc"]
            self.assertEqual(section["distortionMode"], "identity")
            self.assertNotIn("distortionProfilePath", section)

    def test_backup_and_journal_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = self._store(tmp)
            journal = store.apply(S.default_preferences())
            backup_dir = Path(tmp) / "backup"
            self.assertTrue((backup_dir / "settings_before.json").is_file())
            disk = json.loads((backup_dir / "journal.json").read_text(encoding="utf-8"))
            self.assertEqual(disk["schema"], S.DriverSettingStore.SCHEMA)
            self.assertTrue(disk["applied"])
            self.assertEqual(journal, disk)
            self.assertIn("after_absent", disk)
            self.assertIn("section_preserved_sha256", disk)

    def test_preferences_from_driver(self):
        section = {"positionEnabled": False, "positionReacquireBlendMs": 300,
                   "positionInitialOffsetX": 1.0, "imuCalibrationMs": 4000,
                   "distortionProfilePath": "x.json"}
        prefs = S.preferences_from_driver(source=section)
        self.assertEqual(prefs["tracking_mode"], "3dof")
        self.assertIs(prefs["fallback_3dof"], False)
        self.assertEqual(prefs["reacquire_blend_ms"], 300)
        self.assertEqual(prefs["initial_offset_m"], [1.0, 0.0, 0.0])
        self.assertEqual(prefs["imu_calibration_ms"], 4000)
        self.assertEqual(prefs["imu_stale_ms"], 100)
        self.assertEqual(prefs["optics_profile"], "current")


class ImuConfigTests(unittest.TestCase):
    def test_baseline_readonly(self):
        baseline = S.baseline_imu_config()
        self.assertIn("calibration_ms", baseline)
        baseline["calibration_ms"] = -1
        again = S.baseline_imu_config()
        self.assertEqual(again["calibration_ms"], 8000)

    def test_derivative_native_schema_no_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs = S.default_preferences()
            prefs["imu_calibration_ms"] = 4000
            dest = Path(tmp) / "derived.json"
            out = S.derive_imu_config(prefs, dest)
            self.assertEqual(out, dest)
            payload = json.loads(dest.read_text(encoding="utf-8"))
            self.assertEqual(set(payload), {"schema_version", "diagnostic", "tracking"})
            self.assertNotIn("baseline_source", payload)
            self.assertNotIn("suite_derived", payload)
            self.assertEqual(payload["tracking"]["calibration_ms"], 4000)
            self.assertEqual(payload["tracking"]["stale_timeout_ms"], 100)
            self.assertEqual(payload["tracking"]["max_sample_gap_us"], 5000)
            global_tracking = S.baseline_imu_config()
            self.assertEqual(global_tracking["calibration_ms"], 8000)
            with self.assertRaises(FileExistsError):
                S.derive_imu_config(prefs, dest)
            with self.assertRaises(ValueError):
                S.derive_imu_config(prefs, "config.json")


if __name__ == "__main__":
    unittest.main()
