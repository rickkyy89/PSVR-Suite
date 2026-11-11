# SPDX-License-Identifier: MIT
"""Bounded-latency capture tests for the OpenCV path.

OpenCV backends queue frames internally and hand them back in arrival order,
so a consumer slower than the camera would otherwise process an ever older
backlog for the whole session; the native PS3 Eye adapter is single-slot. The
tests below prove the ``LatestFrameCapture`` adapter keeps that same
single-slot contract with an injected fake queueing ``VideoCapture``: a
backlog is dropped (never replayed), a frame is never returned twice, the
capture timestamp travels with the frame, failures stop the reader thread
instead of busy-spinning, and release closes the inner capture exactly once.
No camera, no OpenCV and no network are required.
"""
from __future__ import annotations

from collections import deque
from pathlib import Path
import sys
import threading
import time
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from psvr_camera import camera_backend as CB  # noqa: E402
from psvr_camera import camera_source as CS  # noqa: E402
from psvr_camera.estimator import PositionFilter  # noqa: E402
from psvr_camera.tracker import TrackConfig, run_track  # noqa: E402
from psvr_camera.udp_sender import SequenceAllocator  # noqa: E402


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class QueueCap:
    """Fake ``VideoCapture`` that behaves like a queueing backend.

    ``push()`` appends a frame; ``read()`` returns the oldest queued frame
    immediately, or waits briefly for one, then reports a timed-out read like
    a real backend that has nothing to deliver.
    """

    def __init__(self):
        self._cv = threading.Condition()
        self._queue = deque()
        self.settings = {}
        self.releases = 0
        self.reads = 0
        self.blocking = True

    def push(self, frame):
        with self._cv:
            self._queue.append(frame)
            self._cv.notify()

    def read(self):
        with self._cv:
            self.reads += 1
            if not self._queue and self.blocking:
                self._cv.wait(0.05)
            if self._queue:
                return True, self._queue.popleft()
            return False, None

    def set(self, prop, value):
        self.settings[prop] = value
        return True

    def get(self, prop):
        return 0.0

    def isOpened(self):
        return True

    def release(self):
        self.releases += 1


class StuckCap:
    """Fake backend whose ``read()`` never returns until told to."""

    def __init__(self):
        self.entered = threading.Event()
        self.unblock = threading.Event()
        self.releases = 0

    def read(self):
        self.entered.set()
        self.unblock.wait(5.0)
        return False, None

    def set(self, prop, value):
        return True

    def get(self, prop):
        return 0.0

    def isOpened(self):
        return True

    def release(self):
        self.releases += 1


class RaisingCap:
    def __init__(self):
        self.releases = 0

    def read(self):
        raise RuntimeError("device vanished")

    def set(self, prop, value):
        return True

    def get(self, prop):
        return 0.0

    def isOpened(self):
        return True

    def release(self):
        self.releases += 1


