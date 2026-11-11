# SPDX-License-Identifier: MIT
"""Regressione B11-05: early-return mkdir deve azzerare _thread.

Offline only: mock camera/filesystem, nessun hardware/driver/rete/Tk.
"""
from __future__ import annotations

import copy
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from tools.psvr_suite.tracking import TrackingWorker, default_prefs


def _wait_for(predicate, timeout=5.0, step=0.01):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


class _FakeCap:
    def __init__(self):
        self.released = False
        self.last_frame_monotonic = None

    def read(self):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        frame[100:110, 100:110] = (255, 0, 0)
        self.last_frame_monotonic = time.perf_counter()
        return True, frame

    def release(self):
        self.released = True

    def set_manual_controls(self, exposure, gain):
        pass

    def controls_status(self):
        return {"exposure": 13, "gain": 0}


class _FakeImu:
    def __init__(self):
        self.seq = 0
        self.closed = False
        self._last = None

    def at(self, captured_at, wait_s=0.025):
        self.seq += 1
        row = {
            "session_id": "activation-a", "recenter_epoch": 1, "seq": self.seq,
            "sample_time_s": captured_at - 0.005,
            "sent_time_s": captured_at - 0.004,
            "monotonic_s": captured_at - 0.005, "valid": True,
            "connected": True, "state": "tracking",
            "quaternion_sensor_wxyz": [1.0, 0.0, 0.0, 0.0],
            "world_rotation_wxyz": [1.0, 0.0, 0.0, 0.0],
            "corrected_gyro_rad_s": [0.0, 0.0, 0.0],
        }
        self._last = copy.deepcopy(row)
        return copy.deepcopy(row)

    def latest(self):
        return copy.deepcopy(self._last) if self._last else None

    def close(self):
        self.closed = True


class _FakePose:
    valid = True
    reason = "imu_guided_pattern"
    image_points = np.zeros((5, 2)) + 100.0
    rvec = np.zeros((3, 1))
    tvec = np.array([[0.0], [0.0], [1.0]])
    rms_px = 0.5
    rms_limit_px = 2.5
    image_span_px = 80.0
    led_labels = ["upper_left", "upper_right", "centre",
                  "lower_left", "lower_right"]
    predicted_labels = led_labels
    imu_age_s = 0.005

    def to_dict(self):
        return {"valid": True, "reason": self.reason,
                "rvec": [0.0, 0.0, 0.0], "tvec": [0.0, 0.0, 1.0],
                "rms_px": self.rms_px, "rms_limit_px": self.rms_limit_px,
                "image_span_px": self.image_span_px,
                "led_labels": list(self.led_labels)}


class _FakeTracker:
    def __init__(self, *args, **kwargs):
        self.alignment = np.eye(3)

    def reset(self):
        pass

    def set_imu(self, sample):
        pass

    def search_region(self, width, height, now):
        return None, 1.0

    def update(self, candidates, now):
        return _FakePose()


class _FakeDetector:
    def __init__(self, *args, **kwargs):
        pass

    def detect(self, frame):
        return [{"center_px": [100.0, 100.0], "area_px": 20,
                 "bbox_px": [95, 95, 10, 10]}]

    def annotate(self, frame, candidates):
        return frame.copy()


class _FakePipeline:
    def __init__(self, mode="one_euro", **kwargs):
        self.mode = mode

    def reset(self, reason="manual"):
        pass

    def invalid(self, now):
        pass

    def filter_packet(self, packet, alignment, now, **kwargs):
        if packet is None:
            return None
        out = dict(packet)
        out.setdefault("position_m", [0.0, 0.0, 1.0])
        return out

    def diagnostics(self):
        return {"type": self.mode}


class _FakeSender:
    def __init__(self, port=4271, host="127.0.0.1"):
        self.closed = False
        self.sent = []

    def send(self, packet):
        self.sent.append(copy.deepcopy(packet))
        return True

    def invalidate(self):
        return True

    def close(self):
        self.closed = True
        return True


