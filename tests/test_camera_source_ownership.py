"""Offline owner/control contracts. These tests never open a real device."""
import threading
import types
import unittest
from unittest.mock import Mock, patch

from tools.psvr_camera.camera_backend import CameraError, LatestFrameCapture
from tools.psvr_camera.pseye_backend import PseyeCamera
from tools.psvr_camera.ps4_board_preview import _ControlState
from tools.psvr_camera.ps4_stereo_session import StereoSession, _SuiteControls
from tools.psvr_camera.ps4_suite_source import Ps4SuiteSource


class NativeControl:
    capture_identity_confirmed = True  # synthetic capture/control owner fixture
    def __init__(self, name, writes, *, mismatch=False):
        self.name, self.writes = name, writes
        self.value, self.flags = (-6 if name == "exposure" else 2), 2
        self.mismatch = mismatch
        self.closed = False

    def range(self):
        return {"minimum": -13 if self.name == "exposure" else 0,
                "maximum": -1 if self.name == "exposure" else 8,
                "step": 1, "default": self.value, "capabilities": 2}

    def get(self):
        return {"value": self.value, "flags": self.flags}

    def set_manual(self, value, info):
        self.writes.append((self.name, value, threading.get_ident()))
        self.value = value
        self.flags = 1 if self.mismatch and value == 8 else 2
        return True

    def close(self):
        self.closed = True


class ExplicitPs4ControlsTests(unittest.TestCase):
    def adapter(self, mismatch=False):
        writes = []
        controls = {name: NativeControl(name, writes, mismatch=mismatch and name == "gain")
                    for name in ("exposure", "gain", "brightness")}
        def factory(index, property_name="exposure"):
            self.assertEqual(index, 0)
            return controls[property_name]
        state = _ControlState(-7, 8, 1)
        capture = Mock()
        capture.read.return_value = (True, object())
        adapter = _SuiteControls(capture, Mock(), state, factory, 0)
        return adapter, state, controls, writes

    def test_all_ranges_validated_before_any_write(self):
        adapter, state, controls, writes = self.adapter()
        with self.assertRaises(CameraError):
            adapter._apply_transaction(1, {"exposure": -7, "brightness": 9})
        self.assertEqual(writes, [])
        self.assertEqual(state.snapshot()["transaction"]["state"], "failed")
        self.assertTrue(all(control.closed for control in controls.values()
                            if control.name != "gain"))

    def test_confirmation_is_native_manual_and_owner_only(self):
        adapter, state, controls, writes = self.adapter()
        adapter._apply_transaction(3, {"exposure": -7, "gain": 8, "brightness": 1})
        snapshot = state.snapshot()
        self.assertEqual(snapshot["transaction"]["revision"], 3)
        self.assertEqual(snapshot["transaction"]["state"], "confirmed")
        self.assertEqual(snapshot["readback"], {"exposure": -7, "gain": 8, "brightness": 1})
        self.assertTrue(all(snapshot["readback_confirmed"].values()))
        self.assertTrue(all(owner == threading.get_ident() for _, _, owner in writes))
        with patch("tools.psvr_camera.ps4_stereo_session.threading.get_ident", return_value=-1):
            with self.assertRaises(CameraError):
                adapter._apply_transaction(4, {"gain": 0})

    def test_failed_readback_rolls_back_and_remains_terminal(self):
        adapter, state, controls, writes = self.adapter(mismatch=True)
        before = {name: control.get() for name, control in controls.items()}
        with self.assertRaises(CameraError):
            adapter._apply_transaction(1, {"exposure": -7, "gain": 8})
        self.assertEqual(controls["exposure"].get(), before["exposure"])
        self.assertEqual(controls["gain"].get(), before["gain"])
        transaction = state.snapshot()["transaction"]
        self.assertEqual(transaction["state"], "failed")
        self.assertTrue(transaction["rollback_confirmed"])
        self.assertFalse(state.snapshot()["readback_confirmed"]["gain"])

    def test_unknown_previous_mode_cannot_claim_successful_rollback(self):
        adapter, state, controls, _ = self.adapter(mismatch=True)
        controls["gain"].flags = 1
        with self.assertRaises(CameraError):
            adapter._apply_transaction(1, {"gain": 8})
        self.assertFalse(state.snapshot()["transaction"]["rollback_confirmed"])

    def test_latched_native_readback_drains_only_owner_frames_before_confirmation(self):
        adapter, state, controls, writes = self.adapter()
        completed = [0]
        control = controls["exposure"]
        original_get = control.get
        def get_latched():
            if control.value == -7 and completed[0] < 4:
                return {"value": -6, "flags": 2}
            return original_get()
        def delivered():
            completed[0] += 1
            return True, object()
        control.get = get_latched
        adapter._cap.read.side_effect = delivered
        adapter._apply_transaction(1, {"exposure": -7})
        self.assertEqual(completed[0], 4)
        self.assertEqual(state.snapshot()["transaction"]["state"], "confirmed")
        self.assertEqual(state.snapshot()["native_exposure"]["readback_after_frames"], 4)
        self.assertEqual(len(writes), 1)

    def test_pending_edit_is_detached_and_not_silently_replaced(self):
        session = StereoSession(0, module=Mock(), restore_controls=False)
        session._thread = Mock()  # no threads are started in this test
        values = {"exposure": -7}
        revision = session.request_controls(values)
        values["exposure"] = 13
        self.assertEqual(session._pending_controls, (revision, {"exposure": -7}))
        with self.assertRaises(CameraError):
            session.request_controls({"gain": 0})
        self.assertEqual(session._take_controls(), (revision, {"exposure": -7}))
        with self.assertRaises(CameraError):
            session.request_controls({"gain": 0})  # still applying/pending

    def test_released_source_has_no_readback_and_rejects_edits(self):
        session = Mock()
        session.stop.return_value = True
        cap = Ps4SuiteSource(session)
        self.assertTrue(cap.release())
        self.assertIsNone(cap.controls_status())
        with self.assertRaises(CameraError):
            cap.request_controls({"gain": 8})
        session.request_controls.assert_not_called()

    def test_edit_capabilities_use_each_owner_range_not_profile_bounds(self):
        controls = {"native_" + name: {"capture_identity_confirmed": True, "range": {
            "minimum": low, "maximum": high, "step": 2, "capabilities": 2}}
            for name, low, high in (("exposure", -12, -2), ("gain", 0, 6),
                                    ("brightness", 0, 4))}
        session = Mock()
        session.report.return_value = {"error": None, "controls": controls}
        cap = Ps4SuiteSource(session)
        observed = cap.settings_capabilities()
        self.assertEqual(observed["controls"]["brightness"]["maximum"], 4)
        self.assertEqual(observed["controls"]["exposure"]["step"], 2)
        self.assertEqual(observed["preference_key"], "camera_profiles")
        controls["native_brightness"].pop("capture_identity_confirmed")
        self.assertIsNone(cap.settings_capabilities())
        controls["native_brightness"]["capture_identity_confirmed"] = True
        controls["native_gain"]["range"]["capabilities"] = 1
        self.assertIsNone(cap.settings_capabilities())
        cap.release()
        self.assertIsNone(cap.settings_capabilities())

    def test_terminal_or_stopped_session_never_yields_queued_pair(self):
        session = StereoSession(0, module=Mock(), restore_controls=False)
        session._slot = {"sequence": 1}
        session._error = "native failure"
        self.assertIsNone(session.next_pair(0))
        session._slot = {"sequence": 2}
        session._error = None
        session._stop.set()
        self.assertIsNone(session.next_pair(0))

    def test_owner_done_with_failed_native_close_is_still_unsafe_to_reopen(self):
        session = StereoSession(0, module=Mock(), restore_controls=False)
        closed = threading.Event()
        closed.set()
        session._session = types.SimpleNamespace(_closed=closed,
                                                _cleanup_error=RuntimeError("close failed"))
        session._cleanup_pending = True
        session.done.set()
        report = session.report()
        self.assertTrue(report["cleanup_pending"])
        self.assertIn("close failed", report["error"])


