"""Synthetic PS4 live capture tests: never open hardware or a GUI."""
from pathlib import Path
import queue
import sys
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.psvr_camera import ps4_stereo_session as stereo
from tools.psvr_camera.camera_backend import CameraError


def fake_cv(*, blocked=None, read_error=False, failed_reads=False,
            invalid_geometry=False, reject_gain=False, cached=False,
            blocked_open=None, blocked_release=None):
    """Queue-fed raw capture asserting every driver call has the same owner."""
    caps, work = [], queue.Queue()
    entered, released = threading.Event(), threading.Event()

    class Cap:
        def __init__(self, index, backend):
            self.owner = threading.get_ident()
            self.values = {15: -3, 14: 4, 10: 4}
            self.originals = self.values.copy()
            self.events = []
            self.reads = 0
            self.frame = np.zeros((408, 1748, 3), np.uint8)
            caps.append(self)
            if blocked_open is not None:
                blocked_open.wait(3)

        def event(self, *event):
            assert threading.get_ident() == self.owner
            self.events.append(event)

        def isOpened(self):
            self.event("isOpened")
            return True

        def set(self, prop, value):
            self.event("set", prop, value)
            self.values[prop] = value
            return not (reject_gain and prop == 14 and value == 8)

        def get(self, prop):
            self.event("get", prop)
            return (self.originals if cached and prop in self.originals
                    else self.values).get(prop, 0)

        def read(self):
            self.event("read")
            self.reads += 1
            entered.set()
            if blocked is not None:
                blocked.wait(3)
            if read_error:
                raise RuntimeError("synthetic read failure")
            if failed_reads:
                return False, None
            try:
                value = work.get(timeout=0.02)
            except queue.Empty:
                return False, None
            self.frame.fill(value)
            return True, self.frame[:1] if invalid_geometry else self.frame

        def release(self):
            self.event("release")
            if blocked_release is not None:
                blocked_release.wait(3)
            released.set()

    module = types.SimpleNamespace(VideoCapture=Cap, CAP_DSHOW=700, CAP_ANY=0,
        CAP_PROP_FRAME_WIDTH=3, CAP_PROP_FRAME_HEIGHT=4, CAP_PROP_FPS=5,
        CAP_PROP_FOURCC=6, CAP_PROP_EXPOSURE=15, CAP_PROP_GAIN=14,
        CAP_PROP_BRIGHTNESS=10)
    return module, caps, work, entered, released


