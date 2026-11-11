"""Offline explicit legacy numeric controls; no actual COM/capture/runtime."""
import copy
import ctypes as ct
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from tools.psvr_camera import ps4_legacy_controls as legacy
from tools.psvr_camera import ps4_stereo_session as stereo
from tools.psvr_camera import ps4_suite_source as source
from tools.psvr_camera.ps4_board_preview import _ControlState
from tools.psvr_camera.camera_backend import CameraError
from tools.psvr_suite import tracking
from tests.test_camera_ps4_stereo_session import fake_cv
from tests.test_suite_camera_settings_ui import make_app, profile_tree
from tools.psvr_suite import app as ui

PS4 = "@device:pnp:vid_05a9&pid_058b#one"
CV = SimpleNamespace(CAP_PROP_EXPOSURE=15, CAP_PROP_GAIN=14, CAP_PROP_BRIGHTNESS=10)


class Raw:
    def __init__(self):
        self.owner = threading.get_ident()
        self.values = {15: -3., 14: 4., 10: 4.}
        self.initial = self.values.copy()
        self.events = []
        self.reject = self.raise_write = self.stale = self.unreadable = None

    def get(self, prop):
        assert self.owner == threading.get_ident()
        self.events.append(("get", prop))
        if prop == self.unreadable:
            return float("nan")
        return (self.initial if self.stale == prop else self.values)[prop]

    def set(self, prop, value):
        assert self.owner == threading.get_ident()
        self.events.append(("set", prop, value))
        self.values[prop] = value
        if prop == self.raise_write:
            raise CameraError("fake write mutated then threw")
        return prop != self.reject

    def read(self):
        assert self.owner == threading.get_ident()
        return True, object()

    def release(self):
        assert self.owner == threading.get_ident()
        self.events.append(("release",))


