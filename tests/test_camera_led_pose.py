import unittest
import numpy as np
from tools.psvr_camera.calibration import Intrinsics
from tools.psvr_camera.led_pose import FrontLedTracker


POINTS = np.array([[-.08,-.045,.025],[.08,-.045,.025],[0,0,0],
                   [-.08,.045,.025],[.08,.045,.025]])


class LedPoseTests(unittest.TestCase):
    def setUp(self):
        try:
            import cv2
        except ImportError:
            self.skipTest("OpenCV unavailable")
        self.cv2 = cv2
        self.intrinsics = Intrinsics(np.array([[500.,0,320],[0,500.,240],[0,0,1]]),
                                    np.zeros(5), (640,480), True)
        self.tracker = FrontLedTracker(self.intrinsics, POINTS)

    def candidates(self, r, t, shuffle=(3,0,4,2,1)):
        projected = self.tracker.project(np.asarray(r,float),np.asarray(t,float))
        return [{"center_px": projected[i].tolist()} for i in shuffle]

    def acquire(self,r,t,now=0):
        pose = None
        for index in range(4):
            pose = self.tracker.update(self.candidates(r,t),now+index*.001)
            if index < 3:
                self.assertFalse(pose.valid)
        return pose

    def test_permuted_front_acquisition_and_metric_translation(self):
        t = [.03,-.02,.9]
        pose = self.acquire([.05,.1,.02],t)
        self.assertTrue(pose.valid, pose.reason)
        np.testing.assert_allclose(pose.tvec.reshape(3),t,atol=1e-5)
        self.assertLess(pose.rms_px,1e-4)
        self.assertFalse(pose.to_dict()["geometry_validated"])

    def test_temporal_identity_extra_blob_loss_and_reacquisition(self):
        self.assertTrue(self.acquire([0,0,0],[0,0,1]).valid)
        candidates = self.candidates([.08,.12,.03],[.01,0,.98],(1,4,0,2,3))
        candidates.append({"center_px":[20,20]})
        moved = self.tracker.update(candidates,.02)
        self.assertTrue(moved.valid,moved.reason)
        np.testing.assert_allclose(moved.tvec.reshape(3),[.01,0,.98],atol=1e-5)
        self.assertFalse(self.tracker.update(candidates[:3],.03).valid)
        self.assertTrue(self.acquire([0,0,0],[0,0,1],.5).valid)

    def test_search_region_shrinks_with_distance_and_expires(self):
        self.assertEqual(self.tracker.search_region(640,480,0),(None,1.0))
        self.assertTrue(self.acquire([0,0,0],[0,0,.6]).valid)
        roi,ratio = self.tracker.search_region(640,480,.01)
        self.assertIsNotNone(roi)
        self.assertAlmostEqual(ratio,1.0)
        # Slowly move away so identity matching stays in its per-frame gate.
        for index,z in enumerate(np.linspace(.6,1.8,40)):
            self.assertTrue(self.tracker.update(self.candidates([0,0,0],[0,0,z]),.02+index*.005).valid)
        far_roi,far_ratio = self.tracker.search_region(640,480,.22)
        self.assertLess(far_ratio,.4)
        self.assertLess(far_roi[2]-far_roi[0],roi[2]-roi[0])
        self.assertEqual(self.tracker.search_region(640,480,1),(None,1.0))

    def test_scale_relative_budget_is_bounded_and_diagnostics_are_logged(self):
        self.assertEqual(self.tracker.reprojection_limit(50),2.5)
        self.assertEqual(self.tracker.reprojection_limit(200),5.0)
        self.assertEqual(self.tracker.reprojection_limit(1000),8.0)
        pose = self.acquire([0,0,0],[0,0,.4])
        self.assertTrue(pose.valid,pose.reason)
        self.assertGreater(pose.rms_limit_px,2.5)
        self.assertGreater(pose.to_dict()["image_span_px"],100)

    def test_background_and_uncalibrated_refused(self):
        self.assertFalse(self.tracker.update([{"center_px":[10,10]}]*5,0).valid)
        self.intrinsics.calibrated=False
        with self.assertRaises(ValueError):
            FrontLedTracker(self.intrinsics,POINTS)


if __name__ == "__main__":
    unittest.main()
