# SPDX-License-Identifier: MIT
"""Synthetic stereo patches only: no capture, hardware, pose or publication."""
from __future__ import annotations

import copy
from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from psvr_camera.calibration import CalibrationError, ChessboardSpec, Intrinsics
from psvr_camera.stereo_calibration import StereoCalibration, load_stereo_calibration
from psvr_camera.stereo_leds import StereoLedConfig, triangulate_candidates

try:
    import cv2
except ImportError:
    cv2 = None

SIZE = (640, 400)
K = np.array([[600., 0., 320.], [0., 600., 200.], [0., 0., 1.]])
SPEC = ChessboardSpec(9, 6, 0.025)
POINTS = np.array([[-.10, -.18, 1.2], [.10, -.09, 1.1], [.02, .01, 1.0],
                   [-.04, .12, 1.3], [.13, .20, 1.4]])


def calibration(translation=None, rotation=None, distortion=None, right_k=None):
    """Exact metric synthetic fixture satisfying the strict stereo schema."""
    t = np.array([.08, 0., 0.] if translation is None else translation, dtype=float)
    r = np.eye(3) if rotation is None else rotation
    d = np.zeros(5) if distortion is None else np.asarray(distortion)
    kr = K if right_k is None else right_k
    x, y, z = t
    e = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]]) @ r
    eyes = [Intrinsics(k.copy(), d.copy(), SIZE, True, rms_reprojection_px=0.,
                       views=12, chessboard=SPEC.to_dict(), source="synthetic_test_only",
                       created_utc="2026-10-24T00:00:00+00:00")
            for k in (K, kr)]
    return StereoCalibration(*eyes, r.copy(), t, e, np.linalg.inv(kr).T @ e @ np.linalg.inv(K),
                             0., 12, SIZE, SPEC, mode="640x400",
                             source="synthetic_test_only",
                             training_fingerprints=tuple(f"{i:064x}" for i in range(12)))


def candidate(point):
    return {"center_px": [float(v) for v in point], "area_px": 30,
            "bbox_px": [int(point[0]) - 3, int(point[1]) - 3, 7, 7]}


def project(c, points=POINTS):
    lp = cv2.projectPoints(points, np.zeros(3), np.zeros(3), c.left.camera_matrix,
                           c.left.distortion_coeffs)[0].reshape(-1, 2)
    rp = cv2.projectPoints(points, cv2.Rodrigues(c.R)[0], c.T, c.right.camera_matrix,
                           c.right.distortion_coeffs)[0].reshape(-1, 2)
    return [candidate(p) for p in lp], [candidate(p) for p in rp]


