"""Geometry/continuity regression checks for full and partial LED tracking."""
import unittest
from pathlib import Path

import numpy as np

from tools.psvr_camera.calibration import Intrinsics
from tools.psvr_camera.coherent_imu_led_pose import CoherentImuLedTracker
from tools.psvr_camera.imu_led_pose import load_rotation_rig


class CoherentLedTests(unittest.TestCase):
    def setUp(self):
        try:
            import cv2
        except ImportError:
            self.skipTest('OpenCV unavailable')
        self.cv2 = cv2
        root = Path(__file__).resolve().parents[1]
        rig, points, normals = load_rotation_rig(root / 'configurations/psvr_nine_leds_rotation_trial.json')
        intrinsics = Intrinsics(np.array([[500., 0, 320], [0, 500., 240], [0, 0, 1]]), np.zeros(5), (640, 480), True)
        self.tracker = CoherentImuLedTracker(intrinsics, points, normals, rig['labels'])
        self.translation = np.array([[0.], [0.], [1.]])
        for index in range(4):
            pose = self.frame(1+index*.02)
        self.assertTrue(pose.valid, pose.reason)

    def frame(self, now, angle=0., sensor_bias=0., epoch=0, session='driver-session', noise=None):
        self.tracker.set_imu(dict(valid=True, state='tracking', monotonic_s=now-.001,
                                 quaternion_sensor_wxyz=[np.cos((angle+sensor_bias)/2), 0., -np.sin((angle+sensor_bias)/2), 0.],
                                 session_id=session, recenter_epoch=epoch, recenter_count=epoch))
        rotation = self.cv2.Rodrigues(np.array([0., angle, 0.]))[0]
        ids, projected = self.tracker.predicted(rotation, self.translation)
        observed = projected[ids].copy()
        if noise is not None:
            observed += noise(observed.shape)
        return self.tracker.update([dict(center_px=p.tolist()) for p in observed[::-1]], now)

    def test_rotation_through_front_side_rear_and_back_has_one_position_basis(self):
        alignment = self.tracker.alignment.copy()
        for index, degrees in enumerate(list(range(5, 181, 5))+list(range(175, -1, -5))):
            pose = self.frame(1.1+index*.02, angle=np.deg2rad(degrees))
            self.assertTrue(pose.valid, '%s: %s' % (degrees, pose.reason))
            np.testing.assert_allclose(pose.tvec, self.translation, atol=1e-5)
        np.testing.assert_array_equal(self.tracker.alignment, alignment)

    def test_slow_sensor_innovation_does_not_move_world_alignment(self):
        alignment = self.tracker.alignment.copy()
        errors = []
        for index in range(300):
            pose = self.frame(1.08+index/75, sensor_bias=np.deg2rad(3)*index/299)
            self.assertTrue(pose.valid, pose.reason)
            errors.append(float(np.linalg.norm(pose.tvec-self.translation)))
        self.assertLess(max(errors), .01)
        self.assertGreater(np.linalg.norm(self.tracker.optical_correction-np.eye(3)), .01)
        np.testing.assert_array_equal(self.tracker.alignment, alignment)

    def test_opposite_rotation_with_clutter_keeps_partial_led_identities(self):
        for index, degrees in enumerate(range(-5, -181, -5)):
            now = 1.1+index*.02
            angle = np.deg2rad(degrees)
            self.tracker.set_imu(dict(valid=True, state='tracking', monotonic_s=now-.001,
                                     quaternion_sensor_wxyz=[np.cos(angle/2), 0., -np.sin(angle/2), 0.],
                                     session_id='driver-session', recenter_epoch=0, recenter_count=0))
            rotation = self.cv2.Rodrigues(np.array([0., angle, 0.]))[0]
            ids, projected = self.tracker.predicted(rotation, self.translation)
            candidates = [dict(center_px=projected[i].tolist()) for i in ids[::-1]]
            candidates.extend([dict(center_px=[20., 20.]), dict(center_px=[620., 460.])])
            pose = self.tracker.update(candidates, now)
            self.assertTrue(pose.valid, '%s: %s' % (degrees, pose.reason))
            np.testing.assert_allclose(pose.tvec, self.translation, atol=1e-5)

    def test_short_gap_does_not_turn_real_lateral_motion_into_a_loss(self):
        now = 1.06
        for index in range(20):
            dt = .05 if index == 10 else 1/75
            now += dt
            self.translation[0, 0] += 1.2*dt
            pose = self.frame(now)
            self.assertTrue(pose.valid, pose.reason)
            self.assertLess(float(np.linalg.norm(pose.tvec-self.translation)), .005)

    def test_partial_fast_translation_preserves_motion_with_bounded_lag(self):
        for index in range(18):
            pose = self.frame(1.1+index*.02, angle=np.deg2rad((index+1)*5))
            self.assertTrue(pose.valid, pose.reason)
        errors = []
        for index in range(40):
            self.translation[2, 0] += 1.2/75
            pose = self.frame(1.46+(index+1)/75, angle=np.pi/2)
            self.assertTrue(pose.valid, pose.reason)
            errors.append(float(np.linalg.norm(pose.tvec-self.translation)))
        # 25 mm at 1.2 m/s is <21 ms positional lag, including acceleration onset.
        self.assertLess(max(errors), .025)

    def test_noisy_two_rear_leds_do_not_oscillate_in_depth(self):
        for index, degrees in enumerate(range(5, 181, 5)):
            pose = self.frame(1.1+index*.02, angle=np.deg2rad(degrees))
            self.assertTrue(pose.valid, pose.reason)
        positions = []
        for index in range(80):
            sign = (-1)**index
            pose = self.frame(1.82+(index+1)/75, angle=np.pi,
                              noise=lambda shape: np.array([[sign, 0.], [-sign, 0.]]))
            self.assertTrue(pose.valid, pose.reason)
            positions.append(pose.tvec.reshape(3))
        maximum_step = np.max(np.linalg.norm(np.diff(positions, axis=0), axis=1))
        self.assertLess(maximum_step, .015)
        self.assertLess(np.max(np.linalg.norm(np.asarray(positions)-self.translation.reshape(3), axis=1)), .025)

    def test_teleport_does_not_poison_prior(self):
        before = self.tracker.translation.copy()
        self.translation[2, 0] -= .15
        pose = self.frame(1.08)
        self.assertFalse(pose.valid)
        np.testing.assert_array_equal(self.tracker.translation, before)
        self.translation = before
        self.assertTrue(self.frame(1.1).valid)

    def test_recenter_preserves_optical_correction_and_world_basis(self):
        for index in range(60):
            self.frame(1.08+index/75, sensor_bias=np.deg2rad(2))
        alignment = self.tracker.alignment.copy()
        correction = self.tracker.optical_correction.copy()
        self.frame(2., sensor_bias=np.deg2rad(2), epoch=1)
        np.testing.assert_array_equal(self.tracker.alignment, alignment)
        np.testing.assert_array_equal(self.tracker.optical_correction, correction)

    def test_stale_imu_cannot_publish_front_anchor(self):
        _, projected = self.tracker.predicted(np.eye(3), self.translation)
        pose = self.tracker.update([dict(center_px=p.tolist()) for p in projected[:5]], 2.)
        self.assertFalse(pose.valid)

    def test_new_session_discards_old_optical_correction(self):
        self.tracker.optical_correction = self.cv2.Rodrigues(np.array([0., .02, 0.]))[0]
        pose = self.frame(1.1, session='replacement-session')
        self.assertFalse(pose.valid)
        self.assertIsNone(self.tracker.alignment)
        np.testing.assert_array_equal(self.tracker.optical_correction, np.eye(3))

    def test_two_led_depth_requires_explicit_suite_experiment(self):
        self.tracker.allow_two_led = False
        for index, degrees in enumerate(range(5, 171, 5)):
            pose = self.frame(1.1+index*.02, angle=np.deg2rad(degrees))
            if len(pose.led_labels or []) >= 3:
                self.assertTrue(pose.valid, pose.reason)
        before = self.tracker.translation.copy()
        pose = self.frame(1.8, angle=np.pi)
        self.assertFalse(pose.valid)
        np.testing.assert_array_equal(self.tracker.translation, before)
        self.tracker.allow_two_led = True
        pose = self.frame(1.82, angle=np.pi)
        self.assertTrue(pose.valid, pose.reason)
        self.assertEqual(set(pose.led_labels), {'rear_left', 'rear_right'})

    def test_long_driver_loss_requires_four_distinct_front_frames(self):
        alignment = self.tracker.alignment.copy()
        self.tracker.translation += np.array([[0.], [0.], [.4]])
        for index in range(3):
            pose = self.frame(4.+index/75)
            self.assertFalse(pose.valid, pose.reason)
        pose = self.frame(4.+3/75)
        self.assertTrue(pose.valid, pose.reason)
        np.testing.assert_allclose(pose.tvec, self.translation, atol=1e-5)
        np.testing.assert_array_equal(self.tracker.alignment, alignment)

    def test_full_front_orientation_conflict_cannot_fall_back_to_an_arbitrary_pair(self):
        before = self.tracker.translation.copy()
        correction = self.tracker.optical_correction.copy()
        pose = self.frame(1.08, sensor_bias=np.deg2rad(30))
        self.assertFalse(pose.valid)
        self.assertEqual(pose.reason, 'front_orientation_conflict')
        np.testing.assert_array_equal(self.tracker.translation, before)
        np.testing.assert_array_equal(self.tracker.optical_correction, correction)

    def test_accumulated_slow_drift_past_ten_degrees_does_not_lock_out_tracking(self):
        alignment = self.tracker.alignment.copy()
        for index in range(900):
            pose = self.frame(1.08+index*.04, sensor_bias=np.deg2rad(18)*index/899)
            self.assertTrue(pose.valid, 'frame %d: %s' % (index, pose.reason))
        correction_deg = np.linalg.norm(self.cv2.Rodrigues(self.tracker.optical_correction)[0])*180/np.pi
        self.assertGreater(correction_deg, 15)
        np.testing.assert_array_equal(self.tracker.alignment, alignment)
        self.assertLess(float(np.linalg.norm(pose.tvec-self.translation)), .01)

        # After accumulating drift, a NEW discontinuity must still be rejected.
        before = self.tracker.translation.copy()
        pose = self.frame(37.08, sensor_bias=np.deg2rad(45))
        self.assertFalse(pose.valid)
        self.assertEqual(pose.reason, 'front_orientation_conflict')
        np.testing.assert_array_equal(self.tracker.translation, before)

    def test_extra_approximate_side_led_does_not_pull_a_complete_front_fit(self):
        rotation = np.eye(3)
        ids = np.arange(6)
        image = self.cv2.projectPoints(self.tracker.points[ids], np.zeros(3), self.translation,
                                       self.tracker.k, self.tracker.dist)[0].reshape(-1, 2)
        image[-1] += np.array([6., -3.])
        translation = self.tracker.solve_translation(rotation, ids, image)
        np.testing.assert_allclose(translation, self.translation, atol=1e-6)


if __name__ == '__main__':
    unittest.main()