class LatestFrameCaptureTest(unittest.TestCase):
    def test_consumed_frames_are_not_counted_as_dropped(self):
        cap = QueueCap()
        wrapped = CB.LatestFrameCapture(cap, max_consecutive_failures=100)
        try:
            for name in ("one", "two", "three"):
                cap.push(name)
                self.assertEqual(wrapped.read(timeout_s=1), (True, name))
            self.assertEqual(wrapped.frames_published, 3)
            self.assertEqual(wrapped.frames_dropped, 0)
        finally:
            wrapped.release()

    def test_mixed_consumption_counts_only_the_unread_replacement(self):
        cap = QueueCap()
        wrapped = CB.LatestFrameCapture(cap, max_consecutive_failures=100)
        try:
            cap.push("one")
            self.assertEqual(wrapped.read(timeout_s=1), (True, "one"))
            cap.push("two")
            cap.push("three")
            self.assertTrue(wait_until(lambda: wrapped.frames_published == 3))
            self.assertEqual(wrapped.read(timeout_s=1), (True, "three"))
            self.assertEqual(wrapped.frames_dropped, 1)
        finally:
            wrapped.release()

    def test_backlog_is_dropped_and_only_the_newest_frame_is_delivered(self):
        # The defect this fixes: a queueing backend replays every queued frame
        # in arrival order, so a slow consumer inherits an ever older backlog.
        # The adapter must drain that backlog and keep only the newest frame.
        cap = QueueCap()
        for i in range(20):
            cap.push(f"frame-{i}")
        wrapped = CB.LatestFrameCapture(
            cap, read_timeout_s=0.5, max_consecutive_failures=5
        )
        try:
            self.assertTrue(wait_until(lambda: wrapped.frames_published == 20))
            # 19 frames were replaced before anyone could read them: they are
            # counted as dropped, never replayed later.
            self.assertEqual(wrapped.frames_dropped, 19)
            ok, frame = wrapped.read(timeout_s=0.5)
            self.assertTrue(ok)
            self.assertEqual(frame, "frame-19")
            # No new frames arrive: the read must time out, not replay the
            # backlog this consumer never asked for.
            ok, frame = wrapped.read(timeout_s=0.1)
            self.assertFalse(ok)
            self.assertIsNone(frame)
        finally:
            wrapped.release()
        self.assertEqual(cap.releases, 1)

    def test_same_frame_is_never_returned_twice(self):
        cap = QueueCap()
        wrapped = CB.LatestFrameCapture(
            cap, read_timeout_s=0.15, max_consecutive_failures=5
        )
        try:
            cap.push("a")
            ok, frame = wrapped.read(timeout_s=1.0)
            self.assertTrue(ok)
            self.assertEqual(frame, "a")
            # The slot still holds "a"; a second read must wait for a newer
            # frame instead of treating the old one as fresh.
            ok, frame = wrapped.read(timeout_s=0.15)
            self.assertFalse(ok)
            cap.push("b")
            ok, frame = wrapped.read(timeout_s=1.0)
            self.assertTrue(ok)
            self.assertEqual(frame, "b")
        finally:
            wrapped.release()

    def test_capture_timestamp_travels_with_the_frame(self):
        cap = QueueCap()
        wrapped = CB.LatestFrameCapture(cap, read_timeout_s=0.5)
        try:
            before = time.monotonic()
            cap.push("a")
            ok, _ = wrapped.read(timeout_s=1.0)
            self.assertTrue(ok)
            first = wrapped.last_frame_monotonic
            self.assertIsNotNone(first)
            self.assertGreaterEqual(first, before)
            self.assertLessEqual(first, time.monotonic())
            cap.push("b")
            ok, _ = wrapped.read(timeout_s=1.0)
            self.assertTrue(ok)
            self.assertGreaterEqual(wrapped.last_frame_monotonic, first)
        finally:
            wrapped.release()

    def test_consecutive_failures_stop_the_reader_and_surface_error(self):
        cap = QueueCap()
        cap.blocking = False  # every read fails immediately
        wrapped = CB.LatestFrameCapture(
            cap, read_timeout_s=0.1, max_consecutive_failures=3
        )
        try:
            self.assertTrue(wait_until(lambda: wrapped.error is not None))
            self.assertTrue(wrapped.failed)
            self.assertIn("3 times in a row", wrapped.error)
            self.assertFalse(wrapped.isOpened())
            ok, frame = wrapped.read(timeout_s=0.1)
            self.assertFalse(ok)
            self.assertIsNone(frame)
        finally:
            wrapped.release()
        self.assertEqual(cap.releases, 1)

    def test_reader_exception_is_reported_as_error(self):
        cap = RaisingCap()
        wrapped = CB.LatestFrameCapture(
            cap, read_timeout_s=0.1, max_consecutive_failures=2
        )
        try:
            self.assertTrue(wait_until(lambda: wrapped.error is not None))
            self.assertIn("RuntimeError", wrapped.error)
        finally:
            wrapped.release()
        self.assertEqual(cap.releases, 1)

    def test_release_closes_the_inner_capture_once_and_is_idempotent(self):
        cap = QueueCap()
        cap.push("a")
        wrapped = CB.LatestFrameCapture(cap, read_timeout_s=0.5)
        ok, _ = wrapped.read(timeout_s=1.0)
        self.assertTrue(ok)
        wrapped.release()
        wrapped.release()
        self.assertEqual(cap.releases, 1)
        self.assertFalse(wrapped.abandoned)
        ok, frame = wrapped.read(timeout_s=0.1)
        self.assertFalse(ok)
        self.assertIsNone(frame)

    def test_stuck_backend_read_is_abandoned_not_closed_under_the_thread(self):
        cap = StuckCap()
        wrapped = CB.LatestFrameCapture(
            cap, read_timeout_s=0.1, max_consecutive_failures=10,
            release_join_timeout_s=0.2,
        )
        try:
            self.assertTrue(wait_until(cap.entered.is_set))
            wrapped.release()
            # Closing the capture while the reader is still inside read()
            # would be a use-after-free: the handle is leaked on purpose.
            self.assertTrue(wrapped.abandoned)
            self.assertEqual(cap.releases, 0)
            self.assertFalse(wrapped.isOpened())
            ok, frame = wrapped.read(timeout_s=0.05)
            self.assertFalse(ok)
        finally:
            cap.unblock.set()
            wrapped._thread.join(timeout=2.0)

    def test_set_get_and_unknown_attributes_forward_to_the_inner_capture(self):
        cap = QueueCap()
        wrapped = CB.LatestFrameCapture(cap, read_timeout_s=0.2)
        try:
            self.assertTrue(wrapped.isOpened())
            self.assertTrue(wrapped.set(3, 640.0))
            self.assertEqual(cap.settings[3], 640.0)
            self.assertEqual(wrapped.get(3), 0.0)
            # Attributes only the native adapter exposes must stay absent, so
            # callers keep using their hasattr() feature probes unchanged.
            self.assertFalse(hasattr(wrapped, "set_manual_controls"))
            self.assertIsNone(wrapped.error)
        finally:
            wrapped.release()


