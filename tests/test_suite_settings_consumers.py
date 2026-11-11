"""B1-04/05/06: saved preferences reach real consumers, offline only."""
import copy
import json
from pathlib import Path
import queue
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools.psvr_suite import app as A
from tools.psvr_suite import runtime as R
from tools.psvr_suite import settings as S
from tools.psvr_suite import tracking as T


class FpsConsumerTests(unittest.TestCase):
    def test_saved_fps_reaches_worker_as_positive_integer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prefs.json"
            for fps in (1, 75, 75.0, 120):
                with self.subTest(fps=fps):
                    prefs = dict(S.default_preferences(), fps=fps)
                    S.save_preferences(path, prefs)
                    loaded = S.load_preferences(path)
                    worker = T.normalize_prefs(loaded)
                    self.assertEqual(worker["fps"], fps)
                    self.assertIs(type(loaded["fps"]), int)

    def test_fractional_fps_rejected_before_save_or_backup(self):
        for fps in (0.5, 74.5, True, float("nan"), float("inf")):
            with self.subTest(fps=fps), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "prefs.json"
                original = b'{"fps":75}'
                path.write_bytes(original)
                with self.assertRaisesRegex(ValueError, "fps"):
                    S.save_preferences(path, dict(S.default_preferences(), fps=fps))
                self.assertEqual(path.read_bytes(), original)
                self.assertFalse(path.with_name("prefs.json.bak").exists())

    def test_ui_rejects_fractional_fps_before_worker(self):
        with self.assertRaisesRegex(ValueError, "fps"):
            A.AppModel.validate(dict(S.default_preferences(), fps="74.5"))
        prefs = A.AppModel.validate(dict(S.default_preferences(), fps="75.0"))
        self.assertIs(type(prefs["fps"]), int)
        self.assertEqual(T.normalize_prefs(prefs)["fps"], 75)


class DerivativeConsumerTests(unittest.TestCase):
    def test_native_timing_boundaries_written_exactly(self):
        baseline = (S.ROOT / S.IMU_BASELINE_SOURCE).read_bytes()
        for calibration, stale, gap in ((500, 20, 1000), (10000, 1000, 20000)):
            with self.subTest(gap=gap), tempfile.TemporaryDirectory() as tmp:
                prefs = dict(S.default_preferences(), imu_calibration_ms=calibration,
                             imu_stale_ms=stale, imu_max_gap_us=gap)
                dest = Path(tmp) / "derived.json"
                S.derive_imu_config(prefs, dest)
                tracking = json.loads(dest.read_text(encoding="utf-8"))["tracking"]
                self.assertEqual(tracking["calibration_ms"], calibration)
                self.assertEqual(tracking["stale_timeout_ms"], stale)
                self.assertEqual(tracking["max_sample_gap_us"], gap)
        self.assertEqual((S.ROOT / S.IMU_BASELINE_SOURCE).read_bytes(), baseline)

    def test_driver_only_gap_rejected_before_derivative_creation(self):
        for gap in (20001, 30000, 50000):
            with self.subTest(gap=gap), tempfile.TemporaryDirectory() as tmp:
                prefs = dict(S.default_preferences(), imu_max_gap_us=gap)
                before = copy.deepcopy(prefs)
                # The wider driver contract is preserved without clamping.
                self.assertEqual(S.driver_keys_for(prefs)["imuMaxSampleGapUs"], gap)
                dest = Path(tmp) / "derived.json"
                with self.assertRaisesRegex(ValueError, "20000"):
                    S.derive_imu_config(prefs, dest)
                self.assertFalse(dest.exists())
                self.assertEqual(prefs, before)


