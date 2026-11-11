"""Offline camera metadata: resolved targets are not device readback."""
import copy
from pathlib import Path
import unittest

from tools.psvr_camera.ps4_suite_source import load_suite_profile
from tools.psvr_suite import tracking


ROOT = Path(__file__).resolve().parents[1]


class CameraMetadataTests(unittest.TestCase):
    def ps4_preferences(self):
        return tracking.normalize_prefs({"camera_source": "ps4-stereo"})

    def test_final_ps4_profile_targets_override_only_effective_metadata(self):
        requested = self.ps4_preferences()
        effective, profile = tracking._source_preferences(ROOT, requested)
        before = copy.deepcopy((requested, effective, profile))

        metadata = tracking._camera_metadata(requested, effective, profile)

        self.assertEqual(profile["controls"], {"exposure": -7, "gain": 8, "brightness": 1})
        for key, target in profile["controls"].items():
            self.assertEqual(metadata["effective_camera"][key], target)
        self.assertEqual(metadata["requested_camera"]["exposure"], 13)
        self.assertEqual(metadata["requested_camera"]["gain"], 0)
        self.assertEqual(metadata["requested_camera"]["height"], 480)
        self.assertEqual(metadata["effective_camera"]["height"], 400)
        self.assertEqual(metadata["effective_camera"]["eye_order"], ["right", "left"])
        self.assertIsNone(metadata["effective_camera"]["imu_rig"])
        self.assertEqual(metadata["source_profile"], profile)
        self.assertEqual((requested, effective, profile), before)

    def test_alternative_profile_is_not_replaced_with_final_profile_constants(self):
        requested = self.ps4_preferences()
        effective, profile = tracking._source_preferences(ROOT, requested)
        profile = copy.deepcopy(profile)
        profile["controls"] = {"exposure": -9, "gain": 2, "brightness": 3}
        metadata = tracking._camera_metadata(requested, effective, profile)
        for key, value in profile["controls"].items():
            self.assertEqual(metadata["effective_camera"][key], value)

    def test_unspecified_targets_do_not_invent_runtime_resolution_or_confirmation(self):
        requested = self.ps4_preferences()
        effective, profile = tracking._source_preferences(ROOT, requested)
        profile = copy.deepcopy(profile)
        profile["controls"] = {"exposure": None, "gain": None, "brightness": 0}
        metadata = tracking._camera_metadata(requested, effective, profile)
        self.assertIsNone(metadata["effective_camera"]["exposure"])
        self.assertIsNone(metadata["effective_camera"]["gain"])
        self.assertEqual(metadata["effective_camera"]["brightness"], 0)
        for camera in (metadata["requested_camera"], metadata["effective_camera"]):
            for key in ("readback", "readback_confirmed", "set_ok", "set_flags", "confirmed"):
                self.assertNotIn(key, camera)
        self.assertNotIn("camera_controls", metadata)

    def test_ps3_metadata_is_unchanged_and_keeps_native_controls_and_rig(self):
        requested = tracking.normalize_prefs({"exposure": 17, "gain": 3})
        effective, profile = tracking._source_preferences(ROOT, requested)
        metadata = tracking._camera_metadata(requested, effective, profile)
        expected_keys = ("camera_source", "backend", "index", "width", "height", "fps",
                         "intrinsicsPath", "rigPath", "imu_rig", "min_area", "value_min",
                         "saturation_min", "opening_size", "exposure", "gain")
        expected = {key: requested[key] for key in expected_keys}
        self.assertEqual(metadata, {"requested_camera": expected,
                                    "effective_camera": expected, "source_profile": None})
        self.assertEqual(requested["exposure"], 17)
        self.assertEqual(requested["gain"], 3)


if __name__ == "__main__":
    unittest.main()
