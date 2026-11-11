"""Synthetic diagnostic-probe tests: never open a camera, GUI or UDP socket."""
from __future__ import annotations

from contextlib import redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.psvr_camera import ps4_stereo_probe as probe_module
from tools.psvr_camera import ps4_stereo_session as session_module
from tools.psvr_camera import stereo_leds
from tools.psvr_camera.camera_backend import CameraError


def accepted_calibration():
    return SimpleNamespace(image_size=(640, 400), validation={"accepted": True})


def candidate():
    return {"center_px": [12., 22.], "bbox_px": [10, 20, 5, 5], "area_px": 25}


def point(depth=1., error=0.):
    return {"left_index": 0, "right_index": 0,
            "point_left_camera_m": [.1, .2, depth], "reprojection_px": error}


def result(*matches, state=None):
    return {"state": state or ("points" if matches else "no_matches"),
            "matches": list(matches)}


def fake_detector(config):
    # Annotation's copy contract is real; drawing is deliberately irrelevant to
    # the probe's aggregation and display-only background selection.
    return SimpleNamespace(config=config, detect=Mock(return_value=[candidate()]),
                           annotate=Mock(side_effect=lambda image, patches: image.copy()),
                           cv2=SimpleNamespace(line=Mock(), putText=Mock(),
                                               FONT_HERSHEY_SIMPLEX=0))


class SyntheticProbeTestCase(unittest.TestCase):
    def setUp(self):
        # Patch both entry points even for tests not expected to reach capture.
        # An accidental regression must fail rather than access hardware.
        self.session_factory = self.enterContext(patch.object(
            session_module, "StereoSession",
            side_effect=AssertionError("hardware session must be mocked")))
        self.detector_factory = self.enterContext(patch.object(
            probe_module, "BlueLedDetector", side_effect=fake_detector))
        self.triangulate = self.enterContext(patch.object(
            stereo_leds, "triangulate_candidates", return_value=result()))
        self.calibration = accepted_calibration()
        self.left = np.full((400, 640, 3), 37, dtype=np.uint8)
        self.right = np.full((400, 640, 3), 71, dtype=np.uint8)

    def make_probe(self):
        return probe_module.StereoPointProbe(self.calibration)