@unittest.skipIf(cv2 is None, "OpenCV optional; stereo has no numerical fallback")
class StereoLedsTest(unittest.TestCase):
    def assert_safe(self, report):
        self.assertFalse(report["identities_assigned"])
        self.assertFalse(report["position_published"])
        self.assertFalse(report["physical_order_verified"])
        self.assertEqual(report["coordinate_frame"], "left_camera")
        self.assertEqual(report["translation_units"], "metres")
        json.dumps(report, allow_nan=False)
        self.assertNotIn("pose", report)

    def test_five_shuffled_patches_and_distractor_positive_t(self):
        c = calibration()
        left, right = project(c)
        lorder, rorder = [3, 0, 4, 2, 1], [1, 4, 0, 3, 2]
        left = [left[i] for i in lorder] + [candidate([70, 35])]
        right = [right[i] for i in rorder] + [candidate([550, 365])]
        before = copy.deepcopy((left, right, c.to_dict()))
        report = triangulate_candidates(c, left, right, config=None)
        self.assertEqual(report["state"], "points")
        self.assertEqual(len(report["matches"]), 5)
        self.assertEqual(report["unmatched_left_indices"], [5])
        self.assertEqual(report["unmatched_right_indices"], [5])
        for match in report["matches"]:
            i, j = match["left_index"], match["right_index"]
            self.assertEqual(lorder[i], rorder[j])
            # T positive gives right.x > left.x: never enforce the opposite sign.
            self.assertGreater(right[j]["center_px"][0], left[i]["center_px"][0])
            np.testing.assert_allclose(match["point_left_camera_m"], POINTS[lorder[i]], atol=1e-10)
            self.assertLess(match["reprojection_px"], 1e-9)
            self.assertLess(match["epipolar_px"], 1e-9)
            self.assertGreater(match["ray_angle_deg"], .2)
        self.assertEqual(before, (left, right, c.to_dict()))
        self.assert_safe(report)

    def test_rotated_distorted_unequal_intrinsics(self):
        r = cv2.Rodrigues(np.array([.012, -.025, .007]))[0]
        kr = np.array([[575., 0., 317.], [0., 610., 203.], [0., 0., 1.]])
        c = calibration(rotation=r, distortion=[-.18, .045, .002, -.001, .005], right_k=kr)
        left, right = project(c)
        report = triangulate_candidates(c, left, right[::-1])
        self.assertEqual(report["state"], "points")
        self.assertEqual(len(report["matches"]), 5)
        for match in report["matches"]:
            self.assertEqual(match["right_index"], 4 - match["left_index"])
            np.testing.assert_allclose(match["point_left_camera_m"],
                                       POINTS[match["left_index"]], atol=1e-6)
        self.assert_safe(report)

    def test_negative_t_is_also_supported(self):
        c = calibration(translation=[-.08, 0, 0])
        report = triangulate_candidates(c, *project(c))
        self.assertEqual(len(report["matches"]), 5)
        for match in report["matches"]:
            np.testing.assert_allclose(match["point_left_camera_m"],
                                       POINTS[match["left_index"]], atol=1e-10)

    def test_same_epipolar_row_rejects_opposed_depth_hypotheses(self):
        c = calibration()
        # All four pairs triangulate well with positive depth; row/order alone
        # cannot tell which of the two very different depth assignments is real.
        points = np.array([[-.03, 0., 1.0], [.03, 0., 1.0]])
        report = triangulate_candidates(c, *project(c, points))
        self.assertEqual(report["state"], "ambiguous")
        self.assertEqual(report["matches"], [])
        self.assertEqual(report["assignment"]["maximum_cardinality"], 2)
        self.assertAlmostEqual(report["assignment"]["cost_gap"], 0.)
        self.assertEqual(len(report["withheld_pairs"]), 2)
        self.assert_safe(report)

    def test_duplicate_and_noisy_near_duplicate_right_candidates(self):
        c = calibration()
        for offset in (0., .15):
            left, right = project(c, POINTS[:1])
            duplicate = copy.deepcopy(right[0])
            duplicate["center_px"][1] += offset
            report = triangulate_candidates(c, left, right + [duplicate])
            self.assertEqual(report["state"], "ambiguous")
            self.assertEqual(report["matches"], [])
            self.assertLessEqual(report["assignment"]["cost_gap"], .25)

    def test_partial_preserves_only_certified_unique_edges(self):
        c = calibration()
        points = np.array([[-.03, 0., 1.], [.03, 0., 1.], [.02, .12, 1.2]])
        report = triangulate_candidates(c, *project(c, points))
        self.assertEqual(report["state"], "partial")
        self.assertEqual([(m["left_index"], m["right_index"]) for m in report["matches"]], [(2, 2)])
        self.assertEqual(len(report["withheld_pairs"]), 2)
        self.assert_safe(report)

    def test_partial_certifies_backbone_even_inside_connected_candidate_graph(self):
        c = calibration()
        # l2 can also match r0/r1, but every cardinality-three assignment must
        # reserve r2 for l2. The first two pairs remain geometrically ambiguous.
        left = [candidate([300, 200]), candidate([310, 200]), candidate([320, 200.9])]
        right = [candidate([350, 200]), candidate([360, 200]), candidate([370, 201.8])]
        report = triangulate_candidates(c, left, right)
        self.assertEqual(report["state"], "partial")
        self.assertEqual([(m["left_index"], m["right_index"]) for m in report["matches"]], [(2, 2)])
        self.assertEqual(len(report["withheld_pairs"]), 2)
        # Exhaustion while certifying ambiguity also discards *all* points.
        almost_enough = report["assignment"]["states_evaluated"] - 1
        limited = triangulate_candidates(c, left, right,
                                         StereoLedConfig(assignment_budget=almost_enough))
        self.assertEqual(limited["state"], "assignment_limit")
        self.assertEqual(limited["matches"], [])

    def test_global_maximum_cardinality_not_local_nearest_pair(self):
        c = calibration()
        # l0-r0 is locally cheaper, but l1 has only r0 within the epipolar gate.
        left = [candidate([300., 200.]), candidate([310., 198.9])]
        right = [candidate([350., 199.8]), candidate([360., 200.9])]
        report = triangulate_candidates(c, left, right)
        self.assertEqual(report["state"], "points")
        self.assertEqual({(m["left_index"], m["right_index"]) for m in report["matches"]},
                         {(0, 1), (1, 0)})

    def test_global_runnerup_margin_not_only_exact_ties(self):
        c = calibration()
        left = [candidate([300, 200])]
        right = [candidate([350, 200]), candidate([360, 200.3])]
        report = triangulate_candidates(c, left, right)
        self.assertEqual(report["state"], "ambiguous")
        report = triangulate_candidates(c, left, right, StereoLedConfig(ambiguity_margin=.01))
        self.assertEqual(report["state"], "points")
        self.assertEqual(report["matches"][0]["right_index"], 0)

    def test_epipolar_and_distorted_reprojection_are_independent_gates(self):
        c = calibration()
        left, right = project(c, POINTS[:1])
        right[0]["center_px"][1] += 2.
        report = triangulate_candidates(c, left, right)
        self.assertEqual(report["state"], "no_matches")
        self.assertEqual(report["rejections"], {"epipolar": 1})
        report = triangulate_candidates(c, left, right,
                                        StereoLedConfig(max_epipolar_px=3, max_reprojection_px=.1))
        self.assertEqual(report["state"], "no_matches")
        self.assertEqual(report["rejections"], {"reprojection": 1})

    def test_rejects_behind_either_camera(self):
        configs_points = [
            (calibration(), np.array([[0., 0., -1.]])),
            (calibration(translation=[.08, 0, 2], rotation=np.diag([-1., 1., -1.])),
             np.array([[0., 0., 3.]])),
        ]
        for c, points in configs_points:
            report = triangulate_candidates(c, *project(c, points))
            self.assertEqual(report["state"], "no_matches")
            self.assertEqual(report["rejections"], {"nonpositive_depth": 1})

    def test_zero_and_tiny_disparity_never_produce_far_fake_points(self):
        c = calibration()
        for offset in (0., .001):
            report = triangulate_candidates(c, [candidate([320, 200])],
                                            [candidate([320 + offset, 200])])
            self.assertEqual(report["state"], "no_matches")
            self.assertEqual(report["matches"], [])
            self.assertTrue(set(report["rejections"]) <= {"degenerate_triangulation", "ray_angle"})
            self.assert_safe(report)

    def test_configurable_distance_and_angle_gates(self):
        c = calibration()
        left, right = project(c, POINTS[:1])
        for config, reason in [(StereoLedConfig(max_distance_m=1.), "distance"),
                               (StereoLedConfig(min_distance_m=2.), "distance"),
                               (StereoLedConfig(min_ray_angle_deg=10.), "ray_angle")]:
            report = triangulate_candidates(c, left, right, config)
            self.assertEqual(report["rejections"], {reason: 1})

    def test_assignment_budget_fails_closed_and_no_candidate_truncation(self):
        c = calibration()
        left, right = project(c)
        report = triangulate_candidates(c, left, right, StereoLedConfig(assignment_budget=1))
        self.assertEqual(report["state"], "assignment_limit")
        self.assertFalse(report["assignment"]["complete"])
        self.assertEqual(report["matches"], [])
        report = triangulate_candidates(c, left * 4, right)
        self.assertEqual(report["state"], "candidate_limit")
        self.assertEqual(report["candidate_counts"]["left"], 20)
        self.assertEqual(report["matches"], [])
        self.assert_safe(report)

    def test_dense_sixteen_by_sixteen_search_is_bounded(self):
        c = calibration()
        left = [candidate([280 + i, 200]) for i in range(16)]
        right = [candidate([350 + i, 200]) for i in range(16)]
        report = triangulate_candidates(c, left, right, StereoLedConfig(assignment_budget=100))
        self.assertEqual(report["state"], "assignment_limit")
        self.assertLessEqual(report["assignment"]["states_evaluated"], 101)
        self.assertEqual(report["matches"], [])

    def test_invalid_inputs_and_mutated_calibration(self):
        c = calibration()
        for bad in ([{}], [None], [{"center_px": [float("nan"), 20]}],
                    [{"center_px": [20, float("inf")]}], [{"center_px": [640, 200]}],
                    [{"center_px": [-1, 200]}], [{"center_px": [1, 2, 3]}],
                    [{"center_px": ["1", "2"]}], [{"center_px": [True, False]}]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                triangulate_candidates(c, bad, [candidate([350, 200])])
        for bad in (None, {}, np.zeros((1, 2))):
            with self.assertRaises(ValueError):
                triangulate_candidates(c, bad, [])
        with self.assertRaises(ValueError):
            triangulate_candidates({}, [], [])
        with self.assertRaises(ValueError):
            triangulate_candidates(c, [], [], {})
        c.T[1] = .01
        with self.assertRaises(CalibrationError):
            triangulate_candidates(c, [], [])

    def test_strict_canonical_file_is_read_only_and_supported(self):
        path = ROOT / "configurations" / "ps4_stereo_640x400_calibrated.json"
        if not path.exists():
            self.skipTest("canonical PS4 calibration not available")
        c = load_stereo_calibration(path, expected_image_size=SIZE, expected_mode="640x400")
        self.assertEqual(c.sample_count, 12)
        self.assertTrue(c.validation["accepted"])
        self.assertEqual(c.validation["sample_count"], 3)
        # The positive calibrated T must not be flipped to fit a disparity convention.
        self.assertGreater(c.T[0], .08)
        self.assertLess(c.T[0], .082)
        self.assert_safe(triangulate_candidates(c, [], []))

    def test_empty_eyes_are_json_safe_no_matches(self):
        for left, right in (([], []), ([candidate([300, 200])], []),
                            ([], [candidate([350, 200])])):
            report = triangulate_candidates(calibration(), left, right)
            self.assertEqual(report["state"], "no_matches")
            self.assert_safe(report)


class StereoLedConfigTest(unittest.TestCase):
    def test_invalid_configuration(self):
        for kwargs in ({"max_epipolar_px": 0}, {"max_reprojection_px": float("nan")},
                       {"min_ray_angle_deg": 0}, {"min_ray_angle_deg": 90},
                       {"min_distance_m": -1}, {"max_distance_m": .01},
                       {"ambiguity_margin": -1}, {"max_candidates_per_eye": 17},
                       {"max_candidates_per_eye": True}, {"assignment_budget": 0},
                       {"assignment_budget": 1.5}, {"max_epipolar_px": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                StereoLedConfig(**kwargs)
        self.assertEqual(replace(StereoLedConfig(), ambiguity_margin=0).ambiguity_margin, 0)


if __name__ == "__main__":
    unittest.main()
