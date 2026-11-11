# SPDX-License-Identifier: MIT
"""Camera inventory/probe/capture tests using an injected fake ``cv2``.

No real camera and no OpenCV: the selection, bounding and reporting logic is
exercised deterministically. OpenCV absence is handled explicitly.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from psvr_camera import camera_backend as CB  # noqa: E402
from psvr_camera.capture import capture_frames  # noqa: E402

FRAME = object()


class Behavior:
    def __init__(self, open_indices=(), frame_counts=None, props=None):
        self.open_indices = set(open_indices)
        self.frame_counts = dict(frame_counts or {})
        self.props = dict(props or {})


def make_fake_cv2(behavior: Behavior):
    class FakeCap:
        def __init__(self, index, backend):
            self.index = index
            self.backend = backend
            self.reads = 0
            self.released = False
            self.settings = {}

        def isOpened(self):
            return self.index in behavior.open_indices

        def set(self, prop, value):
            self.settings[prop] = value
            return True

        def read(self):
            if not self.isOpened():
                return False, None
            self.reads += 1
            if self.reads <= behavior.frame_counts.get(self.index, 0):
                return True, FRAME
            return False, None

        def get(self, prop):
            return behavior.props.get(prop, 0)

        def release(self):
            self.released = True

    return types.SimpleNamespace(
        CAP_ANY=0,
        CAP_DSHOW=700,
        CAP_MSMF=1400,
        CAP_V4L2=200,
        CAP_PROP_FRAME_WIDTH=3,
        CAP_PROP_FRAME_HEIGHT=4,
        CAP_PROP_FPS=5,
        CAP_PROP_FOURCC=6,
        CAP_PROP_OPEN_TIMEOUT_MSEC=53,
        CAP_PROP_READ_TIMEOUT_MSEC=54,
        IMREAD_GRAYSCALE=0,
        VideoCapture=FakeCap,
    )


class InventoryTest(unittest.TestCase):
    def test_lists_every_index_not_just_the_first(self):
        behavior = Behavior(open_indices={0, 2}, frame_counts={0: 1, 2: 1})
        fake = make_fake_cv2(behavior)
        descriptors = CB.enumerate_cameras(max_index=4, module=fake)
        self.assertEqual([d.index for d in descriptors], [0, 1, 2, 3])
        self.assertTrue(descriptors[0].opened and descriptors[0].readable)
        self.assertFalse(descriptors[1].opened)
        self.assertTrue(descriptors[2].opened and descriptors[2].readable)
        self.assertFalse(descriptors[3].opened)
        # Two working cameras are both reported: no silent first-camera choice.
        readable = [d.index for d in descriptors if d.readable]
        self.assertEqual(readable, [0, 2])

    def test_opened_but_no_frame_is_reported(self):
        behavior = Behavior(open_indices={1}, frame_counts={1: 0})
        fake = make_fake_cv2(behavior)
        descriptor = CB.probe_index(1, module=fake)
        self.assertTrue(descriptor.opened)
        self.assertFalse(descriptor.readable)
        self.assertIn("no frame", descriptor.error)

    def test_backend_names_resolve_and_unknown_rejected(self):
        fake = make_fake_cv2(Behavior())
        self.assertEqual(CB.resolve_backend("dshow", fake), 700)
        self.assertEqual(CB.resolve_backend("any", fake), 0)
        with self.assertRaises(CB.UnknownBackend):
            CB.resolve_backend("nonsense", fake)

    def test_available_backends_lists_known_ids(self):
        fake = make_fake_cv2(Behavior())
        names = {b["name"] for b in CB.available_backends(fake)}
        self.assertIn("dshow", names)
        self.assertIn("msmf", names)

    def test_explicit_index_required(self):
        fake = make_fake_cv2(Behavior(open_indices={0}))
        with self.assertRaises(CB.CameraError):
            CB.open_camera(-1, module=fake)

    def test_open_failure_raises(self):
        fake = make_fake_cv2(Behavior(open_indices={0}))
        with self.assertRaises(CB.CameraError):
            CB.open_camera(5, module=fake)


class CaptureTest(unittest.TestCase):
    def test_bounded_report_counts_frames_and_timeouts(self):
        behavior = Behavior(open_indices={0}, frame_counts={0: 3})
        fake = make_fake_cv2(behavior)
        report = capture_frames(
            index=0, frames=8, max_seconds=5.0, warmup_frames=0, module=fake
        ).to_dict()
        self.assertTrue(report["opened"])
        self.assertEqual(report["frames_captured"], 3)
        self.assertGreaterEqual(report["timeouts"], 1)
        self.assertEqual(report["frames_dropped"], report["timeouts"])
        self.assertIsNotNone(report["achieved_fps"])
        self.assertEqual(report["schema"], "psvr.camera.capture")

    def test_unopened_camera_reports_error_without_raising(self):
        behavior = Behavior(open_indices=set())
        fake = make_fake_cv2(behavior)
        report = capture_frames(index=3, frames=10, max_seconds=1.0, module=fake).to_dict()
        self.assertFalse(report["opened"])
        self.assertTrue(report["errors"])

    def test_invalid_bounds_rejected(self):
        fake = make_fake_cv2(Behavior(open_indices={0}))
        with self.assertRaises(CB.CameraError):
            capture_frames(index=0, frames=0, module=fake)
        with self.assertRaises(CB.CameraError):
            capture_frames(index=0, frames=1, max_seconds=0.0, module=fake)

    def test_on_frame_can_stop_early(self):
        behavior = Behavior(open_indices={0}, frame_counts={0: 50})
        fake = make_fake_cv2(behavior)
        seen = []

        def stop_after_two(frame):
            seen.append(frame)
            return len(seen) < 2

        report = capture_frames(
            index=0, frames=50, max_seconds=5.0, warmup_frames=0,
            on_frame=stop_after_two, module=fake,
        ).to_dict()
        self.assertEqual(report["frames_captured"], 2)


class OpenCvAbsenceTest(unittest.TestCase):
    def test_injected_module_returned(self):
        sentinel = object()
        self.assertIs(CB.import_cv2(module=sentinel), sentinel)

    @unittest.skipIf(importlib.util.find_spec("cv2") is not None, "cv2 is installed")
    def test_missing_opencv_raises_actionable_error(self):
        self.assertFalse(CB.cv2_available())
        with self.assertRaises(CB.OpenCvUnavailable) as ctx:
            CB.import_cv2()
        self.assertIn("not installed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