def make_fake_cv2(created):
    class FakeCap:
        def __init__(self, index, backend):
            self.index = index
            self.backend = backend
            self.settings = {}
            self.released = False
            created.append(self)

        def isOpened(self):
            return True

        def set(self, prop, value):
            self.settings[prop] = value
            return True

        def get(self, prop):
            return 0.0

        def read(self):
            return False, None

        def release(self):
            self.released = True

    return types.SimpleNamespace(
        CAP_ANY=0,
        CAP_DSHOW=700,
        CAP_PROP_FRAME_WIDTH=3,
        CAP_PROP_FRAME_HEIGHT=4,
        CAP_PROP_FPS=5,
        CAP_PROP_FOURCC=6,
        CAP_PROP_OPEN_TIMEOUT_MSEC=53,
        CAP_PROP_READ_TIMEOUT_MSEC=54,
        CAP_PROP_BUFFERSIZE=38,
        VideoCapture=FakeCap,
    )


class CameraSourceWiringTest(unittest.TestCase):
    def test_opencv_path_is_wrapped_by_default(self):
        created = []
        fake = make_fake_cv2(created)
        source, descriptor = CS.open_camera_source(0, module=fake)
        try:
            self.assertIsInstance(source, CB.LatestFrameCapture)
            self.assertEqual(descriptor.index, 0)
            # The backend is asked for the smallest internal queue it supports.
            self.assertEqual(created[0].settings[38], 1.0)
            ok, frame = source.read(timeout_s=0.05)
            self.assertFalse(ok)
        finally:
            source.release()
        self.assertTrue(created[0].released)

    def test_latest_frame_false_keeps_the_raw_capture(self):
        created = []
        fake = make_fake_cv2(created)
        source, _ = CS.open_camera_source(0, module=fake, latest_frame=False)
        self.assertNotIsInstance(source, CB.LatestFrameCapture)
        source.release()

    def test_pseye_path_is_never_wrapped(self):
        # The native backend already publishes the newest frame in one slot;
        # wrapping it would add a second thread for nothing.
        from psvr_camera import pseye_backend

        sentinel = types.SimpleNamespace(descriptor=object())
        with mock.patch.object(
            pseye_backend.PseyeCamera, "open", return_value=sentinel
        ) as opened:
            source, descriptor = CS.open_camera_source(
                0, backend="pseye", read_timeout_s=0.5
            )
        self.assertIs(source, sentinel)
        self.assertIs(descriptor, sentinel.descriptor)
        self.assertEqual(opened.call_count, 1)


class _NoMarkerDetector:
    def detect(self, frame):
        return {}


class _StampSource:
    """Source that reports a per-frame capture timestamp, like both adapters."""

    def __init__(self, frames, stamps):
        self._frames = list(frames)
        self._stamps = list(stamps)
        self._index = 0
        self.last_frame_monotonic = None

    def read(self):
        if self._index >= len(self._frames):
            return False, None
        self.last_frame_monotonic = self._stamps[self._index]
        frame = self._frames[self._index]
        self._index += 1
        return True, frame


class TrackerCaptureStampTest(unittest.TestCase):
    def test_samples_use_the_source_capture_timestamp(self):
        # The loop clock is deliberately wrong (a fake now_fn far from the
        # stamps): samples must carry the frame's capture time, not the
        # instant before the blocking read.
        stamps = [10.0, 10.5, None]
        source = _StampSource([object()] * 3, stamps)
        samples = []
        config = TrackConfig(frames=3, max_read_failures=2)
        summary = run_track(
            source, _NoMarkerDetector(), None, None, SequenceAllocator(),
            PositionFilter(smoothing_time_s=0.0), config,
            on_sample=samples.append, now_fn=lambda: 999.0,
        )
        self.assertEqual(summary.exit_reason, "frames")
        self.assertEqual([s.monotonic_s for s in samples[:2]], [10.0, 10.5])
        # A source that stops reporting a stamp falls back to the loop clock.
        self.assertEqual(samples[2].monotonic_s, 999.0)
        # The terminating invalid sample keeps the loop clock by design.
        self.assertEqual(samples[-1].monotonic_s, 999.0)

    def test_sources_without_capture_timestamp_keep_the_loop_clock(self):
        class PlainSource:
            def __init__(self):
                self._n = 0

            def read(self):
                if self._n >= 2:
                    return False, None
                self._n += 1
                return True, object()

        samples = []
        clock = {"t": 0.0}

        def now_fn():
            clock["t"] += 1.0
            return clock["t"]

        summary = run_track(
            PlainSource(), _NoMarkerDetector(), None, None, SequenceAllocator(),
            PositionFilter(smoothing_time_s=0.0), TrackConfig(frames=2),
            on_sample=samples.append, now_fn=now_fn,
        )
        self.assertEqual(summary.exit_reason, "frames")
        self.assertTrue(all(s.monotonic_s > 0.0 for s in samples))


if __name__ == "__main__":
    unittest.main()
