"""Offline candidate contract; these tests do not enable it in the live suite."""
import unittest
from pathlib import Path

import numpy as np

from scripts.replay_evo_front_anchor import FrontAnchoredTracker
from tools.psvr_camera.calibration import Intrinsics
from tools.psvr_camera.imu_led_pose import load_rotation_rig


class FrontAnchorCandidateTests(unittest.TestCase):
    def setUp(self):
        try:
            import cv2
        except ImportError:
            self.skipTest('OpenCV unavailable')
        self.cv2 = cv2
        root = Path(__file__).resolve().parents[1]
        rig, points, normals = load_rotation_rig(root / 'configurations/psvr_nine_leds_rotation_trial.json')
        intrinsics = Intrinsics(np.array([[500., 0, 320], [0, 500., 240], [0, 0, 1]]), np.zeros(5), (640, 480), True)
        self.tracker = FrontAnchoredTracker(intrinsics, points, normals, rig['labels'])
        self.translation = np.array([[0.], [0.], [1.]])
        for index in range(4):
            pose = self.frame(1+index*.02)
        self.assertTrue(pose.valid, pose.reason)

    def frame(self, now, physical_angle=0., sensor_angle=None, session='driver-session', epoch=0):
        if sensor_angle is None:
            sensor_angle = physical_angle
        self.tracker.set_imu(dict(valid=True, state='tracking', monotonic_s=now-.001,
                                 quaternion_sensor_wxyz=[np.cos(sensor_angle/2), 0., -np.sin(sensor_angle/2), 0.],
                                 session_id=session, recenter_epoch=epoch, recenter_count=epoch))
        rotation = self.cv2.Rodrigues(np.array([0., physical_angle, 0.]))[0]
        ids, projected = self.tracker.predicted(rotation, self.translation)
        candidates = [dict(center_px=projected[i].tolist()) for i in ids[::-1]]
        return self.tracker.update(candidates, now)

    def test_full_front_prevents_sensor_model_error_from_becoming_position_motion(self):
        alignment = self.tracker.alignment.copy()
        pose = self.frame(1.08, sensor_angle=np.deg2rad(3))
        self.assertTrue(pose.valid, pose.reason)
        self.assertEqual(pose.reason, 'experimental_front_anchor')
        np.testing.assert_allclose(pose.tvec, self.translation, atol=1e-6)
        np.testing.assert_array_equal(self.tracker.alignment, alignment)

    def test_partial_side_still_uses_imu_guided_constellation(self):
        for index, degrees in enumerate(range(5, 181, 5)):
            pose = self.frame(1.1+index*.02, physical_angle=np.deg2rad(degrees))
            self.assertTrue(pose.valid, '%s: %s' % (degrees, pose.reason))
            np.testing.assert_allclose(pose.tvec, self.translation, atol=1e-5)
        self.assertEqual(set(pose.led_labels), {'rear_left', 'rear_right'})

    def test_stale_imu_does_not_publish_an_optical_anchor(self):
        _, projected = self.tracker.predicted(np.eye(3), self.translation)
        pose = self.tracker.update([dict(center_px=p.tolist()) for p in projected[:5]], 2.)
        self.assertFalse(pose.valid)

    def test_front_teleport_is_withheld_without_poisoning_prior(self):
        before = self.tracker.translation.copy()
        self.translation[2, 0] -= .15
        pose = self.frame(1.08)
        self.assertFalse(pose.valid)
        np.testing.assert_array_equal(self.tracker.translation, before)
        self.translation = before.copy()
        pose = self.frame(1.1)
        self.assertTrue(pose.valid, pose.reason)
        np.testing.assert_allclose(pose.tvec, before, atol=1e-6)

    def test_driver_recenter_keeps_absolute_alignment(self):
        alignment = self.tracker.alignment.copy()
        for index in range(4):
            pose = self.frame(1.1+index*.02, epoch=1)
        self.assertTrue(pose.valid, pose.reason)
        np.testing.assert_array_equal(self.tracker.alignment, alignment)

    def test_new_driver_session_requires_new_front_acquisition(self):
        pose = self.frame(1.1, session='replacement-session')
        self.assertFalse(pose.valid)
        self.assertIsNone(self.tracker.alignment)


if __name__ == '__main__':
    unittest.main()
