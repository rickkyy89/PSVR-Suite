"""Regression tests for B7-01/B7-02/B7-03 on DiagnosticImuSource.

Offline only: the source is built with ``__new__`` so no diagnostic process,
camera, HID or SteamVR is involved. Covers API parity with DriverImuSource
(``latest``), quaternion validation in ``_accept`` and argument validation
in ``at``.
"""
import threading
import unittest
from collections import deque

from tools.psvr_camera.imu_clock import ImuClockMapper
from tools.psvr_camera.imu_source import DiagnosticImuSource


def offline_source():
    source = DiagnosticImuSource.__new__(DiagnosticImuSource)
    source.samples = deque(maxlen=200)
    source.lock = threading.Lock()
    source.changed = threading.Condition(source.lock)
    source.clock_mapper = ImuClockMapper()
    source.offset = None
    return source


def imu_row(elapsed=0.0, quat=(1., 0., 0., 0.), **overrides):
    row = {"event": "imu", "host_elapsed_s": elapsed, "recenter_count": 0,
           "valid": True, "quaternion_wxyz": list(quat)}
    row.update(overrides)
    return row


class DiagnosticLatestTests(unittest.TestCase):
    def test_latest_present_and_empty_is_none(self):
        source = offline_source()
        self.assertTrue(hasattr(source, "latest"))
        self.assertIsNone(source.latest())

    def test_latest_returns_isolated_snapshot(self):
        source = offline_source()
        with source.changed:
            self.assertTrue(source._accept(imu_row(0.0), 10.0))
        snapshot = source.latest()
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["quaternion_wxyz"], [1., 0., 0., 0.])
        self.assertEqual(len(source.samples), 1)
        snapshot["quaternion_wxyz"][0] = 0.
        snapshot["host_elapsed_s"] = 999.
        self.assertEqual(source.samples[-1]["quaternion_wxyz"], [1., 0., 0., 0.])
        self.assertEqual(source.samples[-1]["host_elapsed_s"], 0.0)

    def test_trial_driver_binding_pattern_with_at_none(self):
        # Mirrors static_tracking_trial.py driver-mode binding:
        # binding = sample if sample is not None else source.latest().
        # Must not raise AttributeError whether history is empty or not.
        empty = offline_source()
        sample = empty.at(10.0, wait_s=0)
        self.assertIsNone(sample)
        binding = sample if sample is not None else empty.latest()
        self.assertIsNone(binding)
        with empty.changed:
            self.assertTrue(empty._accept(imu_row(0.0), 10.0))
        early = empty.at(empty.samples[-1]["monotonic_s"]-1.0, wait_s=0)
        self.assertIsNone(early)
        binding = early if early is not None else empty.latest()
        self.assertIsNotNone(binding)
        self.assertEqual(binding, empty.samples[-1])


class DiagnosticQuaternionTests(unittest.TestCase):
    def test_nan_or_nonfinite_quaternion_rejected(self):
        for quat in ([float("nan"), 0., 0., 0.], [1., float("inf"), 0., 0.],
                     [0., 0., 0., 0.], [2., 0., 0., 0.], [1., 0., 0.],
                     [1., 0., 0., 0., 0.], "not-a-quat",
                     [True, False, False, False]):
            with self.subTest(quat=quat):
                source = offline_source()
                with source.changed:
                    self.assertFalse(source._accept(imu_row(0.0, quat=quat), 10.0))
                self.assertEqual(len(source.samples), 0)

    def test_sensor_key_validated_like_primary_key(self):
        source = offline_source()
        with source.changed:
            self.assertFalse(source._accept(
                imu_row(0.0, quaternion_sensor_wxyz=[float("nan"), 0., 0., 0.]), 10.0))
            self.assertFalse(source._accept(
                imu_row(0.0, quaternion_sensor_wxyz=[2., 0., 0., 0.]), 10.0))
            self.assertTrue(source._accept(
                imu_row(0.0, quaternion_sensor_wxyz=[0., 1., 0., 0.]), 10.0))
        self.assertEqual(len(source.samples), 1)

    def test_valid_normalized_quaternions_accepted(self):
        source = offline_source()
        with source.changed:
            self.assertTrue(source._accept(imu_row(0.0), 10.0))
            self.assertTrue(source._accept(imu_row(0.05, quat=[0., 0., 0., 1.]), 10.05))
        self.assertEqual(len(source.samples), 2)

    def test_rows_without_quaternion_keys_still_accepted(self):
        source = offline_source()
        row = {"event": "imu", "host_elapsed_s": 0.0, "recenter_count": 0, "valid": True}
        with source.changed:
            self.assertTrue(source._accept(row, 10.0))
        self.assertEqual(len(source.samples), 1)


class DiagnosticAtValidationTests(unittest.TestCase):
    def test_nonfinite_or_negative_wait_rejected(self):
        source = offline_source()
        for captured, wait in ((float("nan"), 0.0), (10.0, float("nan")),
                               (float("inf"), 0.0), (10.0, float("inf")),
                               (10.0, -0.01), ("10", 0.0), (10.0, True)):
            with self.subTest(captured=captured, wait=wait), self.assertRaises(ValueError):
                source.at(captured, wait_s=wait)

    def test_zero_wait_on_empty_history_returns_none(self):
        self.assertIsNone(offline_source().at(10.0, wait_s=0))


if __name__ == "__main__":
    unittest.main()