class StereoPointProbeTests(SyntheticProbeTestCase):
    def test_empty_report_and_render(self):
        probe = self.make_probe()
        report = probe.to_dict()
        self.assertEqual(report["pairs_analyzed"], 0)
        self.assertEqual(report["state_counts"], {})
        self.assertEqual(report["point_observations"], 0)
        self.assertEqual(report["detector"]["value_min"], 160)
        self.assertEqual(report["detector"]["saturation_min"], 90)
        self.assertEqual(report["detector"]["min_area"], 20)
        self.assertTrue(report["detector"]["include_white_cores"])
        for key in ("stream_pair_fps", "point_reprojection_rms_px",
                    "matched_patch_depth_range_m", "last_result"):
            self.assertIsNone(report[key])
        self.assertIsNone(probe.render())
        json.dumps(report, allow_nan=False)

    def test_equal_and_coarse_monotonic_timestamps_are_accepted(self):
        probe = self.make_probe()
        for stamp in (10., 10., 10.25, 10.25, 10.5):
            probe.add_pair(self.left, self.right, stamp)
        self.assertEqual(probe.frames, 5)
        self.assertEqual(probe.to_dict()["stream_pair_fps"], 8.)
        self.assertEqual((probe.first_time, probe.last_time), (10., 10.5))

    def test_all_equal_timestamps_do_not_divide_by_zero(self):
        probe = self.make_probe()
        for _ in range(3):
            probe.add_pair(self.left, self.right, 0.)
        self.assertEqual(probe.frames, 3)
        self.assertIsNone(probe.to_dict()["stream_pair_fps"])

    def test_default_timestamp_uses_host_monotonic(self):
        probe = self.make_probe()
        with patch.object(probe_module.time, "monotonic", return_value=12.5) as clock:
            probe.add_pair(self.left, self.right)
        clock.assert_called_once_with()
        self.assertEqual(probe.last_time, 12.5)

    def test_backwards_negative_and_nonfinite_timestamps_leave_state_unchanged(self):
        probe = self.make_probe()
        probe.add_pair(self.left, self.right, 5.)
        before = copy.deepcopy(probe.to_dict())
        calls = probe.detector.detect.call_count
        for stamp in (4.99, -1., float("nan"), float("inf"), float("-inf")):
            with self.subTest(stamp=stamp), self.assertRaises(CameraError):
                probe.add_pair(self.left, self.right, stamp)
            self.assertEqual(probe.to_dict(), before)
            self.assertEqual(probe.detector.detect.call_count, calls)
        self.triangulate.assert_called_once()

    def test_wrong_eye_size_channels_dtype_and_nonarray_are_rejected(self):
        probe = self.make_probe()
        invalid = (np.zeros((399, 640, 3), np.uint8),
                   np.zeros((400, 639, 3), np.uint8),
                   np.zeros((640, 400, 3), np.uint8),
                   np.zeros((400, 640), np.uint8),
                   np.zeros((400, 640, 4), np.uint8),
                   np.zeros((400, 640, 3), np.float32), None)
        for bad in invalid:
            for eye in ("left", "right"):
                with self.subTest(shape=getattr(bad, "shape", None), eye=eye):
                    pair = (bad, self.right) if eye == "left" else (self.left, bad)
                    with self.assertRaises(CameraError):
                        probe.add_pair(*pair, stamp=1.)
        self.assertEqual(probe.frames, 0)
        probe.detector.detect.assert_not_called()
        self.triangulate.assert_not_called()

    def test_depth_reprojection_and_candidate_histograms_aggregate_per_point(self):
        probe = self.make_probe()
        self.triangulate.side_effect = [result(point(1., 3.), point(4., 4.)),
                                        result(state="ambiguous"),
                                        result(point(2., 0.), state="partial")]
        probe.detector.detect.side_effect = [[candidate()] * 2, [candidate()] * 3,
                                             [], [candidate()],
                                             [candidate()], [candidate()]]
        for stamp in (2., 3., 4.):
            probe.add_pair(self.left, self.right, stamp)
        report = probe.to_dict()
        self.assertEqual(report["pairs_analyzed"], 3)
        self.assertEqual(report["state_counts"], {"points": 1, "ambiguous": 1, "partial": 1})
        self.assertEqual(report["point_count_histogram"], {0: 1, 1: 1, 2: 1})
        self.assertEqual(report["candidate_count_histograms"],
                         {"left": {0: 1, 1: 1, 2: 1}, "right": {1: 2, 3: 1}})
        self.assertEqual(report["point_observations"], 3)
        self.assertEqual(report["matched_patch_depth_range_m"], [1., 4.])
        self.assertAlmostEqual(report["point_reprojection_rms_px"], (25 / 3) ** .5)
        self.assertEqual(report["stream_pair_fps"], 1.)
        self.assertEqual(report["last_result"]["state"], "partial")
        args, kwargs = self.triangulate.call_args
        self.assertIs(args[0], self.calibration)
        self.assertIs(kwargs["config"], probe.matching_config)
        json.dumps(report, allow_nan=False)

    def test_diagnostics_never_claim_identities_pose_or_published_position(self):
        probe = self.make_probe()
        self.triangulate.return_value = result(point())
        probe.add_pair(self.left, self.right, 1.)
        report = probe.to_dict()
        for key in ("identities_assigned", "head_pose_estimated", "position_published",
                    "tracking_6dof", "camera_controls_modified_by_analysis"):
            self.assertIs(report[key], False)
        self.assertEqual(report["coordinate_frame"], "left_camera_stream_not_head_or_world")
        self.assertEqual(report["timing_source"], "host_read_completion_not_exposure")

        def check_keys(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    text = str(key).lower()
                    self.assertNotIn(text, {"id", "led_id", "pose", "head_pose", "udp",
                                            "head_position", "orientation", "quaternion"})
                    self.assertNotIn("udp", text)
                    check_keys(child)
            elif isinstance(value, list):
                for child in value:
                    check_keys(child)
        check_keys(report)

    def test_original_and_masked_render_do_not_mutate_inputs_or_analysis(self):
        probe = self.make_probe()
        self.triangulate.return_value = result(point())
        probe.add_pair(self.left, self.right, 1.)
        before = copy.deepcopy(probe.to_dict())
        originals = (self.left.copy(), self.right.copy())
        masked = probe.render()
        original = probe.render(candidate_background=True)
        self.assertEqual(masked.shape, (400, 1280, 3))
        for offset, image in ((0, self.left), (640, self.right)):
            np.testing.assert_array_equal(original[:, offset:offset + 640], image)
            expected = np.zeros_like(image)
            expected[20:25, 10:15] = image[20:25, 10:15]
            np.testing.assert_array_equal(masked[:, offset:offset + 640], expected)
        for image, saved in zip((self.left, self.right), originals):
            np.testing.assert_array_equal(image, saved)
            self.assertFalse(np.shares_memory(original, image))
            self.assertFalse(np.shares_memory(masked, image))
        self.assertEqual(probe.to_dict(), before)
        self.assertEqual(probe.detector.detect.call_count, 2)
        self.triangulate.assert_called_once()
        self.assertEqual(probe.detector.cv2.line.call_count, 2)
        self.assertEqual(probe.detector.cv2.line.call_args.args[1:3], ((12, 22), (652, 22)))


class RunProbeTests(SyntheticProbeTestCase):
    def setUp(self):
        super().setUp()
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.calibration_path = Path(self.temp) / "synthetic.json"
        self.calibration_path.write_bytes(b'{"synthetic_test_only": true}')
        self.loader = self.enterContext(patch.object(
            probe_module, "load_stereo", return_value=self.calibration))

    def fake_session(self, *, controls_errors=None, capture_error=None, pending=0):
        samples = iter([{"left": self.left, "right": self.right, "host_monotonic": 5.},
                        {"left": self.left, "right": self.right, "host_monotonic": 5.}])
        state = {"stopped": False, "pending": pending, "cleanup_reports": []}

        def next_pair(timeout_s):
            self.assertGreater(timeout_s, 0)
            self.assertLessEqual(timeout_s, .5)
            return next(samples, None)

        def stop(timeout_s):
            state["stopped"] = True

        def report():
            cleanup_pending = state["stopped"] and state["pending"] > 0
            if state["stopped"]:
                state["cleanup_reports"].append(cleanup_pending)
                state["pending"] -= 1
            return {"cleanup_pending": cleanup_pending,
                    "error": capture_error,
                    "controls": {"errors": controls_errors or [],
                                 "restoration_pending": cleanup_pending}}

        session = SimpleNamespace(start=Mock(), next_pair=Mock(side_effect=next_pair),
                                  stop=Mock(side_effect=stop), report=Mock(side_effect=report))
        self.session_factory.side_effect = None
        self.session_factory.return_value = session
        return session, state

    def run_bounded(self, **kwargs):
        # Virtual elapsed time bounds the loop without sleeps, camera input or
        # dependence on the clock granularity of the machine running the suite.
        ticks = iter(i / 100 for i in range(100))
        with patch.object(probe_module.time, "monotonic", side_effect=lambda: next(ticks)), \
                patch.object(probe_module.time, "sleep") as sleep:
            report, probe = probe_module.run_probe(2, self.calibration_path, seconds=.031, **kwargs)
        return report, probe, sleep

    def test_equal_pair_timestamps_none_reads_and_safe_control_defaults(self):
        session, state = self.fake_session()
        self.triangulate.return_value = result(point())
        report, probe, sleep = self.run_bounded()
        self.loader.assert_called_once_with(self.calibration_path,
                                            expected_image_size=(640, 400), expected_mode="640x400")
        self.session_factory.assert_called_once_with(2, mode="640x400", fps=60,
                                                     gain=None, brightness=0,
                                                     exposure=None, module=None)
        session.start.assert_called_once_with()
        session.stop.assert_called_once_with(timeout_s=2)
        self.assertEqual(session.next_pair.call_count, 3)
        self.assertEqual(probe.frames, 2)
        self.assertEqual(probe.point_observations, 2)
        self.assertIsNone(report["stereo_probe"]["stream_pair_fps"])
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["camera_controls_requested"],
                         {"exposure": None, "gain": None, "brightness": 0})
        self.assertEqual(report["calibration_sha256"],
                         hashlib.sha256(self.calibration_path.read_bytes()).hexdigest())
        self.assertEqual(state["cleanup_reports"], [False])
        sleep.assert_not_called()
        json.dumps(report, allow_nan=False)

    def test_control_errors_reported_only_after_cleanup_finishes(self):
        session, state = self.fake_session(controls_errors=["brightness restore failed"], pending=2)
        report, _, sleep = self.run_bounded()
        self.assertEqual(state["cleanup_reports"], [True, True, False])
        self.assertEqual(sleep.call_count, 2)
        for call in sleep.call_args_list:
            self.assertEqual(call.args, (.1,))
        self.assertFalse(report["capture"]["cleanup_pending"])
        self.assertFalse(report["capture"]["controls"]["restoration_pending"])
        self.assertIn("brightness restore failed", report["errors"])
        session.stop.assert_called_once()

    def test_capture_error_survives_cleanup(self):
        session, _ = self.fake_session(capture_error="synthetic capture failure")
        report, _, _ = self.run_bounded()
        self.assertTrue(any("synthetic capture failure" in error for error in report["errors"]))
        session.stop.assert_called_once()

    def test_start_failure_still_stops_session(self):
        session, _ = self.fake_session()
        session.start.side_effect = CameraError("synthetic start failure")
        report, probe, _ = self.run_bounded()
        self.assertIn("CameraError: synthetic start failure", report["errors"])
        self.assertEqual(probe.frames, 0)
        session.next_pair.assert_not_called()
        session.stop.assert_called_once_with(timeout_s=2)

    def test_keep_brightness_passes_none_without_touching_gain_or_exposure(self):
        self.fake_session()
        report, _, _ = self.run_bounded(brightness=None)
        self.assertIsNone(self.session_factory.call_args.kwargs["brightness"])
        self.assertEqual(report["camera_controls_requested"],
                         {"exposure": None, "gain": None, "brightness": None})

    def test_nonaccepted_profiles_fail_before_session_or_detector_creation(self):
        for validation in (None, {}, {"accepted": False}, {"accepted": 1}, {"accepted": "true"}):
            with self.subTest(validation=validation):
                self.calibration.validation = validation
                with self.assertRaisesRegex(CameraError, "accepted held-out calibration"):
                    probe_module.run_probe(0, self.calibration_path)
        self.session_factory.assert_not_called()
        self.detector_factory.assert_not_called()

    def test_invalid_index_mode_and_duration_fail_before_loading_or_capture(self):
        for kwargs in ({"index": -1}, {"index": True}, {"index": None},
                       {"mode": "unknown"}, {"seconds": 0}, {"seconds": 61},
                       {"seconds": float("nan")}, {"seconds": float("inf")}):
            with self.subTest(kwargs=kwargs), self.assertRaises(CameraError):
                probe_module.run_probe(calibration_path=self.calibration_path,
                                       **{"index": 0, **kwargs})
        self.loader.assert_not_called()
        self.session_factory.assert_not_called()


