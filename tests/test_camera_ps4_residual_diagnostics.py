"""Fake-only rejection/shadow diagnostics: no camera, driver, sockets or OpenCV."""
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from tools.psvr_camera import ps4_suite_tracker as module
from tools.psvr_camera.imu_led_pose import MODEL_TO_IMU


class FakeCv:
    @staticmethod
    def projectPoints(points, *_):
        return (np.asarray(points)[:, :2] * 100. + [320., 200.]).reshape(-1, 1, 2), None

    @staticmethod
    def Rodrigues(value):
        return (np.zeros((3, 1)) if np.asarray(value).shape == (3, 3) else np.eye(3)), None


class DeferredThread:
    """Deterministic worker: start never executes the solver on the caller."""
    workers = None

    def __init__(self, *, target, args, name, daemon):
        self.target, self.args = target, args
        assert daemon and name == "ps4-optical-diagnostic"
        self.workers.append(self)

    def start(self):
        pass

    def complete(self):
        self.target(*self.args)


class ResidualDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.workers = []
        DeferredThread.workers = self.workers
        for name, value in (("import_cv2", Mock(return_value=FakeCv())),
                            ("triangulate_candidates", Mock(side_effect=lambda *_: copy.deepcopy(self.tri))),
                            ("estimate_pose_covariance", Mock(return_value=(None, {"accepted": True}))),
                            ("estimate_stereo_head_pose_from_triangulation", Mock()),
                            ("threading.Thread", DeferredThread)):
            p = patch.object(module, name, value) if "." not in name else patch(
                "tools.psvr_camera.ps4_suite_tracker." + name, value)
            p.start()
            self.addCleanup(p.stop)
        points = np.array([[-.08, -.05, .02], [.08, -.05, .02], [0., 0., 0.],
                           [-.08, .05, .02], [.08, .05, .02]])
        eye = SimpleNamespace(camera_matrix=np.eye(3), distortion_coeffs=np.zeros(5))
        calibration = SimpleNamespace(R=np.eye(3), T=np.array([.08, 0., 0.]), left=eye, right=eye)
        rig = SimpleNamespace(object_points_m=points, labels=list("abcde"))
        self.tracker = module.Ps4SuiteTracker(calibration, rig)
        self.bind(10.)
        self.tracker.alignment = MODEL_TO_IMU.copy()
        self.tracker._alignment_at = 9.
        self.tracker.translation = np.array([0., 0., 1.])
        self.tracker.last_match = 9.99
        cloud = points + [0., 0., 1.]
        self.left = [{"center_px": p.tolist()} for p in FakeCv.projectPoints(cloud)[0].reshape(5, 2)]
        self.right = [{"center_px": p.tolist()} for p in FakeCv.projectPoints(cloud + calibration.T)[0].reshape(5, 2)]
        self.tri = {"state": "points", "matches": [dict(left_index=i, right_index=i,
                    point_left_camera_m=p.tolist()) for i, p in enumerate(cloud)]}
        self.bad_left = copy.deepcopy(self.left)
        for item in self.bad_left:
            item["center_px"][0] += 8.
        self.solver = module.estimate_stereo_head_pose_from_triangulation

    def bind(self, now):
        self.tracker.set_imu(dict(session_id="fake", recenter_epoch=1, valid=True,
                                 monotonic_s=now - .01, quaternion_sensor_wxyz=[1., 0., 0., 0.]))

    def reject(self, now=10.):
        self.bind(now)
        pose = self.tracker.update_stereo(self.bad_left, self.right, now)
        self.assertFalse(pose.valid)
        self.assertEqual(pose.reason, "ps4_stereo_guided_residual_gate")
        return self.tracker.diagnostics()

    def optical_report(self, valid=True):
        angle = np.deg2rad(20.)
        rotation = [[np.cos(angle), -np.sin(angle), 0.],
                    [np.sin(angle), np.cos(angle), 0.], [0., 0., 1.]]
        return dict(valid=valid, reason="ok" if valid else "ransac_inlier_gate_failed",
                    state="pose" if valid else "ransac_outlier", ransac={"inlier_count": 5 if valid else 4},
                    residuals={"stereo_rms_px": .5, "rms_3d_m": .002,
                               "per_led_left_px": [.5] * 5, "per_led_right_px": [.5] * 5,
                               "per_led_3d_m": [.002] * 5},
                    pose=dict(rotation_matrix_left_camera=rotation,
                              correspondences=[{"point_3d_index": i} for i in range(5)]) if valid else None)

    def test_rejection_records_all_residuals_gates_and_ages_without_solving_inline(self):
        before = copy.deepcopy((self.tracker.alignment, self.tracker.translation, self.tracker.last_match,
                                self.tri, self.bad_left, self.right))
        diagnostic = self.reject()
        residual = diagnostic["guided_residual_rejection"]
        self.assertEqual(residual["best_permutation"], list(range(5)))
        self.assertEqual(residual["led_labels"], list("abcde"))
        np.testing.assert_allclose(residual["per_led_left_px"], [8.] * 5)
        np.testing.assert_allclose(residual["per_led_right_px"], np.zeros(5))
        np.testing.assert_allclose(residual["per_led_3d_m"], np.zeros(5), atol=1e-15)
        self.assertAlmostEqual(residual["stereo_rms_px"], np.sqrt(32.))
        self.assertEqual((residual["left_rms_px"], residual["right_rms_px"]), (8., 0.))
        self.assertEqual((residual["left_max_px"], residual["right_max_px"], residual["max_reprojection_px"]), (8., 0., 8.))
        self.assertEqual(residual["gates"], dict(rms_3d=True, max_3d=True, rms_2d=False, max_2d=False))
        self.assertAlmostEqual(residual["imu_age_s"], .01)
        self.assertAlmostEqual(residual["alignment_age_s"], 1.)
        self.assertEqual(diagnostic["free_optical_comparison"]["state"], "pending")
        self.solver.assert_not_called()
        np.testing.assert_array_equal(self.tracker.alignment, before[0])
        np.testing.assert_array_equal(self.tracker.translation, before[1])
        self.assertEqual((self.tracker.last_match, self.tri, self.bad_left, self.right), before[2:])
        json.dumps(diagnostic, allow_nan=False)

    def test_successful_shadow_only_logs_delta_and_cannot_change_next_published_pose(self):
        self.reject()
        self.solver.return_value = self.optical_report()
        self.workers[0].complete()
        diagnostic = self.tracker.diagnostics()["free_optical_comparison"]
        self.assertEqual(diagnostic["state"], "complete")
        self.assertTrue(diagnostic["valid"])
        self.assertFalse(diagnostic["published"])
        self.assertFalse(diagnostic["diagnostic_only"])
        self.assertEqual(diagnostic["use"], "validated_bounded_matching_correction_only")
        self.assertAlmostEqual(diagnostic["delta_rotation_deg"], 20.)
        self.assertEqual(diagnostic["best_permutation"], list(range(5)))
        self.assertEqual(diagnostic["sample_monotonic_s"], 10.)
        self.assertEqual(diagnostic["ransac_inlier_count"], 5)
        self.assertGreaterEqual(diagnostic["duration_ms"], 0.)
        self.bind(10.02)
        pose = self.tracker.update_stereo(self.left, self.right, 10.02)
        self.assertTrue(pose.valid)
        self.assertEqual(pose.reason, "ps4_stereo_imu_guided")
        np.testing.assert_allclose(pose.tvec.reshape(3), [0., 0., 1.])
        np.testing.assert_array_equal(self.tracker.alignment, MODEL_TO_IMU)
        np.testing.assert_array_equal(pose.rvec.reshape(3), np.zeros(3))
        self.assertEqual(len(self.workers), 1)
        json.dumps(self.tracker.diagnostics(), allow_nan=False)

    def test_failed_optical_solution_keeps_residuals_and_four_inliers_without_pose(self):
        self.reject()
        self.solver.return_value = self.optical_report(valid=False)
        self.workers[0].complete()
        diagnostic = self.tracker.diagnostics()["free_optical_comparison"]
        self.assertFalse(diagnostic["valid"])
        self.assertEqual(diagnostic["ransac_inlier_count"], 4)
        self.assertEqual(diagnostic["residuals"]["stereo_rms_px"], .5)
        self.assertIsNone(diagnostic["delta_rotation_deg"])
        self.assertIsNone(diagnostic["best_permutation"])
        self.assertEqual(self.tracker.last_match, 9.99)

    def test_shadow_exception_is_logged_and_never_promotes_rejected_frame(self):
        self.reject()
        self.solver.side_effect = RuntimeError("fake diagnostic failure")
        self.workers[0].complete()
        diagnostic = self.tracker.diagnostics()["free_optical_comparison"]
        self.assertEqual((diagnostic["state"], diagnostic["error_type"]), ("error", "RuntimeError"))
        self.assertEqual(self.reject(10.1)["reason"], "ps4_stereo_guided_residual_gate")
        self.assertEqual(self.tracker.last_match, 9.99)

    def test_sampling_is_throttled_and_has_no_backlog(self):
        self.reject()
        self.assertEqual(self.reject(14.)["free_optical_sampling"], "busy")
        self.assertEqual(len(self.workers), 1)
        self.solver.return_value = self.optical_report(False)
        self.workers[0].complete()
        self.assertEqual(self.reject(14.99)["free_optical_sampling"], "throttled")
        self.assertEqual(self.reject(15.)["free_optical_sampling"], "sampled")
        self.assertEqual(len(self.workers), 2)

    def test_reset_discards_old_result_but_preserves_busy_worker_slot(self):
        self.reject()
        self.tracker.reset()
        self.assertIsNone(self.tracker._alignment_at)
        self.assertNotIn("free_optical_comparison", self.tracker.diagnostics())
        self.bind(16.)
        self.tracker.alignment = MODEL_TO_IMU.copy()
        self.assertEqual(self.reject(16.)["free_optical_sampling"], "busy")
        self.assertEqual(len(self.workers), 1)
        self.solver.return_value = self.optical_report()
        self.workers[0].complete()
        self.assertNotIn("free_optical_comparison", self.tracker.diagnostics())
        self.assertEqual(self.reject(16.1)["free_optical_sampling"], "sampled")
        self.assertIsNone(self.tracker.diagnostics()["guided_residual_rejection"]["alignment_age_s"])

    def test_inputs_and_nested_log_snapshots_are_detached_and_indices_remapped(self):
        self.bad_left.insert(0, {"center_px": [1., 1.]})
        self.right.append({"center_px": [600., 390.]})
        for match in self.tri["matches"]:
            match["left_index"] += 1
        original = copy.deepcopy((self.bad_left, self.right, self.tri))
        self.reject()

        def mutate_inputs(calibration, rig, tri, left, right, *, config):
            self.assertEqual(len(left), 5)
            self.assertEqual(len(right), 5)
            self.assertEqual([m["left_index"] for m in tri["matches"]], list(range(5)))
            self.assertEqual([m["right_index"] for m in tri["matches"]], list(range(5)))
            self.assertEqual(left, original[0][1:])
            self.assertEqual(right, original[1][:5])
            calibration.R[:] = 0.
            rig.object_points_m[:] = 0.
            left[0]["center_px"][0] = -999.
            tri["matches"][0]["point_left_camera_m"][0] = -999.
            return self.optical_report()

        self.solver.side_effect = mutate_inputs
        self.workers[0].complete()
        self.assertEqual(self.tracker.diagnostics()["free_optical_comparison"]["state"], "complete")
        self.assertEqual((self.bad_left, self.right, self.tri), original)
        np.testing.assert_array_equal(self.tracker.calibration.R, np.eye(3))
        self.assertGreater(np.linalg.norm(self.tracker.points), 0.)
        diagnostic = self.tracker.diagnostics()
        diagnostic["guided_residual_rejection"]["best_permutation"][0] = 99
        diagnostic["free_optical_comparison"]["residuals"]["per_led_3d_m"][0] = 99
        fresh = self.tracker.diagnostics()
        self.assertEqual(fresh["guided_residual_rejection"]["best_permutation"][0], 0)
        self.assertEqual(fresh["free_optical_comparison"]["residuals"]["per_led_3d_m"][0], .002)
        self.assertEqual(fresh["free_optical_comparison"]["guided"]["matched_candidate_indices"],
                         [[i + 1, i] for i in range(5)])

    def test_each_gate_bit_and_normalized_ranking_are_independent(self):
        cfg = self.tracker.config
        for field, value, failed in (("3d", .016, ("rms_3d",)),
                                     ("3d_one", .026, ("max_3d",)),
                                     ("2d", 1.6, ("rms_2d",)),
                                     ("2d_one", 2.6, ("max_2d",))):
            with self.subTest(field=field):
                e3d, el, er = np.full((120, 5), 10.), np.full((120, 5), 100.), np.full((120, 5), 100.)
                e3d[7], el[7], er[7] = 0., 0., 0.
                if field == "3d":
                    e3d[7] = value
                elif field == "3d_one":
                    e3d[7, 0] = value
                elif field == "2d":
                    el[7], er[7] = value, value
                else:
                    el[7, 0] = value
                rms3d = np.sqrt(np.mean(e3d ** 2, axis=1))
                rms2d = np.sqrt(np.mean((el ** 2 + er ** 2) / 2., axis=1))
                # A distractor has lower 2D RMS but a much worse normalized 3D error.
                el[0], er[0], rms2d[0], e3d[0], rms3d[0] = 0., 0., 0., .1, .1
                self.tracker._diagnose_guided_rejection(
                    self.tri, self.bad_left, self.right, np.eye(3), 10., e3d, el, er, rms3d, rms2d)
                diagnostic = self.tracker.diagnostics()["guided_residual_rejection"]
                self.assertEqual(diagnostic["best_permutation"], self.tracker._permutations[7].tolist())
                self.assertEqual(tuple(k for k, passed in diagnostic["gates"].items() if not passed), failed)
        self.assertEqual(self.tracker.config, cfg)

    def test_diagnostic_preparation_failure_does_not_change_rejection(self):
        with patch.object(self.tracker, "_diagnose_guided_rejection", side_effect=ValueError("fake")):
            diagnostic = self.reject()
        self.assertEqual(diagnostic["guided_residual_diagnostic_error"], "ValueError")
        self.assertEqual(self.tracker.last_match, 9.99)

    def test_worker_start_failure_does_not_change_rejection_or_leave_slot_busy(self):
        with patch("tools.psvr_camera.ps4_suite_tracker.threading.Thread", side_effect=RuntimeError("fake")):
            diagnostic = self.reject()
        self.assertEqual(diagnostic["free_optical_comparison"]["state"], "error")
        self.assertEqual(self.reject(15.)["free_optical_sampling"], "sampled")

    def test_valid_guided_frame_never_launches_shadow_solver(self):
        pose = self.tracker.update_stereo(self.left, self.right, 10.)
        self.assertTrue(pose.valid)
        self.assertEqual(self.workers, [])
        self.solver.assert_not_called()
        self.assertNotIn("guided_residual_rejection", self.tracker.diagnostics())

    def test_alignment_age_starts_only_after_accepted_initial_pose(self):
        self.tracker.reset()
        self.bind(20.)
        report = self.optical_report()
        report["pose"]["rotation_matrix_left_camera"] = np.eye(3).tolist()
        report["pose"]["tvec_left_camera_m"] = [0., 0., 1.]
        for correspondence, left in zip(report["pose"]["correspondences"], self.left):
            correspondence["left_centroid_px"] = left["center_px"]
        self.solver.return_value = report
        self.assertTrue(self.tracker.update_stereo(self.left, self.right, 20.).valid)
        self.assertEqual(self.tracker._alignment_at, 20.)
        self.assertAlmostEqual(self.reject(20.5)["guided_residual_rejection"]["alignment_age_s"], .5)


if __name__ == "__main__":
    unittest.main()
