# SPDX-License-Identifier: MIT
"""Integrated tracking-pipeline tests with a fake backend.

No camera, no OpenCV, no network: a fake source feeds synthetic projections
through the real estimator, filter, tracker loop and UDP sender (with a fake
socket). This exercises the frame bound, the intrinsics image-size gate, the
read-failure stop and the terminating ``valid:false`` packet end to end.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from psvr_camera import geometry as G  # noqa: E402
from psvr_camera import protocol as P  # noqa: E402
from psvr_camera.calibration import Intrinsics  # noqa: E402
from psvr_camera.estimator import (  # noqa: E402
    EstimatorConfig,
    EstimatorError,
    PositionEstimator,
    PositionFilter,
    aruco_geometry,
)
from psvr_camera.tracker import TrackConfig, run_track  # noqa: E402
from psvr_camera.udp_sender import SequenceAllocator, UdpPositionSender  # noqa: E402

K = np.array([[700.0, 0.0, 320.0], [0.0, 690.0, 240.0], [0.0, 0.0, 1.0]])
FRAME = np.zeros((480, 640, 3), dtype=np.uint8)


def calibrated():
    return Intrinsics(
        camera_matrix=K,
        distortion_coeffs=np.zeros(5),
        image_size=(640, 480),
        calibrated=True,
        rms_reprojection_px=0.3,
    )


def projected_corners(geometry, rvec=(0.1, -0.05, 0.02), translation=(0.02, -0.03, 0.5)):
    objects = np.asarray(geometry.object_points_m)
    return G.project_points(K, G.rodrigues(rvec), translation, objects)


class FakeSource:
    def __init__(self, frames):
        self._frames = list(frames)
        self._index = 0
        self.released = False

    def read(self):
        if self._index < len(self._frames):
            frame = self._frames[self._index]
            self._index += 1
            return True, frame
        return False, None

    def release(self):
        self.released = True


class FakeDetector:
    def __init__(self, corners):
        self.corners = corners
        self.calls = 0

    def detect(self, frame):
        self.calls += 1
        return {} if self.corners is None else {0: self.corners}


class _FakeSocket:
    def __init__(self):
        self.sent = []
        self.closed = False

    def settimeout(self, value):
        pass

    def sendto(self, data, address):
        self.sent.append(data)
        return len(data)

    def close(self):
        self.closed = True


def build(config, frames, corners, **kwargs):
    geometry = kwargs.pop("geometry", aruco_geometry(0.10))
    source = FakeSource(frames)
    detector = FakeDetector(corners)
    estimator = PositionEstimator(kwargs.pop("intrinsics", calibrated()), geometry)
    fake_socket = _FakeSocket()
    sender = UdpPositionSender("127.0.0.1", 4271, socket_factory=lambda: fake_socket)
    sequences = SequenceAllocator()
    position_filter = PositionFilter(smoothing_time_s=0.0)
    summary = run_track(
        source, detector, estimator, sender, sequences, position_filter, config,
        **kwargs,
    )
    sender.close()
    packets = [P.decode_packet(data) for data in fake_socket.sent]
    return summary, packets, source, fake_socket


class PipelineTest(unittest.TestCase):
    def test_valid_frames_then_final_invalid_packet(self):
        geometry = aruco_geometry(0.10)
        corners = projected_corners(geometry)
        config = TrackConfig(frames=5, expected_size=(640, 480), source_name="aruco")
        summary, packets, source, _ = build(config, [FRAME] * 5, corners, geometry=geometry)
        self.assertEqual(summary.captured_frames, 5)
        self.assertEqual(summary.valid_frames, 5)
        self.assertEqual(summary.exit_reason, "frames")
        # Five valid samples plus the terminating invalid one.
        self.assertEqual(len(packets), 6)
        self.assertTrue(all(packet["valid"] for packet in packets[:5]))
        self.assertFalse(packets[-1]["valid"])
        self.assertEqual([p["seq"] for p in packets], list(range(6)))
        self.assertEqual(packets[-1]["note"], "frames")
        for packet in packets[:5]:
            self.assertEqual(packet["source"], "aruco")
            np.testing.assert_allclose(
                packet["position_m"], [0.02, 0.03, -0.5], atol=1e-6
            )

    def test_frames_bound_counts_captured_not_valid(self):
        # The marker is never found, but --frames must still terminate the run.
        config = TrackConfig(frames=4, expected_size=(640, 480))
        summary, packets, _, _ = build(config, [FRAME] * 50, None)
        self.assertEqual(summary.captured_frames, 4)
        self.assertEqual(summary.valid_frames, 0)
        self.assertEqual(summary.exit_reason, "frames")
        # Four invalid live samples plus the terminating one.
        self.assertEqual(len(packets), 5)
        self.assertTrue(all(not packet["valid"] for packet in packets))

    def test_wrong_frame_size_fails_closed_before_estimate(self):
        corners = projected_corners(aruco_geometry(0.10))
        config = TrackConfig(frames=10, expected_size=(320, 240))
        summary, packets, _, _ = build(config, [FRAME] * 5, corners)
        self.assertEqual(summary.valid_frames, 0)
        self.assertEqual(summary.exit_reason, "resolution_mismatch")
        self.assertIn("does not match", summary.error)
        # Only the terminating invalid packet is emitted: no wrong-metric pose.
        self.assertEqual(len(packets), 1)
        self.assertFalse(packets[0]["valid"])

    def test_repeated_read_failures_stop_without_spinning(self):
        config = TrackConfig(frames=0, max_seconds=0.0, max_read_failures=3)
        summary, packets, _, _ = build(config, [], None)
        self.assertEqual(summary.read_failures, 3)
        self.assertEqual(summary.exit_reason, "read_failure")
        self.assertEqual(len(packets), 1)
        self.assertFalse(packets[0]["valid"])

    def test_max_seconds_bound(self):
        clock = {"t": 0.0}

        def now_fn():
            clock["t"] += 0.5
            return clock["t"]

        corners = projected_corners(aruco_geometry(0.10))
        config = TrackConfig(frames=0, max_seconds=2.0, expected_size=(640, 480))
        geometry = aruco_geometry(0.10)
        source = FakeSource([FRAME] * 50)
        detector = FakeDetector(corners)
        estimator = PositionEstimator(calibrated(), geometry)
        sequences = SequenceAllocator()
        position_filter = PositionFilter(smoothing_time_s=0.0)
        summary = run_track(
            source, detector, estimator, None, sequences, position_filter, config,
            now_fn=now_fn,
        )
        self.assertEqual(summary.exit_reason, "max_seconds")
        self.assertGreaterEqual(summary.captured_frames, 2)
        self.assertLess(summary.captured_frames, 50)

    def test_final_invalid_clears_filter(self):
        from psvr_camera.tracker import final_invalid_sample

        position_filter = PositionFilter(smoothing_time_s=0.0)
        position_filter.update([1.0, 2.0, 3.0], 0.0)
        sequences = SequenceAllocator()
        sample = final_invalid_sample(sequences, position_filter, 1.0, note="exit")
        self.assertFalse(sample.valid)
        self.assertIsNone(position_filter.value)
        P.validate_packet(sample.to_packet())


class CalibrationRmsGateTest(unittest.TestCase):
    def test_high_rms_intrinsics_are_refused(self):
        bad = Intrinsics(
            camera_matrix=K,
            distortion_coeffs=np.zeros(5),
            image_size=(640, 480),
            calibrated=True,
            rms_reprojection_px=5.0,
        )
        with self.assertRaises(EstimatorError):
            PositionEstimator(bad, aruco_geometry(0.1))
        # An explicit override can still run for diagnostics.
        estimator = PositionEstimator(
            bad, aruco_geometry(0.1),
            EstimatorConfig(max_calibration_rms_px=None),
        )
        self.assertIsNotNone(estimator)


if __name__ == "__main__":
    unittest.main()
