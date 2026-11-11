# SPDX-License-Identifier: MIT
"""Synthetic projection tests for the pure-NumPy geometry core.

No camera and no OpenCV: known intrinsics/pose are projected and recovered, so
the estimator rests on verified math rather than a device.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from psvr_camera import geometry as G  # noqa: E402

K = np.array([[600.0, 0.0, 320.0], [0.0, 610.0, 240.0], [0.0, 0.0, 1.0]])


def square_marker(half: float = 0.05):
    return np.array(
        [[-half, half, 0.0], [half, half, 0.0], [half, -half, 0.0], [-half, -half, 0.0]],
        dtype=float,
    )


def blob_rig():
    return np.array(
        [
            [0.0, 0.0, 0.0],
            [0.10, 0.0, 0.0],
            [0.0, 0.10, 0.0],
            [0.0, 0.0, 0.10],
            [0.10, 0.10, 0.05],
            [-0.05, 0.03, 0.02],
            [0.04, -0.06, 0.08],
            [0.07, 0.02, -0.03],
        ],
        dtype=float,
    )


class RodriguesTest(unittest.TestCase):
    def test_round_trip(self):
        for rvec in ([0.0, 0.0, 0.0], [0.1, -0.2, 0.3], [1.2, 0.5, -0.7]):
            rotation = G.rodrigues(rvec)
            recovered = G.rotation_to_rodrigues(rotation)
            np.testing.assert_allclose(G.rodrigues(recovered), rotation, atol=1e-9)

    def test_orthonormalize_handles_reflection(self):
        raw = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, -1.0]])
        rotation = G.orthonormalize_rotation(raw)
        self.assertAlmostEqual(float(np.linalg.det(rotation)), 1.0, places=12)


class PlanarPoseTest(unittest.TestCase):
    def test_recovers_known_pose(self):
        for translation, rvec in (
            (np.array([0.02, -0.03, 0.5]), np.array([0.1, -0.05, 0.02])),
            (np.array([0.1, 0.05, 0.8]), np.array([0.3, 0.1, -0.2])),
        ):
            objects = square_marker()
            rotation = G.rodrigues(rvec)
            image = G.project_points(K, rotation, translation, objects)
            candidates, inliers = G.planar_pose(objects, image, K, threshold_px=1.0)
            self.assertEqual(int(inliers.sum()), 4)
            self.assertEqual(len(candidates), 2)
            best = candidates[0]
            np.testing.assert_allclose(best.translation, translation, atol=1e-6)
            np.testing.assert_allclose(best.rotation, rotation, atol=1e-6)
            self.assertEqual(best.positive_depth_ratio, 1.0)
            # The two-fold mirror lands behind the camera.
            self.assertEqual(candidates[1].positive_depth_ratio, 0.0)

    def test_planar_pose_rejects_nonplanar_object(self):
        objects = square_marker()
        objects[0, 2] = 0.01
        with self.assertRaises(G.GeometryError):
            G.planar_pose(objects, G.project_points(K, np.eye(3), [0, 0, 0.5], objects), K)

    def test_homography_maps_model_to_image(self):
        objects = square_marker()
        image = G.project_points(K, G.rodrigues([0.1, 0.2, 0.0]), [0.0, 0.0, 0.6], objects)
        homography = G.homography_dlt(objects[:, :2], image)
        model = np.column_stack([objects[:, 0], objects[:, 1], np.ones(4)])
        mapped = (homography @ model.T).T
        mapped = mapped[:, :2] / mapped[:, 2:3]
        np.testing.assert_allclose(mapped, image, atol=1e-9)


class DltPnpTest(unittest.TestCase):
    def test_recovers_known_pose(self):
        objects = blob_rig()
        rotation = G.rodrigues([0.2, -0.1, 0.05])
        translation = np.array([0.05, 0.02, 0.6])
        image = G.project_points(K, rotation, translation, objects)
        candidate = G.dlt_pnp(objects, image, K)
        np.testing.assert_allclose(candidate.translation, translation, atol=1e-6)
        np.testing.assert_allclose(candidate.rotation, rotation, atol=1e-6)

    def test_ransac_rejects_one_outlier(self):
        objects = blob_rig()
        rotation = G.rodrigues([0.15, 0.1, -0.05])
        translation = np.array([0.02, -0.01, 0.55])
        image = G.project_points(K, rotation, translation, objects)
        image[3] += np.array([60.0, -40.0])
        candidate, inliers = G.ransac_pnp(objects, image, K, threshold_px=1.0, seed=7)
        self.assertFalse(bool(inliers[3]))
        self.assertEqual(int(inliers.sum()), objects.shape[0] - 1)
        np.testing.assert_allclose(candidate.translation, translation, atol=1e-5)


class RejectionTest(unittest.TestCase):
    def test_reprojection_error_and_rms(self):
        objects = square_marker()
        rotation = G.rodrigues([0.0, 0.0, 0.0])
        translation = np.array([0.0, 0.0, 0.5])
        image = G.project_points(K, rotation, translation, objects)
        image[0] += np.array([3.0, 4.0])
        errors = G.reprojection_errors(K, rotation, translation, objects, image)
        self.assertAlmostEqual(float(errors[0]), 5.0, places=6)
        self.assertGreater(G.rms_reprojection(K, rotation, translation, objects, image), 0.0)

    def test_missing_data_raises(self):
        with self.assertRaises(G.InsufficientCorrespondences):
            G.homography_dlt([[0, 0], [1, 0], [0, 1]], [[0, 0], [1, 0], [0, 1]])
        with self.assertRaises(G.InsufficientCorrespondences):
            G.dlt_pnp([[0, 0, 0]] * 5, [[0, 0]] * 5, K)

    def test_non_finite_rejected(self):
        with self.assertRaises(G.GeometryError):
            G.project_points(K, np.eye(3), [0, 0, 0.5], [[0, 0, float("nan")]])

    def test_point_on_camera_plane_rejected(self):
        with self.assertRaises(G.GeometryError):
            G.project_points(K, np.eye(3), [0, 0, 0.0], [[0, 0, 0]])

    def test_degenerate_homography_rejected(self):
        collinear = [[0.0, 0.0], [1.0, 1.0], [2.0, 2.0], [3.0, 3.0]]
        with self.assertRaises(G.GeometryError):
            G.homography_dlt(collinear, collinear)
        duplicate = [[0.0, 0.0]] * 4
        with self.assertRaises(G.GeometryError):
            G.homography_dlt(duplicate, duplicate)

    def test_depth_conditioning_separates_planar_and_volumetric(self):
        planar = square_marker()
        self.assertLess(G.depth_conditioning(planar), 1e-6)
        self.assertGreater(G.depth_conditioning(blob_rig()), 0.02)


class UndistortTest(unittest.TestCase):
    def test_zero_distortion_is_identity(self):
        points = [[100.0, 120.0], [400.0, 300.0]]
        np.testing.assert_allclose(
            G.undistort_points(K, [0, 0, 0, 0, 0], points), points, atol=1e-9
        )

    def test_distortion_then_undistortion_round_trip(self):
        # Build distorted pixels by applying the forward model, then invert it.
        camera = K
        k1, k2, p1, p2 = 0.12, -0.03, 0.001, -0.002
        ideal = np.array([[100.0, 100.0], [500.0, 380.0], [320.0, 240.0]])
        homogeneous = np.column_stack([ideal, np.ones(len(ideal))])
        normalized = (np.linalg.inv(camera) @ homogeneous.T).T
        x, y = normalized[:, 0], normalized[:, 1]
        r2 = x * x + y * y
        radial = 1.0 + k1 * r2 + k2 * r2 * r2
        xd = x * radial + 2 * p1 * x * y + p2 * (r2 + 2 * x * x)
        yd = y * radial + p1 * (r2 + 2 * y * y) + 2 * p2 * x * y
        distorted = (camera @ np.column_stack([xd, yd, np.ones_like(xd)]).T).T[:, :2]
        recovered = G.undistort_points(camera, [k1, k2, p1, p2, 0.0], distorted)
        np.testing.assert_allclose(recovered, ideal, atol=1e-4)


if __name__ == "__main__":
    unittest.main()
