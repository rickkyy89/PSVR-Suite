"""Offline Suite PS4 control-ownership tests: fake cv2, no hardware, no binds.

The Suite acquisition (``Ps4SuiteSource.open`` / ``StereoSession`` with
``restore_controls=False``) owns the profile brightness persistently:
apply once after open, verify readback with the calibration tool's field
shape, warn-and-continue on mismatch, never restore previous values.
The temporary calibration tool (``ps4_board_preview``) keeps its own
restore behaviour untouched.

Injected cv2 controls exercise the offline adapter seam, not verified hardware
identity. Public controls_status() describes an unreleased source only; retain
a live snapshot for assertions and use the session report for close diagnostics.
"""
import copy
import json
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.psvr_camera import ps4_suite_source as source
from tools.psvr_camera import ps4_stereo_session as stereo
from tools.psvr_camera.camera_backend import CameraError

import queue as queue_module

ROOT = Path(__file__).resolve().parents[1]


def fake_suite_cv(*, cached=False, reject_brightness=False):
    """Queue-fed raw capture; every driver call asserts single-owner use."""
    caps, work = [], queue_module.Queue()
    released = threading.Event()

    class Cap:
        def __init__(self, index, backend):
            self.owner = threading.get_ident()
            self.values = {15: -3, 14: 4, 10: 4}
            self.originals = self.values.copy()
            self.events = []
            self.frame = np.zeros((408, 1748, 3), np.uint8)
            caps.append(self)

        def _event(self, *event):
            assert threading.get_ident() == self.owner
            self.events.append(event)

        def isOpened(self):
            self._event("isOpened")
            return True

        def set(self, prop, value):
            self._event("set", prop, value)
            if reject_brightness and prop == 10:
                return False
            self.values[prop] = value
            return True

        def get(self, prop):
            self._event("get", prop)
            if cached and prop in self.originals:
                return self.originals[prop]
            return self.values.get(prop, 0)

        def read(self):
            self._event("read")
            try:
                value = work.get(timeout=0.02)
            except queue_module.Empty:
                return False, None
            self.frame.fill(value)
            return True, self.frame

        def release(self):
            self._event("release")
            released.set()

    module = types.SimpleNamespace(
        VideoCapture=Cap, CAP_DSHOW=700, CAP_ANY=0,
        CAP_PROP_FRAME_WIDTH=3, CAP_PROP_FRAME_HEIGHT=4, CAP_PROP_FPS=5,
        CAP_PROP_FOURCC=6, CAP_PROP_EXPOSURE=15, CAP_PROP_GAIN=14,
        CAP_PROP_BRIGHTNESS=10)
    return module, caps, work, released


def suite_controls():
    return {"exposure": None, "gain": None, "brightness": 0}


