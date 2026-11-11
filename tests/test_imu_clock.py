# SPDX-License-Identifier: MIT
"""Clock-mapping unit and long-run soak tests for the IMU sources.

Offline only: no HID, camera, GUI, diagnostic process or SteamVR. The soak
feeds the real ``_accept`` paths of DriverImuSource and DiagnosticImuSource with
synthetic clock streams so a >=20 minute session runs without real-time waits.

The regression this guards: the previous lifetime ``min(received - sent)``
offset froze at its earliest observation, so a driver clock that runs slow
relative to the local monotonic clock let ``monotonic_s`` fall arbitrarily far
behind ``received`` (tracking degradation). The bounded sliding window must keep
fresh streams near local time and non-regressive within a clock origin, without
rejuvenating genuinely stale samples. Separate tests exercise resets and
single-origin runs longer than ten minutes.
"""
import copy
import random
import threading
import unittest
from collections import deque

from tools.psvr_camera.driver_imu_source import DriverImuSource
from tools.psvr_camera.imu_clock import ImuClockMapper
from tools.psvr_camera.imu_source import DiagnosticImuSource


def imu_row(session, seq, epoch, sample, sent, valid=True):
    return dict(protocol="psvr.imu", version=1, frame="psvr_imu_unrecentered",
                clock="driver_steady_seconds", session_id=session, seq=seq,
                recenter_epoch=epoch, sample_time_s=sample, sent_time_s=sent,
                connected=True, valid=valid, state="tracking" if valid else "lost",
                quaternion_sensor_wxyz=[1., 0., 0., 0.], world_rotation_wxyz=[1., 0., 0., 0.],
                corrected_gyro_rad_s=[0., 0., 0.])


def _driver_source():
    source = DriverImuSource.__new__(DriverImuSource)
    source._closed = threading.Event()
    source.retired_sessions = set()
    source.session_id = None
    source.samples = deque(maxlen=200)
    source.clock_mapper = ImuClockMapper()
    source.offset = None
    source.changed = threading.Condition()
    return source


def _diagnostic_source():
    source = DiagnosticImuSource.__new__(DiagnosticImuSource)
    source.samples = deque(maxlen=200)
    source.lock = threading.Lock()
    source.changed = threading.Condition(source.lock)
    source.clock_mapper = ImuClockMapper()
    source.offset = None
    return source


class ImuClockMapperTests(unittest.TestCase):
    def test_monotonic_and_equal_sample_time_maps_identically(self):
        mapper = ImuClockMapper()
        out = []
        for i in range(200):
            sample = 1000.0 + i * 0.01
            out.append(mapper.map(sample, sample + 0.001, sample + 0.003))
        self.assertEqual(out, sorted(out))
        self.assertGreater(out[-1], out[0])
        # A stalled sensor keeps sending with an equal sample time: the mapped
        # timestamp must stay frozen, never regress nor jump forward.
        frozen = mapper.map(1001.99, 1002.001, 1002.01)
        self.assertEqual(mapper.map(1001.99, 1002.002, 1002.02), frozen)

    def test_negative_drift_follows_within_window(self):
        # Driver clock runs slow: received - sent grows. The old lifetime min
        # froze at the earliest observation; the window must track it instead.
        mapper = ImuClockMapper()
        drift = -2e-4
        last = None
        max_fresh = 0.0
        for n in range(400):
            sent = n * 0.05 * (1 + drift)
            received = n * 0.05 + 0.003
            mapped = mapper.map(sent - 0.001, sent, received)
            if last is not None:
                self.assertGreaterEqual(mapped, last - 1e-9)
            max_fresh = max(max_fresh, received - mapped)
            last = mapped
        self.assertLess(max_fresh, 0.02)

    def test_positive_drift_follows_within_window(self):
        mapper = ImuClockMapper()
        drift = +2e-4
        last = None
        max_fresh = 0.0
        for n in range(400):
            sent = n * 0.05 * (1 + drift)
            received = n * 0.05 + 0.003
            mapped = mapper.map(sent - 0.001, sent, received)
            if last is not None:
                self.assertGreaterEqual(mapped, last - 1e-9)
            max_fresh = max(max_fresh, received - mapped)
            last = mapped
        self.assertLess(max_fresh, 0.02)

    def test_late_arrival_spike_does_not_poison_offset(self):
        mapper = ImuClockMapper()
        steady = None
        for n in range(200):
            sent = n * 0.05
            received = sent + 0.003
            mapped = mapper.map(sent - 0.001, sent, received)
            if n == 100:
                steady = mapped
        # A packet that arrives 60ms late is ignored by the sliding minimum;
        # the mapping must return to the same floor, not latch the spike.
        spiked = mapper.map(200 * 0.05 - 0.001, 200 * 0.05, 200 * 0.05 + 0.063)
        self.assertGreaterEqual(spiked, steady)
        recovered = mapper.map(201 * 0.05 - 0.001, 201 * 0.05, 201 * 0.05 + 0.003)
        self.assertAlmostEqual(recovered - spiked, 0.05, places=4)

    def test_reset_starts_a_fresh_origin(self):
        mapper = ImuClockMapper()
        mapper.map(10.0, 10.001, 10.003)
        mapper.map(10.01, 10.011, 10.013)
        before = mapper.last_mapped
        mapper.reset()
        self.assertIsNone(mapper.offset)
        # New origin maps close to the new receive time, not the old session.
        after = mapper.map(0.0, 0.001, 20.003)
        self.assertAlmostEqual(after, 20.002, places=3)
        self.assertGreater(after, before)

    def test_window_expiry_does_not_rejuvenate_delayed_samples(self):
        mapper = ImuClockMapper()
        original = mapper.map(0., 0., 10.)
        delayed = mapper.map(.05, .05, 10.+mapper.WINDOW_S+1.)
        self.assertLess(delayed-original, .06)
        self.assertGreater(13.-delayed, 2.9)

    def test_stagnation_past_window_and_recovery(self):
        mapper = ImuClockMapper()
        frozen = mapper.map(0., 0., 10.)
        for n in range(1, 101):
            self.assertEqual(mapper.map(0., n*.05, 10.+n*.05), frozen)
        recovered = mapper.map(5.05, 5.05, 15.05)
        self.assertAlmostEqual(recovered, 15.05)
        self.assertLessEqual(len(mapper.observations), 41)


