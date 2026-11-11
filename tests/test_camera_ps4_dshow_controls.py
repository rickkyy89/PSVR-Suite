"""PS4 exposure policy and COM adapter mocks; never open real hardware."""
import ctypes as ct
import threading
import unittest
from unittest.mock import Mock, patch

from tools.psvr_camera.camera_backend import CameraError
from tools.psvr_camera import ps4_dshow_controls as controls
from tools.psvr_camera.ps4_board_preview import _ControlState
from tools.psvr_camera.ps4_stereo_session import _SuiteControls, StereoSession
from tests.test_camera_ps4_suite_controls import fake_suite_cv
from tools.psvr_camera import ps4_suite_source as source


RANGE = {"minimum": -13, "maximum": -1, "step": 1,
         "default": -3, "capabilities": 3}


class ExposurePolicyTests(unittest.TestCase):
    def test_nominal_60fps_selects_short_exposure_not_brightness(self):
        self.assertEqual(controls.choose_led_exposure(RANGE), -6)
        self.assertEqual(controls.choose_led_exposure(RANGE, 120), -7)
        self.assertEqual(controls.choose_led_exposure(RANGE, 30), -5)

    def test_range_step_never_rounds_towards_longer_exposure(self):
        self.assertEqual(controls.choose_led_exposure(dict(RANGE, step=4)), -9)
        self.assertEqual(controls.choose_led_exposure(dict(RANGE, maximum=-9)), -9)

    def test_no_supported_short_exposure_does_not_clamp_long(self):
        for changes in ({"minimum": -5}, {"capabilities": 1}, {"step": 0},
                        {"minimum": 0, "maximum": -1}):
            with self.subTest(changes=changes), self.assertRaises(CameraError):
                controls.choose_led_exposure(dict(RANGE, **changes))

    def test_policy_only_allowed_for_persistent_suite(self):
        with self.assertRaises(CameraError):
            StereoSession(0, exposure_policy="led_frame")
        with self.assertRaises(CameraError):
            StereoSession(0, restore_controls=False, exposure_policy="guess")


class NativeSetTests(unittest.TestCase):
    def setUp(self):
        self.native = object.__new__(controls.Ps4ExposureControl)
        self.native._owner = threading.get_ident()
        self.native._control = ct.c_void_p(123)
        self.native._property = controls.EXPOSURE
        self.native.capture_identity_confirmed = True

    def test_set_uses_exact_manual_flag_and_exposure_property(self):
        with patch.object(controls, "_call", return_value=0) as call:
            self.assertTrue(self.native.set_manual(-7, RANGE))
        self.assertEqual(call.call_args.args[1], 4)  # IAMCameraControl.Set slot
        self.assertEqual(call.call_args.args[-3:], (4, -7, 2))

    def test_reject_invalid_or_off_step_without_native_write(self):
        for target in (-14, 0, -7.5, float("nan"), True, "-7"):
            with self.subTest(target=target), patch.object(controls, "_call") as call:
                with self.assertRaises(CameraError):
                    self.native.set_manual(target, RANGE)
                call.assert_not_called()
        with patch.object(controls, "_call") as call:
            with self.assertRaises(CameraError):
                self.native.set_manual(-8, dict(RANGE, step=2))
            call.assert_not_called()

    def test_standard_videoproc_properties_and_manual_flag(self):
        for prop in (0, 9):
            self.native._property = prop
            with self.subTest(prop=prop), patch.object(controls, "_call", return_value=0) as call:
                self.native.set_manual(0, dict(RANGE, minimum=0, maximum=8))
                self.assertEqual(call.call_args.args[-3:], (prop, 0, 2))

    def test_rejected_hresult_not_success(self):
        with patch.object(controls, "_call", return_value=-2147024809):
            with self.assertRaisesRegex(CameraError, "80070057"):
                self.native.set_manual(-7, RANGE)

    def test_native_calls_reject_cross_thread(self):
        self.native._owner = -1
        with patch.object(controls, "_call") as call:
            with self.assertRaises(CameraError):
                self.native.set_manual(-7, RANGE)
            call.assert_not_called()


