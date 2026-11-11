import unittest
from pathlib import Path
import numpy as np
from collections import deque
import threading
from tools.psvr_camera.calibration import Intrinsics
from tools.psvr_camera.imu_led_pose import ImuLedTracker,load_rotation_rig,rotation_from_quaternion
from tools.psvr_camera.imu_source import DiagnosticImuSource
from tools.psvr_camera.protocol import quaternion_from_rotation


class RotationLedTests(unittest.TestCase):
    def setUp(self):
        try:
            import cv2
        except ImportError:
            self.skipTest("OpenCV unavailable")
        self.cv2 = cv2
        rig,points,normals = load_rotation_rig(Path(__file__).resolve().parents[1]/"configurations/psvr_nine_leds_rotation_trial.json")
        self.points,self.normals = points,normals
        intrinsics = Intrinsics(np.array([[500.,0,320],[0,500.,240],[0,0,1]]),np.zeros(5),(640,480),True)
        self.tracker = ImuLedTracker(intrinsics,points,normals,rig["labels"])
        self.translation = np.array([[0.],[0.],[1.]])

    def sample(self,angle,now,epoch=0):
        self.tracker.set_imu({"valid":True,"state":"tracking","monotonic_s":now-.001,
            "quaternion_wxyz":[np.cos(angle/2),0,-np.sin(angle/2),0],"recenter_count":epoch})

    def observed(self,angle):
        rotation = self.cv2.Rodrigues(np.array([0.,angle,0.]))[0]
        ids,projected = self.tracker.predicted(rotation,self.translation)
        candidates = [{"center_px":projected[i].tolist()} for i in ids[::-1]]
        return ids,candidates

    def acquire(self):
        for index in range(4):
            now = 1+index*.02
            self.sample(0,now)
            _,candidates = self.observed(0)
            pose = self.tracker.update(candidates,now)
        self.assertTrue(pose.valid,pose.reason)

    def test_rotation_front_side_rear_with_four_and_two_visible(self):
        self.acquire()
        for index,degrees in enumerate(range(5,181,5)):
            now = 1.1+index*.02
            angle = np.deg2rad(degrees)
            self.sample(angle,now)
            ids,candidates = self.observed(angle)
            pose = self.tracker.update(candidates,now)
            self.assertTrue(pose.valid,"%sdeg: %s" % (degrees,pose.reason))
            np.testing.assert_allclose(pose.tvec,self.translation,atol=1e-5)
            self.assertEqual(set(pose.led_labels),{self.tracker.labels[i] for i in ids})
            if degrees == 90:
                self.assertEqual(len(ids),4)
                self.assertIn("side_right",pose.led_labels)
                self.assertIn("rear_right",pose.led_labels)
            if degrees == 180:
                self.assertEqual(set(pose.led_labels),{"rear_left","rear_right"})

    def test_stale_imu_and_recenter_require_new_alignment(self):
        self.acquire()
        _,candidates = self.observed(0)
        self.assertFalse(self.tracker.update(candidates,2).valid)
        self.sample(0,2.1,epoch=1)
        self.assertFalse(self.tracker.update(candidates,2.1).valid)
        self.assertIsNone(self.tracker.alignment)
        for i in range(3):
            now=2.12+i*.02
            self.sample(0,now,epoch=1)
            pose=self.tracker.update(candidates,now)
        self.assertTrue(pose.valid,pose.reason)

    def test_bad_quaternion_is_rejected(self):
        with self.assertRaises(ValueError):
            rotation_from_quaternion([0,0,0,0])

    def test_single_frame_translation_teleport_does_not_poison_prior(self):
        self.acquire()
        before=self.tracker.translation.copy()
        self.translation[2,0] += .15
        now=1.06+1/75
        self.sample(0,now)
        _,candidates=self.observed(0)
        self.assertFalse(self.tracker.update(candidates,now).valid)
        np.testing.assert_allclose(self.tracker.translation,before)
        self.translation=before.copy()
        now+=1/75
        self.sample(0,now)
        _,candidates=self.observed(0)
        pose=self.tracker.update(candidates,now)
        self.assertTrue(pose.valid,pose.reason)
        np.testing.assert_allclose(pose.tvec,before,atol=1e-5)

    def test_real_fast_translation_and_short_loss_remain_trackable(self):
        self.acquire()
        now=1.06
        for i in range(12):
            dt=.05 if i == 6 else 1/75
            now+=dt
            self.translation[0,0] += 1.2*dt
            self.sample(0,now)
            _,candidates=self.observed(0)
            pose=self.tracker.update(candidates,now)
            self.assertTrue(pose.valid,pose.reason)
            np.testing.assert_allclose(pose.tvec,self.translation,atol=1e-5)

    def test_camera_time_interpolation_does_not_cross_recenter(self):
        source=DiagnosticImuSource.__new__(DiagnosticImuSource)
        source.lock=threading.Lock()
        source.changed=threading.Condition(source.lock)
        source.samples=deque([
            {"monotonic_s":1.,"valid":True,"recenter_count":0,"quaternion_wxyz":[1,0,0,0]},
            {"monotonic_s":1.04,"valid":True,"recenter_count":0,
             "quaternion_wxyz":[np.cos(.1),0,np.sin(.1),0]}])
        sample=source.at(1.02,wait_s=0)
        self.assertTrue(sample["interpolated"])
        np.testing.assert_allclose(sample["quaternion_wxyz"],[np.cos(.05),0,np.sin(.05),0],atol=1e-8)
        source.samples[-1]["recenter_count"]=1
        self.assertNotIn("interpolated",source.at(1.02,wait_s=0))
        self.assertIsNone(source.at(.5,wait_s=0))

    def test_one_side_lamp_missing_does_not_require_five(self):
        self.acquire()
        for i,degrees in enumerate(range(5,91,5)):
            now=1.1+i*.02
            self.sample(np.deg2rad(degrees),now)
            ids,candidates=self.observed(np.deg2rad(degrees))
            if degrees == 90:
                candidates=candidates[1:]
            pose=self.tracker.update(candidates,now)
            self.assertTrue(pose.valid,pose.reason)
        self.assertEqual(len(pose.led_labels),3)

    def test_physical_sensor_basis_ignores_neutral_head_offset(self):
        neutral = self.cv2.Rodrigues(np.array([.6,.1,0.]))[0]
        def set_sample(angle,now):
            raw = rotation_from_quaternion([np.cos(angle/2),0,-np.sin(angle/2),0])
            self.tracker.set_imu({"valid":True,"state":"tracking","monotonic_s":now-.001,
                "quaternion_sensor_wxyz":quaternion_from_rotation(raw),
                "quaternion_wxyz":quaternion_from_rotation(raw@neutral),"recenter_count":0})
        for i in range(4):
            now=1+i*.02
            set_sample(0,now)
            _,candidates=self.observed(0)
            pose=self.tracker.update(candidates,now)
        self.assertTrue(pose.valid)
        for i,degrees in enumerate(range(5,181,5)):
            now=1.1+i*.02
            angle=np.deg2rad(degrees)
            set_sample(angle,now)
            _,candidates=self.observed(angle)
            pose=self.tracker.update(candidates,now)
            self.assertTrue(pose.valid,"%s: %s" % (degrees,pose.reason))
            np.testing.assert_allclose(pose.tvec,self.translation,atol=1e-5)

    def test_faster_near_rotation_with_clutter_and_partial_patterns(self):
        self.translation=np.array([[.01],[0.],[.55]])
        self.acquire()
        for i,degrees in enumerate(range(12,181,12)):
            now=1.1+i*.04  # 300 degrees/second, not a slow rotation sequence
            angle=np.deg2rad(degrees)
            self.translation[0,0] += .001
            self.sample(angle,now)
            _,candidates=self.observed(angle)
            candidates.extend([{"center_px":[20,20]},{"center_px":[620,460]}])
            pose=self.tracker.update(candidates,now)
            self.assertTrue(pose.valid,"%s degrees: %s" % (degrees,pose.reason))
            np.testing.assert_allclose(pose.tvec,self.translation,atol=1e-5)
