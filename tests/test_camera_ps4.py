"""PS4 stereo acquisition contract: synthetic frames, no hardware required."""
import contextlib
import io
from pathlib import Path
import sys
import time
import types
import unittest
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.psvr_camera import ps4_camera as ps4
from tools.psvr_camera.camera_backend import CameraError


def fake_cv(frame, slow=False, read_delay=0):
    caps = []

    class Cap:
        def __init__(self, index, backend):
            self.index, self.backend = index, backend
            self.settings = {}
            self.released = False
            caps.append(self)

        def isOpened(self):
            return True

        def set(self, prop, value):
            self.settings[prop] = value
            return True

        def get(self, prop):
            return self.settings.get(prop, 0)

        def read(self):
            if slow:
                time.sleep(0.8)
            elif read_delay:
                time.sleep(read_delay)
            return frame is not None, frame

        def release(self):
            self.released = True

    return types.SimpleNamespace(VideoCapture=Cap, CAP_MSMF=1400, CAP_DSHOW=700,
        CAP_ANY=0, CAP_PROP_FRAME_WIDTH=3, CAP_PROP_FRAME_HEIGHT=4,
        CAP_PROP_FPS=5, CAP_PROP_FOURCC=6), caps


class StereoTests(unittest.TestCase):
    def test_all_transport_modes_crop_prefix_auxiliary_and_trailing_rows(self):
        for name, spec in ps4.MODES.items():
            with self.subTest(mode=name):
                frame = np.full((spec.height, spec.width, 3), 255, np.uint8)
                frame[:spec.eye_height, 48:48+spec.eye_width] = (1, 2, 3)
                frame[:spec.eye_height, 48+spec.eye_width:48+2*spec.eye_width] = (4, 5, 6)
                left, right = ps4.split_stereo(frame, name)
                self.assertEqual(left.shape, (spec.eye_height, spec.eye_width, 3))
                np.testing.assert_array_equal(left[0, 0], (1, 2, 3))
                np.testing.assert_array_equal(right[-1, -1], (4, 5, 6))
                self.assertFalse(np.any(left == 255))
                self.assertFalse(np.any(right == 255))
                left[:] = 0
                self.assertEqual(frame[0, 48, 0], 1)

    def test_reject_other_camera_scaled_raw_or_non_byte_frames(self):
        for frame in (None, np.zeros((400, 1280, 3), np.uint8),
                      np.zeros((408, 1748, 2), np.uint8),
                      np.zeros((408, 1748, 3), np.float32)):
            with self.assertRaises(CameraError):
                ps4.split_stereo(frame)

    def test_capture_explicit_index_atomic_pair_and_no_pose(self):
        frame = np.zeros((408, 1748, 3), np.uint8)
        module, caps = fake_cv(frame)
        pairs = []
        report = ps4.capture_stereo(3, module=module,
            on_pair=lambda left, right: pairs.append((left, right)) or False)
        self.assertEqual(len(caps), 1)
        self.assertEqual(caps[0].index, 3)
        self.assertEqual(caps[0].backend, 700)
        self.assertTrue(caps[0].released)
        self.assertEqual(caps[0].settings[3], 1748)
        self.assertEqual(caps[0].settings[4], 408)
        self.assertEqual(report["ps4_stereo"]["pairs_captured"], 1)
        self.assertFalse(report["ps4_stereo"]["tracking_6dof"])
        self.assertFalse(report["ps4_stereo"]["calibrated"])
        self.assertEqual(report["errors"], [])

    def test_wrong_negotiated_shape_fails_without_pair_or_fallback(self):
        module, caps = fake_cv(np.zeros((480, 640, 3), np.uint8))
        report = ps4.capture_stereo(2, module=module)
        self.assertEqual(len(caps), 1)
        self.assertTrue(report["errors"])
        self.assertEqual(report["ps4_stereo"]["pairs_captured"], 0)
        self.assertTrue(caps[0].released)

    def test_no_frames_is_not_success(self):
        module, _ = fake_cv(None)
        report = ps4.capture_stereo(0, seconds=0.05, module=module)
        self.assertTrue(report["errors"])
        self.assertEqual(report["ps4_stereo"]["pairs_captured"], 0)

    def test_normal_duration_stops_before_scheduling_deadline_timeout(self):
        module, caps = fake_cv(np.zeros((408, 1748, 3), np.uint8), read_delay=0.02)
        report = ps4.capture_stereo(0, seconds=0.3, module=module)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["timeouts"], 0)
        self.assertFalse(report["cleanup_pending"])
        self.assertGreater(report["ps4_stereo"]["pairs_captured"], 0)
        self.assertLess(report["ps4_stereo"]["pairs_captured"], 18)
        self.assertTrue(caps[0].released)

    def test_preview_works_without_opencv_highgui(self):
        module, _ = fake_cv(np.zeros((408, 1748, 3), np.uint8))
        # No cv2.imshow/waitKey/destroyAllWindows in this headless fake.
        tk = Mock()
        count = []
        with patch.dict(sys.modules, {"tkinter": tk}):
            report = ps4.capture_stereo(0, preview=True, module=module,
                on_pair=lambda l, r: count.append(1) or len(count) < 2)
        self.assertEqual(report["errors"], [])
        tk.PhotoImage.assert_called_once()
        tk.Tk.return_value.update.assert_called_once()
        tk.Tk.return_value.destroy.assert_called_once()

    def test_read_timeout_is_bounded_and_cleanup_reported(self):
        module, caps = fake_cv(np.zeros((408, 1748, 3), np.uint8), slow=True)
        started = time.monotonic()
        report = ps4.capture_stereo(0, seconds=0.02, module=module)
        self.assertLess(time.monotonic() - started, 0.7)
        self.assertTrue(report["errors"])
        self.assertTrue(report["cleanup_pending"])
        self.assertEqual(report["timeouts"], 1)
        time.sleep(0.4)
        self.assertTrue(caps[0].released)

    def test_invalid_modes_rates_duration_backend_before_open(self):
        for kwargs in ({"mode": "foo"}, {"fps": 240}, {"seconds": float("nan")},
                       {"seconds": 61}, {"seconds": 0}, {"backend": "any"}):
            with self.assertRaises(CameraError):
                ps4.capture_stereo(0, **kwargs)