class B1105MkdirThreadCleanupTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "logs").mkdir(parents=True, exist_ok=True)

    def _prefs(self, **changes):
        base = default_prefs()
        base["log"] = "logs/b11_05_probe.jsonl"
        base.update(changes)
        return base

    def test_mkdir_failure_clears_thread_before_and_after_stop(self):
        """mkdir fallita -> _thread None sia prima che dopo stop."""
        events = []
        worker = TrackingWorker(self.root, lambda e: events.append(e))
        prefs = self._prefs(log="logs/b11_05_mkdir_fail.jsonl")
        with patch.object(Path, "mkdir",
                           side_effect=OSError("mocked mkdir failure")):
            worker.start(prefs)
            # Il worker fallisce subito: stato error, running False.
            ok = _wait_for(lambda: worker.status().get("state") == "error",
                           timeout=5.0)
            self.assertTrue(ok, "worker did not report mkdir error")
            # Invariante B11-05: nessun riferimento morto conservato.
            ok = _wait_for(lambda: worker._thread is None, timeout=5.0)
            self.assertIsNone(worker._thread,
                              "_thread must be None after mkdir early-return")
            self.assertFalse(worker.status()["running"])
        # Dopo stop: ancora None, stop True (worker già terminato).
        result = worker.stop(timeout=5.0)
        self.assertTrue(result)
        self.assertIsNone(worker._thread,
                          "_thread must stay None after stop")
        self.assertFalse(worker.status()["running"])
        kinds = [e.get("kind") for e in events]
        self.assertIn("error", kinds)

    def test_normal_path_lifecycle_unchanged(self):
        """Path normale: thread vivo prima, None dopo stop; restart ok."""
        cap = _FakeCap()
        imu = _FakeImu()
        intrinsics = Mock()
        intrinsics.image_size = (640, 480)
        intrinsics.camera_matrix = np.eye(3)
        intrinsics.distortion_coeffs = np.zeros(5)
        intrinsics.calibrated = True
        front_rig = {"provenance": "s",
                     "labels": ["a", "b", "c", "d", "e"]}
        rot_rig = {"provenance": "s",
                   "labels": ["a", "b", "c", "d", "e", "f", "g", "h", "i"]}
        with patch("tools.psvr_suite.tracking.load_intrinsics",
                   return_value=intrinsics), \
             patch("tools.psvr_suite.tracking.load_led_rig",
                   return_value=(front_rig, np.zeros((5, 3)))), \
             patch("tools.psvr_suite.tracking.load_rotation_rig",
                   return_value=(rot_rig, np.zeros((9, 3)), np.zeros((9, 3)))), \
             patch("tools.psvr_suite.tracking.load_position_config",
                   return_value={}), \
             patch("tools.psvr_suite.tracking.open_camera_source",
                   return_value=(cap, None)), \
             patch("tools.psvr_suite.tracking.DriverImuSource",
                   return_value=imu), \
             patch("tools.psvr_suite.tracking.BlueLedDetector", _FakeDetector), \
             patch("tools.psvr_suite.tracking.ImuLedTracker", _FakeTracker), \
             patch("tools.psvr_suite.tracking.PositionPipeline", _FakePipeline), \
             patch("tools.psvr_suite.tracking.DriverPositionSender", _FakeSender), \
             patch("tools.psvr_suite.tracking.build_driver_position_packet",
                   return_value={"valid": False, "state": "lost",
                                 "position_m": None,
                                 "session_id": "activation-a",
                                 "recenter_epoch": 1, "seq": 0}), \
             patch("tools.psvr_suite.tracking.estimate_pose_covariance",
                   return_value=(None, {"reliable": False})):
            worker = TrackingWorker(self.root, lambda e: None)
            worker.start(self._prefs(log="logs/b11_05_normal.jsonl"))
            try:
                ok = _wait_for(lambda: worker.status()["captured_frames"] >= 2,
                               timeout=5.0)
                self.assertTrue(ok, "normal worker did not capture")
                # Thread vivo prima dello stop.
                self.assertIsNotNone(worker._thread)
                self.assertTrue(worker._thread.is_alive())
                ok = worker.stop(timeout=5.0)
                self.assertTrue(ok)
                self.assertIsNone(worker._thread)
                self.assertFalse(worker.status()["running"])
            finally:
                try:
                    worker.stop(timeout=5.0)
                except Exception:
                    pass


if __name__ == "__main__":
    unittest.main()
