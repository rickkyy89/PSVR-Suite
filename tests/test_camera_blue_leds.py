import unittest

import numpy as np

from tools.psvr_camera.blue_leds import BlueLedConfig, BlueLedDetector


class BlueLedTests(unittest.TestCase):
    def setUp(self):
        try:
            import cv2
        except ImportError:
            self.skipTest("OpenCV unavailable")
        self.cv2 = cv2
        self.detector = BlueLedDetector()

    def test_blue_centers_ignore_other_colors_noise_and_clipped_patch(self):
        frame = np.zeros((120, 180, 3), np.uint8)
        self.cv2.circle(frame, (40, 50), 7, (255, 80, 0), -1)
        self.cv2.circle(frame, (130, 60), 9, (255, 80, 0), -1)
        self.cv2.circle(frame, (80, 90), 7, (0, 0, 255), -1)
        self.cv2.circle(frame, (80, 30), 7, (255, 255, 255), -1)
        self.cv2.circle(frame, (0, 60), 7, (255, 80, 0), -1)
        frame[20, 20] = (255, 80, 0)
        result = self.detector.detect(frame)
        self.assertEqual(len(result), 2)
        np.testing.assert_allclose(sorted(r["center_px"] for r in result), [[40, 50], [130, 60]])
        self.assertTrue(all("id" not in r for r in result))

    def test_merged_lamps_remain_one_unlabelled_candidate(self):
        frame = np.zeros((100, 100, 3), np.uint8)
        frame[40:55, 30:60] = (255, 80, 0)
        self.assertEqual(len(self.detector.detect(frame)), 1)

    def test_no_blue_and_oversize(self):
        frame = np.full((100, 100, 3), 255, np.uint8)
        self.assertEqual(self.detector.detect(frame), [])
        frame[5:95, 5:95] = (255, 80, 0)
        self.assertEqual(self.detector.detect(frame), [])

    def test_invalid_inputs_and_config(self):
        for frame in (None, np.zeros((20, 20), np.uint8), np.zeros((20, 20, 3), float)):
            with self.assertRaises(ValueError):
                self.detector.detect(frame)
        for kwargs in ({"hue_max": 180}, {"min_area": 0}, {"value_min": -1}):
            with self.assertRaises(ValueError):
                BlueLedConfig(**kwargs)

    def test_stricter_live_settings_remove_dim_regions_and_thin_noise(self):
        frame = np.zeros((120,180,3),np.uint8)
        self.cv2.circle(frame,(40,50),9,(255,80,0),-1)
        self.cv2.circle(frame,(130,60),9,(100,30,0),-1)
        frame[90,20:100] = (255,80,0)  # area 80 but only a thin noise streak
        detector = BlueLedDetector(BlueLedConfig(min_area=60,value_min=140,opening_size=3))
        candidates = detector.detect(frame)
        self.assertEqual(len(candidates),1)
        np.testing.assert_allclose(candidates[0]["center_px"],[40,50])

    def test_white_core_and_blue_fringe_are_a_single_lamp(self):
        frame = np.zeros((120,180,3),np.uint8)
        frame[45:55,30:50] = (255,255,255)
        frame[45:55,28:30] = (255,80,0)
        frame[45:55,50:52] = (255,80,0)
        frame[80:90,100:120] = (255,255,255)
        detector = BlueLedDetector(BlueLedConfig(min_area=6,value_min=140,include_white_cores=True))
        candidates = detector.detect(frame)
        self.assertEqual(len(candidates),1)
        np.testing.assert_allclose(candidates[0]["center_px"],[39.5,49.5])
        self.assertEqual(candidates[0]["bbox_px"],[28,45,24,10])

    def test_small_white_core_survives(self):
        frame = np.zeros((100,100,3),np.uint8)
        frame[48:50,48:51] = (255,255,255)
        frame[48:50,47] = (255,80,0)
        detector = BlueLedDetector(BlueLedConfig(min_area=3,include_white_cores=True))
        self.assertEqual(len(detector.detect(frame)),1)


if __name__ == "__main__":
    unittest.main()
