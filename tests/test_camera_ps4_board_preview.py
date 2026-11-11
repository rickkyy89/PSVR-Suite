"""Synthetic-only tests: no hardware, firmware, preferences or GUI is opened."""
import contextlib
import io
import json
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.psvr_camera import ps4_board_preview as board
from tools.psvr_camera.camera_backend import CameraError


def fake_cv(original_exposure=-3, original_gain=4, behavior=None, read_error=False,
            cache_controls=False):
    caps = []
    frame = np.zeros((408, 1748, 3), np.uint8)

    class Cap:
        def __init__(self, index, backend):
            self.index, self.backend = index, backend
            self.values = {15: original_exposure, 14: original_gain, 10: 4}
            self.cached = self.values.copy()
            self.events = []
            self.released = False
            self.owner = threading.get_ident()
            caps.append(self)

        def event(self, *event):
            assert threading.get_ident() == self.owner
            self.events.append(event)

        def isOpened(self):
            return True

        def set(self, prop, value):
            self.event("set", prop, value)
            if behavior is not None:
                result = behavior(self, prop, value)
                if result is not None:
                    return result
            self.values[prop] = value
            return True

        def get(self, prop):
            self.event("get", prop)
            if cache_controls and prop in self.cached:
                return self.cached[prop]
            return self.values.get(prop, 0)

        def read(self):
            self.event("read")
            if read_error:
                raise RuntimeError("synthetic read failure")
            return True, frame

        def release(self):
            self.event("release")
            self.released = True

    return types.SimpleNamespace(VideoCapture=Cap, CAP_DSHOW=700, CAP_ANY=0,
        CAP_PROP_FRAME_WIDTH=3, CAP_PROP_FRAME_HEIGHT=4, CAP_PROP_FPS=5,
        CAP_PROP_FOURCC=6, CAP_PROP_EXPOSURE=15, CAP_PROP_GAIN=14,
        CAP_PROP_BRIGHTNESS=10), caps