class ProbeCliTests(SyntheticProbeTestCase):
    def invoke(self, *, errors=None, points=0, flags=(), exception=None):
        report = {"errors": errors or [], "synthetic_test_only": True}
        probe = SimpleNamespace(point_observations=points)
        output = io.StringIO()
        with patch.object(probe_module, "run_probe", return_value=(report, probe),
                          side_effect=exception) as run, redirect_stdout(output):
            code = probe_module.main(["--index", "2", "--calibration", "synthetic.json", *flags])
        return code, json.loads(output.getvalue()), run

    def test_exit_two_for_report_errors_even_if_points_exist(self):
        code, report, _ = self.invoke(errors=["synthetic control error"], points=3)
        self.assertEqual(code, 2)
        self.assertEqual(report["errors"], ["synthetic control error"])

    def test_exit_three_for_no_matches(self):
        code, report, _ = self.invoke()
        self.assertEqual(code, 3)
        self.assertEqual(report["errors"], [])

    def test_exit_zero_for_points(self):
        code, _, _ = self.invoke(points=1)
        self.assertEqual(code, 0)

    def test_exit_two_for_invalid_configuration_or_file_errors(self):
        for error in (ValueError("invalid config"), OSError("unreadable profile"),
                      TypeError("invalid type")):
            with self.subTest(error=error):
                code, report, _ = self.invoke(exception=error)
                self.assertEqual(code, 2)
                self.assertEqual(report["errors"], [str(error)])

    def test_cli_default_and_explicit_brightness_and_keep_override(self):
        for flags, expected in (((), 0), (("--brightness", "7"), 7.),
                                (("--keep-brightness",), None),
                                (("--brightness", "7", "--keep-brightness"), None)):
            with self.subTest(flags=flags):
                _, _, run = self.invoke(flags=flags)
                self.assertEqual(run.call_args.kwargs["brightness"], expected)
                self.assertEqual(run.call_args.args[6].value_min, 160)

    def test_preview_and_display_background_flags_are_forwarded(self):
        _, _, run = self.invoke(flags=("--preview", "--original-background"))
        self.assertTrue(run.call_args.args[5])
        self.assertTrue(run.call_args.kwargs["candidate_background"])

    def test_invalid_detector_threshold_exits_before_run_probe(self):
        code, report, run = self.invoke(flags=("--value-min", "256"))
        self.assertEqual(code, 2)
        self.assertTrue(report["errors"])
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