class NativeBindingTests(unittest.TestCase):
    def test_index_only_native_binding_is_disabled_before_com_enumeration(self):
        with patch.object(controls.sys, "platform", "win32"), \
             patch.object(controls.ct, "WinDLL", create=True) as dll:
            with self.assertRaisesRegex(CameraError, "capture-owned DirectShow identity unavailable"):
                controls.Ps4DshowControl(0)
        dll.assert_not_called()

    def test_same_vid_pid_different_ps4_moniker_is_rejected_before_bind(self):
        actual = 'usb#vid_05a9&pid_058b#second-ps4'
        captured = 'usb#vid_05a9&pid_058b#first-ps4'
        ole, call, events = self.setup_native(actual)
        with patch.object(controls.sys, "platform", "win32"), \
             patch.object(controls.ct, "WinDLL", return_value=ole, create=True), \
             patch.object(controls, "_call", side_effect=call):
            with self.assertRaisesRegex(CameraError, "differs from open capture"):
                controls.Ps4DshowControl(0, capture_device_path=captured)
        self.assertNotIn((3, 8), events)
        self.assertNotIn((6, 4), events)
        ole.CoUninitialize.assert_called_once()

    def setup_native(self, identity, coinit=0):
        self.capture_identity = identity  # simulated already-open capture identity
        events = []
        display = ct.create_unicode_buffer(identity)
        ole = Mock()
        ole.CoInitializeEx.return_value = coinit
        def create_instance(*args):
            args[-1]._obj.value = 1
            return 0
        def bind_context(*args):
            args[-1]._obj.value = 4
            return 0
        ole.CoCreateInstance.side_effect = create_instance
        ole.CreateBindCtx.side_effect = bind_context
        ole.CoUninitialize.side_effect = lambda: events.append(("uninitialize",))
        def call(ptr, slot, args, *values):
            events.append((ptr.value, slot))
            if ptr.value == 1 and slot == 3:
                values[1]._obj.value = 2
            elif ptr.value == 2 and slot == 3:
                values[1]._obj.value = 3
                values[2]._obj.value = 1
            elif ptr.value == 3 and slot == 20:
                values[-1]._obj.value = ct.cast(display, ct.c_void_p).value
            elif ptr.value == 3 and slot == 8:
                values[-1]._obj.value = 5
            elif ptr.value == 5 and slot == 0:
                values[-1]._obj.value = 6
            elif ptr.value == 6 and slot == 0:
                values[-1]._obj.value = 7
            return 0
        return ole, call, events

    def test_reject_other_camera_before_bind_and_release_before_com_shutdown(self):
        ole, call, events = self.setup_native('@device:pnp:usb#vid_1234&pid_5678#other')
        with patch.object(controls.sys, "platform", "win32"), \
                patch.object(controls.ct, "WinDLL", return_value=ole, create=True), \
                patch.object(controls, "_call", side_effect=call):
            with self.assertRaisesRegex(CameraError, "not a PS4 runtime"):
                controls.Ps4DshowControl(0, capture_device_path=self.capture_identity)
        self.assertNotIn((3, 8), events)
        self.assertEqual(events[-1], ("uninitialize",))
        ole.CoUninitialize.assert_called_once()

    def test_ps4_only_binds_requested_moniker_and_close_is_idempotent(self):
        ole, call, events = self.setup_native('@device:pnp:usb#vid_05a9&pid_058b&mi_00#camera')
        with patch.object(controls.sys, "platform", "win32"), \
                patch.object(controls.ct, "WinDLL", return_value=ole, create=True), \
                patch.object(controls, "_call", side_effect=call):
            native = controls.Ps4DshowControl(0, capture_device_path=self.capture_identity)
            self.assertIn((3, 8), events)
            native.close()
            native.close()
        self.assertEqual(events.count((6, 2)), 1)
        ole.CoUninitialize.assert_called_once()

    def test_preexisting_other_com_apartment_is_not_uninitialized(self):
        ole, call, _ = self.setup_native('usb#vid_05a9&pid_058a#camera', coinit=-2147417850)
        with patch.object(controls.sys, "platform", "win32"), \
                patch.object(controls.ct, "WinDLL", return_value=ole, create=True), \
                patch.object(controls, "_call", side_effect=call):
            native = controls.Ps4DshowControl(0, capture_device_path=self.capture_identity)
            native.close()
        ole.CoUninitialize.assert_not_called()

    def test_all_control_views_share_one_bound_ps4_filter(self):
        ole, call, events = self.setup_native('usb#vid_05a9&pid_058b#camera')
        with patch.object(controls.sys, "platform", "win32"), \
                patch.object(controls.ct, "WinDLL", return_value=ole, create=True), \
                patch.object(controls, "_call", side_effect=call):
            native = controls.Ps4DshowControl(0, capture_device_path=self.capture_identity)
            exposure = native.for_property("exposure")
            gain = native.for_property("gain")
            brightness = native.for_property("brightness")
            exposure.close(); gain.close(); brightness.close()
            self.assertNotIn((6, 2), events)
            self.assertEqual(events.count((3, 8)), 1)
            native.close()
        self.assertEqual(events.count((7, 2)), 2)
        self.assertEqual(events.count((6, 2)), 1)
        ole.CoUninitialize.assert_called_once()