class ReleasePendingTests(unittest.TestCase):
    def test_ps3_pending_release_is_not_success_on_second_call(self):
        cap = object.__new__(PseyeCamera)
        cap._lock = threading.Lock()
        cap._cleanup_complete = threading.Event()
        cap._frame_event, cap._stop = threading.Event(), threading.Event()
        cap._released, cap._abandoned = False, False
        cap._cam, cap._lib = object(), Mock()
        cap._thread = Mock()
        cap._thread.is_alive.return_value = True
        self.assertFalse(cap.release())
        self.assertFalse(cap.release())
        self.assertTrue(cap.cleanup_pending)
        cap._lib.close.assert_not_called()
        self.assertIsNone(cap.controls_status())
        with self.assertRaises(CameraError):
            cap.set_manual_controls(13, 0)

    def test_opencv_pending_release_never_closes_under_reader(self):
        cap = object.__new__(LatestFrameCapture)
        cap._state = threading.Lock()
        cap._cleanup_complete = threading.Event()
        cap._frame_event, cap._stop = threading.Event(), threading.Event()
        cap._released, cap._abandoned = False, False
        cap._release_join_timeout_s = 0
        cap._cap, cap._thread = Mock(), Mock()
        cap._thread.is_alive.return_value = True
        self.assertFalse(cap.release())
        self.assertFalse(cap.release())
        self.assertTrue(cap.cleanup_pending)
        cap._cap.release.assert_not_called()


if __name__ == "__main__":
    unittest.main()
