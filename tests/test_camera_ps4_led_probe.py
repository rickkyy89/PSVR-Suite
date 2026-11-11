"""Offline PS4 LED diagnostics: synthetic BGR images, never camera hardware."""
import contextlib
import gc
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
import weakref

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.psvr_camera import ps4_led_probe as led
from tools.psvr_camera.camera_backend import CameraError


def candidates(centers):
    return [{"center_px": list(center), "area_px": 9,
             "bbox_px": [int(center[0]) - 1, int(center[1]) - 1, 3, 3]}
            for center in centers]


def blue_frame(centers, shape=(100, 140, 3)):
    image = np.zeros(shape, dtype=np.uint8)
    for x, y in centers:
        image[y - 2:y + 3, x - 2:x + 3] = (255, 0, 0)
    return image


class EyeStatsTests(unittest.TestCase):
    centers = np.array([[15, 20], [40, 20], [65, 20], [90, 20], [115, 20]], float)

    def test_permutations_preserve_one_to_one_motion_and_initial_reference(self):
        baseline, permuted = led._EyeStats(), led._EyeStats()
        for offset, order in ((0, [3, 1, 4, 0, 2]),
                              (1, [2, 4, 1, 3, 0]),
                              (3, [4, 0, 3, 2, 1])):
            centers = self.centers + (offset, 0)
            baseline.add(candidates(centers))
            permuted.add(candidates(centers[order]))
        expected = baseline.to_dict()["static_reference_motion"]
        self.assertEqual(permuted.to_dict()["static_reference_motion"], expected)
        self.assertEqual(expected["matched_frames"], 3)
        self.assertAlmostEqual(expected["rms_px"], np.sqrt(10 / 3))
        self.assertEqual(expected["max_px"], 3)
        np.testing.assert_array_equal(baseline.reference, self.centers)
        np.testing.assert_array_equal(permuted.reference, self.centers[[3, 1, 4, 0, 2]])

    def test_gate_accepts_four_pixels_but_rejects_motion_past_reference_gate(self):
        stats = led._EyeStats()
        stats.add(candidates(self.centers))
        stats.add(candidates(self.centers + (4, 0)))
        stats.add(candidates(self.centers + (4.01, 0)))
        report = stats.to_dict()
        motion = report["static_reference_motion"]
        self.assertEqual(report["frames_with_five_candidates"], 3)
        self.assertEqual(motion["matched_frames"], 2)
        self.assertEqual(motion["gate_px"], 4)
        self.assertEqual(motion["max_px"], 4)
        self.assertAlmostEqual(motion["rms_px"], np.sqrt(8))
        np.testing.assert_array_equal(stats.reference, self.centers)

    def test_duplicate_nearest_neighbors_are_rejected_even_inside_gate(self):
        reference = np.array([[10, 20], [13, 20], [40, 20], [60, 20], [80, 20]])
        shifted = reference.astype(float)
        shifted[:2, 0] = (11.4, 16)
        distances = np.linalg.norm(reference[:, None] - shifted[None, :], axis=2)
        self.assertTrue(np.all(distances.min(axis=1) < 4))
        self.assertLess(len(set(distances.argmin(axis=1))), 5)
        stats = led._EyeStats()
        stats.add(candidates(reference))
        stats.add(candidates(shifted))
        self.assertEqual(stats.to_dict()["static_reference_motion"]["matched_frames"], 1)
        self.assertEqual(stats.max_motion, 0)

    def test_ambiguous_near_ties_are_rejected_despite_unique_nearest(self):
        reference = np.array([[10, 20], [14, 20], [40, 20], [60, 20], [80, 20]])
        shifted = reference.astype(float)
        shifted[:2, 0] = (11.8, 12.2)
        distances = np.linalg.norm(reference[:, None] - shifted[None, :], axis=2)
        self.assertEqual(len(set(distances.argmin(axis=1))), 5)
        stats = led._EyeStats()
        stats.add(candidates(reference))
        stats.add(candidates(shifted))
        self.assertEqual(stats.matched_frames, 1)

    def test_dropouts_merges_and_extra_candidates_do_not_replace_reference(self):
        stats = led._EyeStats()
        stats.add(candidates(self.centers[:4]))
        self.assertIsNone(stats.reference)
        stats.add(candidates(self.centers))
        stats.add(candidates(self.centers[:4]))
        stats.add(candidates(np.vstack((self.centers, [130, 20]))))
        stats.add([])
        stats.add(candidates(self.centers[::-1]))
        report = stats.to_dict()
        self.assertEqual(report["candidate_count_histogram"], {0: 1, 4: 2, 5: 2, 6: 1})
        self.assertEqual(report["frames_with_candidates"], 5)
        self.assertEqual(report["static_reference_motion"]["matched_frames"], 2)
        np.testing.assert_array_equal(stats.reference, self.centers)