class StereoSessionTests(unittest.TestCase):
    def wait_for(self, predicate, timeout=1.0):
        deadline = time.monotonic() + timeout
        while not predicate():
            self.assertLess(time.monotonic(), deadline, "condition did not become true")
            threading.Event().wait(0.002)

    def make_session(self, *, fake=None, **kwargs):
        module, caps, work, entered, released = fake_cv(**(fake or {}))
        session = stereo.StereoSession(0, module=module, **kwargs)
        self.addCleanup(session.stop)
        return session, caps, work, entered, released

    def test_validation_happens_before_worker_or_import(self):
        bad = ({"index": -1}, {"index": True}, {"index": None},
               {"mode": "unknown"}, {"mode": []}, {"fps": 59},
               {"fps": float("nan")}, {"fps": float("inf")}, {"fps": "60"},
               {"read_timeout_s": 0}, {"read_timeout_s": -1},
               {"read_timeout_s": float("inf")}, {"read_timeout_s": float("nan")},
               {"gain": -1}, {"brightness": float("nan")},
               {"exposure": float("inf")}, {"allow_delayed_readback": 1})
        for args in bad:
            with self.subTest(args=args), patch.object(stereo, "import_cv2") as imp, \
                    patch.object(stereo.threading, "Thread") as worker:
                with self.assertRaises(CameraError):
                    stereo.StereoSession(**{"index": 0, **args})
                imp.assert_not_called()
                worker.assert_not_called()

    def test_stop_before_start_is_idempotent_and_does_not_open(self):
        session, caps, *_ = self.make_session()
        self.assertTrue(session.stop(0))
        self.assertTrue(session.stop())
        self.assertTrue(session.done.is_set())
        self.assertEqual(caps, [])
        self.assertIsNone(session.next_pair(0))
        with self.assertRaises(CameraError):
            session.start()

    def test_pair_contract_independent_copies_and_same_driver_owner(self):
        session, caps, work, _, released = self.make_session(exposure=-2)
        work.put(23)
        self.assertIs(session.start(), session)
        self.assertIs(session.start(), session)
        self.assertTrue(session._thread.daemon)
        pair = session.next_pair(0.5)
        self.assertEqual(set(pair), {"sequence", "host_monotonic", "left", "right"})
        self.assertEqual(pair["sequence"], 1)
        self.assertIsInstance(pair["host_monotonic"], float)
        for eye in ("left", "right"):
            self.assertEqual(pair[eye].shape, (400, 640, 3))
            self.assertEqual(pair[eye].dtype, np.uint8)
            self.assertTrue(np.all(pair[eye] == 23))
            self.assertFalse(np.shares_memory(pair[eye], caps[0].frame))
        self.assertFalse(np.shares_memory(pair["left"], pair["right"]))
        caps[0].frame.fill(42)
        self.assertTrue(np.all(pair["left"] == 23))
        self.assertTrue(session.stop())
        self.assertTrue(released.is_set())
        self.assertNotEqual(caps[0].owner, threading.get_ident())
        self.assertEqual([caps[0].values[p] for p in (15, 14, 10)], [-3, 4, 4])
        self.assertFalse(session.report()["controls"]["restoration_pending"])

    def test_slow_consumer_only_gets_newest_and_counts_replaced_pairs(self):
        session, _, work, _, _ = self.make_session()
        for value in range(1, 11):
            work.put(value)
        session.start()
        self.wait_for(lambda: session.report()["frames_published"] == 10)
        pair = session.next_pair(0)
        self.assertEqual(pair["sequence"], 10)
        self.assertTrue(np.all(pair["left"] == 10))
        self.assertEqual(session.report()["dropped"], 9)
        self.assertIsNone(session.next_pair(0.005))
        work.put(11)
        newer = session.next_pair(0.5)
        self.assertEqual(newer["sequence"], 11)
        self.assertGreater(newer["host_monotonic"], pair["host_monotonic"])
        self.assertEqual(session.report()["dropped"], 9)

    def test_timestamp_is_observed_before_split(self):
        session, _, work, _, _ = self.make_session()
        observed = []
        original = stereo.split_stereo

        def split(frame, mode):
            observed.append(time.monotonic())
            return original(frame, mode)

        work.put(1)
        before = time.monotonic()
        with patch.object(stereo, "split_stereo", side_effect=split):
            session.start()
            pair = session.next_pair(0.5)
            self.assertTrue(session.stop())
        self.assertLessEqual(before, pair["host_monotonic"])
        self.assertLessEqual(pair["host_monotonic"], observed[0])
        self.assertIn("not_exposure", session.report()["timing_source"])

    def test_empty_next_pair_wait_is_bounded(self):
        gate = threading.Event()
        session, _, _, entered, _ = self.make_session(fake={"blocked": gate})
        self.addCleanup(gate.set)
        session.start()
        self.assertTrue(entered.wait(0.5))
        before = time.monotonic()
        self.assertIsNone(session.next_pair(0.025))
        self.assertLess(time.monotonic() - before, 0.2)
        gate.set()

    def test_read_exception_is_terminal_and_restores_controls(self):
        session, caps, _, _, released = self.make_session(fake={"read_error": True},
                                                         exposure=-2)
        session.start()
        self.assertTrue(session.done.wait(1))
        self.assertIn("synthetic read failure", session.report()["error"])
        self.assertTrue(session.stop())
        self.assertEqual(caps[0].reads, 1)
        self.assertEqual([caps[0].values[p] for p in (15, 14, 10)], [-3, 4, 4])
        self.assertTrue(released.is_set())

    def test_rejected_control_with_side_effect_rolls_back_without_raw_read(self):
        session, caps, *_ = self.make_session(fake={"reject_gain": True})
        session.start()
        self.assertTrue(session.done.wait(1))
        report = session.report()
        self.assertIn("gain", report["error"])
        self.assertEqual(caps[0].reads, 0)
        self.assertEqual(caps[0].values[14], 4)
        self.assertTrue(report["controls"]["restore_write_accepted"]["gain"])
        self.assertFalse(report["cleanup_pending"])

    def test_delayed_readback_is_allowed_but_not_claimed_verified(self):
        session, caps, work, _, _ = self.make_session(fake={"cached": True})
        work.put(1)
        session.start()
        self.assertIsNotNone(session.next_pair(0.5))
        self.assertTrue(session.stop())
        controls = session.report()["controls"]
        self.assertFalse(controls["readback_confirmed"]["gain"])
        self.assertFalse(controls["readback_confirmed"]["brightness"])
        self.assertFalse(controls["restoration_independently_verified"])
        self.assertEqual(caps[0].values[14], 4)
        self.assertTrue(controls["warnings"])

    def test_five_false_reads_stop_without_busy_loop(self):
        session, caps, *_ = self.make_session(fake={"failed_reads": True})
        before = time.monotonic()
        session.start()
        self.assertTrue(session.done.wait(1))
        self.assertGreaterEqual(time.monotonic() - before, 0.06)
        self.assertEqual(caps[0].reads, 5)
        self.assertIn("5 times", session.report()["error"])

    def test_invalid_geometry_stops_and_restores_without_fallback(self):
        session, caps, work, _, _ = self.make_session(fake={"invalid_geometry": True})
        work.put(1)
        session.start()
        self.assertTrue(session.done.wait(1))
        self.assertIn("PS4 UVC requires BGR", session.report()["error"])
        self.assertEqual(session.report()["frames_published"], 0)
        self.assertEqual(caps[0].reads, 1)
        self.assertEqual(caps[0].values[14], 4)

    def test_stuck_read_stop_is_bounded_never_releases_under_active_read(self):
        gate = threading.Event()
        session, caps, _, entered, released = self.make_session(
            fake={"blocked": gate}, read_timeout_s=0.05)
        self.addCleanup(gate.set)
        session.start()
        self.assertTrue(entered.wait(0.5))
        before = time.monotonic()
        self.assertFalse(session.stop(0.01))
        self.assertLess(time.monotonic() - before, 0.15)
        self.assertFalse(released.is_set())
        self.assertTrue(session.report()["cleanup_pending"])
        self.assertTrue(session.done.wait(1.2))
        self.assertIn("read timed out", session.report()["error"])
        self.assertTrue(session.report()["controls"]["restoration_pending"])
        self.assertFalse(session.stop(0))
        self.assertFalse(released.is_set())
        self.assertEqual(caps[0].reads, 1)
        gate.set()
        self.assertTrue(released.wait(0.5))
        self.assertTrue(session.stop(0))
        self.assertFalse(session.report()["cleanup_pending"])
        self.assertEqual(caps[0].values[14], 4)
        self.assertFalse(session.report()["controls"]["restoration_pending"])

    def test_slow_release_pending_is_reported_until_owner_finishes(self):
        gate = threading.Event()
        session, _, _, _, released = self.make_session(
            fake={"read_error": True, "blocked_release": gate})
        self.addCleanup(gate.set)
        session.start()
        self.assertTrue(session.done.wait(1.2))
        self.assertFalse(released.is_set())
        self.assertTrue(session.report()["cleanup_pending"])
        gate.set()
        self.assertTrue(released.wait(0.5))
        self.assertTrue(session.stop(0))

    def test_report_is_independent_snapshot(self):
        session, *_ = self.make_session()
        report = session.report()
        report["controls"]["requested"]["gain"] = 99
        self.assertEqual(session.report()["controls"]["requested"]["gain"], 8)

    def test_bounded_session_budgets_and_negotiated_transport(self):
        module, *_ = fake_cv()
        inner = Mock()
        inner.open.return_value.to_dict.return_value = {"readable": False}
        inner.read.side_effect = CameraError("terminal synthetic timeout")
        inner.release.return_value = True
        owners = []

        def factory(*args, **kwargs):
            owners.append(threading.get_ident())
            return inner

        with patch.object(stereo, "BoundedCameraSession", side_effect=factory) as factory_mock:
            session = stereo.StereoSession(3, mode="320x192", fps=120,
                                           module=module, read_timeout_s=0.2).start()
            self.assertTrue(session.done.wait(1))
            self.assertTrue(session.stop())
        self.assertNotEqual(owners[0], threading.get_ident())
        args, kwargs = factory_mock.call_args
        self.assertEqual(args, (3,))
        self.assertEqual((kwargs["width"], kwargs["height"], kwargs["fps"]),
                         (898, 200, 120))
        self.assertEqual(kwargs["backend"], "dshow")
        self.assertEqual(kwargs["open_timeout_ms"], 3000)
        self.assertEqual(kwargs["read_timeout_ms"], 200)
        inner.open.assert_called_once_with(timeout_s=3.0)
        inner.read.assert_called_once_with(timeout_s=0.2)
        inner.release.assert_called_once_with(timeout_s=0.75)

    def test_method_timeouts_must_be_finite_and_nonnegative(self):
        session, *_ = self.make_session()
        for value in (-1, float("nan"), float("inf"), "1", True):
            for method in (session.stop, session.next_pair):
                with self.subTest(value=value, method=method), self.assertRaises(CameraError):
                    method(value)


if __name__ == "__main__":
    unittest.main()
