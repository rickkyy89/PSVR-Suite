import unittest
import numpy as np
from tools.psvr_camera.guided_calibration import CaptureGuide


class GuideTests(unittest.TestCase):
    def test_requires_detection_and_stability_then_movement(self):
        guide = CaptureGuide(target=3)
        points = np.array([[10., 10.], [30., 10.], [10., 30.], [30., 30.]])
        self.assertFalse(guide.step(None, 0)[1])
        self.assertFalse(guide.step(points, 1)[1])
        self.assertFalse(guide.step(points, 2)[1])
        self.assertTrue(guide.step(points, 4)[1])
        self.assertEqual(len(guide.views), 1)
        self.assertIn("MUOVI", guide.step(points+20, 5)[0])
        self.assertIn("CAMBIA POSA", guide.step(points, 8)[0])
        self.assertFalse(guide.step(points+20, 9)[1])
        self.assertFalse(guide.step(points+20, 10)[1])
        self.assertTrue(guide.step(points+20, 12)[1])

    def test_motion_or_missing_board_restarts_hold(self):
        guide = CaptureGuide()
        points = np.zeros((54, 2))
        guide.step(points, 0)
        guide.step(points, 1)
        self.assertFalse(guide.step(points+5, 2.9)[1])
        guide.step(points+5, 3)
        guide.step(None, 4)
        guide.step(points+5, 5)
        self.assertFalse(guide.step(points+5, 6)[1])
        self.assertTrue(guide.step(points+5, 8)[1])


if __name__ == "__main__":
    unittest.main()
