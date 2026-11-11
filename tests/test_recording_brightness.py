# SPDX-License-Identifier: MIT
import importlib.util
import unittest
from pathlib import Path

import numpy as np

SCRIPT = Path(__file__).resolve().parents[1] / "tools/psvr_runtime_probe/analyze_recording.py"
spec = importlib.util.spec_from_file_location("recording_brightness", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class BrightnessTests(unittest.TestCase):
    def test_broad_white_pulse_is_a_candidate(self):
        dark = np.full(100, 20, dtype=np.uint8)
        bright = np.full(100, 100, dtype=np.uint8)
        self.assertTrue(module.brightness_change(dark, bright)["candidate_broad_flash"])
        self.assertFalse(module.brightness_change(bright, dark)["candidate_broad_flash"])

    def test_uint8_subtraction_does_not_wrap_into_false_brightness(self):
        light = np.full(100, 250, dtype=np.uint8)
        dark = np.full(100, 5, dtype=np.uint8)
        change = module.brightness_change(light, dark)
        self.assertEqual(change["mean_luma_delta"], -245)
        self.assertFalse(change["candidate_broad_flash"])

    def test_small_bright_overlay_is_not_a_global_flash(self):
        dark = np.zeros(100, dtype=np.uint8)
        overlay = dark.copy()
        overlay[:10] = 255
        self.assertFalse(module.brightness_change(dark, overlay)["candidate_broad_flash"])

    def test_static_and_small_noise_are_not_flashes(self):
        frame = np.full(100, 40, dtype=np.uint8)
        self.assertFalse(module.brightness_change(frame, frame)["candidate_broad_flash"])
        self.assertFalse(module.brightness_change(frame, frame + 1)["candidate_broad_flash"])

    def test_pulse_duration_and_final_frame_are_reported(self):
        levels = [20, 20, 100, 100, 20, 20, 20, 100]
        runs = module.bright_runs(levels, 30)
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0]["duration_seconds"], 2 / 30)
        self.assertEqual(runs[1]["end_frame_exclusive"], len(levels))


if __name__ == "__main__":
    unittest.main()