class SuiteControlsTests(unittest.TestCase):
    def test_brightness_applied_once_exposure_gain_never_touched(self):
        module, caps, work, _ = fake_suite_cv()
        for value in (23, 24, 25):
            work.put(value)
        with patch.object(source, "_active_session", None):
            cap = source.Ps4SuiteSource.open(
                0, module=module, controls=suite_controls())
            try:
                self.assertTrue(cap.read()[0])
                controls = cap.controls_status()
            finally:
                self.assertTrue(cap.release())
        self.assertIsNone(cap.controls_status())
        self.assertIsNone(cap.settings_capabilities())
        final_controls = cap._session.report()["controls"]
        self.assertEqual(len(caps), 1)
        brightness_sets = [e for e in caps[0].events
                           if e[0] == "set" and e[1] == 10]
        # Single application point: exactly one brightness write per open.
        self.assertEqual(brightness_sets, [("set", 10, 0)])
        self.assertFalse(any(e[0] == "set" and e[1] in (14, 15)
                             for e in caps[0].events))
        self.assertFalse(any(e[0] == "get" and e[1] in (14, 15)
                             for e in caps[0].events))
        self.assertEqual(controls["requested"],
                         {"exposure": None, "gain": None, "brightness": 0})
        self.assertEqual(controls["before"]["brightness"], 4)
        self.assertTrue(controls["set_ok"]["brightness"])
        self.assertEqual(controls["readback"]["brightness"], 0)
        self.assertTrue(controls["readback_confirmed"]["brightness"])
        self.assertIsNone(controls["readback"]["exposure"])
        self.assertIsNone(controls["readback"]["gain"])
        # Suite owns brightness persistently: no rollback on release.
        self.assertEqual(caps[0].values[10], 0)
        self.assertIsNone(final_controls["restored"]["brightness"])
        self.assertFalse(final_controls["restoration_pending"])
        self.assertFalse(any(e == ("set", 10, 4) for e in caps[0].events))

    def test_stale_readback_warns_and_continues_without_restore(self):
        module, caps, work, _ = fake_suite_cv(cached=True)
        for value in (31, 32, 33):
            work.put(value)
        with patch.object(source, "_active_session", None):
            cap = source.Ps4SuiteSource.open(
                0, module=module, controls=suite_controls())
            try:
                # Mismatch must never kill tracking fail-closed.
                self.assertTrue(cap.read()[0])
                self.assertIsNone(cap.error)
                controls = cap.controls_status()
            finally:
                self.assertTrue(cap.release())
        self.assertIsNone(cap.controls_status())
        self.assertIsNone(cap.settings_capabilities())
        final_controls = cap._session.report()["controls"]
        self.assertFalse(controls["readback_confirmed"]["brightness"])
        self.assertTrue(any("continuing with unconfirmed readback" in w
                           for w in controls["warnings"]))
        # The write was applied; the stale observation changes nothing.
        self.assertEqual(caps[0].values[10], 0)
        self.assertIsNone(final_controls["restored"]["brightness"])
        self.assertFalse(final_controls["restoration_pending"])

    def test_rejected_write_warns_and_continues_without_restore(self):
        module, caps, work, _ = fake_suite_cv(reject_brightness=True)
        for value in (41, 42, 43):
            work.put(value)
        with patch.object(source, "_active_session", None):
            cap = source.Ps4SuiteSource.open(
                0, module=module, controls=suite_controls())
            try:
                self.assertTrue(cap.read()[0])
                self.assertIsNone(cap.error)
                controls = cap.controls_status()
            finally:
                self.assertTrue(cap.release())
        self.assertIsNone(cap.controls_status())
        self.assertIsNone(cap.settings_capabilities())
        self.assertFalse(controls["set_ok"]["brightness"])
        self.assertFalse(controls["readback_confirmed"]["brightness"])
        self.assertTrue(any("continuing with unconfirmed readback" in w
                           for w in controls["warnings"]))
        self.assertFalse(any(e == ("set", 10, 4) for e in caps[0].events))

    def test_default_open_is_profile_driven_not_hardcoded(self):
        module, caps, work, _ = fake_suite_cv()
        for value in (51, 52, 53):
            work.put(value)
        with patch.object(source, "_active_session", None):
            cap = source.Ps4SuiteSource.open(0, module=module)
            try:
                self.assertTrue(cap.read()[0])
                controls = cap.controls_status()
            finally:
                self.assertTrue(cap.release())
        self.assertIsNone(cap.controls_status())
        self.assertIsNone(cap.settings_capabilities())
        profile = source.load_suite_profile(ROOT)
        self.assertEqual(controls["requested"], profile["controls"])
        self.assertEqual([e for e in caps[0].events
                          if e[0] == "set" and e[1] == 10],
                         [("set", 10, profile["controls"]["brightness"])])

    def test_out_of_range_profile_rejected_before_any_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "configurations").mkdir()
            data = json.loads((ROOT / source.PROFILE_PATH).read_bytes())
            data["controls"] = {"brightness": 9, "exposure": None, "gain": None}
            (root / source.PROFILE_PATH).write_text(json.dumps(data),
                                                    encoding="utf-8")
            with self.assertRaises(ValueError):
                source.load_suite_profile(root)
            with patch.object(source, "StereoSession") as factory:
                with self.assertRaises(ValueError):
                    source.Ps4SuiteSource.open(0, module=object(), root=root)
                factory.assert_not_called()

    def test_restore_flag_validation(self):
        with self.assertRaises(CameraError):
            stereo.StereoSession(0, restore_controls=1)


if __name__ == "__main__":
    unittest.main()