class StaticLedProbeTests(unittest.TestCase):
    centers = [(15, 20), (40, 20), (65, 20), (90, 20), (115, 20)]

    def setUp(self):
        import cv2
        hardware_guard = patch.object(
            cv2, "VideoCapture", side_effect=AssertionError("hardware access forbidden"))
        hardware_guard.start()
        self.addCleanup(hardware_guard.stop)

    def assert_diagnostic_only(self, report):
        for key in ("camera_controls_modified", "calibrated", "tracking_6dof",
                    "stereo_correspondences_assigned"):
            self.assertIs(report[key], False)
        self.assertNotIn("pose", report)
        for profile in report["profiles"].values():
            for eye in ("left", "right"):
                stats = profile[eye]
                self.assertEqual(stats["static_reference_motion"]["meaning"],
                                 "unlabelled_temporal_patches_not_led_ids_or_pose")
                for candidate in stats["last_candidates"]:
                    self.assertEqual(set(candidate), {"center_px", "area_px", "bbox_px"})

    def test_zero_frames_have_explicit_empty_statistics(self):
        probe = led.StaticLedProbe()
        report = probe.to_dict()
        self.assertEqual(report["state"], "no_frames")
        self.assertEqual(report["pairs_analyzed"], 0)
        self.assertEqual(report["schema"], "psvr.camera.ps4.led_probe")
        self.assertEqual(report["version"], 1)
        self.assertIsNone(probe.last_pair)
        self.assertFalse(probe.save_snapshot("unused.jpg"))
        for signal in report["signal"].values():
            self.assertEqual(signal, {"all_zero_frames": 0, "max_value": 0, "mean_value": None})
        for profile in report["profiles"].values():
            self.assertEqual(profile["pairs_with_candidates_both_eyes"], 0)
            for eye in ("left", "right"):
                self.assertEqual(profile[eye]["frames"], 0)
                self.assertEqual(profile[eye]["candidate_count_histogram"], {})
                self.assertEqual(profile[eye]["last_candidates"], [])
                self.assertIsNone(profile[eye]["static_reference_motion"]["rms_px"])
                self.assertIsNone(profile[eye]["static_reference_motion"]["max_px"])
        self.assert_diagnostic_only(report)

    def test_all_zero_images_are_observed_frames_not_capture_errors(self):
        probe = led.StaticLedProbe()
        black = np.zeros((40, 60, 3), np.uint8)
        for _ in range(3):
            self.assertTrue(probe.add_pair(black, black.copy()))
        report = probe.to_dict()
        self.assertEqual(report["state"], "all_zero_images")
        self.assertEqual(report["pairs_analyzed"], 3)
        self.assertNotIn("error", report)
        for signal in report["signal"].values():
            self.assertEqual(signal, {"all_zero_frames": 3, "max_value": 0, "mean_value": 0})
        for profile in report["profiles"].values():
            for eye in ("left", "right"):
                self.assertEqual(profile[eye]["candidate_count_histogram"], {0: 3})
                self.assertEqual(profile[eye]["frames_with_candidates"], 0)
        self.assert_diagnostic_only(report)

    def test_nonzero_dark_images_are_not_labelled_all_zero(self):
        probe = led.StaticLedProbe()
        image = np.full((40, 60, 3), 2, np.uint8)
        probe.add_pair(image, image.copy())
        report = probe.to_dict()
        self.assertEqual(report["state"], "no_bilateral_candidates")
        for signal in report["signal"].values():
            self.assertEqual(signal, {"all_zero_frames": 0, "max_value": 2, "mean_value": 2})

    def test_five_blue_patches_both_eyes_are_candidates_not_pose_or_controls(self):
        probe = led.StaticLedProbe()
        left = blue_frame(self.centers)
        right_centers = [(x + 2, y + 10) for x, y in self.centers]
        right = blue_frame(right_centers)
        for _ in range(2):
            probe.add_pair(left, right)
        report = probe.to_dict()
        self.assertEqual(report["state"], "candidates_visible_both_eyes")
        self.assertEqual(report["pairs_analyzed"], 2)
        for profile in report["profiles"].values():
            self.assertEqual(profile["pairs_with_candidates_both_eyes"], 2)
            for eye, expected in (("left", self.centers), ("right", right_centers)):
                stats = profile[eye]
                self.assertEqual(stats["candidate_count_histogram"], {5: 2})
                self.assertEqual(stats["frames_with_five_candidates"], 2)
                self.assertEqual(stats["frames_with_candidates"], 2)
                np.testing.assert_allclose(sorted(c["center_px"] for c in stats["last_candidates"]),
                                           sorted(expected))
                self.assertEqual(stats["static_reference_motion"]["matched_frames"], 2)
                self.assertEqual(stats["static_reference_motion"]["rms_px"], 0)
        self.assert_diagnostic_only(report)
        json.dumps(report)  # Reports contain native JSON values, not ndarray/scalars.

    def test_candidates_in_alternating_eyes_are_not_bilateral_pairs(self):
        probe = led.StaticLedProbe()
        blue = blue_frame(self.centers)
        black = np.zeros_like(blue)
        probe.add_pair(blue, black)
        probe.add_pair(black, blue)
        report = probe.to_dict()
        self.assertEqual(report["state"], "no_bilateral_candidates")
        for profile in report["profiles"].values():
            self.assertEqual(profile["pairs_with_candidates_both_eyes"], 0)
            for eye in ("left", "right"):
                self.assertEqual(profile[eye]["candidate_count_histogram"], {0: 1, 5: 1})

    def test_small_threshold_patch_and_clipped_patch_use_real_detector(self):
        probe = led.StaticLedProbe()
        image = np.zeros((40, 60, 3), np.uint8)
        image[10:12, 10:13] = (255, 0, 0)  # Six pixels: default minimum, inclusive.
        image[20, 20:23] = (255, 0, 0)  # Only the sensitive profile accepts three.
        image[5:10, :4] = (255, 0, 0)  # Clipped at the left border: always reject.
        image[30:35, -4:] = (255, 0, 0)  # Clipped at the right border too.
        probe.add_pair(image, image.copy())
        report = probe.to_dict()
        for name, profile in report["profiles"].items():
            expected = (2 if name == "sensitive_blue" else
                        0 if name.startswith("static_min_area") else 1)
            for eye in ("left", "right"):
                self.assertEqual(profile[eye]["candidate_count_histogram"], {expected: 1})
                if expected:
                    self.assertEqual(profile[eye]["last_candidates"][0]["area_px"], 6)

    def test_invalid_pair_types_shapes_and_dtype_are_rejected_atomically(self):
        valid = np.zeros((10, 12, 3), np.uint8)
        invalid = (None, [], "image", np.zeros((10, 12), np.uint8),
                   np.zeros((10, 12, 1), np.uint8), np.zeros((10, 12, 4), np.uint8),
                   np.zeros((10, 12, 3), np.float32), np.zeros((10, 12, 3), np.int16),
                   np.zeros((0, 12, 3), np.uint8), np.zeros((10, 0, 3), np.uint8),
                   np.zeros((11, 12, 3), np.uint8), np.zeros((10, 13, 3), np.uint8))
        probe = led.StaticLedProbe()
        probe.add_pair(valid, valid.copy())
        before = probe.to_dict()
        previous_pair = probe.last_pair
        for bad in invalid:
            for pair in ((bad, valid), (valid, bad)):
                with self.subTest(kind=type(bad).__name__, shape=getattr(bad, "shape", None),
                                  invalid_left=pair[0] is bad):
                    with self.assertRaises(ValueError):
                        probe.add_pair(*pair)
                    self.assertEqual(probe.to_dict(), before)
                    self.assertIs(probe.last_pair, previous_pair)

    def test_report_caps_last_candidates_at_64_without_losing_counts(self):
        centers = [(5 + x * 10, 5 + y * 10) for y in range(10) for x in range(10)]
        image = blue_frame(centers, shape=(110, 110, 3))
        probe = led.StaticLedProbe()
        probe.add_pair(image, image.copy())
        for profile in probe.to_dict()["profiles"].values():
            for eye in ("left", "right"):
                self.assertEqual(profile[eye]["candidate_count_histogram"], {100: 1})
                self.assertEqual(len(profile[eye]["last_candidates"]), 64)
                self.assertEqual(profile[eye]["frames_with_five_candidates"], 0)
                self.assertEqual(profile[eye]["static_reference_motion"]["matched_frames"], 0)

    def test_only_last_pair_is_retained_without_image_or_candidate_history(self):
        probe = led.StaticLedProbe()
        old_images = []
        for index in range(15):
            left = blue_frame(self.centers if index == 0 else [])
            right = left.copy()
            old_images.extend((weakref.ref(left), weakref.ref(right)))
            probe.add_pair(left, right)
        gc.collect()
        self.assertTrue(all(reference() is None for reference in old_images[:-2]))
        self.assertIs(probe.last_pair[0], left)
        self.assertIs(probe.last_pair[1], right)
        for profile in probe.to_dict()["profiles"].values():
            for eye in ("left", "right"):
                self.assertEqual(profile[eye]["frames"], 15)
                self.assertEqual(profile[eye]["candidate_count_histogram"], {0: 14, 5: 1})
                self.assertEqual(profile[eye]["last_candidates"], [])
        self.assertTrue(all(pair == ([], []) for pair in probe.last_candidates.values()))

    def test_run_probe_wires_callback_to_aggregate_without_opening_hardware(self):
        left = blue_frame(self.centers)
        right = left.copy()
        capture_report = {"errors": [], "cleanup_pending": False}

        def fake_capture(index, backend, mode, fps, seconds, *, on_pair):
            self.assertEqual((index, backend, mode, fps, seconds), (7, "msmf", "640x400", 30, 0.5))
            self.assertTrue(on_pair(left, right))
            return capture_report

        with patch.object(led, "capture_stereo", side_effect=fake_capture) as capture:
            report, probe = led.run_probe(7, "msmf", "640x400", 30, 0.5)
        capture.assert_called_once()
        self.assertIs(report["capture"], capture_report)
        self.assertEqual(report["led_probe"], probe.to_dict())
        self.assertEqual(report["led_probe"]["pairs_analyzed"], 1)


