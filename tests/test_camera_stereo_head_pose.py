# SPDX-License-Identifier: MIT
"""Synthetic-only tests for the offline stereo head-pose prototype.

No camera hardware, no UDP, no tracking activation, no real log files. The two
canonical JSON configurations are opened read-only and their bytes are hashed
before/after to prove they are never modified.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from psvr_camera.calibration import CalibrationError
from psvr_camera.camera_backend import OpenCvUnavailable
from psvr_camera.stereo_calibration import load_stereo_calibration
from psvr_camera.stereo_leds import StereoLedConfig, triangulate_candidates
import psvr_camera.stereo_head_pose as head_pose
from psvr_camera.stereo_head_pose import (
    FrontLedRig,
    StereoHeadPoseConfig,
    estimate_stereo_head_pose,
    estimate_stereo_head_pose_from_triangulation,
    front_rig_from_dict,
    load_default_stereo_and_rig,
    load_front_led_rig,
)

try:
    import cv2
except ImportError:
    cv2 = None

CAL_PATH = ROOT / "configurations" / "ps4_stereo_640x400_calibrated.json"
RIG_PATH = ROOT / "configurations" / "psvr_front_leds_refined_20261011.json"
T_TRUE = np.array([0.05, -0.03, 0.9])


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def rotation_angle_deg(r1, r2):
    cosine = max(-1.0, min(1.0, 0.5 * (float(np.trace(r1.T @ r2)) - 1.0)))
    return math.degrees(math.acos(cosine))


def project_both(cal, points):
    left = cv2.projectPoints(points, np.zeros(3), np.zeros(3),
                             cal.left.camera_matrix, cal.left.distortion_coeffs)[0].reshape(-1, 2)
    right = cv2.projectPoints(points, cv2.Rodrigues(cal.R)[0], cal.T,
                              cal.right.camera_matrix, cal.right.distortion_coeffs)[0].reshape(-1, 2)
    return left, right


def synth(rig, cal, rvec=(0.0, 0.0, 0.0), t=T_TRUE, scale=1.0, noise2d=0.0,
          noise3d=0.0, seed=0):
    rotation = cv2.Rodrigues(np.asarray(rvec, dtype=float))[0]
    points = (rotation @ (rig.object_points_m * scale).T).T + np.asarray(t, float)
    rng = np.random.default_rng(seed)
    if noise3d:
        points = points + rng.normal(0.0, noise3d, points.shape)
    left, right = project_both(cal, points)
    if noise2d:
        left = left + rng.normal(0.0, noise2d, left.shape)
        right = right + rng.normal(0.0, noise2d, right.shape)
    return points, left, right, rotation


def candidate(point):
    return {"center_px": [float(point[0]), float(point[1])], "area_px": 25,
            "bbox_px": [int(point[0]) - 2, int(point[1]) - 2, 5, 5]}


@unittest.skipIf(cv2 is None, "OpenCV optional; stereo head pose has no numerical fallback")
@unittest.skipUnless(CAL_PATH.exists() and RIG_PATH.exists(), "canonical configs unavailable")
class StereoHeadPoseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cal, cls.rig = load_default_stereo_and_rig(CAL_PATH, RIG_PATH)

    def assert_safe(self, report):
        self.assertEqual(report["schema"], "psvr.camera.stereo_head_pose")
        self.assertEqual(report["version"], 1)
        for flag in ("udp_published", "position_published", "tracking_activated",
                     "tracking_6dof", "hardware_accessed", "metrological_truth",
                     "physical_scale_validated", "physical_order_verified"):
            self.assertIs(report[flag], False)
        self.assertIs(report["geometry_hypothesis_only"], True)
        self.assertEqual(report["coordinate_frame"], "left_camera")
        self.assertEqual(report["translation_units"], "metres")
        self.assertIn("head_centre", report["pose_origin"])
        self.assertTrue("not_anatomical" in report["pose_origin"]
                        or "not_metrological" in report["pose_origin"])
        self.assertEqual(report["valid"], report["state"] == "pose")
        self.assertEqual(report["pose"] is not None, report["valid"])
        json.dumps(report, allow_nan=False)

    def test_noise_free_exact_pose_with_permuted_inputs(self):
        points, left, right, rotation = synth(self.rig, self.cal, rvec=(0.08, -0.05, 0.03))
        order = [3, 0, 4, 2, 1]
        report = estimate_stereo_head_pose(
            self.cal, self.rig,
            [points[i] for i in order], [left[i] for i in order], [right[i] for i in order])
        self.assert_safe(report)
        self.assertEqual(report["state"], "pose")
        self.assertEqual(report["reason"], "ok")
        pose = report["pose"]
        np.testing.assert_allclose(pose["tvec_left_camera_m"], T_TRUE, atol=1e-6)
        estimated = np.asarray(pose["rotation_matrix_left_camera"])
        self.assertLess(rotation_angle_deg(estimated, rotation), 1e-4)
        np.testing.assert_allclose(pose["head_position_left_camera_m"], T_TRUE, atol=1e-6)
        self.assertEqual(pose["led_labels"], list(self.rig.labels))
        self.assertEqual(len(pose["correspondences"]), 5)
        for row in pose["correspondences"]:
            self.assertEqual(row["led_label"], self.rig.labels[row["led_index"]])
            np.testing.assert_allclose(row["transformed_model_left_m"],
                                       row["triangulated_point_left_m"], atol=1e-6)
        residuals = report["residuals"]
        self.assertLess(residuals["stereo_rms_px"], 1e-6)
        self.assertLess(residuals["rms_3d_m"], 1e-9)
        self.assertLess(residuals["max_reprojection_px"], 1e-6)
        self.assertEqual(report["ransac"]["inlier_count"], 5)
        self.assertEqual(report["ransac"]["outlier_led_indices"], [])
        self.assertFalse(report["ambiguity"]["ambiguous"])
        self.assertIsNone(report["ambiguity"]["runner_up"])
        uncertainty = report["uncertainty"]
        self.assertTrue(uncertainty["accepted"])
        self.assertEqual(uncertainty["rank"], 6)
        self.assertEqual(uncertainty["dof"], 14)
        self.assertEqual(uncertainty["sigma_px"], 0.75)  # floor, zero residuals
        covariance = np.asarray(uncertainty["position_covariance_m2"])
        np.testing.assert_allclose(covariance, covariance.T, atol=1e-15)
        eigenvalues = np.asarray(uncertainty["position_eigenvalues_m2"])
        np.testing.assert_allclose(eigenvalues, np.linalg.eigvalsh(covariance), rtol=1e-9)
        self.assertGreaterEqual(eigenvalues[0], 0.0015 ** 2 - 1e-12)
        self.assertAlmostEqual(uncertainty["worst_position_std_m"], math.sqrt(eigenvalues[-1]))
        np.testing.assert_allclose(uncertainty["position_axis_std_m"],
                                   np.sqrt(np.diag(covariance)), rtol=1e-9)
        self.assertLess(uncertainty["worst_position_std_m"], 0.02)
        self.assertLess(uncertainty["worst_rotation_std_deg"], 6.0)
        self.assertEqual(np.asarray(uncertainty["pose_covariance_6x6"]).shape, (6, 6))

    def test_noisy_pose_recovers_with_finite_uncertainty(self):
        points, left, right, rotation = synth(
            self.rig, self.cal, rvec=(0.14, -0.11, 0.07), noise2d=0.3, noise3d=3e-4, seed=7)
        report = estimate_stereo_head_pose(self.cal, self.rig, points, left, right)
        self.assert_safe(report)
        self.assertEqual(report["state"], "pose")
        pose = report["pose"]
        np.testing.assert_allclose(pose["tvec_left_camera_m"], T_TRUE, atol=0.01)
        self.assertLess(rotation_angle_deg(np.asarray(pose["rotation_matrix_left_camera"]),
                                           rotation), 2.0)
        self.assertLess(report["residuals"]["stereo_rms_px"], 0.8)
        self.assertFalse(report["ambiguity"]["ambiguous"])
        self.assertTrue(report["uncertainty"]["accepted"])
        self.assertGreater(report["uncertainty"]["worst_position_std_m"], 0.0)
        self.assertGreaterEqual(report["uncertainty"]["sigma_px"], 0.75)

    def test_report_deterministic_and_right_camera_pose_consistent(self):
        points, left, right, _ = synth(self.rig, self.cal, rvec=(0.05, 0.04, -0.02))
        first = estimate_stereo_head_pose(self.cal, self.rig, points, left, right)
        second = estimate_stereo_head_pose(self.cal, self.rig, points, left, right)
        for report in (first, second):
            self.assertEqual(report["state"], "pose")
        for key in ("tvec_left_camera_m", "rvec_left_camera_rad",
                    "tvec_right_camera_m", "rvec_right_camera_rad"):
            np.testing.assert_allclose(first["pose"][key], second["pose"][key], atol=1e-12)
        rotation_left = np.asarray(first["pose"]["rotation_matrix_left_camera"])
        rotation_right = cv2.Rodrigues(np.asarray(first["pose"]["rvec_right_camera_rad"]))[0]
        np.testing.assert_allclose(rotation_right, self.cal.R @ rotation_left, atol=1e-9)
        np.testing.assert_allclose(first["pose"]["tvec_right_camera_m"],
                                   self.cal.R @ T_TRUE + self.cal.T, atol=1e-9)

    def test_input_arrays_and_configs_are_never_mutated(self):
        points, left, right, _ = synth(self.rig, self.cal)
        before = (points.copy(), left.copy(), right.copy(), self.rig.object_points_m.copy(),
                  self.cal.to_dict())
        config = StereoHeadPoseConfig()
        report = estimate_stereo_head_pose(self.cal, self.rig, points, left, right, config)
        self.assertEqual(report["state"], "pose")
        np.testing.assert_array_equal(points, before[0])
        np.testing.assert_array_equal(left, before[1])
        np.testing.assert_array_equal(right, before[2])
        np.testing.assert_array_equal(self.rig.object_points_m, before[3])
        self.assertEqual(self.cal.to_dict(), before[4])
        self.assertEqual(config, StereoHeadPoseConfig())

    def test_canonical_configuration_files_are_read_only(self):
        digests = (file_digest(CAL_PATH), file_digest(RIG_PATH))
        points, left, right, _ = synth(self.rig, self.cal)
        report = estimate_stereo_head_pose(self.cal, self.rig, points, left, right)
        self.assertEqual(report["state"], "pose")
        self.assertEqual((file_digest(CAL_PATH), file_digest(RIG_PATH)), digests)
        calibration = load_stereo_calibration(CAL_PATH, expected_image_size=(640, 400),
                                              expected_mode="640x400")
        self.assertEqual(calibration.sample_count, 12)
        self.assertTrue(calibration.validation["accepted"])
        rig = load_front_led_rig(RIG_PATH)
        self.assertEqual(rig.labels, ("upper_left", "upper_right", "centre",
                                      "lower_left", "lower_right"))
        self.assertFalse(rig.physical_scale_validated)

    def test_adapter_consumes_unambiguous_triangulation_report(self):
        points, left, right, _ = synth(self.rig, self.cal, rvec=(0.06, -0.03, 0.02))
        left_candidates = [candidate(p) for p in left]
        right_candidates = [candidate(p) for p in right]
        stereo_report = triangulate_candidates(self.cal, left_candidates, right_candidates,
                                               StereoLedConfig())
        self.assertEqual(stereo_report["state"], "points")
        self.assertEqual(len(stereo_report["matches"]), 5)
        report = estimate_stereo_head_pose_from_triangulation(
            self.cal, self.rig, stereo_report, left_candidates, right_candidates)
        self.assert_safe(report)
        self.assertEqual(report["state"], "pose")
        np.testing.assert_allclose(report["pose"]["tvec_left_camera_m"], T_TRUE, atol=1e-6)

    def test_adapter_fails_closed_on_non_points_states(self):
        points, left, right, _ = synth(self.rig, self.cal)
        left_candidates = [candidate(p) for p in left]
        right_candidates = [candidate(p) for p in right]
        for state, expected in (("ambiguous", "ambiguous"), ("partial", "ambiguous"),
                                ("no_matches", "point_count_mismatch")):
            fake = {"state": state, "matches": []}
            report = estimate_stereo_head_pose_from_triangulation(
                self.cal, self.rig, fake, left_candidates, right_candidates)
            self.assert_safe(report)
            self.assertFalse(report["valid"])
            self.assertEqual(report["state"], expected)
            self.assertEqual(report["reason"], f"triangulation_state_{state}")
            self.assertEqual(report["pose"], None)
        with self.assertRaises(ValueError):
            estimate_stereo_head_pose_from_triangulation(self.cal, self.rig, "x", [], [])
        with self.assertRaises(ValueError):
            estimate_stereo_head_pose_from_triangulation(
                self.cal, self.rig, {"state": "points", "matches": "x"}, [], [])

    def test_outlier_3d_point_shifted_along_ray_is_ransac_rejected(self):
        """One stereo-coherent but off-rig 3D point: gating must never average it in."""
        points, left, right, _ = synth(self.rig, self.cal)
        ray = cv2.undistortPoints(left[0].reshape(1, 1, 2), self.cal.left.camera_matrix,
                                  self.cal.left.distortion_coeffs).reshape(2)
        direction = np.array([ray[0], ray[1], 1.0])
        direction /= np.linalg.norm(direction)
        contaminated = points.copy()
        contaminated[0] = direction * (np.linalg.norm(points[0]) + 0.35)
        # Projections are unchanged: the 2D evidence is perfect, only the 3D point is wrong.
        report = estimate_stereo_head_pose(self.cal, self.rig, contaminated, left, right)
        self.assert_safe(report)
        self.assertFalse(report["valid"])
        self.assertEqual(report["state"], "ransac_outlier")
        self.assertEqual(report["reason"], "ransac_inlier_gate_failed")
        self.assertEqual(report["ransac"]["inlier_count"], 4)
        self.assertEqual(report["ransac"]["outlier_led_indices"], [0])
        self.assertEqual(report["ransac"]["outlier_point_3d_indices"], [0])
        self.assertIsNotNone(report["residuals"])

    def test_outlier_3d_point_with_coherent_2d_is_ransac_rejected(self):
        points, left, right, _ = synth(self.rig, self.cal)
        contaminated = points.copy()
        contaminated[0] += (0.10, -0.05, 0.12)
        report = estimate_stereo_head_pose(self.cal, self.rig, contaminated, left, right)
        self.assert_safe(report)
        self.assertEqual(report["state"], "ransac_outlier")
        self.assertEqual(report["ransac"]["inlier_count"], 4)
        self.assertEqual(report["ransac"]["outlier_led_indices"], [0])

    def test_outlier_2d_left_centroid_is_ransac_rejected(self):
        points, left, right, _ = synth(self.rig, self.cal)
        left_bad = left.copy()
        left_bad[3] += (15.0, -12.0)
        report = estimate_stereo_head_pose(self.cal, self.rig, points, left_bad, right)
        self.assert_safe(report)
        self.assertEqual(report["state"], "ransac_outlier")
        self.assertEqual(report["ransac"]["inlier_count"], 4)
        self.assertEqual(report["ransac"]["outlier_led_indices"], [3])
        self.assertEqual(report["ransac"]["outlier_point_3d_indices"], [3])

    def test_outlier_2d_right_centroid_is_ransac_rejected(self):
        points, left, right, _ = synth(self.rig, self.cal)
        right_bad = right.copy()
        right_bad[1] += (-18.0, 9.0)
        report = estimate_stereo_head_pose(self.cal, self.rig, points, left, right_bad)
        self.assert_safe(report)
        self.assertEqual(report["state"], "ransac_outlier")
        self.assertEqual(report["ransac"]["inlier_count"], 4)
        self.assertEqual(report["ransac"]["outlier_led_indices"], [1])

    def test_globally_shifted_3d_points_conflict_with_2d_centroids(self):
        """2D and 3D evidence mutually inconsistent: reject before any pose solve."""
        points, left, right, _ = synth(self.rig, self.cal)
        shifted = points + np.array([0.0, 0.0, 0.3])
        report = estimate_stereo_head_pose(self.cal, self.rig, shifted, left, right)
        self.assert_safe(report)
        self.assertEqual(report["state"], "triangulation_2d_inconsistent")
        self.assertEqual(report["reason"], "triangulation_reprojection_mismatch")
        self.assertIsNone(report["residuals"])

    def test_off_rig_scaled_geometry_is_rejected_by_residual_gates(self):
        """A 30% scale error is a wrong geometric hypothesis, never a pose."""
        points, left, right, _ = synth(self.rig, self.cal, scale=1.3)
        report = estimate_stereo_head_pose(self.cal, self.rig, points, left, right)
        self.assert_safe(report)
        self.assertEqual(report["state"], "rejected")
        self.assertEqual(report["reason"], "reprojection_rms_exceeded")
        self.assertGreater(report["residuals"]["stereo_rms_px"],
                           StereoHeadPoseConfig().max_rms_reprojection_px)

    def test_duplicate_3d_points_are_rejected_as_ambiguous(self):
        points, left, right, _ = synth(self.rig, self.cal)
        points[3] = points[0]
        left[3] = left[0]
        right[3] = right[0]
        report = estimate_stereo_head_pose(self.cal, self.rig, points, left, right)
        self.assert_safe(report)
        self.assertEqual(report["state"], "ambiguous")
        self.assertEqual(report["reason"], "ambiguous_2d_3d_association")
        self.assertTrue(report["ambiguity"]["ambiguous"])

    def test_duplicate_2d_centroids_are_rejected_as_ambiguous(self):
        points, left, right, _ = synth(self.rig, self.cal)
        left[1] = left[0]
        report = estimate_stereo_head_pose(self.cal, self.rig, points, left, right)
        self.assert_safe(report)
        self.assertEqual(report["state"], "ambiguous")
        self.assertEqual(report["reason"], "ambiguous_2d_3d_association")

    def test_upside_down_pose_fails_only_the_orientation_convention(self):
        """The front rig is nearly roll-180 symmetric: convention, not residuals, rejects."""
        flip = np.diag([1.0, -1.0, -1.0])
        points = (flip @ self.rig.object_points_m.T).T + np.array([0.0, 0.0, 0.9])
        left, right = project_both(self.cal, points)
        report = estimate_stereo_head_pose(self.cal, self.rig, points, left, right)
        self.assert_safe(report)
        self.assertEqual(report["state"], "rejected")
        self.assertEqual(report["reason"], "orientation_gate_failed")
        self.assertEqual(report["ransac"]["inlier_count"], 5)
        self.assertLess(report["residuals"]["stereo_rms_px"], 1e-6)

    def test_relaxed_orientation_exposes_near_flip_ambiguity(self):
        """Without the upright convention the refined rig is flip-ambiguous: fail closed."""
        relaxed = StereoHeadPoseConfig(min_upright_y_cos=-1.0, min_facing_z_cos=-1.0)
        points, left, right, _ = synth(self.rig, self.cal)
        report = estimate_stereo_head_pose(self.cal, self.rig, points, left, right, relaxed)
        self.assert_safe(report)
        self.assertEqual(report["state"], "ambiguous")
        self.assertEqual(report["reason"], "ambiguous_pose_hypotheses")
        self.assertTrue(report["ambiguity"]["ambiguous"])
        self.assertIsNotNone(report["ambiguity"]["runner_up"])
        self.assertLessEqual(report["ambiguity"]["cost_gap"], relaxed.ambiguity_margin + 1e-9)

    def test_relaxed_orientation_on_flipped_data_is_also_ambiguous(self):
        relaxed = StereoHeadPoseConfig(min_upright_y_cos=-1.0, min_facing_z_cos=-1.0)
        flip = np.diag([1.0, -1.0, -1.0])
        points = (flip @ self.rig.object_points_m.T).T + np.array([0.0, 0.0, 0.9])
        left, right = project_both(self.cal, points)
        report = estimate_stereo_head_pose(self.cal, self.rig, points, left, right, relaxed)
        self.assert_safe(report)
        self.assertEqual(report["state"], "ambiguous")

    def test_perfectly_symmetric_rig_is_ambiguous_without_orientation_gate(self):
        symmetric = {
            "schema": "psvr.camera.led_rig", "version": 1,
            "provenance": "synthetic_test_only", "source": "test", "notes": "",
            "labels": ["upper_left", "upper_right", "centre", "lower_left", "lower_right"],
            "object_points_m": [[-0.08, -0.05, 0.025], [0.08, -0.05, 0.025], [0.0, 0.0, 0.0],
                                [-0.08, 0.05, 0.025], [0.08, 0.05, 0.025]],
        }
        rig = front_rig_from_dict(symmetric)
        relaxed = StereoHeadPoseConfig(min_upright_y_cos=-1.0, min_facing_z_cos=-1.0)
        points, left, right, _ = synth(rig, self.cal)
        report = estimate_stereo_head_pose(self.cal, rig, points, left, right, relaxed)
        self.assert_safe(report)
        self.assertEqual(report["state"], "ambiguous")
        self.assertEqual(report["reason"], "ambiguous_pose_hypotheses")

    def test_tight_reprojection_gate_rejects_noisy_pose(self):
        points, left, right, _ = synth(self.rig, self.cal, noise2d=0.3, noise3d=3e-4, seed=7)
        report = estimate_stereo_head_pose(
            self.cal, self.rig, points, left, right,
            StereoHeadPoseConfig(max_rms_reprojection_px=0.1))
        self.assert_safe(report)
        self.assertEqual(report["state"], "rejected")
        self.assertEqual(report["reason"], "reprojection_rms_exceeded")
        self.assertGreater(report["residuals"]["stereo_rms_px"], 0.1)
        self.assertEqual(report["ransac"]["inlier_count"], 5)

    def test_uncertainty_gate_rejects_overconfident_limit(self):
        points, left, right, _ = synth(self.rig, self.cal)
        report = estimate_stereo_head_pose(
            self.cal, self.rig, points, left, right,
            StereoHeadPoseConfig(max_position_std_m=0.001))
        self.assert_safe(report)
        self.assertEqual(report["state"], "rejected")
        self.assertEqual(report["reason"], "excessive_uncertainty")
        self.assertFalse(report["uncertainty"]["accepted"])
        self.assertGreater(report["uncertainty"]["worst_position_std_m"], 0.001)

    def test_head_offset_model_shifts_origin_and_covariance(self):
        points, left, right, _ = synth(self.rig, self.cal)
        base = estimate_stereo_head_pose(self.cal, self.rig, points, left, right)
        offset = StereoHeadPoseConfig(head_offset_model_m=(0.0, 0.0, 0.1))
        moved = estimate_stereo_head_pose(self.cal, self.rig, points, left, right, offset)
        self.assertEqual(moved["state"], "pose")
        np.testing.assert_allclose(moved["pose"]["head_position_left_camera_m"],
                                   T_TRUE + np.array([0.0, 0.0, 0.1]), atol=1e-6)
        self.assertEqual(moved["pose"]["head_offset_model_m"], [0.0, 0.0, 0.1])
        self.assertIn("model_offset", moved["pose_origin"])
        self.assertFalse(np.allclose(moved["uncertainty"]["position_covariance_m2"],
                                     base["uncertainty"]["position_covariance_m2"]))
        self.assert_safe(moved)

    def test_point_count_mismatch_is_fail_closed(self):
        points, left, right, _ = synth(self.rig, self.cal)
        six_points = np.vstack((points, points[:1]))
        six_left = np.vstack((left, left[:1]))
        for args in ((points[:4], left[:4], right[:4]), (points, left[:4], right),
                     (six_points, six_left, right), ([], [], [])):
            report = estimate_stereo_head_pose(self.cal, self.rig, *args)
            self.assert_safe(report)
            self.assertEqual(report["state"], "point_count_mismatch")
            self.assertFalse(report["valid"])
        report = estimate_stereo_head_pose(self.cal, self.rig, points[:4], left[:4], right[:4])
        self.assertEqual(report["input_counts"], {"points_3d": 4, "left_centroids_2d": 4,
                                                  "right_centroids_2d": 4})

    def test_small_span_degenerate_depth_distance_and_budget_gates(self):
        points, left, right, _ = synth(self.rig, self.cal)
        tiny = np.array([[10., 10.], [11., 10.], [10., 11.], [11., 11.], [10.5, 10.5]])
        cases = []
        cases.append((estimate_stereo_head_pose(self.cal, self.rig, points, tiny, tiny),
                      "rejected", "small_span"))
        line = np.array([[-0.10, 0.0, 0.90], [-0.05, 0.0, 0.90], [0.0, 0.0, 0.90],
                         [0.05, 0.0, 0.90], [0.10, 0.0, 0.90]])
        line_left, line_right = project_both(self.cal, line)
        cases.append((estimate_stereo_head_pose(self.cal, self.rig, line, line_left, line_right),
                      "rejected", "degenerate_geometry"))
        behind = points.copy()
        behind[:, 2] = -behind[:, 2]
        cases.append((estimate_stereo_head_pose(self.cal, self.rig, behind, left, right),
                      "rejected", "nonpositive_depth"))
        far = points.copy()
        far[:, 2] += 10.0
        cases.append((estimate_stereo_head_pose(self.cal, self.rig, far, left, right),
                      "rejected", "distance_gate_failed"))
        cases.append((estimate_stereo_head_pose(self.cal, self.rig, points, left, right,
                                                StereoHeadPoseConfig(hypothesis_budget=1)),
                      "hypothesis_limit", "hypothesis_budget_exceeded"))
        for report, state, reason in cases:
            with self.subTest(reason=reason):
                self.assert_safe(report)
                self.assertEqual(report["state"], state)
                self.assertEqual(report["reason"], reason)
                self.assertFalse(report["valid"])

    def test_opencv_unavailable_is_reported_not_raised(self):
        points, left, right, _ = synth(self.rig, self.cal)
        with patch.object(head_pose, "import_cv2",
                          side_effect=OpenCvUnavailable("synthetic")):
            report = estimate_stereo_head_pose(self.cal, self.rig, points, left, right)
        self.assert_safe(report)
        self.assertFalse(report["valid"])
        self.assertEqual(report["reason"], "opencv_unavailable")

    def test_invalid_inputs_raise_value_error(self):
        points, left, right, _ = synth(self.rig, self.cal)
        bad_points = (
            np.full((5, 3), np.nan), np.zeros((5, 2)), np.zeros((5, 3, 1)),
            [["a", "b", "c"]] * 5, [[0, 0, float("inf")]] * 5, [{"point": [0, 0, 1]}] * 5,
            "not-a-list", 42,
        )
        for bad in bad_points:
            with self.subTest(bad=type(bad)), self.assertRaises(ValueError):
                estimate_stereo_head_pose(self.cal, self.rig, bad, left, right)
        bad_centroids = (
            np.full((5, 2), np.nan), np.zeros((5, 3)),
            [[640.0, 200.0]] * 5, [[-1.0, 200.0]] * 5, [[True, False]] * 5,
            [{"centroid": [1, 2]}] * 5, None,
        )
        for bad in bad_centroids:
            with self.subTest(bad=type(bad)), self.assertRaises(ValueError):
                estimate_stereo_head_pose(self.cal, self.rig, points, bad, right)
            with self.assertRaises(ValueError):
                estimate_stereo_head_pose(self.cal, self.rig, points, left, bad)
        with self.assertRaises(ValueError):
            estimate_stereo_head_pose(self.cal, self.rig, points, left, right, config={})
        with self.assertRaises(ValueError):
            estimate_stereo_head_pose({}, self.rig, points, left, right)
        with self.assertRaises(ValueError):
            estimate_stereo_head_pose(self.cal, 42, points, left, right)

    def test_mutated_calibration_fails_closed(self):
        points, left, right, _ = synth(self.rig, self.cal)
        mutated = copy.deepcopy(self.cal)
        mutated.T[1] += 0.01
        with self.assertRaises(CalibrationError):
            estimate_stereo_head_pose(mutated, self.rig, points, left, right)

    def test_kabsch_returns_proper_rotation_even_for_reflected_targets(self):
        model = self.rig.object_points_m
        rotation = cv2.Rodrigues(np.array([0.2, -0.1, 0.3]))[0]
        target = (rotation @ model.T).T + np.array([0.1, 0.2, 0.9])
        fitted_r, fitted_t, rms = head_pose._kabsch_rigid_transform(model, target)
        np.testing.assert_allclose(fitted_r, rotation, atol=1e-10)
        np.testing.assert_allclose(fitted_t, [0.1, 0.2, 0.9], atol=1e-10)
        self.assertLess(rms, 1e-10)
        reflected = (np.diag([-1.0, 1.0, 1.0]) @ target.T).T
        fitted_r, _, reflected_rms = head_pose._kabsch_rigid_transform(model, reflected)
        self.assertAlmostEqual(float(np.linalg.det(fitted_r)), 1.0, places=10)
        self.assertGreater(reflected_rms, 0.01)


class StereoHeadPoseConfigTest(unittest.TestCase):
    def test_invalid_configuration_values(self):
        bad = (
            {"max_triangulation_reprojection_px": 0}, {"max_epipolar_px": -1},
            {"min_ray_angle_deg": 0}, {"min_ray_angle_deg": 90},
            {"min_distance_m": 0}, {"max_distance_m": 0.1},
            {"min_span_px": 0}, {"ransac_reprojection_px": float("nan")},
            {"ransac_3d_threshold_m": True}, {"ransac_iterations": 0},
            {"ransac_confidence": 1.0}, {"min_inliers": 4},
            {"max_rms_reprojection_px": 0}, {"max_point_reprojection_px": 1.0},
            {"max_rms_3d_residual_m": 0.02, "max_point_3d_residual_m": 0.01},
            {"min_upright_y_cos": 2.0}, {"min_facing_z_cos": -1.5},
            {"ambiguity_margin": float("inf")}, {"pixel_sigma_floor_px": 0},
            {"position_sigma_floor_m": -1}, {"max_information_condition": 1.0},
            {"max_position_std_m": 0}, {"max_rotation_std_deg": float("nan")},
            {"head_offset_model_m": (0.0, 0.0)}, {"head_offset_model_m": (0.0, 0.0, "x")},
            {"hypothesis_budget": 0}, {"hypothesis_budget": 1.5},
        )
        for kwargs in bad:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                StereoHeadPoseConfig(**kwargs)

    def test_defaults_are_sane_and_frozen(self):
        config = StereoHeadPoseConfig()
        self.assertEqual(config.min_inliers, 5)
        self.assertEqual(config.head_offset_model_m, (0.0, 0.0, 0.0))
        with self.assertRaises(Exception):
            config.max_rms_reprojection_px = 99.0


class FrontLedRigTest(unittest.TestCase):
    def valid_payload(self):
        return {
            "schema": "psvr.camera.led_rig", "version": 1,
            "provenance": "synthetic_test_only", "source": "test", "notes": "",
            "labels": ["a", "b", "c", "d", "e"],
            "object_points_m": [[-0.08, -0.05, 0.025], [0.08, -0.05, 0.025], [0.0, 0.0, 0.0],
                                [-0.08, 0.05, 0.025], [0.08, 0.05, 0.025]],
        }

    def test_roundtrip_and_immutability(self):
        rig = front_rig_from_dict(self.valid_payload())
        again = front_rig_from_dict(rig.to_dict())
        np.testing.assert_array_equal(rig.object_points_m, again.object_points_m)
        self.assertEqual(rig.labels, again.labels)
        with self.assertRaises(Exception):
            rig.object_points_m[0, 0] = 1.0

    def test_invalid_rigs_are_rejected(self):
        for mutate in (
            lambda d: d.update(schema="other"),
            lambda d: d.update(version=2),
            lambda d: d.update(labels=["a", "a", "c", "d", "e"]),
            lambda d: d.update(labels=["a", "b", "c", "d"]),
            lambda d: d.update(object_points_m=[[0.0, 0.0, 0.0]] * 5),
            lambda d: d.update(object_points_m=[[x, y, 0.0] for x, y, _ in
                                                d["object_points_m"]]),
            lambda d: d.update(object_points_m=[d["object_points_m"][0]] * 5),
            lambda d: d.update(provenance=""),
            lambda d: d.update(source=""),
            lambda d: d.update(object_points_m=[[0.0, 0.0, float("nan")]] * 5),
        ):
            payload = self.valid_payload()
            mutate(payload)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                front_rig_from_dict(payload)
        with self.assertRaises(ValueError):
            front_rig_from_dict(["not", "a", "dict"])
        payload = self.valid_payload()
        payload["refinement"] = {"physical_scale_validated": "yes"}
        with self.assertRaises(ValueError):
            front_rig_from_dict(payload)

    @unittest.skipUnless(RIG_PATH.exists(), "canonical rig unavailable")
    def test_canonical_rig_loads_as_hypothesis_not_truth(self):
        rig = load_front_led_rig(RIG_PATH)
        self.assertFalse(rig.physical_scale_validated)
        self.assertIn("approximate", rig.notes)
        self.assertEqual(rig.object_points_m.shape, (5, 3))
        # load_led_rig-compatible tuple is accepted but points must agree.
        from psvr_camera.led_pose import load_led_rig
        from psvr_camera.stereo_head_pose import _coerce_rig
        data, points = load_led_rig(RIG_PATH)
        coerced = _coerce_rig((data, points))
        np.testing.assert_array_equal(coerced.object_points_m, rig.object_points_m)
        with self.assertRaises(ValueError):
            _coerce_rig((data, points + 0.01))


if __name__ == "__main__":
    unittest.main()