class DriverSoakTests(unittest.TestCase):
    def test_future_rejection_is_non_mutating_and_drift_after_gap_recovers(self):
        for drift in (-2e-4, 2e-4):
            source = _driver_source()
            with source.changed:
                self.assertTrue(source._accept(imu_row("a", 1, 0, 0., 0.), 10.))
                before = source.latest()
                observations = list(source.clock_mapper.observations)
                # An isolated future outlier must not poison seq/clock/history.
                self.assertFalse(source._accept(imu_row("a", 999, 0, 1., 1.), 10.05))
                self.assertEqual(source.latest(), before)
                self.assertEqual(list(source.clock_mapper.observations), observations)
                sent = 1200.*(1+drift)
                self.assertTrue(source._accept(imu_row("a", 2, 0, sent, sent), 1210.))
                self.assertTrue(source._accept(imu_row("a", 3, 0, sent+.05, sent+.05), 1210.05))
                self.assertLess(1210.05-source.latest()["monotonic_s"], .02)

    def test_stall_retains_real_age_and_history_is_owned(self):
        source = _driver_source()
        row = imu_row("a", 1, 0, 0., 0.)
        with source.changed:
            self.assertTrue(source._accept(row, 10.))
        original = source.latest()
        row["quaternion_sensor_wxyz"][0] = 0.
        exposed = source.latest()
        exposed["quaternion_sensor_wxyz"][0] = 0.
        with source.changed:
            self.assertTrue(source._accept(imu_row("a", 2, 0, .05, .05), 13.))
        self.assertEqual(source.samples[0], original)
        self.assertFalse(source.at(13., wait_s=0)["valid"])
        with source.changed:
            self.assertTrue(source._accept(imu_row("a", 3, 0, 3.05, 3.05), 13.05))
        self.assertTrue(source.at(13.05, wait_s=0)["valid"])

    def test_session_and_epoch_boundaries(self):
        source = _driver_source()
        with source.changed:
            self.assertTrue(source._accept(imu_row("a", 1, 0, 100., 100.), 10.))
            self.assertTrue(source._accept(imu_row("a", 2, 1, 100.05, 100.05), 10.05))
            self.assertEqual(len(source.samples), 1)
            self.assertTrue(source._accept(imu_row("b", 1, 0, 0., 0.), 20.))
            self.assertEqual(len(source.samples), 1)
            self.assertAlmostEqual(source.latest()["monotonic_s"], 20.)
            self.assertFalse(source._accept(imu_row("a", 3, 1, 110., 110.), 20.1))
        self.assertIsNone(source.at(19., wait_s=0))

    def _run(self, duration_s, drift, seed=0, resets=True):
        source = _driver_source()
        dt, base_latency, lead, jitter = 0.05, 0.003, 0.001, 0.002
        rng = random.Random(seed)
        total = int(duration_s / dt)
        third = total // 3
        session, epoch, seq = "soak-a", 1, 0
        prev_mapped, max_clock_error = None, 0.0
        for n in range(total):
            if resets and n in (third, 2 * third):
                epoch += 1
                session = "soak-%c%d" % (chr(97 + epoch), epoch)
                seq = 0
            sent = n * dt * (1 + drift)
            sample = sent - lead
            latency = base_latency + rng.uniform(-jitter, jitter)
            if n % 2500 == 0 and n:
                latency += 0.05  # late arrival: the window minimum must ignore it
            received = n * dt + latency
            seq += 1
            row = imu_row(session, seq, epoch, sample, sent)
            with source.changed:
                self.assertTrue(source._accept(row, received))
            published = source.samples[-1]
            self.assertEqual(published["sample_time_s"], sample)
            self.assertEqual(published["sent_time_s"], sent)
            mapped = published["monotonic_s"]
            self.assertLessEqual(mapped, received + 1e-9)
            if prev_mapped is not None:
                self.assertGreaterEqual(mapped, prev_mapped - 1e-9)
            prev_mapped = mapped
            # Map error against the sample's true local time: immune to
            # transport spikes, so only clock drift can grow it.
            if n * dt > 2.0:
                max_clock_error = max(max_clock_error, abs(n * dt - mapped))
        return source, received, max_clock_error

    def test_driver_20min_negative_drift_jitter_reset(self):
        source, received, max_clock_error = self._run(1200.0, drift=-2e-4)
        latest = source.samples[-1]["monotonic_s"]
        self.assertLess(received - latest, 0.05)
        # Bounded mapping error is the fix: the old lifetime min would
        # accumulate ~drift*duration = 0.24 s of lag here.
        self.assertLess(max_clock_error, 0.02)

    def test_driver_positive_drift_stays_fresh(self):
        _, _, max_clock_error = self._run(60.0, drift=+2e-4)
        self.assertLess(max_clock_error, 0.02)

    def test_driver_20min_single_session_both_drift_signs(self):
        for drift in (-2e-4, 2e-4):
            with self.subTest(drift=drift):
                source, _, error = self._run(1200., drift, resets=False)
                self.assertEqual(source.session_id, "soak-a")
                self.assertFalse(source.retired_sessions)
                self.assertLess(error, .02)


