"""Default PS4 wiring fixtures: fake acquisition, never cv2 capture or COM."""
import copy
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch

from tools.psvr_camera.camera_backend import CameraError, OpenCvUnavailable
from tools.psvr_camera.camera_source import open_camera_source
from tools.psvr_camera import ps4_owned_dshow as owned
from tools.psvr_camera import ps4_stereo_session as stereo
from tools.psvr_camera import ps4_legacy_controls as legacy_controls
from tools.psvr_camera import ps4_suite_source as suite
from tests.test_camera_ps4_owned_dshow import FakeNative
from tests.test_camera_ps4_stereo_session import fake_cv


class FakeOwnedCapture:
    def __init__(self, requested):
        self.requested = dict(requested)
        self.revision = self.sequence = 0
        self.error = None
        self.cleanup_pending = True
        self.fail_cleanup = False
        self.calls = []
        self._tick = threading.Event()

    def controls_status(self):
        return {"requested": dict(self.requested),
                "transaction": {"state": "confirmed", "revision": self.revision},
                **{"native_" + name: {"target": target, "confirmed": True,
                    "capture_identity_confirmed": True,
                    "range": {"minimum": -13 if name == "exposure" else 0,
                              "maximum": -1 if name == "exposure" else 8,
                              "step": 1, "capabilities": 3}}
                   for name, target in self.requested.items()}}

    def next_pair(self, timeout_s=0.5, module=None):
        import numpy as np
        self._tick.wait(min(timeout_s, 0.005))
        self.sequence += 1
        return {"left": np.zeros((400, 640, 3), dtype=np.uint8),
                "right": np.ones((400, 640, 3), dtype=np.uint8),
                "sequence": self.sequence, "host_monotonic": float(self.sequence),
                "controls_revision": self.revision}

    def request_controls(self, controls, timeout_s=1.5):
        self.calls.append(("request", dict(controls)))
        self.requested.update(controls)
        self.revision += 1
        return self.revision

    def release(self, timeout_s=0.5):
        self.calls.append(("release", timeout_s))
        if self.fail_cleanup:
            raise CameraError("fixture pending native owner")
        self.cleanup_pending = False


class WiringTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(suite, "_active_session", None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sessions = []
        self.addCleanup(self.close_sessions)

    def close_sessions(self):
        for session in self.sessions:
            if isinstance(session._session, FakeOwnedCapture):
                session._session.fail_cleanup = False
            session.stop(1)

    def factory(self, index, dll, **kwargs):
        self.open_kwargs = copy.deepcopy({k: v for k, v in kwargs.items() if k not in ("_native_factory", "cancel_token")})
        self.index, self.dll = index, dll
        self.capture = FakeOwnedCapture(kwargs["initial_controls"])
        return self.capture

    def test_explicit_gate_routes_owned_not_videocapture(self):
        decoder = Mock()
        with patch.dict(stereo.os.environ, {"PSVR_PS4_DSHOW_ENABLE": "1"}), \
             patch.object(stereo, "import_cv2", return_value=decoder), \
             patch.object(owned.OwnedDshowCapture, "open_index", side_effect=self.factory):
            session = stereo.StereoSession(0, exposure=-7, gain=8, brightness=1,
                restore_controls=False, strict_controls=True)
            self.sessions.append(session)
            session.start().wait_ready(1)
            self.assertTrue(session._use_owned)
            self.assertEqual(self.open_kwargs["initial_controls"], {"exposure": -7, "gain": 8, "brightness": 1})
            self.assertFalse(self.open_kwargs["restore_controls"])
            self.assertEqual(session.report()["descriptor"]["backend"], "ps4-owned-dshow")
            decoder.VideoCapture.assert_not_called()
            self.assertTrue(session.stop(1))

    def test_only_exact_gate_one_selects_owned_production_transport(self):
        for value in (None, "0", "", "true", " 1", "1 ", "1"):
            with self.subTest(gate=value), patch.dict(stereo.os.environ):
                stereo.os.environ.pop("PSVR_PS4_DSHOW_ENABLE", None)
                if value is not None:
                    stereo.os.environ["PSVR_PS4_DSHOW_ENABLE"] = value
                session = stereo.StereoSession(0, restore_controls=False, strict_controls=True)
                self.assertEqual(session._use_owned, value == "1")
                self.assertEqual(session._strict_controls, value == "1")
                self.assertTrue(session.stop(0))  # no thread or device started

    def test_closed_gate_suite_dispatch_legacy_preview_without_unverified_targets(self):
        for injected_module in (False, True):
            with self.subTest(real_named_cv2=injected_module):
                module, caps, work, _, released = fake_cv()
                module.__name__ = "cv2"  # exercise production routing, fake capture only
                module.VideoCapture = Mock(wraps=module.VideoCapture)
                for value in range(10):
                    work.put(value)
                control = Mock(spec=["capture_identity_confirmed", "range", "get", "set_manual", "close"])
                control.capture_identity_confirmed = False
                with patch.dict(stereo.os.environ, {"PSVR_PS4_DSHOW_ENABLE": "0"}), \
                     patch.object(stereo, "import_cv2", return_value=module), \
                     patch.object(stereo, "Ps4DshowControl", return_value=control), \
                     patch.object(legacy_controls, "enumerate_dshow_devices",
                                  return_value=["@device:pnp:vid_05a9&pid_058b#fixture"]), \
                     patch.object(owned.OwnedDshowCapture, "open_index") as owned_open:
                    source, descriptor = open_camera_source(0, backend="ps4-stereo",
                        module=module if injected_module else None, strict_controls=True)
                    self.sessions.append(source._session)
                    try:
                        self.assertTrue(source.read()[0])
                        self.assertFalse(source._session._use_owned)
                        self.assertFalse(source._session._strict_controls)
                        module.VideoCapture.assert_called_once_with(0, module.CAP_DSHOW)
                        owned_open.assert_not_called()
                        self.assertEqual(descriptor["capture_backend"], "ps4-opencv-legacy")
                        self.assertFalse(descriptor["capture_identity_confirmed"])
                        report = source._session.report()
                        self.assertEqual(report["descriptor"]["fourcc"], "YUY2")
                        self.assertEqual((report["descriptor"]["width"],
                            report["descriptor"]["height"], report["descriptor"]["fps"]), (1748, 408, 60))
                        self.assertIn("OpenCV DirectShow legacy (non-strict)",
                            " ".join(source.controls_status()["warnings"]))
                        self.assertEqual(source.settings_capabilities()["control_contract"],
                                         legacy_controls.CONTRACT)
                        with self.assertRaisesRegex(CameraError, "identity not confirmed"):
                            source.request_controls({"gain": 7})
                        self.assertIsNone(source.error)
                        work.put(42)
                        self.assertTrue(source.read()[0])  # rejected edit does not kill preview
                        control.range.assert_not_called()
                        control.get.assert_not_called()
                        control.set_manual.assert_not_called()
                        writes = [e for e in caps[0].events if e[0] == "set"]
                        self.assertFalse(any(e[1] in (10, 14, 15) for e in writes))
                    finally:
                        self.assertTrue(source.release())
                    self.assertTrue(released.is_set())

    def test_explicit_gate_owned_open_failure_does_not_downgrade(self):
        with patch.dict(stereo.os.environ, {"PSVR_PS4_DSHOW_ENABLE": "1"}), \
             patch.object(stereo, "import_cv2", return_value=Mock()), \
             patch.object(owned.OwnedDshowCapture, "open_index",
                          side_effect=CameraError("ABI-v2 DLL missing")), \
             patch.object(stereo, "BoundedCameraSession") as legacy:
            with self.assertRaisesRegex(suite.Ps4SourceOpenError, "no retry.*DLL missing"):
                suite.Ps4SuiteSource.open(0, strict_controls=True)
            legacy.assert_not_called()

    def test_real_source_session_and_transaction_together_with_fake_native(self):
        import numpy as np
        fake = FakeNative()
        fake.continuous = True
        decoder = Mock(COLOR_YUV2BGR_YUY2=116)
        decoder.cvtColor.return_value = np.zeros((408, 1748, 3), dtype=np.uint8)
        source = suite.Ps4SuiteSource.open(0, module=decoder,
            owned_capture_factory=owned.OwnedDshowCapture.open_index,
            owned_native_factory=lambda _: fake)
        self.sessions.append(source._session)
        self.assertTrue(source.read()[0])
        self.assertEqual(source.controls_status()["transaction"]["state"], "confirmed")
        revision = source.request_controls({"exposure": -8, "gain": 7})
        self.assertTrue(source.read()[0])
        self.assertEqual(source.last_pair["controls_revision"], revision)
        self.assertEqual(source.controls_status()["requested"], {"exposure": -8, "gain": 7, "brightness": 1})
        decoder.VideoCapture.assert_not_called()
        self.assertEqual(sum(call[0] == "open" for call in fake.calls), 1)
        self.assertTrue(source.release())
        self.assertIsNone(owned._active_owner)

    def test_opencv_decoder_absence_fails_before_native_open_no_fallback(self):
        factory = Mock()
        session = stereo.StereoSession(0, restore_controls=False, owned_capture_factory=factory)
        self.sessions.append(session)
        with patch.object(stereo, "import_cv2", side_effect=OpenCvUnavailable("fixture cv2 absent")):
            session.start()
            with self.assertRaisesRegex(CameraError, "cv2 absent"):
                session.wait_ready(1)
        factory.assert_not_called()
        self.assertTrue(session.stop(1))

    def test_real_native_hardware_gate_defaults_closed_before_dll_load(self):
        with patch.dict(owned.os.environ, {"PSVR_PS4_DSHOW_ENABLE": "0"}), \
             patch.object(owned.ct, "CDLL") as load:
            with self.assertRaisesRegex(CameraError, "gate is closed"):
                owned.resolve_owned_dll_path("C:\\fixture\\not-loaded.dll")
            load.assert_not_called()

    def test_owned_open_failure_never_constructs_legacy_capture(self):
        session = stereo.StereoSession(0, restore_controls=False,
            owned_capture_factory=Mock(side_effect=CameraError("ABI-v2 DLL missing")))
        self.sessions.append(session)
        with patch.object(stereo, "import_cv2", return_value=Mock()), \
             patch.object(stereo, "BoundedCameraSession") as legacy:
            session.start()
            with self.assertRaisesRegex(CameraError, "DLL missing"):
                session.wait_ready(1)
            legacy.assert_not_called()

    def test_suite_profile_and_eye_order_preserved_strict_edit_compatible(self):
        root = Path(__file__).resolve().parents[1]
        profile = suite.load_suite_profile(root)
        with patch.object(stereo, "import_cv2", return_value=Mock()):
            source = suite.Ps4SuiteSource.open(0, root=root, strict_controls=True,
                owned_capture_factory=self.factory)
            self.sessions.append(source._session)
            self.assertEqual(self.open_kwargs["initial_controls"], profile["controls"])
            self.assertEqual(profile["controls"], {"exposure": -7, "gain": 8, "brightness": 1})
            self.assertEqual(profile["detector"], {"value_min": 160, "saturation_min": 90,
                "min_area": 20, "opening_size": 0})
            self.assertTrue(source.read()[0])
            self.assertEqual(source.last_pair["left"][0, 0, 0], 1)  # right eye is reference
            revision = source.request_controls({"exposure": -8, "gain": 7})
            self.assertEqual(revision, 1)
            self.assertIsNone(source.last_pair)
            self.assertIsNotNone(source.settings_capabilities())
            self.assertTrue(source.release())

    def test_pending_cleanup_blocks_reopen_and_readonly_report_does_not_release(self):
        with patch.object(stereo, "import_cv2", return_value=Mock()):
            source = suite.Ps4SuiteSource.open(0, owned_capture_factory=self.factory)
            self.sessions.append(source._session)
            self.capture.fail_cleanup = True
            self.assertFalse(source.release())
            self.assertTrue(source.cleanup_pending)
            before = len(self.capture.calls)
            for _ in range(3):
                source._session.report()
            self.assertEqual(len(self.capture.calls), before)
            with self.assertRaisesRegex(CameraError, "cleanup pending"):
                suite.Ps4SuiteSource.open(0, owned_capture_factory=Mock())

    def test_tracking_ps4_never_resolves_ps3_dll_and_preserves_profile(self):
        from tools.psvr_suite import tracking
        prefs = {"camera_source": "ps4-stereo", "backend": "ps4-stereo", "index": 0,
                 "width": 640, "height": 400, "fps": 60, "dll": "ps3-do-not-use.dll"}
        profile = {"controls": {"exposure": -7, "gain": 8, "brightness": 1}, "controls_explicit": True}
        with patch.object(tracking, "resolve_dll_path") as resolver, \
             patch.object(tracking, "open_camera_source") as opener:
            tracking._open_effective_camera(Path.cwd(), prefs, profile)
        resolver.assert_not_called()
        self.assertIsNone(opener.call_args.kwargs["dll_path"])
        self.assertEqual(opener.call_args.kwargs["controls"], profile["controls"])
        self.assertTrue(opener.call_args.kwargs["strict_controls"])


if __name__ == "__main__":
    unittest.main()
