"""Fake-only bounded PS4 optical correction; no hardware, sockets or OpenCV."""
import copy
import json
import threading
import unittest

import numpy as np

from tests import test_camera_ps4_residual_diagnostics as diagnostic_fakes
from tools.psvr_camera import ps4_suite_tracker as module
from tools.psvr_camera.imu_led_pose import MODEL_TO_IMU


def rotation_vector(matrix):
    matrix = np.asarray(matrix)
    axis = np.array([matrix[2, 1] - matrix[1, 2], matrix[0, 2] - matrix[2, 0],
                     matrix[1, 0] - matrix[0, 1]]) * .5
    sine = np.linalg.norm(axis)
    if sine < 1e-12:
        return axis
    angle = np.arctan2(sine, (np.trace(matrix) - 1.) * .5)
    return axis * (angle / sine)


def rotation(vector):
    vector = np.asarray(vector, float).reshape(3)
    angle = np.linalg.norm(vector)
    if angle < 1e-12:
        return np.eye(3)
    x, y, z = vector / angle
    skew = np.array([[0., -z, y], [z, 0., -x], [-y, x, 0.]])
    return np.eye(3) + np.sin(angle) * skew + (1. - np.cos(angle)) * (skew @ skew)


class RotationCv(diagnostic_fakes.FakeCv):
    @staticmethod
    def Rodrigues(value):
        value = np.asarray(value)
        return (rotation_vector(value).reshape(3, 1) if value.shape == (3, 3)
                else rotation(value)), None