class DiagnosticSoakTests(unittest.TestCase):
    def test_diagnostic_20min_drift_jitter_elapsed_reset(self):
        self._run(resets=True)

    def test_diagnostic_20min_single_session_both_drift_signs(self):
        for drift in (-2e-4, 2e-4):
            with self.subTest(drift=drift):
                self._run(resets=False, drift=drift)

    def _run(self, resets, drift=-2e-4):
        source = _diagnostic_source()
        dt, base_latency, lead, jitter = 0.05, 0.003, 0.001, 0.002
        rng = random.Random(7)
        total = int(1200.0 / dt)
        third, two_thirds = total // 3, 2 * total // 3
        elapsed, recenter, prev_mapped, max_clock_error = 0.0, 0, None, 0.0
        for n in range(total):
            if resets and n in (third, two_thirds):
                # Simulated process restart: elapsed drops back to near zero.
                elapsed = 0.0
                recenter = 0
            elapsed += dt * (1 + drift)
            received = n * dt + base_latency + rng.uniform(-jitter, jitter)
            row = {"event": "imu", "host_elapsed_s": elapsed,
                   "recenter_count": recenter, "valid": True,
                   "quaternion_wxyz": [1., 0., 0., 0.]}
            with source.changed:
                self.assertTrue(source._accept(row, received))
            mapped = source.samples[-1]["monotonic_s"]
            self.assertLessEqual(mapped, received + 1e-9)
            if prev_mapped is not None:
                self.assertGreaterEqual(mapped, prev_mapped - 1e-9)
            prev_mapped = mapped
            if n * dt > 2.0:
                max_clock_error = max(max_clock_error, abs(n * dt - mapped))
        self.assertLess(max_clock_error, 0.02)

    def test_stagnation_stall_reset_and_immutable_history(self):
        source = _diagnostic_source()
        row = dict(event="imu", host_elapsed_s=0., recenter_count=0,
                   valid=True, quaternion_wxyz=[1., 0., 0., 0.])
        with source.changed:
            self.assertTrue(source._accept(row, 10.))
        original = copy.deepcopy(source.samples[0])
        row["quaternion_wxyz"][0] = .5
        self.assertEqual(source.samples[0], original)
        row["quaternion_wxyz"] = [0., 1., 0., 0.]
        with source.changed:
            self.assertTrue(source._accept(row, 13.))
            self.assertEqual(source.samples[-1]["monotonic_s"], 10.)
            row["host_elapsed_s"] = .05
            self.assertTrue(source._accept(row, 13.05))
        stale = source.at(13.05, wait_s=0)
        self.assertFalse(stale["valid"])
        stale["quaternion_wxyz"][0] = 42.
        self.assertEqual(source.samples[0], original)
        self.assertNotEqual(source.samples[-1]["quaternion_wxyz"][0], 42.)
        with source.changed:
            row["host_elapsed_s"] = 0.
            self.assertTrue(source._accept(row, 20.))
        self.assertEqual(len(source.samples), 1)
        self.assertAlmostEqual(source.samples[0]["monotonic_s"], 20.)
        self.assertIsNone(source.at(19., wait_s=0))

    def test_diagnostic_stale_sample_is_invalidated(self):
        source = _diagnostic_source()
        with source.changed:
            source.samples.append({"event": "imu", "host_elapsed_s": 0.0,
                                   "recenter_count": 0, "valid": True,
                                   "monotonic_s": 1.0, "quaternion_wxyz": [1., 0., 0., 0.]})
        self.assertIsNone(source.at(0.5, wait_s=0))
        stale = source.at(1.2, wait_s=0)
        self.assertIsNotNone(stale)
        self.assertFalse(stale["valid"])


if __name__ == "__main__":
    unittest.main()
