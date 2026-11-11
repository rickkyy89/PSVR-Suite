import unittest
import numpy as np
from tools.psvr_camera.calibration import Intrinsics
from tools.psvr_camera.refine_led_rig import fit_proportions, outline


class RefinementTests(unittest.TestCase):
    def test_recovers_outline_at_fixed_external_scale(self):
        try:
            import cv2
        except ImportError:
            self.skipTest("OpenCV unavailable")
        reference = np.array([[-.08,-.045,.025],[.08,-.045,.025],[0,0,0],
                              [-.08,.045,.025],[.08,.045,.025]])
        truth = outline(np.array([.051,.049,.073]),reference)
        intrinsics = Intrinsics(np.array([[500.,0,320],[0,500.,240],[0,0,1]]),np.zeros(5),(640,480),True)
        images = []
        random = np.random.default_rng(341)
        for i in range(18):
            r = np.array([-.25+i*.025,.3*np.sin(i),.1*np.cos(i)])
            t = np.array([.06*np.sin(i),.03*np.cos(i),.5+i*.045])
            image = cv2.projectPoints(truth,r,t,intrinsics.camera_matrix,intrinsics.distortion_coeffs)[0].reshape(-1,2)
            images.append(image+random.normal(0,.03,image.shape))
        fitted,errors = fit_proportions(images,intrinsics,reference)
        np.testing.assert_allclose(fitted,truth,atol=.0005)
        self.assertLess(float(np.median(errors)),.1)
        self.assertEqual(fitted[1,0]-fitted[0,0],.16)
        np.testing.assert_equal(fitted[:,2],reference[:,2])
