# SPDX-License-Identifier: MIT
"""B5-01/B5-02/B5-03 regression tests (pure NumPy + mocks, no hardware)."""
from __future__ import annotations

import sys
from pathlib import Path
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from psvr_camera import geometry as G  # noqa: E402
from psvr_camera.calibration import (  # noqa: E402
    CalibrationError,
    ChessboardSpec,
    calibrate_from_images,
    intrinsics_from_dict,
    load_intrinsics,
    solve_intrinsics,
)

K = np.array([[700.0, 0.0, 320.0], [0.0, 690.0, 240.0], [0.0, 0.0, 1.0]])
SPEC = ChessboardSpec(9, 6, 0.025)
POSES = [
    ([0.3, 0.2, 0.05], [-0.1, -0.05, 0.5]),
    ([-0.35, 0.25, 0.1], [0.12, -0.08, 0.6]),
    ([0.25, -0.3, 0.15], [0.05, 0.1, 0.55]),
    ([0.4, 0.15, -0.1], [-0.05, -0.1, 0.45]),
]


def make_views(camera=K):
    objects = SPEC.object_points()
    return objects, [
        G.project_points(camera, G.rodrigues(rvec), t, objects)
        for rvec, t in POSES
    ]


def free_homography_rms(objects, views):
    errors = []
    for points in views:
        homography = G.homography_dlt(objects[:, :2], points)
        model = np.column_stack(
            [objects[:, 0], objects[:, 1], np.ones(len(objects))]
        )
        mapped = (homography @ model.T).T
        mapped = mapped[:, :2] / mapped[:, 2:3]
        errors.append(np.linalg.norm(mapped - points, axis=1))
    flat = np.concatenate(errors)
    return float(np.sqrt(float(np.mean(flat * flat))))


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
        "calibration": {"rms_reprojection_px": 0.35, "views": 12,
                        "chessboard": SPEC.to_dict()},
        "provenance": {"source": "chessboard_opencv", "notes": "unit",
                       "metric": True},
    }


class CoherentRmsTest(unittest.TestCase):
    def test_exact_synthetic_stays_exact(self):
        objects, views = make_views()
        solved = solve_intrinsics(views, (640, 480), SPEC, prefer_opencv=False)
        np.testing.assert_allclose(solved.camera_matrix, K, atol=1e-6)
        self.assertLess(solved.rms_reprojection_px, 1e-6)

    def test_incoherent_views_fail_closed(self):
        # Two views from a camera with +5% fx, two from K: no single K fits.
        objects = SPEC.object_points()
        alt = K.copy()
        alt[0, 0] *= 1.05
        views = []
        for index, (rvec, trans) in enumerate(POSES):
            cam = alt if index < 2 else K
            views.append(
                G.project_points(cam, G.rodrigues(rvec), trans, objects)
            )
        # Free homographies fit each planar view almost exactly (optimistic).
        self.assertLess(free_homography_rms(objects, views), 1e-6)
        # The shared-K coherent score exposes the compromise fit.
        solved = solve_intrinsics(views, (640, 480), SPEC, prefer_opencv=False)
        self.assertGreater(solved.rms_reprojection_px, 0.1)
        with self.assertRaises(CalibrationError):
            solve_intrinsics(
                views, (640, 480), SPEC, prefer_opencv=False, max_rms_px=0.01
            )

    def test_opencv_bad_output_rejected(self):
        _, views = make_views()

        class BadCalibrate:
            def calibrateCamera(self, *args, **kwargs):
                bad = np.full((3, 3), np.nan)
                return 0.0, bad, np.zeros(5), None, None

        with self.assertRaises(CalibrationError):
            solve_intrinsics(views, (640, 480), SPEC, prefer_opencv=True,
                             module=BadCalibrate())

    def test_opencv_nonfinite_rms_rejected(self):
        _, views = make_views()

        class BadRms:
            def calibrateCamera(self, *args, **kwargs):
                return float("nan"), K.copy(), np.zeros(5), None, None

        with self.assertRaises(CalibrationError):
            solve_intrinsics(views, (640, 480), SPEC, prefer_opencv=True,
                             module=BadRms())