class MatchingCorrectionTests(unittest.TestCase):
    # Reuse only fixture helpers, not the inherited diagnostic test cases.
    bind = diagnostic_fakes.ResidualDiagnosticTests.bind
    reject = diagnostic_fakes.ResidualDiagnosticTests.reject
    optical_report = diagnostic_fakes.ResidualDiagnosticTests.optical_report

    def setUp(self):
        diagnostic_fakes.ResidualDiagnosticTests.setUp(self)
        self.tracker.cv2 = RotationCv()

    def angle(self):
        return float(np.rad2deg(np.linalg.norm(rotation_vector(self.tracker.matching_correction))))

    def complete_sample(self, now=10., degrees=2., axis=(1., 0., 0.), valid=True, inliers=5):
        self.reject(now)
        report = self.optical_report(valid)
        report["ransac"]["inlier_count"] = inliers
        if valid:
            # Absolute target relative to the immutable alignment*IMU base.
            report["pose"]["rotation_matrix_left_camera"] = rotation(np.deg2rad(degrees) * np.asarray(axis)).tolist()
            report["pose"]["tvec_left_camera_m"] = [777., 888., 999.]
        self.solver.return_value = report
        self.workers[-1].complete()
        return self.tracker._optical_job

    def consume(self, now=10.1):
        self.bind(now)
        return self.tracker.update_stereo(self.left, self.right, now)

    def test_only_completed_five_point_proof_updates_once_and_publishes_imu_rotation(self):
        before = self.tracker.alignment.copy()
        self.reject()
        self.assertTrue(self.consume(10.01).valid)
        self.assertEqual(self.angle(), 0.)
        report = self.optical_report()
        report["pose"]["rotation_matrix_left_camera"] = rotation(np.deg2rad([2., 0., 0.])).tolist()
        report["pose"]["tvec_left_camera_m"] = [777., 888., 999.]
        self.solver.return_value = report
        self.workers[0].complete()
        pose = self.consume()
        self.assertTrue(pose.valid, pose.reason)
        self.assertAlmostEqual(self.angle(), 2. * (1. - np.exp(-.25 / 2.)), places=8)
        np.testing.assert_array_equal(self.tracker.alignment, before)
        np.testing.assert_allclose(pose.rvec, np.zeros(3), atol=1e-12)
        expected_t = np.array([0., 0., 1.]) + self.tracker.points.mean(axis=0) - (
            self.tracker.matching_correction @ self.tracker.points.mean(axis=0))
        np.testing.assert_allclose(pose.tvec.reshape(3), expected_t, atol=1e-12)
        self.assertLess(np.linalg.norm(pose.tvec), 2.)
        previous = self.tracker.matching_correction.copy()
        self.assertTrue(self.consume(10.2).valid)
        np.testing.assert_array_equal(self.tracker.matching_correction, previous)
        diagnostic = self.tracker.diagnostics()["optical_matching_correction"]
        self.assertEqual(diagnostic["reason"], "validated_five_update")
        self.assertEqual(diagnostic["published_rotation"], "alignment_imu_only")
        json.dumps(self.tracker.diagnostics(), allow_nan=False)

    def test_full_matrix_reconstruction_uses_sampled_correction_not_double_integration(self):
        self.complete_sample()
        self.consume()
        old = self.tracker.matching_correction.copy()
        job = self.complete_sample(15., degrees=2., axis=(0., 1., 0.))
        np.testing.assert_allclose(job["matching_correction"], old)
        np.testing.assert_allclose(job["correction_target"], rotation(np.deg2rad([0., 2., 0.])), atol=1e-12)
        self.assertAlmostEqual(job["result"]["delta_rotation_deg"], 2., places=8)
        self.assertGreater(job["result"]["delta_matching_rotation_deg"], 2.)
        self.consume(15.1)
        self.assertLessEqual(self.angle(), 3. + 1e-10)

    def test_step_and_absolute_amplitude_clamps_with_repeated_validated_targets(self):
        previous = np.eye(3)
        for n in range(55):
            now = 10. + 5. * n
            self.complete_sample(now, degrees=4.)
            pose = self.consume(now + .1)
            self.assertTrue(pose.valid, pose.reason)
            step = np.rad2deg(np.linalg.norm(rotation_vector(self.tracker.matching_correction @ previous.T)))
            self.assertLessEqual(step, .25 + 1e-9)
            self.assertLessEqual(self.angle(), 3. + 1e-9)
            np.testing.assert_allclose(self.tracker.matching_correction.T @ self.tracker.matching_correction,
                                       np.eye(3), atol=1e-12)
            np.testing.assert_array_equal(self.tracker.alignment, MODEL_TO_IMU)
            previous = self.tracker.matching_correction.copy()
        self.assertAlmostEqual(self.angle(), 3., places=8)

    def test_invalid_four_inliers_or_orientation_rejected_sources_never_train(self):
        for valid, inliers in ((False, 4), (False, 5), (True, 4)):
            with self.subTest(valid=valid, inliers=inliers):
                now = 10. + 5. * len(self.workers)
                self.complete_sample(now, valid=valid, inliers=inliers)
                self.consume(now + .1)
                self.assertEqual(self.angle(), 0.)
                self.assertIsNone(self.tracker._correction_validated_at)
                self.assertEqual(self.tracker._correction_reason, "discarded_unvalidated_five")

    def test_stale_large_or_invalid_rotations_are_discarded(self):
        self.complete_sample()
        self.consume(12.01)
        self.assertEqual(self.angle(), 0.)
        self.assertEqual(self.tracker._correction_reason, "discarded_stale_result")
        self.complete_sample(15., degrees=6.)
        self.consume(15.1)
        self.assertEqual(self.angle(), 0.)
        self.assertEqual(self.tracker._correction_reason, "discarded_large_innovation")
        for offset, bad in enumerate((np.diag([1., 1., -1.]), np.full((3, 3), np.nan), np.ones((2, 2)))):
            now = 20. + 5. * offset
            job = self.complete_sample(now)
            job["correction_target"] = bad
            self.consume(now + .1)
            self.assertEqual(self.angle(), 0.)
            self.assertEqual(self.tracker._correction_reason, "discarded_invalid_rotation")

    def test_hold_then_decay_even_without_any_leds_and_no_accumulation_from_rejected_frames(self):
        self.complete_sample()
        self.consume()
        before = self.angle()
        self.tri = {"state": "no_matches", "matches": []}
        for now in (11., 15., 18.):
            self.bind(now)
            self.assertFalse(self.tracker.update_stereo([], [], now).valid)
            self.assertAlmostEqual(self.angle(), before)
        for now in np.arange(18.1, 23., .1):
            self.bind(float(now))
            self.assertFalse(self.tracker.update_stereo([], [], float(now)).valid)
            self.assertLessEqual(self.angle(), before)
            before = self.angle()
        self.assertLess(self.angle(), .16)
        self.assertEqual(self.tracker._correction_validated_at, 10.)
        self.assertEqual(self.tracker._correction_reason, "decaying_without_validated_five")

    def test_four_or_three_leds_and_two_led_flag_cannot_use_correction_as_fallback(self):
        self.complete_sample()
        self.consume()
        before = self.tracker.matching_correction.copy()
        for count in (4, 3):
            self.tri["matches"] = self.tri["matches"][:count]
            self.bind(10.2)
            pose = self.tracker.update_stereo(self.left[:count], self.right[:count], 10.2)
            self.assertFalse(pose.valid)
            self.assertEqual(pose.reason, "ps4_stereo_five_unambiguous_matches_required")
        self.tracker.allow_two_led = True
        self.assertFalse(self.tracker.update_stereo(self.left, self.right, 10.3).valid)
        np.testing.assert_array_equal(self.tracker.matching_correction, before)

    def test_recenter_clears_correction_and_rejects_inflight_result_without_rotating_alignment(self):
        self.complete_sample()
        self.consume()
        self.reject(15.)
        before = self.tracker.alignment.copy()
        sample = copy.deepcopy(self.tracker.imu)
        sample["recenter_epoch"] = 2
        self.tracker.set_imu(sample)
        self.assertEqual(self.angle(), 0.)
        np.testing.assert_array_equal(self.tracker.alignment, before)
        self.workers[-1].complete()
        self.tracker._consume_matching_correction(15.1)
        self.assertEqual(self.angle(), 0.)
        self.assertEqual(self.tracker._correction_reason, "discarded_generation")

    def test_session_and_clock_reset_remove_correction_and_old_evidence(self):
        self.complete_sample()
        self.consume()
        self.tracker._advance_matching_correction(9.)
        self.assertEqual(self.angle(), 0.)
        self.assertEqual(self.tracker._correction_reason, "clock_reset")
        self.tracker._consume_matching_correction(10.2)
        self.assertEqual(self.angle(), 0.)
        self.assertEqual(self.tracker._correction_reason, "discarded_generation")
        sample = copy.deepcopy(self.tracker.imu)
        sample["session_id"] = "other-fake"
        self.tracker.set_imu(sample)
        self.assertEqual(self.angle(), 0.)
        self.assertIsNone(self.tracker.alignment)
        self.assertIsNone(self.tracker._correction_validated_at)

    def test_decay_and_new_proof_share_one_step_budget_and_nonincreasing_samples_do_not_train(self):
        self.complete_sample()
        self.consume()
        self.complete_sample(20., degrees=-4.)
        before = self.tracker.matching_correction.copy()
        self.consume(20.1)
        delta = np.rad2deg(np.linalg.norm(rotation_vector(self.tracker.matching_correction @ before.T)))
        self.assertLessEqual(delta, .25 + 1e-10)
        job = copy.copy(self.tracker._optical_job)
        job["done"] = threading.Event()
        job["done"].set()
        self.tracker._optical_job = job
        self.tracker._consume_matching_correction(20.2)
        self.assertEqual(self.tracker._correction_reason, "discarded_nonincreasing_sample")

    def test_existing_pose_gates_config_is_unchanged(self):
        before = self.tracker.config
        self.complete_sample()
        self.consume()
        self.assertEqual(self.tracker.config, before)
        self.assertEqual(self.tracker.config, module.StereoHeadPoseConfig())


if __name__ == "__main__":
    unittest.main()