class LedProbeCliTests(unittest.TestCase):
    def test_exit_codes_distinguish_darkness_visibility_and_capture_failure(self):
        cases = [("no_frames", [], False, 3), ("all_zero_images", [], False, 3),
                 ("no_bilateral_candidates", [], False, 3),
                 ("candidates_visible_both_eyes", [], False, 0),
                 ("candidates_visible_both_eyes", ["capture failed"], False, 2),
                 ("all_zero_images", ["capture failed"], False, 2),
                 ("candidates_visible_both_eyes", [], True, 2)]
        for state, errors, pending, expected in cases:
            with self.subTest(state=state, errors=errors, pending=pending):
                report = {"capture": {"errors": errors, "cleanup_pending": pending},
                          "led_probe": {"state": state}}
                probe = Mock()
                output = io.StringIO()
                with patch.object(led, "run_probe", return_value=(report, probe)) as run:
                    with contextlib.redirect_stdout(output):
                        result = led.main(["--index", "4", "--seconds", "0.25"])
                self.assertEqual(result, expected)
                self.assertEqual(json.loads(output.getvalue()), report)
                run.assert_called_once_with(4, "dshow", "640x400", 60, 0.25)
                probe.save_snapshot.assert_not_called()

    def test_capture_exception_is_error_two_not_no_candidates_three(self):
        for error in (CameraError("camera unavailable"), OSError("capture failure"),
                      ValueError("invalid frame")):
            with self.subTest(error=type(error).__name__):
                output = io.StringIO()
                with patch.object(led, "run_probe", side_effect=error):
                    with contextlib.redirect_stdout(output):
                        result = led.main(["--index", "0"])
                self.assertEqual(result, 2)
                self.assertEqual(json.loads(output.getvalue()), {"error": str(error)})


