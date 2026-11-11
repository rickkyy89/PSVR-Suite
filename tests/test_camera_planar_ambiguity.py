# SPDX-License-Identifier: MIT
"""Regression tests for B5-04: physical front/front planar ambiguity.

The homography ``+/-`` pair only flips the sign (one branch lands behind the
camera), so ``ambiguity_ratio=0`` never proved the absence of the real
perspective ambiguity where *both* poses sit in front of the lens. The
estimator now compares two physical IPPE branches by reprojection
(``best/other`` against the existing ``ambiguity_ratio_threshold``) when
OpenCV is available, and reports ``physical_ambiguity_checked=False`` for the
honest pure-NumPy fallback instead of claiming the ambiguity is resolved.

The IPPE solve always uses generic ``SOLVEPNP_IPPE`` on rays normalised with
the full ``K`` inverse (never ``IPPE_SQUARE``, which is only valid for
centred, correctly ordered squares, and never pixels+K direct, which makes
OpenCV ignore the skew ``K[0, 1]``).

Fixture: 10 cm square marker at 2 m, 0.1 px Gaussian noise, seed 4. Both IPPE
branches stay in front with close reprojection scores and rotations tens of
degrees apart, so the frame must be ``ambiguous``, not a metric pose.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from psvr_camera import geometry as G  # noqa: E402
from psvr_camera import planar_solver as PS  # noqa: E402
from psvr_camera.calibration import Intrinsics  # noqa: E402
from psvr_camera.estimator import (  # noqa: E402
    EstimatorConfig,
    EstimatorError,
    PositionEstimator,
    aruco_geometry,
    optical_to_world,
)

K = np.array([[620.0, 0.0, 320.0], [0.0, 615.0, 240.0], [0.0, 0.0, 1.0]])
K_SKEW = np.array([[620.0, 15.0, 320.0], [0.0, 615.0, 240.0], [0.0, 0.0, 1.0]])

MARKER_M = 0.10
TRANSLATION_M = np.array([0.0, 0.0, 2.0])
RVEC = np.array([0.3, 0.0, 0.0])
NOISE_PX = 0.1
SEED = 4

# Generic (non-square, non-centred) quadrilateral: the SQUARE-only IPPE
# variant must never be applied to this shape.
QUAD = np.array(
    [[-0.05, 0.05, 0.0], [0.06, 0.04, 0.0], [0.04, -0.05, 0.0], [-0.06, -0.04, 0.0]]
)


def _calibrated(matrix=K):
    return Intrinsics(
        camera_matrix=np.asarray(matrix, dtype=np.float64),
        distortion_coeffs=np.zeros(5),
        image_size=(640, 480),
        calibrated=True,
    )


def _noisy_fixture():
    geometry = aruco_geometry(MARKER_M)
    objects = np.asarray(geometry.object_points_m)
    rotation = G.rodrigues(RVEC)
    clean = G.project_points(K, rotation, TRANSLATION_M, objects)
    rng = np.random.default_rng(SEED)
    observed = clean + rng.normal(0.0, NOISE_PX, size=clean.shape)
    return geometry, objects, observed


def _ippe_available() -> bool:
    try:
        from psvr_camera.camera_backend import import_cv2
    except ImportError:
        return False
    try:
        cv2 = import_cv2()
    except Exception:
        return False
    return hasattr(cv2, "solvePnPGeneric") and hasattr(cv2, "SOLVEPNP_IPPE")


class ReviewNumbersTest(unittest.TestCase):
    def test_review_rms_ratio_is_ambiguous_under_default_threshold(self):
        # Literal probe from review_261015.md §8 B5-04: IPPE finds two front
        # poses at RMS 0.091454/0.127241 px, 34.416 deg apart. The score must
        # stay above the default 0.5 floor so such a frame is ambiguous, and
        # 34 deg is far above the numeric-dedup tolerance (1e-4 deg), i.e. a
        # genuine second hypothesis, not the same pose twice.
        ratio = PS.reprojection_ambiguity_ratio(0.091454, 0.127241)
        self.assertAlmostEqual(ratio, 0.718743, places=4)
        self.assertGreaterEqual(ratio, EstimatorConfig().ambiguity_ratio_threshold)
        self.assertGreater(34.416, PS.DEDUP_ROTATION_DEG)


@unittest.skipUnless(_ippe_available(), "OpenCV IPPE not available")
class PhysicalAmbiguityTest(unittest.TestCase):
    def test_both_front_ippe_branches_are_ambiguous(self):
        geometry, objects, observed = _noisy_fixture()
        branches = PS.ippe_branches(objects, observed, K)
        self.assertIsNotNone(branches)
        assert branches is not None
        self.assertGreaterEqual(len(branches), 2)
        best, other = branches[0], branches[1]
        # Both hypotheses sit in front of the lens: the +/- homography pair
        # can never produce this case.
        self.assertEqual(best.positive_depth_ratio, 1.0)
        self.assertEqual(other.positive_depth_ratio, 1.0)
        self.assertFalse(PS.branches_are_duplicates(best, other))
        # Close reprojection scores (fixture: ~0.09 vs ~0.15 px here, review
        # probe 0.091 vs 0.127 px) and widely separated rotations.
        self.assertGreater(best.reprojection_px, 0.0)
        self.assertGreater(other.reprojection_px, best.reprojection_px)
        for branch in (best, other):
            self.assertTrue(np.all(np.isfinite(branch.rotation)))
            self.assertTrue(np.all(np.isfinite(branch.translation)))
            self.assertTrue(np.isfinite(branch.reprojection_px))
        ratio = PS.reprojection_ambiguity_ratio(
            best.reprojection_px, other.reprojection_px
        )
        self.assertGreaterEqual(
            ratio, EstimatorConfig().ambiguity_ratio_threshold
        )
        separation = PS.rotation_separation_deg(best.rotation, other.rotation)
        self.assertGreater(
            separation,
            10.0,
            f"expected a meaningful distinct branch, got {separation:.2f}deg",
        )
        result = PositionEstimator(_calibrated(), geometry).estimate(observed)
        self.assertFalse(result.valid)
        self.assertEqual(result.state, "ambiguous")
        self.assertIn("IPPE", result.reason)
        self.assertTrue(result.physical_ambiguity_checked)
        self.assertGreaterEqual(
            float(result.ambiguity_ratio or 0.0),
            EstimatorConfig().ambiguity_ratio_threshold,
        )

    def test_noise_free_normal_view_is_not_ambiguous(self):
        geometry = aruco_geometry(MARKER_M)
        objects = np.asarray(geometry.object_points_m)
        rotation = G.rodrigues([0.1, -0.05, 0.02])
        translation = np.array([0.02, -0.03, 0.5])
        observed = G.project_points(K, rotation, translation, objects)
        result = PositionEstimator(_calibrated(), geometry).estimate(observed)
        self.assertTrue(result.valid, result.reason)
        self.assertEqual(result.state, "tracking")
        self.assertLess(result.reprojection_px or 0.0, 1e-6)
        self.assertLess(
            float(result.ambiguity_ratio or 0.0),
            EstimatorConfig().ambiguity_ratio_threshold,
        )
        self.assertTrue(result.physical_ambiguity_checked)

    def test_exact_fronto_parallel_duplicate_is_not_ambiguous(self):
        # Noise-free square facing the camera: IPPE returns the same pose
        # twice (numeric dedup: rotation AND translation agree to solver
        # noise). That is one hypothesis, not an ambiguity.
        geometry = aruco_geometry(MARKER_M)
        objects = np.asarray(geometry.object_points_m)
        observed = G.project_points(K, np.eye(3), TRANSLATION_M, objects)
        branches = PS.ippe_branches(objects, observed, K)
        self.assertIsNotNone(branches)
        assert branches is not None
        self.assertGreaterEqual(len(branches), 2)
        self.assertTrue(PS.branches_are_duplicates(branches[0], branches[1]))
        result = PositionEstimator(_calibrated(), geometry).estimate(observed)
        self.assertTrue(result.valid, result.reason)
        self.assertEqual(result.state, "tracking")

    def test_behind_alternative_does_not_invalidate_front_best(self):
        # A behind-camera alternative is not a front/front competitor: even
        # with a close reprojection score it must not turn a valid best
        # pose into an ambiguity. Uses the noisy fixture so the best RMS
        # is non-trivial and the ratio alone would look ambiguous.
        import psvr_camera.estimator as estimator_module

        geometry, objects, observed = _noisy_fixture()
        real = PS.ippe_branches(objects, observed, K)
        self.assertIsNotNone(real)
        assert real is not None
        best = real[0]
        self.assertEqual(best.positive_depth_ratio, 1.0)
        fake_other = G.PoseCandidate(
            rotation=np.asarray(best.rotation).copy(),
            translation=np.array([0.0, 0.0, -2.0]),
            reprojection_px=float(best.reprojection_px * 1.1),
            positive_depth_ratio=0.0,
        )
        # Sanity: the reprojection ratio alone would look ambiguous.
        self.assertGreaterEqual(
            PS.reprojection_ambiguity_ratio(
                best.reprojection_px, fake_other.reprojection_px
            ),
            EstimatorConfig().ambiguity_ratio_threshold,
        )
        real_ippe = estimator_module.ippe_branches
        estimator_module.ippe_branches = lambda *a, **k: [best, fake_other]  # noqa: E731
        try:
            result = PositionEstimator(_calibrated(), geometry).estimate(observed)
        finally:
            estimator_module.ippe_branches = real_ippe
        self.assertTrue(result.valid, result.reason)
        self.assertEqual(result.state, "tracking")
        self.assertTrue(result.physical_ambiguity_checked)

    def test_generic_quad_has_finite_branches_and_stays_usable(self):
        # Non-square quadrilateral: generic IPPE must return finite branches
        # (no SQUARE misuse) and a clean view must not be blanket-invalidated.
        from psvr_camera.estimator import MarkerGeometry

        geometry = MarkerGeometry(
            kind="blob_led",
            provenance="user_measured",
            object_points_m=tuple(tuple(float(v) for v in p) for p in QUAD),
            description="generic non-square quad",
        )
        rotation = G.rodrigues([0.15, 0.1, 0.0])
        translation = np.array([0.01, -0.02, 0.45])
        observed = G.project_points(K, rotation, translation, QUAD)
        branches = PS.ippe_branches(QUAD, observed, K)
        self.assertIsNotNone(branches)
        assert branches is not None
        self.assertGreaterEqual(len(branches), 2)
        for branch in branches:
            self.assertTrue(np.all(np.isfinite(branch.rotation)))
            self.assertTrue(np.all(np.isfinite(branch.translation)))
            self.assertTrue(np.isfinite(branch.reprojection_px))
            self.assertTrue(np.isfinite(branch.positive_depth_ratio))
        result = PositionEstimator(_calibrated(), geometry).estimate(observed)
        self.assertTrue(result.valid, result.reason)
        self.assertEqual(result.state, "tracking")
        self.assertTrue(result.physical_ambiguity_checked)

    def test_skewed_intrinsics_stay_metric(self):
        # K with skew: pixels are normalised with the full K inverse before
        # IPPE, so the metric pose stays exact. Passing skewed pixels + K
        # straight to OpenCV mis-solves (cm-level depth error on this
        # fixture); this test locks the correct behaviour.
        geometry = aruco_geometry(MARKER_M)
        objects = np.asarray(geometry.object_points_m)
        rotation = G.rodrigues([0.1, -0.05, 0.02])
        translation = np.array([0.02, -0.03, 0.5])
        observed = G.project_points(K_SKEW, rotation, translation, objects)
        branches = PS.ippe_branches(objects, observed, K_SKEW)
        self.assertIsNotNone(branches)
        assert branches is not None
        for branch in branches:
            self.assertTrue(np.all(np.isfinite(branch.rotation)))
            self.assertTrue(np.all(np.isfinite(branch.translation)))
        result = PositionEstimator(_calibrated(K_SKEW), geometry).estimate(observed)
        self.assertTrue(result.valid, result.reason)
        self.assertEqual(result.state, "tracking")
        np.testing.assert_allclose(
            result.position_world_m, optical_to_world(translation), atol=1e-6
        )
        self.assertTrue(result.physical_ambiguity_checked)


class NumpyFallbackHonestyTest(unittest.TestCase):
    def test_fallback_stays_usable_but_reports_unchecked(self):
        import psvr_camera.estimator as estimator_module

        geometry = aruco_geometry(MARKER_M)
        objects = np.asarray(geometry.object_points_m)
        rotation = G.rodrigues([0.1, -0.05, 0.02])
        translation = np.array([0.02, -0.03, 0.5])
        observed = G.project_points(K, rotation, translation, objects)
        real_ippe = estimator_module.ippe_branches
        estimator_module.ippe_branches = lambda *a, **k: None  # noqa: E731
        try:
            result = PositionEstimator(_calibrated(), geometry).estimate(observed)
        finally:
            estimator_module.ippe_branches = real_ippe
        # No blanket disable of valid planar tracking ...
        self.assertTrue(result.valid, result.reason)
        self.assertEqual(result.state, "tracking")
        # ... but the fallback must not claim the physical check ran.
        self.assertFalse(result.physical_ambiguity_checked)


class MalformedIntrinsicsTest(unittest.TestCase):
    def test_nontriangular_k_is_refused(self):
        bad = Intrinsics(
            np.array([[620.0, 0.0, 320.0], [40.0, 615.0, 240.0], [0.0, 0.0, 1.0]]),
            np.zeros(5),
            (640, 480),
            calibrated=True,
        )
        with self.assertRaises(EstimatorError):
            PositionEstimator(bad, aruco_geometry(MARKER_M))

    def test_singular_k_is_refused(self):
        bad = Intrinsics(
            np.array([[700.0, 700.0, 320.0], [690.0, 690.0, 240.0], [0.0, 0.0, 1.0]]),
            np.zeros(5),
            (640, 480),
            calibrated=True,
        )
        with self.assertRaises(EstimatorError):
            PositionEstimator(bad, aruco_geometry(MARKER_M))

    def test_singular_estimate_is_error_not_exception(self):
        # A K that passes construction shape checks but cannot be inverted
        # must surface as an error result, never as a raw LinAlgError.
        geometry = aruco_geometry(MARKER_M)
        estimator = PositionEstimator(_calibrated(), geometry)
        estimator.camera_matrix = np.array(
            [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0]]
        )
        objects = np.asarray(geometry.object_points_m)
        observed = G.project_points(K, np.eye(3), TRANSLATION_M, objects)
        result = estimator.estimate(observed)
        self.assertFalse(result.valid)
        self.assertEqual(result.state, "error")


if __name__ == "__main__":
    unittest.main()