class OfflineCollectorSizeTest(unittest.TestCase):
    def _views(self):
        _, views = make_views()
        return views

    def test_first_image_without_board_does_not_define_size(self):
        import psvr_camera.calibration as C

        views = self._views()
        ordered = ["a.png", "b.png", "c.png", "d.png", "e.png"]

        class FakeCv2:
            IMREAD_GRAYSCALE = 0

            def imread(self, path, _flag):
                if str(path) == "a.png":
                    return np.zeros((480, 640), dtype=np.uint8)
                return np.zeros((240, 320), dtype=np.uint8)

        payload = list(views[:4])

        def fake_detect(image, spec, module=None):
            if image.shape == (480, 640):
                return False, None
            return True, payload.pop(0)

        orig_detect = C.detect_chessboard
        orig_import = C.import_cv2
        C.detect_chessboard = fake_detect  # type: ignore
        C.import_cv2 = lambda module=None: FakeCv2()  # type: ignore
        try:
            intr = calibrate_from_images(
                ordered, SPEC, prefer_opencv=False, module=FakeCv2()
            )
        finally:
            C.detect_chessboard = orig_detect
            C.import_cv2 = orig_import
        self.assertEqual(intr.image_size, (320, 240))
        self.assertEqual(intr.views, 4)

    def test_mismatched_useful_sizes_fail_closed(self):
        import psvr_camera.calibration as C

        views = self._views()
        ordered = ["a.png", "b.png", "c.png"]
        sizes = [(240, 320), (240, 320), (480, 640)]

        class FakeCv2:
            IMREAD_GRAYSCALE = 0

            def imread(self, path, _flag):
                size = sizes[ordered.index(str(path))]
                return np.zeros((size[0], size[1]), dtype=np.uint8)

        fake = FakeCv2()
        payload = list(views[:3])

        def fake_detect(image, spec, module=None):
            return True, payload.pop(0)

        orig_detect = C.detect_chessboard
        orig_import = C.import_cv2
        C.detect_chessboard = fake_detect  # type: ignore
        C.import_cv2 = lambda module=None: fake  # type: ignore
        try:
            with self.assertRaises(CalibrationError) as ctx:
                calibrate_from_images(
                    ordered, SPEC, prefer_opencv=False, module=fake
                )
        finally:
            C.detect_chessboard = orig_detect
            C.import_cv2 = orig_import
        self.assertIn("refusing to mix", str(ctx.exception))


class StrictKTest(unittest.TestCase):
    def test_skew_k10_rejected(self):
        data = valid_dict()
        data["camera_matrix"][1][0] = 40.0
        with self.assertRaises(CalibrationError):
            intrinsics_from_dict(data)

    def test_singular_k_rejected(self):
        data = valid_dict()
        data["camera_matrix"] = [[700, 700, 320], [690, 690, 240], [0, 0, 1]]
        with self.assertRaises(CalibrationError):
            intrinsics_from_dict(data)

    def test_singular_diag_rejected_as_not_invertible(self):
        data = valid_dict()
        data["camera_matrix"] = [[700.0, 0.0, 320.0], [0.0, 690.0, 240.0],
                                 [0.0, 0.0, 0.0]]
        with self.assertRaises(CalibrationError):
            intrinsics_from_dict(data)

    def test_accepted_blue_profile_unchanged(self):
        profile = ROOT / "configurations" / "pseye_blue_640x480_calibrated.json"
        loaded = load_intrinsics(profile)
        self.assertTrue(loaded.calibrated)
        self.assertEqual(loaded.image_size, (640, 480))
        self.assertEqual(loaded.camera_matrix[1, 0], 0.0)
        self.assertTrue(np.all(np.isfinite(loaded.camera_matrix)))
        self.assertTrue(
            np.all(np.isfinite(np.linalg.inv(loaded.camera_matrix)))
        )


if __name__ == "__main__":
    unittest.main()