class BoardPreviewTests(unittest.TestCase):
    def run_fake(self, **kwargs):
        module, caps = fake_cv(**kwargs.pop("fake", {}))
        report = board.run_preview(0, seconds=0.1, module=module, **kwargs)
        return report, caps[0]

    def test_default_does_not_read_or_write_controls(self):
        report, cap = self.run_fake()
        self.assertEqual(report["errors"], [])
        self.assertTrue(cap.released)
        self.assertFalse(any(e[0] in ("set", "get") and e[1] in (10, 14, 15)
                             for e in cap.events))
        self.assertFalse(report["calibrated"])
        self.assertFalse(report["tracking_6dof"])

    def test_after_format_snapshot_apply_and_restore_on_same_owner(self):
        report, cap = self.run_fake(exposure=-1, gain=8)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["before"], {"exposure": -3, "gain": 4, "brightness": 4})
        self.assertEqual(report["readback"], {"exposure": -1, "gain": 8, "brightness": None})
        self.assertEqual(report["restored"], {"exposure": -3, "gain": 4, "brightness": None})
        self.assertEqual(report["set_ok"], {"exposure": True, "gain": True, "brightness": None})
        self.assertEqual(report["restoration_set_ok"], report["set_ok"])
        self.assertLess(cap.events.index(("set", 5, 60.0)),
                        cap.events.index(("get", 15)))
        self.assertEqual(cap.events[-1], ("release",))
        self.assertNotEqual(cap.owner, threading.get_ident())
        self.assertFalse(report["restoration_failed"])

    def test_only_requested_changed_control_is_restored(self):
        report, cap = self.run_fake(exposure=-2)
        self.assertEqual(report["errors"], [])
        self.assertFalse(any(e[0] == "set" and e[1] == 14 for e in cap.events))
        self.assertEqual(cap.values[15], -3)

    def test_brightness_is_opt_in_and_restored(self):
        report, cap = self.run_fake(brightness=12)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["before"]["brightness"], 4)
        self.assertEqual(report["requested"]["brightness"], 12)
        self.assertEqual(report["readback"]["brightness"], 12)
        self.assertEqual(report["restored"]["brightness"], 4)
        self.assertEqual(cap.values[10], 4)
        self.assertFalse(any(e[0] == "set" and e[1] in (14, 15) for e in cap.events))

    def test_same_target_has_no_control_set_or_rollback(self):
        report, cap = self.run_fake(exposure=-3, gain=4)
        self.assertEqual(report["errors"], [])
        self.assertFalse(any(e[0] == "set" and e[1] in (14, 15) for e in cap.events))

    def test_negative_one_exposure_original_is_accepted(self):
        report, cap = self.run_fake(exposure=-2, fake={"original_exposure": -1})
        self.assertEqual(report["errors"], [])
        self.assertEqual(cap.values[15], -1)

    def test_unreadable_requested_original_fails_closed_before_any_set(self):
        for originals in ({"original_gain": -1}, {"original_exposure": float("nan")},
                          {"original_exposure": float("inf")}):
            with self.subTest(originals=originals):
                report, cap = self.run_fake(exposure=-2, gain=8, fake=originals)
                self.assertTrue(report["errors"])
                self.assertTrue(cap.released)
                self.assertFalse(any(e[0] == "set" and e[1] in (14, 15)
                                     for e in cap.events))
                json.dumps(report, allow_nan=False)

    def test_read_exception_still_restores(self):
        report, cap = self.run_fake(exposure=-2, gain=8, fake={"read_error": True})
        self.assertTrue(report["errors"])
        self.assertEqual(cap.values[15], -3)
        self.assertEqual(cap.values[14], 4)
        self.assertTrue(cap.released)

    def test_rejected_set_with_side_effect_is_rolled_back(self):
        def behavior(cap, prop, value):
            if prop == 15 and value == -2:
                cap.values[prop] = value
                return False
        report, cap = self.run_fake(exposure=-2, fake={"behavior": behavior})
        self.assertFalse(report["set_ok"]["exposure"])
        self.assertEqual(report["readback"]["exposure"], -2)
        self.assertEqual(cap.values[15], -3)
        self.assertTrue(report["errors"])
        self.assertFalse(report["restoration_failed"])

    def test_rejected_unchanged_set_still_writes_original(self):
        def behavior(cap, prop, value):
            if prop == 15 and value == -2:
                return False
        report, cap = self.run_fake(exposure=-2, fake={"behavior": behavior})
        self.assertTrue(report["errors"])
        self.assertEqual(report["readback"]["exposure"], -3)
        self.assertEqual([e for e in cap.events if e[0] == "set" and e[1] == 15],
                         [("set", 15, -2), ("set", 15, -3)])
        self.assertFalse(report["restoration_failed"])

    def test_cached_get_default_aborts_but_always_rolls_back_actual_change(self):
        report, cap = self.run_fake(exposure=-2, fake={"cache_controls": True})
        self.assertTrue(report["errors"])
        self.assertFalse(report["readback_confirmed"]["exposure"])
        self.assertEqual(report["readback"]["exposure"], -3)
        self.assertEqual(cap.values[15], -3)
        self.assertEqual([e for e in cap.events if e[0] == "set" and e[1] == 15],
                         [("set", 15, -2), ("set", 15, -3)])
        self.assertTrue(report["restore_write_accepted"]["exposure"])
        self.assertFalse(report["restoration_independently_verified"])

    def test_delayed_readback_opt_in_delivers_pairs_and_rolls_back_all_attempts(self):
        module, caps = fake_cv(cache_controls=True)
        observed = []

        def on_pair(left, right):
            observed.append(caps[0].values.copy())
            return False

        report = board.run_preview(0, seconds=0.1, exposure=-2, gain=8, brightness=8,
                                   allow_delayed_readback=True, on_pair=on_pair,
                                   module=module)
        self.assertEqual(report["errors"], [])
        self.assertEqual(len(caps), 1)  # No reopen or automatic independent check.
        self.assertEqual(len(observed), 1)
        self.assertEqual([observed[0][p] for p in (15, 14, 10)], [-2, 8, 8])
        self.assertEqual(len(report["warnings"]), 3)
        self.assertEqual(report["readback_confirmed"],
                         dict.fromkeys(("exposure", "gain", "brightness"), False))
        for prop, original in ((15, -3), (14, 4), (10, 4)):
            self.assertIn(("set", prop, original), caps[0].events)
            self.assertEqual(caps[0].values[prop], original)
        self.assertTrue(all(report["restore_write_accepted"].values()))
        self.assertTrue(report["restoration_readback_is_observation"])
        self.assertFalse(report["restoration_independently_verified"])
        self.assertTrue(caps[0].released)

    def test_delayed_readback_does_not_allow_rejected_write_or_unreadable_original(self):
        def behavior(cap, prop, value):
            if prop == 15 and value == -2:
                cap.values[prop] = value
                return False
        report, cap = self.run_fake(exposure=-2, allow_delayed_readback=True,
                                    fake={"behavior": behavior, "cache_controls": True})
        self.assertTrue(report["errors"])
        self.assertEqual(cap.values[15], -3)
        report, cap = self.run_fake(gain=8, allow_delayed_readback=True,
                                    fake={"original_gain": -1})
        self.assertTrue(report["errors"])
        self.assertFalse(any(e[0] == "set" and e[1] == 14 for e in cap.events))

    def test_clamped_target_is_reported_and_rolled_back(self):
        def behavior(cap, prop, value):
            if prop == 15 and value == -1:
                cap.values[prop] = -2
                return True
        report, cap = self.run_fake(exposure=-1, fake={"behavior": behavior})
        self.assertTrue(report["set_ok"]["exposure"])
        self.assertEqual(report["readback"]["exposure"], -2)
        self.assertTrue(report["errors"])
        self.assertEqual(cap.values[15], -3)

    def test_raising_set_with_side_effect_and_other_control_restore(self):
        def behavior(cap, prop, value):
            if prop == 14 and value == 8:
                cap.values[prop] = value
                raise RuntimeError("synthetic set failure")
        report, cap = self.run_fake(exposure=-2, gain=8, fake={"behavior": behavior})
        self.assertTrue(report["errors"])
        self.assertEqual(cap.values[15], -3)
        self.assertEqual(cap.values[14], 4)
        self.assertTrue(cap.released)

    def test_restoration_failure_visible_and_other_control_still_restores(self):
        def behavior(cap, prop, value):
            if prop == 14 and value == 4:
                return False
        report, cap = self.run_fake(exposure=-2, gain=8, fake={"behavior": behavior})
        self.assertTrue(report["restoration_failed"])
        self.assertFalse(report["restoration_set_ok"]["gain"])
        self.assertTrue(any("gain restoration failed" in e for e in report["errors"]))
        self.assertEqual(cap.values[15], -3)
        self.assertTrue(cap.released)

    def test_restoration_exception_does_not_skip_other_controls_or_release(self):
        def behavior(cap, prop, value):
            if prop == 14 and value == 4:
                raise RuntimeError("synthetic restoration failure")
        report, cap = self.run_fake(exposure=-2, gain=8, fake={"behavior": behavior})
        self.assertTrue(report["restoration_failed"])
        self.assertEqual(cap.values[15], -3)
        self.assertTrue(cap.released)

    def test_unreadable_readback_still_attempts_rollback(self):
        def behavior(cap, prop, value):
            if prop == 15 and value == -2:
                cap.values[prop] = float("nan")
                return True
        report, cap = self.run_fake(exposure=-2, fake={"behavior": behavior})
        self.assertTrue(report["errors"])
        self.assertEqual(cap.values[15], -3)
        self.assertFalse(report["restoration_failed"])
        json.dumps(report, allow_nan=False)

    def test_missing_requested_property_fails_closed(self):
        module, caps = fake_cv()
        del module.CAP_PROP_BRIGHTNESS
        report = board.run_preview(0, seconds=0.1, exposure=-2,
                                   brightness=12, module=module)
        self.assertTrue(report["errors"])
        self.assertFalse(any(e[0] == "set" and e[1] in (10, 14, 15)
                             for e in caps[0].events))
        self.assertTrue(caps[0].released)

    def test_capture_pending_cleanup_never_claims_success(self):
        module, _ = fake_cv()
        with patch.object(board, "capture_stereo", return_value={
                "errors": [], "cleanup_pending": True}):
            report = board.run_preview(0, exposure=-2, module=module)
        self.assertTrue(report["errors"])
        self.assertIn("restoration is not confirmed", report["errors"][0])

    def test_release_without_read_and_repeated_release_are_safe(self):
        module, caps = fake_cv()
        state = board._ControlState(-2, 8)
        proxy = board._Cv2Proxy(module, state)
        cap = proxy.VideoCapture(0, 700)
        cap.release()
        cap.release()
        self.assertEqual(caps[0].events, [("release",)])
        self.assertFalse(state.snapshot()["restoration_pending"])

    def test_proxy_forwards_module_and_capture_attributes(self):
        module, _ = fake_cv()
        proxy = board._Cv2Proxy(module, board._ControlState(None, None))
        self.assertEqual(proxy.CAP_DSHOW, 700)
        self.assertTrue(proxy.VideoCapture(3, 700).isOpened())

    def test_preview_uses_tk_not_opencv_highgui(self):
        module, _ = fake_cv()
        tk = Mock()
        with patch.dict(sys.modules, {"tkinter": tk}):
            report = board.run_preview(0, seconds=0.1, preview=True, module=module)
        self.assertEqual(report["errors"], [])
        tk.Tk.return_value.destroy.assert_called_once()
        tk.PhotoImage.assert_called()

    def test_validation_before_import_or_open(self):
        for args in ({"index": -1}, {"index": None}, {"seconds": 61},
                     {"seconds": 0}, {"seconds": float("nan")},
                     {"exposure": float("inf")}, {"gain": -1}):
            with self.subTest(args=args), patch.object(board, "import_cv2") as imp:
                with self.assertRaises(CameraError):
                    board.run_preview(**{"index": 0, **args})
                imp.assert_not_called()

    def test_cli_requires_index_and_forwards_only_explicit_targets(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            board.main([])
        with patch.object(board, "run_preview", return_value={
                "errors": [], "restoration_failed": False}) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(board.main(["--index", "0", "--preview"]), 0)
            run.assert_called_once_with(0, 10.0, None, None, True, brightness=None,
                                        allow_delayed_readback=False)

    def test_cli_restoration_failure_exit_code(self):
        with patch.object(board, "run_preview", return_value={
                "errors": ["restore failure"], "restoration_failed": True}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(board.main(["--index", "0"]), 2)

    def test_cli_explicit_brightness_target(self):
        with patch.object(board, "run_preview", return_value={
                "errors": [], "restoration_failed": False}) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(board.main(["--index", "0", "--exposure", "-2",
                                         "--gain", "8", "--brightness", "12"]), 0)
            run.assert_called_once_with(0, 10.0, -2.0, 8.0, False, brightness=12.0,
                                        allow_delayed_readback=False)

    def test_cli_allow_delayed_readback_is_explicit(self):
        with patch.object(board, "run_preview", return_value={
                "errors": [], "restoration_failed": False}) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(board.main(["--index", "0", "--allow-delayed-readback"]), 0)
            self.assertTrue(run.call_args.kwargs["allow_delayed_readback"])


if __name__ == "__main__":
    unittest.main()
