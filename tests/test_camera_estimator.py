# SPDX-License-Identifier: MIT
"""Estimator tests: metric pose, rejection, ambiguity, missing data, filtering."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from psvr_camera import geometry as G  # noqa: E402
from psvr_camera.calibration import Intrinsics  # noqa: E402
from psvr_camera.estimator import (  # noqa: E402
    EstimatorConfig,
    EstimatorError,
    MarkerGeometry,
    PositionEstimator,
    PositionFilter,
    aruco_geometry,
    load_marker_geometry,
    optical_to_world,
)

K = np.array([[620.0, 0.0, 320.0], [0.0, 615.0, 240.0], [0.0, 0.0, 1.0]])


def calibrated():
    return Intrinsics(
        camera_matrix=K,
        distortion_coeffs=np.zeros(5),
        image_size=(640, 480),
        calibrated=True,
    )


def planar_grid(n=3, spacing=0.03):
    points = [(i * spacing, j * spacing, 0.0) for i in range(n) for j in range(n)]
    return MarkerGeometry(
        kind="blob_led",
        provenance="user_measured",
        object_points_m=tuple(points),
        description="synthetic planar grid",
    )


def blob_rig_geometry():
    points = (
        (0.0, 0.0, 0.0),
        (0.10, 0.0, 0.0),
        (0.0, 0.10, 0.0),
        (0.0, 0.0, 0.10),
        (0.10, 0.10, 0.05),
        (-0.05, 0.03, 0.02),
        (0.04, -0.06, 0.08),
        (0.07, 0.02, -0.03),
    )
    return MarkerGeometry(
        kind="blob_led",
        provenance="user_measured",
        object_points_m=points,
        description="synthetic non-planar rig",
    )


class GeometryPolicyTest(unittest.TestCase):
    def test_builtin_led_geometry_is_refused(self):
        with self.assertRaises(EstimatorError) as ctx:
            MarkerGeometry(
                kind="aruco",
                provenance="psvr_builtin",
                object_points_m=((-0.05, 0.05, 0), (0.05, 0.05, 0), (0.05, -0.05, 0), (-0.05, -0.05, 0)),
                marker_length_m=0.1,
            )
        self.assertIn("not calibrated", str(ctx.exception))

    def test_aruco_requires_measured_length(self):
        with self.assertRaises(EstimatorError):
            MarkerGeometry(kind="aruco", provenance="user_measured", object_points_m=((0, 0, 0),) * 4)

    def test_uncalibrated_intrinsics_refused_by_default(self):
        uncalibrated = Intrinsics(K, np.zeros(5), (640, 480), calibrated=False)
        with self.assertRaises(EstimatorError):
            PositionEstimator(uncalibrated, aruco_geometry(0.1))
        # Diagnostics may opt in explicitly, but no metric claim is made.
        estimator = PositionEstimator(
            uncalibrated, aruco_geometry(0.1), EstimatorConfig(require_calibrated=False)
        )
        self.assertIsNotNone(estimator)

    def test_geometry_json_round_trip(self):
        geometry = aruco_geometry(0.08, dictionary="DICT_4X4_50", marker_id=3)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "geometry.json"
            path.write_text(json.dumps(geometry.to_dict()), encoding="utf-8")
            loaded = load_marker_geometry(path)
        self.assertEqual(loaded.kind, "aruco")
        self.assertEqual(loaded.marker_length_m, 0.08)
        self.assertEqual(loaded.aruco["id"], 3)

    def test_config_rejects_bad_limits(self):
        for kwargs in (
            {"max_reprojection_px": 0},
            {"ambiguity_ratio_threshold": 1.5},
            {"ransac_threshold_px": 0},
            {"ransac_iterations": 0},
            {"min_depth_conditioning": -1},
            {"max_calibration_rms_px": 0},
        ):
            with self.assertRaises(EstimatorError):
                EstimatorConfig(**kwargs)

    def test_high_rms_calibration_is_refused(self):
        bad = Intrinsics(
            K, np.zeros(5), (640, 480), calibrated=True, rms_reprojection_px=3.0
        )
        with self.assertRaises(EstimatorError):
            PositionEstimator(bad, aruco_geometry(0.1))
        allowed = PositionEstimator(
            bad, aruco_geometry(0.1),
            EstimatorConfig(max_calibration_rms_px=None),
        )
        self.assertIsNotNone(allowed)


class MetricPoseTest(unittest.TestCase):
    def test_aruco_position_is_metric_in_driver_frame(self):
        geometry = aruco_geometry(0.10)
        objects = np.asarray(geometry.object_points_m)
        rotation = G.rodrigues([0.1, -0.05, 0.02])
        translation = np.array([0.02, -0.03, 0.5])
        image = G.project_points(K, rotation, translation, objects)
        result = PositionEstimator(calibrated(), geometry).estimate(image)
        self.assertTrue(result.valid, result.reason)
        self.assertEqual(result.state, "tracking")
        np.testing.assert_allclose(result.position_world_m, optical_to_world(translation), atol=1e-6)
        self.assertLess(result.reprojection_px, 1e-6)
        self.assertEqual(result.inliers, 4)
        norm = float(np.linalg.norm(result.orientation_world_wxyz))
        self.assertAlmostEqual(norm, 1.0, places=9)

    def test_nonplanar_rig_position_is_metric(self):
        geometry = blob_rig_geometry()
        objects = np.asarray(geometry.object_points_m)
        rotation = G.rodrigues([0.2, -0.1, 0.05])
        translation = np.array([0.05, 0.02, 0.6])
        image = G.project_points(K, rotation, translation, objects)
        result = PositionEstimator(calibrated(), geometry).estimate(image)
        self.assertTrue(result.valid, result.reason)
        np.testing.assert_allclose(result.position_world_m, optical_to_world(translation), atol=1e-5)


class RejectionTest(unittest.TestCase):
    def test_planar_outlier_is_rejected(self):
        geometry = planar_grid(3)
        objects = np.asarray(geometry.object_points_m)
        rotation = G.rodrigues([0.15, 0.1, 0.0])
        translation = np.array([0.01, -0.02, 0.45])
        image = G.project_points(K, rotation, translation, objects)
        image[4] += np.array([80.0, 60.0])
        result = PositionEstimator(calibrated(), geometry).estimate(image)
        self.assertTrue(result.valid, result.reason)
        self.assertEqual(result.inliers, objects.shape[0] - 1)
        np.testing.assert_allclose(result.position_world_m, optical_to_world(translation), atol=1e-3)

    def test_nonplanar_outlier_is_rejected(self):
        geometry = blob_rig_geometry()
        objects = np.asarray(geometry.object_points_m)
        rotation = G.rodrigues([0.15, 0.1, -0.05])
        translation = np.array([0.02, -0.01, 0.55])
        image = G.project_points(K, rotation, translation, objects)
        image[5] += np.array([70.0, -55.0])
        result = PositionEstimator(calibrated(), geometry).estimate(image)
        self.assertTrue(result.valid, result.reason)
        self.assertEqual(result.inliers, objects.shape[0] - 1)

    def test_reprojection_limit_rejects_bad_pose(self):
        geometry = aruco_geometry(0.10)
        objects = np.asarray(geometry.object_points_m)
        translation = np.array([0.0, 0.0, 0.5])
        image = G.project_points(K, np.eye(3), translation, objects)
        image[0] += np.array([25.0, 25.0])
        result = PositionEstimator(calibrated(), geometry).estimate(image)
        self.assertFalse(result.valid)
        self.assertIn(result.state, {"lost", "ambiguous"})

    def test_ambiguous_planar_pose_is_not_emitted(self):
        # A marker plane crossing the camera centre puts points behind the lens.
        geometry = aruco_geometry(0.10)
        objects = np.asarray(geometry.object_points_m)
        rotation = G.rodrigues([0.0, np.pi / 2.0, 0.0])
        translation = np.array([0.0, 0.0, 0.02])
        image = G.project_points(K, rotation, translation, objects)
        result = PositionEstimator(calibrated(), geometry).estimate(image)
        self.assertFalse(result.valid)
        self.assertEqual(result.state, "ambiguous")


class MissingDataTest(unittest.TestCase):
    def test_point_count_mismatch_is_error(self):
        geometry = blob_rig_geometry()
        result = PositionEstimator(calibrated(), geometry).estimate(np.zeros((3, 2)))
        self.assertFalse(result.valid)
        self.assertEqual(result.state, "error")

    def test_too_few_marker_points_are_refused_at_construction(self):
        with self.assertRaises(EstimatorError):
            MarkerGeometry(
                kind="blob_led",
                provenance="user_measured",
                object_points_m=((0, 0, 0), (0.1, 0, 0), (0, 0.1, 0)),
            )

    def test_non_finite_points_are_error(self):
        geometry = aruco_geometry(0.1)
        image = np.zeros((4, 2))
        image[1, 0] = float("nan")
        result = PositionEstimator(calibrated(), geometry).estimate(image)
        self.assertFalse(result.valid)
        self.assertEqual(result.state, "error")


class FilterTest(unittest.TestCase):
    def test_time_based_smoothing_converges(self):
        filt = PositionFilter(smoothing_time_s=0.1)
        first = filt.update([0.0, 0.0, 0.0], 0.0)
        np.testing.assert_allclose(first, [0, 0, 0])
        early = filt.update([1.0, 0.0, 0.0], 0.05)
        self.assertGreater(early[0], 0.0)
        self.assertLess(early[0], 1.0)
        late = filt.update([1.0, 0.0, 0.0], 0.5)
        self.assertGreater(late[0], 0.99)

    def test_deadzone_suppresses_tiny_motion(self):
        filt = PositionFilter(smoothing_time_s=0.0, deadzone_m=0.01)
        filt.update([0.0, 0.0, 0.0], 0.0)
        held = filt.update([0.005, 0.0, 0.0], 0.1)
        np.testing.assert_allclose(held, [0.0, 0.0, 0.0])
        moved = filt.update([0.5, 0.0, 0.0], 0.2)
        self.assertAlmostEqual(moved[0], 0.5)

    def test_stale_reset(self):
        filt = PositionFilter(stale_timeout_s=0.2)
        filt.update([1.0, 2.0, 3.0], 0.0)
        self.assertTrue(filt.mark_missing(0.1))
        self.assertFalse(filt.mark_missing(0.5))
        filt.reset()
        self.assertIsNone(filt.value)


if __name__ == "__main__":
    unittest.main()
