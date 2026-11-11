"""Real finite camera paths with blocking OpenCV doubles; no hardware."""
import threading
import time
import types
import unittest

from tools.psvr_camera import camera_backend as CB
from tools.psvr_camera.capture import capture_frames


class Fixture:
    def __init__(self, block):
        self.block = block
        self.entered = threading.Event()
        self.unblock = threading.Event()
        self.closed = threading.Event()
        self.owner = None
        self.closer = None
        fixture = self

        class Cap:
            def __init__(self, *args):
                fixture.owner = threading.get_ident()
                if fixture.block == "open": fixture.wait()
            def isOpened(self):
                if fixture.block == "status_error": raise RuntimeError("status query failed")
                return True
            def set(self, *args): return False
            def get(self, *args): return 0
            def read(self):
                if fixture.block == "read": fixture.wait()
                return True, object()
            def release(self):
                fixture.closer = threading.get_ident()
                if fixture.block == "close": fixture.wait()
                fixture.closed.set()
        self.cv = types.SimpleNamespace(CAP_ANY=0, VideoCapture=Cap,
            CAP_PROP_OPEN_TIMEOUT_MSEC=53, CAP_PROP_READ_TIMEOUT_MSEC=54)

    def wait(self):
        self.entered.set()
        if not self.unblock.wait(5): raise RuntimeError("fixture was not cleaned up")

    def finish(self):
        self.unblock.set()
        assert self.closed.wait(2), "owning thread did not release"
        assert self.owner == self.closer, "close ran under another thread's active call"


class BoundedCallsTest(unittest.TestCase):
    def test_probe_open_read_and_close_cannot_hold_caller(self):
        for phase in ("open", "read", "close"):
            with self.subTest(phase=phase):
                f = Fixture(phase)
                start = time.monotonic()
                try:
                    d = CB.probe_index(0, module=f.cv, open_timeout_ms=100, read_timeout_ms=100)
                    self.assertLess(time.monotonic()-start, 1)
                    self.assertTrue(f.entered.is_set())
                    self.assertTrue(d.cleanup_pending)
                    self.assertFalse(f.closed.is_set())
                    if phase != "close": self.assertIn("timed out", d.error)
                finally: f.finish()

    def test_capture_budget_covers_stuck_open_read_and_close(self):
        for phase in ("open", "read", "close"):
            with self.subTest(phase=phase):
                f = Fixture(phase)
                start = time.monotonic()
                try:
                    report = capture_frames(0, frames=2, max_seconds=.1, warmup_frames=0, module=f.cv)
                    self.assertLess(time.monotonic()-start, 1)
                    self.assertTrue(report.cleanup_pending)
                    self.assertFalse(f.closed.is_set())
                    if phase != "close": self.assertTrue(report.errors)
                finally: f.finish()

    def test_unsupported_timeout_properties_do_not_reject_working_camera(self):
        f = Fixture(None)
        report = capture_frames(0, frames=3, max_seconds=2, warmup_frames=0, module=f.cv)
        self.assertEqual(report.frames_captured, 3)
        self.assertFalse(report.errors)
        self.assertFalse(report.cleanup_pending)
        f.finish()

    def test_callback_still_runs_on_callers_thread(self):
        f = Fixture(None)
        owner = threading.get_ident()
        seen = []
        report = capture_frames(0, frames=3, max_seconds=2, warmup_frames=0,
            module=f.cv, on_frame=lambda frame: seen.append(threading.get_ident()))
        self.assertEqual(report.frames_captured, 3)
        self.assertEqual(seen, [owner]*3)
        f.finish()

    def test_open_status_exception_releases_on_owning_thread(self):
        f = Fixture("status_error")
        report = capture_frames(0, frames=1, max_seconds=1, module=f.cv)
        self.assertIn("status query failed", report.errors[0])
        self.assertFalse(report.cleanup_pending)
        f.finish()
