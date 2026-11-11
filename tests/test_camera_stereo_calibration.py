# SPDX-License-Identifier: MIT
"""Synthetic/offline tests only: never write an accepted PS4 configuration."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from psvr_camera.calibration import CalibrationError, ChessboardSpec
from psvr_camera.camera_backend import OpenCvUnavailable
from psvr_camera import stereo_calibration as S

try:
    import cv2
except ImportError:
    cv2 = None

SPEC = ChessboardSpec(9, 6, 0.025)
SIZE = (640, 400)
K_LEFT = np.array([[600., 0., 320.], [0., 605., 200.], [0., 0., 1.]])
K_RIGHT = np.array([[610., 0., 318.], [0., 608., 202.], [0., 0., 1.]])


def observations(count=16, baseline=0.06, noise=0.0):
    """Known metric baseline; seeded distinct tilts/positions, zero distortion."""
    rng = np.random.default_rng(712)
    rotation = cv2.Rodrigues(np.array([0.003, -0.007, 0.002]))[0]
    translation = np.array([-baseline, 0., 0.])
    left, right = [], []
    for _ in range(count):
        rvec = rng.uniform([-0.45, -0.4, -0.18], [0.45, 0.4, 0.18])
        tvec = rng.uniform([-0.14, -0.10, 0.60], [-0.07, -0.04, 0.95])
        board_r = cv2.Rodrigues(rvec)[0]
        right_rvec = cv2.Rodrigues(rotation @ board_r)[0]
        lp = cv2.projectPoints(SPEC.object_points(), rvec, tvec,
                               K_LEFT, np.zeros(5))[0].reshape(-1, 2)
        rp = cv2.projectPoints(SPEC.object_points(), right_rvec,
                               rotation @ tvec + translation,
                               K_RIGHT, np.zeros(5))[0].reshape(-1, 2)
        left.append(lp + rng.normal(0, noise, lp.shape))
        right.append(rp + rng.normal(0, noise, rp.shape))
    return left, right, rotation, translation


@unittest.skipIf(cv2 is None, "OpenCV optional: stereo deliberately has no fallback")
class StereoSolveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.left, cls.right, cls.rotation, cls.translation = observations()
        cls.calibration = S.solve_stereo(
            cls.left[:12], cls.right[:12], SIZE, SPEC, source="synthetic_test_only")

    def test_recovers_metric_transform_and_separate_intrinsics(self):
        c = self.calibration
        self.assertEqual(c.sample_count, 12)
        self.assertEqual(c.image_size, SIZE)
        self.assertEqual(c.mode, "640x400")
        self.assertLess(c.rms, 0.001)
        np.testing.assert_allclose(c.R, self.rotation, atol=2e-5)
        np.testing.assert_allclose(c.T, self.translation, atol=2e-5)
        np.testing.assert_allclose(c.left.camera_matrix, K_LEFT, atol=0.02)
        np.testing.assert_allclose(c.right.camera_matrix, K_RIGHT, atol=0.02)
        self.assertIsNotNone(c.Q)
        self.assertFalse(c.to_dict()["provenance"]["physical_order_verified"])
        self.assertEqual(c.to_dict()["provenance"]["hardware_acceptance"], "unverified")

    def test_independent_holdout_does_not_refit(self):
        calibration = copy.deepcopy(self.calibration)
        before = calibration.to_dict()
        with patch.object(S, "solve_intrinsics", side_effect=AssertionError("holdout refit")), \
                patch.object(cv2, "calibrateCamera", side_effect=AssertionError("holdout refit")), \
                patch.object(cv2, "stereoCalibrate", side_effect=AssertionError("holdout refit")):
            report = S.validate_stereo(calibration, self.left[12:], self.right[12:])
        self.assertEqual(report["sample_count"], 4)
        self.assertIs(report["accepted"], True)
        self.assertEqual(calibration.validation, report)
        self.assertLess(report["rms_reprojection_px"], 0.001)
        self.assertLess(report["epipolar_rms_px"], 0.001)
        after = calibration.to_dict()
        before.pop("validation")
        after.pop("validation")
        self.assertEqual(before, after)

    def test_failed_revalidation_clears_previous_acceptance(self):
        calibration = copy.deepcopy(self.calibration)
        S.validate_stereo(calibration, self.left[12:], self.right[12:])
        with self.assertRaises(CalibrationError):
            S.validate_stereo(calibration, self.left[:2], self.right[:2])
        self.assertIsNone(calibration.validation)

    def test_holdout_rejects_training_reuse_and_bad_pairs(self):
        with self.assertRaisesRegex(CalibrationError, "training"):
            S.validate_stereo(self.calibration, self.left[:2], self.right[:2])
        with self.assertRaises(CalibrationError):
            S.validate_stereo(self.calibration, self.left[12:], self.right[12:][::-1])

    def test_noisy_realistic_data_still_passes(self):
        left, right, _, translation = observations(noise=0.08)
        c = S.solve_stereo(left[:12], right[:12], SIZE, SPEC, source="synthetic_test_only")
        np.testing.assert_allclose(c.T, translation, atol=0.003)
        self.assertLess(S.validate_stereo(c, left[12:], right[12:])["rms_reprojection_px"], 1)

    def test_joint_refinement_corrects_biased_monocular_initializers(self):
        left, right, _, _ = observations(noise=0.08)
        solve_intrinsics = S.solve_intrinsics
        stereo_calibrate = cv2.stereoCalibrate
        initializers, fitted = [], []

        def biased_initializer(views, *args, **kwargs):
            intrinsic = solve_intrinsics(views, *args, **kwargs)
            intrinsic.camera_matrix[0, 0] *= 1.08 if not initializers else 0.92
            # A valid but deliberately unrelated initializer metric must not
            # survive as the RMS of the returned jointly refined intrinsics.
            intrinsic.rms_reprojection_px = 0.75
            initializers.append(copy.deepcopy(intrinsic))
            return intrinsic

        def fit_training_only(objects, lp, rp, *args, **kwargs):
            self.assertEqual(len(objects), 12)
            for supplied, training in ((lp, left[:12]), (rp, right[:12])):
                for actual, expected in zip(supplied, training):
                    np.testing.assert_array_equal(actual, expected.astype(np.float32))
            result = stereo_calibrate(objects, lp, rp, *args, **kwargs)
            fitted.append(result)
            return result

        with patch.object(S, "solve_intrinsics", side_effect=biased_initializer) as mono, \
                patch.object(cv2, "stereoCalibrate", side_effect=fit_training_only) as stereo:
            c = S.solve_stereo(left[:12], right[:12], SIZE, SPEC,
                               source="synthetic_test_only")
        self.assertEqual(mono.call_count, 2)
        for call, training in zip(mono.call_args_list, (left[:12], right[:12])):
            self.assertEqual(len(call.args[0]), 12)
            for actual, expected in zip(call.args[0], training):
                np.testing.assert_array_equal(actual, expected)
        self.assertEqual(stereo.call_count, 1)
        self.assertEqual(stereo.call_args.kwargs["flags"], cv2.CALIB_USE_INTRINSIC_GUESS)
        self.assertEqual(stereo.call_args.kwargs["criteria"],
                         (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_MAX_ITER, 200, 1e-10))

        for eye, truth, views, k, d in (
                (c.left, K_LEFT, left[:12], fitted[0][1], fitted[0][2]),
                (c.right, K_RIGHT, right[:12], fitted[0][3], fitted[0][4])):
            np.testing.assert_array_equal(eye.camera_matrix, k)
            np.testing.assert_array_equal(eye.distortion_coeffs, d.reshape(-1))
            np.testing.assert_allclose(eye.camera_matrix, truth, atol=3.0)
            squared_errors = []
            for points in views:
                ok, rvec, tvec = cv2.solvePnP(SPEC.object_points(), points,
                                             eye.camera_matrix, eye.distortion_coeffs)
                self.assertTrue(ok)
                projected = cv2.projectPoints(SPEC.object_points(), rvec, tvec,
                                              eye.camera_matrix, eye.distortion_coeffs)[0]
                squared_errors.extend(np.sum((projected.reshape(-1, 2) - points)**2, axis=1))
            expected_rms = float(np.sqrt(np.mean(squared_errors)))
            self.assertAlmostEqual(eye.rms_reprojection_px, expected_rms, places=12)
            self.assertLess(eye.rms_reprojection_px, 0.2)
            provenance = eye.to_dict()["provenance"]
            self.assertEqual(provenance["source"], "chessboard_opencv_stereo_refined")
            self.assertIn("Monocular OpenCV calibrateCamera initialization", provenance["notes"])
            self.assertIn("joint OpenCV stereoCalibrate refinement", provenance["notes"])
            self.assertIn("training pairs only", provenance["notes"])
        for initial, refined in zip(initializers, (c.left, c.right)):
            self.assertGreater(abs(initial.camera_matrix[0, 0] - refined.camera_matrix[0, 0]), 30)
            self.assertEqual(initial.rms_reprojection_px, 0.75)
        report = S.validate_stereo(c, left[12:], right[12:])
        self.assertTrue(report["accepted"])
        self.assertLess(report["max_view_rms_px"], 1.5)
        self.assertLess(report["max_view_epipolar_px"], 1.0)

    def test_rejects_pairing_and_180_degree_order_errors(self):
        for right in (self.right[:12][::-1],
                      [self.right[0][::-1]] + self.right[1:12],
                      [p[::-1] for p in self.right[:12]]):
            with self.subTest(pattern=right[0][0].tolist()):
                with self.assertRaises(CalibrationError):
                    S.solve_stereo(self.left[:12], right, SIZE, SPEC)

    def test_rejects_duplicate_insufficient_nonfinite_and_outside_views(self):
        invalid = [
            (self.left[:7], self.right[:7]),
            (self.left[:8], self.right[:9]),
            ([self.left[0]] * 8, [self.right[0]] * 8),
            (self.left[:7] + [self.left[0] + 0.01], self.right[:8]),
        ]
        for value in (np.nan, np.inf, -1.0, 10000.0):
            left = [p.copy() for p in self.left[:12]]
            left[0][0, 0] = value
            invalid.append((left, self.right[:12]))
        for left, right in invalid:
            with self.subTest(count=len(left)):
                with self.assertRaises(CalibrationError):
                    S.solve_stereo(left, right, SIZE, SPEC)
        for size in ((0, 400), (640.0, 400), (True, 400)):
            with self.assertRaises(CalibrationError):
                S.solve_stereo(self.left[:12], self.right[:12], size, SPEC)

    def test_rejects_zero_baseline(self):
        left, right, _, _ = observations(baseline=0)
        with self.assertRaises(CalibrationError):
            S.solve_stereo(left[:12], right[:12], SIZE, SPEC)

    def test_rejects_translation_only_views_even_with_known_intrinsics(self):
        left, right = [], []
        for index in range(12):
            translation = np.array([-0.12 + index * 0.002, -0.07, 0.65 + index * 0.01])
            left.append(cv2.projectPoints(SPEC.object_points(), np.zeros(3), translation,
                                           K_LEFT, np.zeros(5))[0].reshape(-1, 2))
            right.append(cv2.projectPoints(
                SPEC.object_points(), cv2.Rodrigues(self.rotation)[0],
                self.rotation @ translation + self.translation,
                K_RIGHT, np.zeros(5))[0].reshape(-1, 2))
        initializers = copy.deepcopy([self.calibration.left, self.calibration.right])
        for intrinsic in initializers:
            intrinsic.source = "chessboard_opencv"
        with patch.object(S, "solve_intrinsics", side_effect=initializers), \
                patch.object(cv2, "stereoCalibrate") as stereo:
            with self.assertRaisesRegex(CalibrationError, "pose diversity"):
                S.solve_stereo(left, right, SIZE, SPEC)
        stereo.assert_not_called()

    def test_bad_rms_limits(self):
        for value in (np.nan, np.inf, 0, -1, True):
            with self.assertRaises(CalibrationError):
                S.solve_stereo(self.left[:12], self.right[:12], SIZE, SPEC, max_rms_px=value)

    def test_opencv_unavailable_has_no_stereo_fallback(self):
        with patch.object(S, "import_cv2", side_effect=OpenCvUnavailable("missing")):
            with self.assertRaises(OpenCvUnavailable):
                S.solve_stereo(self.left[:12], self.right[:12], SIZE, SPEC)

    def test_existing_intrinsics_fallback_is_refused(self):
        class BrokenOptimizer:
            def calibrateCamera(self, *args, **kwargs):
                raise RuntimeError("optimizer failed")
        with self.assertRaisesRegex(CalibrationError, "no NumPy fallback"):
            S.solve_stereo(self.left[:12], self.right[:12], SIZE, SPEC,
                           module=BrokenOptimizer())

    def test_atomic_exclusive_roundtrip(self):
        calibration = copy.deepcopy(self.calibration)
        S.validate_stereo(calibration, self.left[12:], self.right[12:],
                          max_epipolar_px=1.0, max_reprojection_px=1.5)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic_only.json"
            S.save_stereo_calibration(path, calibration)
            first = path.read_bytes()
            loaded = S.load_stereo_calibration(path, expected_image_size=SIZE,
                                              expected_mode="640x400")
            np.testing.assert_array_equal(loaded.T, calibration.T)
            self.assertEqual(loaded.to_dict(), calibration.to_dict())
            self.assertTrue(loaded.validation["accepted"])
            with self.assertRaises(FileExistsError):
                S.save_stereo_calibration(path, calibration)
            self.assertEqual(first, path.read_bytes())
            self.assertEqual([p.name for p in Path(directory).iterdir()], [path.name])
            with self.assertRaises(CalibrationError):
                S.load_stereo_calibration(path, expected_image_size=(320, 192))
            with self.assertRaises(CalibrationError):
                S.load_stereo_calibration(path, expected_mode="1280x800")

    def test_publication_failure_leaves_no_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic_only.json"
            with patch.object(S.os, "link", side_effect=OSError("no hardlinks")):
                with self.assertRaises(OSError):
                    S.save_stereo_calibration(path, self.calibration)
            self.assertFalse(path.exists())
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_strict_schema_rejects_bad_geometry_and_metadata(self):
        mutations = [
            ("schema", "psvr.camera.intrinsics"), ("version", True),
            ("created_utc", "2026-10-24"), ("sample_count", 7),
            ("rms_reprojection_px", float("nan")), ("T", [0., 0., 0.]),
            ("R", np.zeros((3, 3)).tolist()), ("R", (-np.eye(3)).tolist()),
            ("E", np.eye(3).tolist()), ("F", np.eye(3).tolist()),
            ("mode", "320x192"), ("training_fingerprints", []),
            ("extra", "not allowed"),
        ]
        for key, value in mutations:
            data = self.calibration.to_dict()
            data[key] = value
            with self.subTest(key=key):
                with self.assertRaises(CalibrationError):
                    S.stereo_from_dict(data)
        mutations_nested = [
            ("left", "image_size", [320, 192]),
            ("left", "camera_matrix", np.zeros((3, 3)).tolist()),
            ("right", "distortion_coeffs", [float("inf")] * 5),
            ("left", "extra", True),
            ("provenance", "physical_order_verified", True),
            ("provenance", "translation_units", "millimetres"),
            ("rectification", "R1", np.zeros((3, 3)).tolist()),
            ("rectification", "P2", np.zeros((3, 4)).tolist()),
            ("rectification", "Q", np.zeros((4, 4)).tolist()),
            ("rectification", "Q", np.eye(4).tolist()),
        ]
        for section, key, value in mutations_nested:
            data = self.calibration.to_dict()
            data[section][key] = value
            with self.subTest(section=section, key=key):
                with self.assertRaises(CalibrationError):
                    S.stereo_from_dict(data)

    def test_strict_json_and_bounded_loader(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            for payload in ('{}', '{', '{"schema":"x","schema":"y"}',
                            '[]', '"text"', ' ' * (S._MAX_FILE_BYTES + 1)):
                path.write_text(payload, encoding="utf-8")
                with self.assertRaises(CalibrationError):
                    S.load_stereo_calibration(path)
            data = self.calibration.to_dict()
            del data["F"]
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(CalibrationError):
                S.load_stereo_calibration(path)

    def test_absent_rectification_supported_but_partial_refused(self):
        data = self.calibration.to_dict()
        data["rectification"] = None
        self.assertIsNone(S.stereo_from_dict(data).Q)
        data["rectification"] = {"R1": np.eye(3).tolist()}
        with self.assertRaises(CalibrationError):
            S.stereo_from_dict(data)

    def test_malformed_persisted_heldout_evidence_refused(self):
        calibration = copy.deepcopy(self.calibration)
        S.validate_stereo(calibration, self.left[12:], self.right[12:])
        for key, value in (("accepted", False), ("sample_count", 1),
                           ("rms_reprojection_px", -1), ("max_view_rms_px", 99),
                           ("per_view_rms_px", [0.0]), ("max_reprojection_px", 0)):
            data = calibration.to_dict()
            data["validation"] = dict(data["validation"])
            data["validation"][key] = value
            with self.subTest(key=key):
                with self.assertRaises(CalibrationError):
                    S.stereo_from_dict(data)


if __name__ == "__main__":
    unittest.main()