class InventoryTests(unittest.TestCase):
    def device(self, pid, status="OK"):
        return {"InstanceId": f"USB\\VID_05A9&PID_{pid}\\123", "Status": status}

    def test_absent_boot_runtime_and_driver_error(self):
        self.assertEqual(ps4.classify_devices([])["state"], "not_detected")
        self.assertEqual(ps4.classify_devices([self.device("0580")])["state"], "firmware_required")
        for pid in ("058A", "058B"):
            self.assertEqual(ps4.classify_devices([self.device(pid)])["state"], "uvc_present")
            self.assertEqual(ps4.classify_devices([self.device(pid, "Error")])["state"], "device_error")

    def test_psvr_and_ps3eye_are_not_ps4_camera(self):
        result = ps4.classify_devices([
            {"InstanceId": "USB\\VID_054C&PID_09AF", "Status": "Error"},
            {"InstanceId": "USB\\VID_054C&PID_0D0A", "Status": "OK"},
            {"InstanceId": "USB\\VID_1415&PID_2000", "Status": "OK"}])
        self.assertEqual(result["devices"], [])
        self.assertIsNone(result["uvc_index"])

    def test_inventory_does_not_open_camera(self):
        with patch.object(ps4, "inventory", return_value=ps4.classify_devices([])), \
             patch.object(ps4, "capture_stereo") as capture, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ps4.main(["--inventory"]), 2)
            capture.assert_not_called()

    def test_cli_requires_index(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            ps4.main([])


if __name__ == "__main__":
    unittest.main()