class PanelSummaryTests(unittest.TestCase):
    def test_large_led_report_preserves_state_errors_and_full_report_path(self):
        from tools.psvr_suite.ps4_camera_panel import summarize_output
        image = blue_frame([(5 + x * 10, 5 + y * 10)
                            for y in range(10) for x in range(10)], (110, 110, 3))
        probe = led.StaticLedProbe()
        probe.add_pair(image, image.copy())
        report = {"capture": {"errors": ["test capture failure"], "cleanup_pending": True,
                              "ps4_stereo": {"stream_pair_fps": 60}},
                  "led_probe": probe.to_dict(), "report_file": "full-report.json",
                  "snapshot": "last-pair.jpg"}
        original = json.dumps(report, indent=2)
        self.assertGreater(len(original), 16000)
        text = summarize_output(original)
        summary = json.loads(text)
        self.assertLess(len(text), 16000)
        self.assertEqual(summary["state"], "candidates_visible_both_eyes")
        self.assertEqual(summary["errors"], ["test capture failure"])
        self.assertTrue(summary["cleanup_pending"])
        self.assertEqual(summary["report_file"], "full-report.json")
        for profile in summary["profiles"].values():
            for eye in ("left", "right"):
                self.assertNotIn("last_candidates", profile[eye])

    def test_non_led_json_and_invalid_output_use_bounded_fallback(self):
        from tools.psvr_suite.ps4_camera_panel import summarize_output
        inventory = json.dumps({"state": "uvc_present"})
        self.assertEqual(summarize_output(inventory), inventory)
        self.assertEqual(len(summarize_output("x" * 20000)), 16000)
        self.assertEqual(summarize_output("[]"), "[]")


if __name__ == "__main__":
    unittest.main()