class SuiteExposureTests(unittest.TestCase):
    def wrapper(self, *, before=-3, flags=2, delayed=None, reject=False):
        module, caps, work, _ = fake_suite_cv()
        raw = module.VideoCapture(0, module.CAP_DSHOW)
        events = []

        class Native:
            capture_identity_confirmed = True  # simulated shared capture owner
            def __init__(self, index):
                events.append(("open", index, threading.get_ident()))
                self.current = {"value": before, "flags": flags}
                self.written = False
            def range(self):
                events.append(("range",))
                return RANGE.copy()
            def get(self):
                events.append(("get",))
                if self.written and delayed is None:
                    return {"value": -6, "flags": 2}
                if self.written and delayed is not None and len([e for e in events if e[0] == "get"]) >= 3:
                    return delayed.copy()
                return self.current.copy()
            def set_manual(self, target, info):
                events.append(("set", target, controls.MANUAL))
                self.written = True
                if reject:
                    raise CameraError("manual write rejected")
                return True
            def close(self):
                events.append(("close",))

        state = _ControlState(None, None, None, True)
        wrapper = _SuiteControls(raw, module, state, Native, 0, "led_frame", 60)
        return wrapper, state, events, raw, work

    def test_query_range_before_manual_write_apply_once_never_restore(self):
        wrapper, state, events, raw, _ = self.wrapper()
        wrapper._apply()
        for _ in range(4):
            wrapper._check_native_readback()
        wrapper.release()
        self.assertLess(events.index(("range",)), events.index(("set", -6, 2)))
        self.assertEqual([e for e in events if e[0] == "set"], [("set", -6, 2)])
        self.assertEqual(events.count(("close",)), 1)
        self.assertTrue(state.snapshot()["native_exposure"]["confirmed"])
        self.assertFalse(any(e[0] == "set" and e[1] == 15 for e in raw.events))
        self.assertFalse(state.snapshot()["restoration_pending"])

    def test_same_numeric_auto_still_writes_manual(self):
        wrapper, _, events, _, _ = self.wrapper(before=-6, flags=1)
        wrapper._apply()
        wrapper.release()
        self.assertIn(("set", -6, 2), events)

    def test_delayed_readback_confirms_value_and_manual_after_frames(self):
        wrapper, state, _, _, _ = self.wrapper(delayed={"value": -6, "flags": 2})
        wrapper._apply()
        self.assertFalse(state.snapshot()["native_exposure"]["confirmed"])
        for _ in range(4):
            wrapper._check_native_readback()
        native = state.snapshot()["native_exposure"]
        self.assertTrue(native["confirmed"])
        self.assertEqual(native["readback"]["value"], -3)
        self.assertEqual(native["delayed_readback"], {"value": -6, "flags": 2})
        wrapper.release()

    def test_value_match_without_manual_is_not_confirmation(self):
        wrapper, state, events, _, _ = self.wrapper(delayed={"value": -6, "flags": 1})
        wrapper._apply()
        for _ in range(60):
            wrapper._check_native_readback()
        self.assertFalse(state.snapshot()["native_exposure"]["confirmed"])
        self.assertEqual(events.count(("close",)), 1)
        wrapper.release()

    def test_no_cv2_exposure_fallback_on_native_rejection(self):
        wrapper, state, _, raw, work = self.wrapper(reject=True)
        work.put(1)
        self.assertTrue(wrapper.read()[0])
        wrapper.release()
        self.assertTrue(state.snapshot()["warnings"])
        self.assertFalse(any(e[0] == "set" and e[1] == 15 for e in raw.events))

    def test_unspecified_exposure_with_no_led_policy_is_untouched(self):
        module, _, _, _ = fake_suite_cv()
        state = _ControlState(None, None, 0, True)
        wrapper = _SuiteControls(module.VideoCapture(0, 700), module, state)
        wrapper._apply()
        self.assertNotIn("native_exposure", state.snapshot())
        wrapper.release()

    def test_source_routes_all_profile_targets_to_native_camera_owner(self):
        module, caps, work, _ = fake_suite_cv()
        events = []
        class Native:
            capture_identity_confirmed = True  # simulated shared capture owner
            def __init__(self, index, property_name="exposure"):
                self.name = property_name
                self.value = -3 if property_name == "exposure" else 4
                events.append(("open", self.name, threading.get_ident()))
            def range(self):
                return RANGE.copy() if self.name == "exposure" else dict(RANGE, minimum=0, maximum=8)
            def get(self):
                return {"value": self.value, "flags": 2}
            def set_manual(self, target, info):
                self.value = target
                events.append(("set", self.name, target, threading.get_ident()))
                return True
            def close(self):
                events.append(("close", self.name, threading.get_ident()))
        for _ in range(6):
            work.put(1)
        with patch.object(source, "_active_session", None):
            cap = source.Ps4SuiteSource.open(0, module=module, exposure_control_factory=Native)
            try:
                self.assertTrue(cap.read()[0])
                observed_controls = cap.controls_status()
            finally:
                self.assertTrue(cap.release())
        self.assertEqual([(e[1], e[2]) for e in events if e[0] == "set"],
                         [("exposure", -7), ("gain", 8), ("brightness", 1)])
        owners = {e[-1] for e in events}
        self.assertEqual(len(owners), 1)
        self.assertNotIn(threading.get_ident(), owners)
        self.assertFalse(any(e[0] == "set" and e[1] in (10, 14, 15) for e in caps[0].events))
        self.assertEqual(observed_controls["native_exposure"]["policy"], "profile")
        self.assertIsNone(cap.controls_status())  # released owners expose no readback


if __name__ == "__main__":
    unittest.main()
