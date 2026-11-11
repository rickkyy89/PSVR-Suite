# SPDX-License-Identifier: MIT
"""Calibration math and strict intrinsics I/O tests (no OpenCV required)."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from psvr_camera import geometry as G  # noqa: E402
from psvr_camera.calibration import (  # noqa: E402
    CalibrationError,
    ChessboardSpec,
    intrinsics_from_dict,
    load_intrinsics,
    save_intrinsics,
    solve_intrinsics,
    zhang_intrinsics,
)

K = np.array([[700.0, 0.0, 320.0], [0.0, 690.0, 240.0], [0.0, 0.0, 1.0]])
SPEC = ChessboardSpec(9, 6, 0.025)


def synthetic_views(camera=K):
    objects = SPEC.object_points()
    poses = [
        ([0.3, 0.2, 0.05], [-0.1, -0.05, 0.5]),
        ([-0.35, 0.25, 0.1], [0.12, -0.08, 0.6]),
        ([0.25, -0.3, 0.15], [0.05, 0.1, 0.55]),
        ([0.4, 0.15, -0.1], [-0.05, -0.1, 0.45]),
    ]
    views = []
    for rvec, translation in poses:
        views.append(
            G.project_points(camera, G.rodrigues(rvec), translation, objects)
        )
    return objects, views


def valid_dict():
    return {
        "schema": "psvr.camera.intrinsics",
        "version": 1,
        "created_utc": "2026-10-05T00:00:00+00:00",
        "image_size": [640, 480],
        "camera_matrix": [[700.0, 0.0, 320.0], [0.0, 690.0, 240.0], [0.0, 0.0, 1.0]],
        "distortion_model": "plumb_bob",
        "distortion_coeffs": [0.01, -0.002, 0.0001, 0.0002, 0.0],
        "calibrated": True,
        "calibration": {"rms_reprojection_px": 0.35, "views": 12, "chessboard": SPEC.to_dict()},
        "provenance": {"source": "chessboard_opencv", "notes": "unit", "metric": True},
    }


class ChessboardSpecTest(unittest.TestCase):
    def test_object_points_geometry(self):
        spec = ChessboardSpec(3, 2, 0.04)
        points = spec.object_points()
        self.assertEqual(points.shape, (6, 3))
        np.testing.assert_allclose(points[0], [0, 0, 0])
        np.testing.assert_allclose(points[1], [0.04, 0, 0])
        np.testing.assert_allclose(points[3], [0, 0.04, 0])
        self.assertEqual(spec.corner_count, 6)

    def test_invalid_spec(self):
        for args in ((1, 5, 0.02), (5, 1, 0.02), (5, 5, 0.0), (5, 5, -1.0)):
            with self.assertRaises(CalibrationError):
                ChessboardSpec(*args)


class ZhangTest(unittest.TestCase):
    def test_recovers_exact_intrinsics(self):
        objects, views = synthetic_views()
        homographies = [G.homography_dlt(objects[:, :2], v) for v in views]
        recovered = zhang_intrinsics(homographies)
        np.testing.assert_allclose(recovered, K, atol=1e-6)

    def test_needs_three_views(self):
        with self.assertRaises(CalibrationError):
            zhang_intrinsics([np.eye(3), np.eye(3)])

    def test_solve_intrinsics_offline(self):
        _, views = synthetic_views()
        intrinsics = solve_intrinsics(views, (640, 480), SPEC, prefer_opencv=False)
        self.assertTrue(intrinsics.calibrated)
        self.assertEqual(intrinsics.source, "chessboard_zhang")
        self.assertEqual(intrinsics.views, 4)
        np.testing.assert_allclose(intrinsics.camera_matrix, K, atol=1e-6)
        self.assertLess(intrinsics.rms_reprojection_px, 1e-6)

    def test_solve_intrinsics_requires_three_views(self):
        objects, views = synthetic_views()
        with self.assertRaises(CalibrationError):
            solve_intrinsics(views[:2], (640, 480), SPEC, prefer_opencv=False)

    def test_solve_intrinsics_rejects_wrong_point_count(self):
        _, views = synthetic_views()
        views[0] = views[0][:-1]
        with self.assertRaises(CalibrationError):
            solve_intrinsics(views, (640, 480), SPEC, prefer_opencv=False)

    def test_max_rms_gate_fails_closed(self):
        _, views = synthetic_views()
        with self.assertRaises(CalibrationError):
            solve_intrinsics(
                views, (640, 480), SPEC, prefer_opencv=False, max_rms_px=1e-15
            )
        solved = solve_intrinsics(
            views, (640, 480), SPEC, prefer_opencv=False, max_rms_px=1.0
        )
        self.assertTrue(solved.calibrated)


class StrictLoadTest(unittest.TestCase):
    def test_round_trip(self):
        intrinsics = intrinsics_from_dict(valid_dict())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "intrinsics.json"
            save_intrinsics(path, intrinsics)
            loaded = load_intrinsics(path)
        np.testing.assert_allclose(loaded.camera_matrix, intrinsics.camera_matrix)
        np.testing.assert_allclose(loaded.distortion_coeffs, intrinsics.distortion_coeffs)
        self.assertTrue(loaded.calibrated)
        self.assertEqual(loaded.image_size, (640, 480))

    def test_image_size_mismatch_rejected(self):
        with self.assertRaises(CalibrationError):
            intrinsics_from_dict(valid_dict(), expected_image_size=(1280, 720))

    def test_wrong_schema_rejected(self):
        data = valid_dict()
        data["schema"] = "psvr.camera.other"
        with self.assertRaises(CalibrationError):
            intrinsics_from_dict(data)

    def test_wrong_version_rejected(self):
        data = valid_dict()
        data["version"] = 2
        with self.assertRaises(CalibrationError):
            intrinsics_from_dict(data)

    def test_unknown_key_rejected_in_strict_mode(self):
        data = valid_dict()
        data["surprise"] = 1
        with self.assertRaises(CalibrationError):
            intrinsics_from_dict(data)
        # Lenient mode tolerates unknown keys but still validates the known ones.
        self.assertTrue(intrinsics_from_dict(data, strict=False).calibrated)

    def test_non_finite_rejected(self):
        data = valid_dict()
        data["camera_matrix"][0][0] = float("nan")
        with self.assertRaises(CalibrationError):
            intrinsics_from_dict(data)

    def test_bad_camera_shape_rejected(self):
        data = valid_dict()
        data["camera_matrix"] = [[1, 0], [0, 1]]
        with self.assertRaises(CalibrationError):
            intrinsics_from_dict(data)

    def test_negative_focal_rejected(self):
        data = valid_dict()
        data["camera_matrix"][0][0] = -1.0
        with self.assertRaises(CalibrationError):
            intrinsics_from_dict(data)

    def test_principal_point_outside_image_rejected(self):
        data = valid_dict()
        data["camera_matrix"][0][2] = 5000.0
        with self.assertRaises(CalibrationError):
            intrinsics_from_dict(data)

    def test_unknown_distortion_model_rejected(self):
        data = valid_dict()
        data["distortion_model"] = "fisheye"
        with self.assertRaises(CalibrationError):
            intrinsics_from_dict(data)

    def test_wrong_distortion_length_rejected(self):
        data = valid_dict()
        data["distortion_coeffs"] = [0.1, 0.2]
        with self.assertRaises(CalibrationError):
            intrinsics_from_dict(data)

    def test_non_bool_calibrated_rejected(self):
        data = valid_dict()
        data["calibrated"] = "yes"
        with self.assertRaises(CalibrationError):
            intrinsics_from_dict(data)

    def test_missing_file_rejected(self):
        with self.assertRaises(CalibrationError):
            load_intrinsics(Path(tempfile.gettempdir()) / "does-not-exist-psvr.json")

    def test_malformed_json_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(CalibrationError):
                load_intrinsics(path)


if __name__ == "__main__":
    unittest.main()