class DriverDefaultConsumerTests(unittest.TestCase):
    def test_position_opt_in_matches_native_package(self):
        package = S.ROOT / "driver/package/resources/settings/default.vrsettings"
        defaults = json.loads(package.read_text(encoding="utf-8"))[S.DRIVER_SECTION]
        self.assertIs(defaults["positionEnabled"], False)
        for section, mode in (({}, "3dof"), ({"positionEnabled": False}, "3dof"),
                              ({"positionEnabled": True}, "6dof")):
            with self.subTest(section=section):
                before = copy.deepcopy(section)
                prefs = S.preferences_from_driver(source=section)
                self.assertEqual(prefs["tracking_mode"], mode)
                self.assertEqual(T.normalize_prefs(prefs)["tracking_mode"], mode)
                self.assertEqual(section, before)
        self.assertEqual(S.default_preferences()["tracking_mode"], "6dof")

    def test_missing_position_key_imported_by_initial_ui_load_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "steamvr.vrsettings"
            raw = b'{"driver_psvrpc":{"imuCalibrationMs":4000}}'
            path.write_bytes(raw)
            app = A.SuiteApp.__new__(A.SuiteApp)
            app._settings = S
            app.root_path = Path(tmp)
            app.user_path = Path(tmp) / "absent.json"
            app.preferences_arg = None
            app.lang = "it"
            app.display = queue.Queue()
            with patch.object(S, "DEFAULT_VRSETTINGS", path):
                prefs = app._load_initial_prefs()
            self.assertEqual(prefs["tracking_mode"], "3dof")
            self.assertEqual(prefs["imu_calibration_ms"], 4000)
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_invalid_position_flag_is_not_presented_as_a_valid_mode(self):
        for value in (None, 0, 1, "false", "true"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "positionEnabled"):
                S.preferences_from_driver(source={"positionEnabled": value})


def runtime_shell(root):
    """Use real backend path logic; intercept every external operation."""
    backend = R.DefaultBackend(root)
    backend.run = Mock(return_value=SimpleNamespace(
        returncode=0, stdout='F0\n{"event":"summary","valid_count":1}', stderr=""))
    backend.popen = Mock()
    backend.processes = Mock(side_effect=[set(), {"vrserver"}])
    backend.quit_runtime = Mock(return_value=True)
    ctrl = R.RuntimeController(root=root, backend=backend, sleep=lambda _: None)
    ctrl.lock = Mock()  # Never acquire the real suite lock.
    return ctrl, backend


class RuntimeConfigConsumerTests(unittest.TestCase):
    def test_saved_custom_config_reaches_both_start_commands_and_cinema(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for selected in ("custom config.json", str(root / "absolute config.json")):
                with self.subTest(selected=selected):
                    prefs = dict(S.default_preferences(), tracking_config_path=selected)
                    S.save_preferences(root / "prefs.json", prefs)
                    loaded = S.load_preferences(root / "prefs.json")
                    ctrl, backend = runtime_shell(root)
                    self.addCleanup(ctrl.close)
                    self.assertTrue(ctrl.start_vr(loaded))
                    self.assertTrue(ctrl.to_cinema(loaded))
                    diag = [call.args[0] for call in backend.run.call_args_list
                            if call.kwargs.get("log_name") == "diag"]
                    self.assertEqual(len(diag), 3)
                    expected = (root / selected).resolve()
                    for argv in diag:
                        self.assertEqual(argv[argv.index("--config") + 1], str(expected))
                    probe = [call.args[0] for call in backend.run.call_args_list
                             if call.kwargs.get("log_name") == "probe"]
                    self.assertTrue(probe)
                    self.assertTrue(all("--config" not in argv for argv in probe))

    def test_omitted_preferences_keep_backend_default_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctrl, backend = runtime_shell(Path(tmp))
            self.addCleanup(ctrl.close)
            self.assertTrue(ctrl.start_vr())
            self.assertTrue(ctrl.to_cinema())
            for call in backend.run.call_args_list:
                if call.kwargs.get("log_name") == "diag":
                    argv = call.args[0]
                    self.assertEqual(argv[argv.index("--config") + 1], str(backend.config_file()))

    def test_bad_selected_config_does_not_fall_back_or_launch_vr(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctrl, backend = runtime_shell(Path(tmp))
            self.addCleanup(ctrl.close)
            backend.run.return_value = SimpleNamespace(returncode=1, stdout="bad config")
            prefs = dict(S.default_preferences(), tracking_config_path="missing.json")
            self.assertFalse(ctrl.start_vr(prefs))
            backend.run.assert_called_once()
            argv = backend.run.call_args.args[0]
            self.assertEqual(argv[argv.index("--config") + 1], str(Path(tmp) / "missing.json"))
            backend.popen.assert_not_called()
