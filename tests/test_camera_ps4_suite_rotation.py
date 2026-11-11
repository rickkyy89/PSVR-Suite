"""PS4 fast-path regression with non-identity IMU and camera alignment."""
import unittest
from unittest.mock import patch

import numpy as np

from tests.test_camera_ps4_suite_source import cv2, imu, new_tracker, synthetic
from tools.psvr_camera.driver_position import build_driver_position_packet
from tools.psvr_camera.imu_led_pose import MODEL_TO_IMU, rotation_from_quaternion


@unittest.skipIf(cv2 is None, "requires optional OpenCV")
class GuidedRotationTests(unittest.TestCase):
    def test_optical_imu_disagreement_cannot_realign_or_update_translation(self):
        tracker = new_tracker()
        left, right, _ = synthetic(tracker)
        tracker.set_imu(imu(10.))
        self.assertTrue(tracker.update_stereo(left, right, 10.).valid)
        alignment = tracker.alignment.copy()
        translation = tracker.translation.copy()
        left, right, _ = synthetic(tracker, rvec=(.5, -.4, .2))
        tracker.set_imu(imu(10.02))
        with patch("tools.psvr_camera.ps4_suite_tracker.estimate_stereo_head_pose_from_triangulation",
                   side_effect=AssertionError("conflict must not redefine alignment")):
            pose = tracker.update_stereo(left, right, 10.02)
        self.assertFalse(pose.valid)
        np.testing.assert_array_equal(tracker.alignment, alignment)
        np.testing.assert_array_equal(tracker.translation, translation)

    def test_absolute_imu_rotation_and_head_offset_use_existing_bridge(self):
        tracker = new_tracker()
        initial_imu = imu(10.)
        initial_imu["quaternion_sensor_wxyz"] = [np.cos(.11), 0., np.sin(.11), 0.]
        left, right, optical_rotation = synthetic(tracker)
        tracker.set_imu(initial_imu)
        self.assertTrue(tracker.update_stereo(left, right, 10.).valid)
        alignment = tracker.alignment.copy()
        np.testing.assert_allclose(
            alignment,
            optical_rotation @ MODEL_TO_IMU.T
            @ rotation_from_quaternion(initial_imu["quaternion_sensor_wxyz"]).T,
            atol=1e-5)

        moved_imu = imu(10.02, epoch=2)
        moved_imu["quaternion_sensor_wxyz"] = [np.cos(.12), 0., np.sin(.12), 0.]
        rotation = alignment @ rotation_from_quaternion(
            moved_imu["quaternion_sensor_wxyz"]) @ MODEL_TO_IMU
        translation = np.array([.012, -.01, .7])
        left, right, _ = synthetic(
            tracker, t=translation, rvec=cv2.Rodrigues(rotation)[0].reshape(3))
        tracker.set_imu(moved_imu)
        with patch("tools.psvr_camera.ps4_suite_tracker.estimate_stereo_head_pose_from_triangulation",
                   side_effect=AssertionError("free solve after alignment")):
            pose = tracker.update_stereo(left[::-1], right[::-1], 10.02)
        self.assertTrue(pose.valid, pose.reason)
        np.testing.assert_allclose(pose.tvec.reshape(3), translation, atol=1e-5)
        np.testing.assert_allclose(cv2.Rodrigues(pose.rvec)[0], rotation, atol=1e-5)
        np.testing.assert_array_equal(tracker.alignment, alignment)

        offset = np.array([.02, -.03, .04])
        packet = build_driver_position_packet(
            pose, moved_imu, alignment, 2, scale=1.2,
            head_offset_model_m=offset, now=10.02)
        self.assertTrue(packet["valid"])
        self.assertEqual(packet["recenter_epoch"], 2)
        np.testing.assert_allclose(
            packet["position_m"], alignment.T @ (1.2 * translation + rotation @ offset),
            atol=1e-5)
