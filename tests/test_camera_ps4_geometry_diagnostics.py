"""Recorded PS4 centroids and synthetic stereo clutter; no live camera/IMU."""
import copy
import hashlib
import json
from pathlib import Path
import unittest

import numpy as np

from tools.psvr_camera.ps4_geometry_diagnostics import diagnose_pair
from tools.psvr_camera.ps4_suite_source import load_suite_profile
from tools.psvr_camera.stereo_leds import triangulate_candidates
from tools.psvr_camera.stereo_head_pose import estimate_stereo_head_pose_from_triangulation
from tools.psvr_camera.ps4_front5_selection import unique_front5_subset
from tests.test_camera_ps4_suite_source import new_tracker, synthetic, imu, cv2

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/ps4_front5_unmatched_2d_20261101.json"


@unittest.skipIf(cv2 is None, "requires optional OpenCV")
class Ps4GeometryDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tracker = new_tracker()
        self.left, self.right, _ = synthetic(self.tracker)

    def test_exact_synthetic_geometry_diagnostics_agree_with_production(self):
        report = diagnose_pair(self.tracker.calibration, self.left, self.right)
        self.assertEqual(len(report["edges"]), 25)
        self.assertEqual(report["input_space"], "raw_distorted_pixels")
        self.assertEqual(report["epipolar_space"], "undistorted_pixels")
        result = report["assignment_result"]
        self.assertEqual(len(result["matches"]), 5)
        for match in result["matches"]:
            edge = next(e for e in report["edges"] if e["left_index"] == match["left_index"]
                        and e["right_index"] == match["right_index"])
            self.assertTrue(edge["eligible"])
            self.assertTrue(edge["selected"])
            self.assertIsNone(edge["rejection"])
            self.assertAlmostEqual(edge["epipolar_px"], match["epipolar_px"], places=9)
            self.assertLess(edge["epipolar_px"], 1e-6)
        self.assertTrue(any(e["rejection"] == "epipolar" for e in report["edges"]))

    def test_unmatched_clutter_keeps_all_existing_pose_gates(self):
        left = [{"center_px": [10., 10.]}] + self.left + [{"center_px": [500., 380.]}]
        right = self.right + [{"center_px": [10., 370.]}, {"center_px": [500., 20.]}]
        self.tracker.set_imu(imu(10.))  # offline synthetic IMU, never live publication
        original = copy.deepcopy((left, right))
        pose = self.tracker.update_stereo(left, right, 10.)
        self.assertTrue(pose.valid, pose.reason)
        self.assertEqual(self.tracker.diagnostics()["stereo_matches"], 5)
        self.assertEqual(self.tracker.diagnostics()["unmatched_2d_counts"], {"left": 2, "right": 2})
        self.assertEqual((left, right), original)
        self.tracker.set_imu(imu(10.02))
        self.assertTrue(self.tracker.update_stereo(left[::-1], right[::-1], 10.02).valid)

    def test_ambiguous_extra_blob_still_rejected(self):
        self.tracker.set_imu(imu(10.))
        pose = self.tracker.update_stereo(self.left + [copy.deepcopy(self.left[0])], self.right, 10.)
        self.assertFalse(pose.valid)
        self.assertIn(self.tracker.diagnostics()["stereo_state"], ("partial", "ambiguous"))
        self.assertIsNone(self.tracker.alignment)

    def test_unique_rigid_front5_can_exclude_one_far_stereo_clutter_point(self):
        c = self.tracker.calibration
        point = np.array([[.2, -.25, 1.5]])
        other = (c.R @ point.T).T + c.T
        project = lambda pts, intr: cv2.projectPoints(pts, np.zeros(3), np.zeros(3),
            intr.camera_matrix, intr.distortion_coeffs)[0].reshape(-1, 2)[0].tolist()
        left = self.left + [{"center_px": project(point, c.left)}]
        right = self.right + [{"center_px": project(other, c.right)}]
        self.tracker.set_imu(imu(10.))
        pose = self.tracker.update_stereo(left, right, 10.)
        self.assertTrue(pose.valid, pose.reason)
        self.assertEqual(self.tracker.diagnostics()["stereo_matches"], 6)
        self.assertEqual(self.tracker.diagnostics()["front5_distance_subsets"], 1)
        self.assertEqual(self.tracker.diagnostics()["selected_stereo_matches"], 5)

    def test_two_distance_compatible_constellations_are_not_ranked_for_success(self):
        model = self.tracker.points
        clouds = np.concatenate((model + [0, 0, .8], model + [.5, .3, 1.5]))
        matches = [{"point_left_camera_m": p.tolist()} for p in clouds]
        selection, count = unique_front5_subset(matches, model, self.tracker.config.ransac_3d_threshold_m)
        self.assertIsNone(selection)
        self.assertGreaterEqual(count, 2)

    def test_distance_signature_is_only_a_prefilter_not_pose_acceptance(self):
        model = self.tracker.points
        matches = [{"point_left_camera_m": p.tolist()} for p in np.concatenate((model, [[3., 4., 5.]]))]
        indices, count = unique_front5_subset(matches, model, .015)
        self.assertEqual(count, 1)
        self.assertEqual(indices.tolist(), [0, 1, 2, 3, 4])
        with self.assertRaises(ValueError):
            unique_front5_subset(matches, model, 0.)
        self.assertEqual(unique_front5_subset(matches * 3, model, .015), (None, 0))

    def test_real_recorded_unmatched_blobs_do_not_invalidate_five_certified_leds(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        p = load_suite_profile(ROOT)
        self.assertEqual(p["calibration_sha256"], fixture["calibration_sha256"])
        self.assertEqual(p["eye_order"], fixture["eye_order"])
        left, right = fixture["left"], fixture["right"]
        report = diagnose_pair(self.tracker.calibration, left, right)
        tri = report["assignment_result"]
        self.assertEqual(tri["state"], "points")
        self.assertEqual([[m["left_index"], m["right_index"]] for m in tri["matches"]],
                         fixture["expected_match_indices"])
        self.assertLess(max(m["epipolar_px"] for m in tri["matches"]), 1.)
        self.tracker.set_imu(imu(10.))  # offline only; live tests use real driver IMU
        pose = self.tracker.update_stereo(left, right, 10.)
        self.assertTrue(pose.valid, pose.reason)
        self.assertEqual(pose.reason, "ps4_stereo_initial_alignment")
        self.assertEqual(self.tracker.diagnostics()["matched_candidate_indices"],
                         fixture["expected_match_indices"])
        self.assertAlmostEqual(pose.rms_px, fixture["expected_stereo_rms_px"], places=5)

    def test_real_recorded_stereo_clutter_requires_one_rigid_constellation(self):
        fixture = json.loads((FIXTURE.parent / "ps4_front5_stereo_clutter_20261101.json").read_text(encoding="utf-8"))
        left, right = fixture["left"], fixture["right"]
        tri = triangulate_candidates(self.tracker.calibration, left, right)
        self.assertEqual(tri["state"], "points")
        self.assertEqual(len(tri["matches"]), fixture["expected_stereo_matches"])
        indices, count = unique_front5_subset(tri["matches"], self.tracker.points,
                                             self.tracker.config.ransac_3d_threshold_m)
        self.assertEqual(count, 1)
        self.assertEqual(indices.tolist(), fixture["expected_selected_match_indices"])
        self.tracker.set_imu(imu(10.))
        pose = self.tracker.update_stereo(left, right, 10.)
        self.assertTrue(pose.valid, pose.reason)
        self.assertEqual(self.tracker.diagnostics()["matched_candidate_indices"], fixture["expected_raw_match_indices"])
        self.assertAlmostEqual(pose.rms_px, fixture["expected_stereo_rms_px"], places=5)

    def test_profile_controls_reject_booleans_and_out_of_native_bounds(self):
        from tools.psvr_camera.ps4_suite_source import _profile_controls_valid
        self.assertTrue(_profile_controls_valid({"exposure": -7, "gain": 8, "brightness": 1}))
        self.assertTrue(_profile_controls_valid({"exposure": None, "gain": None, "brightness": 0}))
        for invalid in ({"brightness": True}, {"gain": 9}, {"exposure": -14},
                        {"brightness": None}, {"exposure": "-7"}):
            self.assertFalse(_profile_controls_valid({"exposure": -7, "gain": 8, "brightness": 1} | invalid))