class LegacyTests(unittest.TestCase):
    def setUp(self):
        inventory = patch.object(legacy, "enumerate_dshow_devices", return_value=[PS4])
        self.inventory = inventory.start()
        self.addCleanup(inventory.stop)
        self.raw = Raw()
        self.state = _ControlState(-7, 8, 1)

    def wrap(self, targets):
        return legacy.LegacyControls(self.raw, CV, self.state, 0, targets)

    def test_snapshot_apply_verify_restore_numeric_not_identity(self):
        cap = self.wrap({"exposure": -8, "gain": 3, "brightness": 2})
        cap.read()
        controls = self.state.snapshot()
        self.assertEqual(controls["before"], {"exposure": -3., "gain": 4., "brightness": 4.})
        self.assertEqual(controls["transaction"]["state"], "confirmed")
        self.assertTrue(all(controls["readback_confirmed"].values()))
        self.assertFalse(controls["legacy_opencv"]["capture_identity_confirmed"])
        self.assertFalse(controls["restoration_independently_verified"])
        first_write = next(i for i, e in enumerate(self.raw.events) if e[0] == "set")
        self.assertEqual({e[1] for e in self.raw.events[:first_write]}, {10, 14, 15})
        cap.release()
        self.assertEqual(self.raw.values, self.raw.initial)
        final = self.state.snapshot()
        self.assertEqual(final["legacy_opencv"]["restore_state"], "readback_confirmed")
        self.assertTrue(all(final["legacy_opencv"]["restore_readback_confirmed"].values()))
        self.assertFalse(final["restoration_pending"])
        events = self.raw.events.copy()
        cap.release()
        self.assertEqual(self.raw.events, events)

    def test_default_preview_snapshots_only_never_applies_profile(self):
        cap = self.wrap({})
        cap.read()
        cap.release()
        self.assertFalse(any(e[0] == "set" for e in self.raw.events))
        self.assertEqual(self.state.snapshot()["legacy_opencv"]["restore_state"], "not_requested")
        self.assertEqual(self.state.snapshot()["readback"], {"exposure": -3., "gain": 4., "brightness": 4.})

    def test_rejected_throwing_and_stale_sets_never_claim_success(self):
        for failure in ("reject", "raise_write", "stale"):
            with self.subTest(failure=failure):
                self.raw, self.state = Raw(), _ControlState(-7, 8, 1)
                setattr(self.raw, failure, 15)
                cap = self.wrap({"exposure": -8, "gain": 3})
                cap.read()
                self.assertEqual(self.state.snapshot()["transaction"]["state"], "unconfirmed")
                self.assertFalse(self.state.snapshot()["readback_confirmed"]["exposure"])
                self.assertTrue(self.state.snapshot()["readback_confirmed"]["gain"])
                cap.release()
                self.assertEqual(self.raw.values, self.raw.initial)
                if failure != "stale":
                    self.assertEqual(self.state.snapshot()["legacy_opencv"]["restore_state"], "unconfirmed")

    def test_unreadable_snapshot_refuses_all_writes_even_other_controls(self):
        self.raw.unreadable = 10
        cap = self.wrap({"exposure": -8})
        cap.read()
        cap.release()
        self.assertFalse(any(e[0] == "set" for e in self.raw.events))
        self.assertEqual(self.state.snapshot()["transaction"]["state"], "unconfirmed")

    def test_ambiguous_wrong_index_missing_or_changed_inventory_refuses_writes(self):
        for paths in ([], [PS4, PS4 + "two"], ["other", PS4], ["vid_05a9&pid_0580#boot"], None):
            with self.subTest(paths=paths):
                self.inventory.return_value = paths
                self.raw, self.state = Raw(), _ControlState(-7, 8, 1)
                cap = self.wrap({"gain": 3})
                cap.read()
                cap.release()
                self.assertFalse(any(e[0] == "set" for e in self.raw.events))
                self.assertEqual(self.state.snapshot()["transaction"]["state"], "unconfirmed")

    def test_topology_change_before_write_or_restore_never_writes_blind(self):
        self.inventory.side_effect = [[PS4], [PS4 + "replaced"]]
        cap = self.wrap({"gain": 3})
        cap.read()
        self.assertFalse(any(e[0] == "set" for e in self.raw.events))
        cap.release()
        self.inventory.side_effect = None
        self.inventory.return_value = [PS4]
        self.raw, self.state = Raw(), _ControlState(-7, 8, 1)
        cap = self.wrap({"gain": 3})
        cap.read()
        self.inventory.return_value = [PS4, PS4 + "two"]
        cap.release()
        self.assertEqual([e for e in self.raw.events if e[0] == "set"], [("set", 14, 3)])
        self.assertEqual(self.state.snapshot()["legacy_opencv"]["restore_state"], "unconfirmed")
        self.assertFalse(self.state.snapshot()["legacy_opencv"]["restore_readback_confirmed"]["gain"])

    def test_exception_enumerating_and_invalid_targets_fail_safe(self):
        self.inventory.side_effect = CameraError("inventory unavailable")
        cap = self.wrap({"gain": 3})
        cap.read()
        cap.release()
        self.assertFalse(any(e[0] == "set" for e in self.raw.events))
        for targets in ({"gain": True}, {"gain": -1}, {"exposure": -14}, {"wrong": 0}):
            with self.assertRaises(CameraError):
                self.wrap(targets)

    def test_cancelled_during_enumeration_does_not_apply_late_targets(self):
        cancel = threading.Event()
        def enumerate_then_cancel():
            cancel.set()
            return [PS4]
        self.inventory.side_effect = enumerate_then_cancel
        cap = legacy.LegacyControls(self.raw, CV, self.state, 0, {"gain": 3}, cancel)
        cap.read()
        cap.release()
        self.assertFalse(any(e[0] == "set" for e in self.raw.events))
        self.assertEqual(self.state.snapshot()["transaction"]["state"], "unconfirmed")

    def test_real_suite_reopen_payload_and_release_report_with_fake_capture(self):
        module, caps, work, _, released = fake_cv()
        module.__name__ = "cv2"
        for _ in range(15):
            work.put(1)
        with patch.dict(stereo.os.environ, {"PSVR_PS4_DSHOW_ENABLE": "0"}), \
             patch.object(source, "_active_session", None), \
             patch.object(stereo, "import_cv2", return_value=module):
            cap = source.Ps4SuiteSource.open(0, legacy_controls={"exposure": -8, "gain": 3, "brightness": 2})
            try:
                self.assertTrue(cap.read()[0])
                self.assertEqual(cap.controls_status()["transaction"]["state"], "confirmed")
                self.assertEqual(cap.settings_capabilities()["control_contract"], legacy.CONTRACT)
            finally:
                self.assertTrue(cap.release())
            self.assertTrue(released.is_set())
            self.assertIsNone(cap.controls_status())
            self.assertIsNone(cap.settings_capabilities())
            diag = cap.release_diagnostics()
            self.assertEqual(diag["legacy_opencv"]["restore_state"], "readback_confirmed")
            self.assertEqual(caps[0].values[15], -3.)
            self.assertEqual(caps[0].values[14], 4.)
            self.assertEqual(caps[0].values[10], 4.)

    def test_tracking_only_forwards_explicit_overrides_at_closed_gate(self):
        prefs = {"camera_source": "ps4-stereo", "backend": "ps4-stereo", "index": 0,
                 "width": 640, "height": 400, "fps": 60}
        for gate in ("0", "1"):
            with patch.dict(stereo.os.environ, {"PSVR_PS4_DSHOW_ENABLE": gate}), \
                 patch.object(tracking, "open_camera_source") as opener:
                profile = {"controls": {"exposure": -8, "gain": 8, "brightness": 1},
                           "control_overrides": {"exposure": -8}, "controls_explicit": True}
                tracking._open_effective_camera(Path.cwd(), prefs, profile)
                self.assertEqual(opener.call_args.kwargs.get("legacy_controls"),
                                 {"exposure": -8} if gate == "0" else None)
                opener.reset_mock()
                profile.pop("control_overrides")
                tracking._open_effective_camera(Path.cwd(), prefs, profile)
                self.assertNotIn("legacy_controls", opener.call_args.kwargs)

    def test_forced_reopen_request_is_bound_to_current_resource_not_preferences(self):
        worker = tracking.TrackingWorker(Path.cwd())
        worker._thread = Mock()
        worker._thread.is_alive.return_value = True
        worker._cap = owner = object()
        worker._status.update(state="running", running=True, camera_source="ps4-stereo",
            camera_settings_capabilities={"control_contract": legacy.CONTRACT})
        prefs = tracking.normalize_prefs({"camera_source": "ps4-stereo",
            "camera_profiles": {"ps4-stereo": {"controls": {"gain": 3}}}})
        before = copy.deepcopy(prefs)
        for _ in range(2):  # repeated identical Apply must still carry reopen intent
            self.assertTrue(worker.update_settings(prefs, force_legacy_reopen=True))
            request = worker._drain_settings_latest()
            self.assertIs(request.legacy_owner, owner)
            self.assertEqual(dict(request), before)
        self.assertEqual(prefs, before)
        worker._status["camera_source"] = "pseye"
        with self.assertRaisesRegex(RuntimeError, "connected single-PS4"):
            worker.update_settings(prefs, force_legacy_reopen=True)
        self.assertIsNone(worker._drain_settings_latest())

    def test_worker_reopens_identical_apply_and_rejects_stale_owner_with_fake_pipeline(self):
        profile = source.load_suite_profile(Path(__file__).resolve().parents[1])
        for stale_owner in (False, True):
            with self.subTest(stale_owner=stale_owner), tempfile.TemporaryDirectory() as tmp:
                worker = tracking.TrackingWorker(tmp)
                initial = tracking.normalize_prefs({"camera_source": "ps4-stereo",
                    "camera_profiles": {"ps4-stereo": {"controls": {"gain": 3}}}})
                opens, events = [], []
                frame = np.zeros((400, 640, 3), np.uint8)
                class Capture:
                    error, cleanup_pending, abandoned = None, False, False
                    def __init__(self):
                        self.reads = 0
                    def read(self):
                        self.reads += 1
                        if len(opens) == 1 and self.reads == 1:
                            worker._settings.put_nowait(tracking._LegacyReopenSettings(
                                initial, object() if stale_owner else self))
                        else:
                            worker._stop.set()
                        self.last_frame_monotonic = time.perf_counter()
                        self.last_pair = {"left": frame, "right": frame.copy(), "sequence": self.reads,
                                          "host_monotonic": self.last_frame_monotonic}
                        return True, frame
                    def controls_status(self):
                        return {"transaction": {"state": "confirmed", "revision": 0}}
                    def release(self):
                        events.append("release")
                        return True
                def open_camera(*args, **kwargs):
                    events.append("open")
                    cap = Capture()
                    opens.append(cap)
                    return cap, None
                tracker = Mock(alignment=None)
                tracker.search_region.return_value = (None, 1.)
                tracker.diagnostics.return_value = {}
                pipeline = Mock()
                pipeline.diagnostics.return_value = {}
                detector = Mock(cv2=None)
                detector.detect.return_value = []
                detector.annotate.side_effect = lambda image, candidates: image
                imu = Mock()
                imu.at.return_value = imu.latest.return_value = None
                optical = (SimpleNamespace(image_size=(640, 400)), {}, np.zeros((5, 3)), {},
                           np.zeros((5, 3)), None, [], tracker)
                with patch.object(tracking, "open_camera_source", side_effect=open_camera), \
                     patch.object(tracking, "_load_optical_setup", return_value=optical), \
                     patch.object(source, "load_suite_profile", return_value=profile), \
                     patch.object(tracking, "_load_kalman_table", return_value={}), \
                     patch.object(tracking, "_make_pipeline", return_value=pipeline), \
                     patch.object(tracking, "BlueLedDetector", return_value=detector), \
                     patch.object(tracking, "DriverImuSource", return_value=imu), \
                     patch.object(tracking, "DriverPositionSender", return_value=Mock(last_packet=None)):
                    worker._run(initial)
                self.assertEqual(len(opens), 1 if stale_owner else 2)
                if not stale_owner:
                    self.assertEqual(events[:3], ["open", "release", "open"])
                else:
                    self.assertIn("owner changed", worker.status()["control_edit"]["error"])

    def test_ui_connected_only_apply_reopen_without_saving_and_restore_display(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_tree(root)
            app = make_app(root)
            app.camera_source_menu.select("PS4 Camera stereo")
            editor = app._source_editors["ps4-stereo"]
            self.assertTrue(all(e.options["state"] == "disabled" for e in editor["entries"].values()))
            self.assertIn("identità del filtro di controllo NON verificabile", app.camera_source_summary.options["text"])
            caps = {"source": "ps4-stereo", "preference_key": "camera_profiles", "apply_mode": "reopen",
                "control_contract": legacy.CONTRACT, "capture_identity_confirmed": False,
                "controls": {k: {"minimum": lo, "maximum": hi, "step": 1,
                    "validation": "profile_policy_not_measured"} for k, (lo, hi) in legacy.POLICY_BOUNDS.items()}}
            status = {"running": True, "state": "running", "camera_source": "ps4-stereo",
                "prefs": {"camera_source": "ps4-stereo"}, "camera_settings_capabilities": caps,
                "camera_controls": {"legacy_opencv": {"single_ps4_verified": True},
                    "transaction": {"state": "unconfirmed", "error": "OpenCV targets NOT confirmed"},
                    "readback": {"exposure": -3}, "readback_confirmed": {"exposure": False}}}
            app._handle_event("status", {"runtime": {}, "tracking": status})
            self.assertTrue(all(e.options["state"] == "normal" for e in editor["entries"].values()))
            self.assertIn("policy, NOT measured", editor["note"].options["text"])
            self.assertIn("unconfirmed", app.camera_source_summary.options["text"])
            app._run_serial = Mock(return_value=True)
            app.tracking.update_settings.return_value = True
            app.on_apply_tracking()  # unchanged visible defaults are explicit only on Apply
            self.assertTrue(app._run_serial.call_args.args[1]())
            self.assertEqual(app.tracking.update_settings.call_args.kwargs, {"force_legacy_reopen": True})
            self.assertEqual(app.tracking.update_settings.call_args.args[0]["camera_profiles"]["ps4-stereo"]["controls"],
                             {"exposure": -7, "gain": 8, "brightness": 1})
            for name, literal in {"exposure": "-8", "gain": "3", "brightness": "2"}.items():
                editor["vars"][name].set(literal)
            original_prefs = copy.deepcopy(app._camera_profiles_committed)
            app._run_serial = Mock(return_value=True)
            app.tracking.update_settings.return_value = True
            app.on_apply_tracking()
            task = app._run_serial.call_args.args[1]
            self.assertTrue(task())  # mock tracking, no hardware
            submitted = app.tracking.update_settings.call_args.args[0]
            self.assertEqual(submitted["camera_profiles"]["ps4-stereo"]["controls"],
                             {"exposure": -8, "gain": 3, "brightness": 2})
            self.assertIn("NON conferma applicazione", app._run_serial.call_args.args[0])
            self.assertFalse(app.user_path.exists())
            self.assertEqual(app._camera_profiles_committed, original_prefs)
            diagnostic = {"source": "ps4-stereo", "cleanup_pending": False,
                "legacy_opencv": {"restore_state": "unconfirmed", "restore_readback_confirmed": {"gain": False}},
                "restored": {"gain": 6.0}}
            stopped = dict(status, running=False, state="stopped", camera_source=None,
                           camera_controls=None, camera_settings_capabilities=None, camera_restore=diagnostic)
            app._handle_event("status", {"runtime": {}, "tracking": stopped})
            self.assertTrue(all(e.options["state"] == "disabled" for e in editor["entries"].values()))
            self.assertIn("Ultimo restore OpenCV: unconfirmed", app.camera_source_summary.options["text"])
            self.assertIn("restore gain: letto 6.000", app.camera_source_summary.options["text"])
            self.assertIn("NON verificabile", app.camera_source_summary.options["text"])
            app.camera_source_menu.select("PS3 Eye")
            self.assertFalse(editor["frame"].visible)

    def test_tracking_publishes_restore_only_as_historical_status(self):
        worker = tracking.TrackingWorker.__new__(tracking.TrackingWorker)
        worker._set_status = Mock()
        cap = Mock()
        diagnostic = {"source": "ps4-stereo", "cleanup_pending": False,
                      "legacy_opencv": {"restore_state": "unconfirmed"}}
        cap.release_diagnostics.return_value = diagnostic
        worker._publish_camera_restore(cap)
        worker._set_status.assert_called_once_with(camera_restore=diagnostic)

    def test_late_restore_completion_refreshes_historical_status_not_live_controls(self):
        worker = tracking.TrackingWorker(Path.cwd())
        cap = Mock()
        worker._retired_cap = cap
        worker._status.update(camera_controls=None, camera_settings_capabilities=None,
                              camera_restore={"cleanup_pending": True})
        cap.release_diagnostics.return_value = {"source": "ps4-stereo", "cleanup_pending": False,
            "legacy_opencv": {"restore_state": "readback_confirmed"}}
        current = worker.status()
        self.assertFalse(current["camera_restore"]["cleanup_pending"])
        self.assertEqual(current["camera_restore"]["legacy_opencv"]["restore_state"], "readback_confirmed")
        self.assertIsNone(current["camera_controls"])
        self.assertIsNone(current["camera_settings_capabilities"])


class EnumerationTests(unittest.TestCase):
    def enumerate_fake(self, paths, *, apartment=0, fail_next=False):
        buffers, freed, released = [], [], []
        ole = SimpleNamespace(CoInitializeEx=Mock(return_value=apartment), CoUninitialize=Mock(),
            CoCreateInstance=Mock(), CreateBindCtx=Mock(), CoTaskMemFree=Mock(side_effect=lambda p: freed.append(p.value)))
        def create(*args):
            args[-1]._obj.value = 1
            return 0
        def context(*args):
            args[-1]._obj.value = 3
            return 0
        ole.CoCreateInstance.side_effect = create
        ole.CreateBindCtx.side_effect = context
        count = 0
        def call(ptr, slot, args, *values):
            nonlocal count
            if ptr.value == 1 and slot == 3:
                values[1]._obj.value = 2
                return 0
            if ptr.value == 2 and slot == 3:
                if fail_next:
                    return -1
                if count == len(paths):
                    values[2]._obj.value = 0
                    return 1
                values[1]._obj.value = 100 + count
                values[2]._obj.value = 1
                count += 1
                return 0
            if ptr.value >= 100 and slot == 20:
                buffer = ct.create_unicode_buffer(paths[ptr.value - 100])
                buffers.append(buffer)
                values[-1]._obj.value = ct.addressof(buffer)
                return 0
            raise AssertionError("unexpected COM call")
        with patch.object(legacy.sys, "platform", "win32"), \
             patch.object(legacy.ct, "WinDLL", return_value=ole), \
             patch.object(legacy, "_call", side_effect=call), \
             patch.object(legacy, "_release", side_effect=lambda p: released.append(p.value) if p else None):
            if fail_next or len(paths) > 64:
                with self.assertRaises(CameraError):
                    legacy.enumerate_dshow_devices()
                result = None
            else:
                result = legacy.enumerate_dshow_devices()
        return result, ole, freed, released

    def test_metadata_enumeration_order_lowercase_and_balanced_cleanup(self):
        paths = ["@Other", PS4.upper()]
        result, ole, freed, released = self.enumerate_fake(paths)
        self.assertEqual(result, [p.lower() for p in paths])
        self.assertEqual(len(freed), 2)
        self.assertCountEqual(released, [100, 101, 3, 2, 1])
        ole.CoUninitialize.assert_called_once_with()

    def test_metadata_failure_and_bound_cleanup_no_uninitialize_changed_apartment(self):
        for kwargs in ({"fail_next": True}, {"apartment": -2147417850}, {}):
            with self.subTest(kwargs=kwargs):
                paths = [PS4] * 65 if not kwargs else [PS4]
                _, ole, freed, released = self.enumerate_fake(paths, **kwargs)
                self.assertTrue(all(i in released for i in (1, 2, 3)))
                if kwargs.get("apartment"):
                    ole.CoUninitialize.assert_not_called()
                else:
                    ole.CoUninitialize.assert_called_once_with()
                if len(paths) > 64:
                    self.assertEqual(len(freed), 64)
                    self.assertIn(164, released)  # overflow moniker released too


if __name__ == "__main__":
    unittest.main()
