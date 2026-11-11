"""Source segregation and transition tests with no hardware, sockets or threads."""
import copy
import json
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch

import numpy as np

from tools.psvr_camera import ps4_suite_source
from tools.psvr_suite import tracking

ROOT = Path(__file__).resolve().parents[1]


class SourceSnapshotTests(unittest.TestCase):
    def test_explicit_edit_is_bound_to_current_resource_and_detaches_targets(self):
        worker = tracking.TrackingWorker(ROOT)
        worker._thread = Mock()
        worker._thread.is_alive.return_value = True
        worker._cap = resource = object()
        worker._status.update(running=True, state="running", camera_source="ps4-stereo",
                              effective_camera={"camera_source": "ps4-stereo"})
        targets = {"exposure": -7}
        identity = worker.apply_source_controls("ps4-stereo", targets)
        targets["exposure"] = 13
        queued_id, queued_resource, queued_targets = worker._control_edits.get_nowait()
        self.assertEqual(queued_id, identity)
        self.assertIs(queued_resource, resource)
        self.assertEqual(queued_targets, {"exposure": -7})
        worker._status["camera_source"] = None  # reopen has invalidated identity
        with self.assertRaises(RuntimeError):
            worker.apply_source_controls("ps4-stereo", {"gain": 0})
        self.assertEqual(tracking.TrackingWorker.camera_profile_contract, 1)

    def test_ps3_overrides_are_effective_not_a_saved_preference_rewrite(self):
        requested = tracking.normalize_prefs({"source_preferences": {
            "pseye": {"exposure": 20, "gain": 3, "value_min": 72}}})
        before = copy.deepcopy(requested)
        effective, profile = tracking._source_preferences(ROOT, requested)
        self.assertEqual((effective["exposure"], effective["gain"], effective["value_min"]),
                         (20, 3, 72))
        self.assertEqual(requested, before)
        self.assertEqual(requested["exposure"], 13)
        self.assertIsNone(profile)

    def test_ps4_uses_only_ps4_controls_and_detaches_profile_snapshot(self):
        requested = tracking.normalize_prefs({"camera_source": "ps4-stereo",
            "source_preferences": {"pseye": {"exposure": 200, "gain": 42}},
            "camera_profiles": {"ps4-stereo": {"controls": {"exposure": -8}}}})
        before = copy.deepcopy(requested)
        effective, profile = tracking._source_preferences(ROOT, requested)
        self.assertEqual((effective["exposure"], effective["gain"], effective["brightness"]),
                         (-8, 8, 1))
        self.assertIsNone(effective["imu_rig"])
        self.assertEqual(requested, before)
        with patch.object(tracking, "open_camera_source", return_value=(object(), None)) as opened:
            tracking._open_effective_camera(ROOT, effective, profile)
            passed = opened.call_args.kwargs["controls"]
            self.assertEqual(passed, {"exposure": -8, "gain": 8, "brightness": 1})
            self.assertTrue(opened.call_args.kwargs["strict_controls"])
            profile["controls"]["exposure"] = -2
            self.assertEqual(passed["exposure"], -8)
        self.assertEqual(ps4_suite_source.load_suite_profile(ROOT)["controls"]["exposure"], -7)

    def test_legacy_ps4_default_is_preserved_and_not_strict(self):
        requested = tracking.normalize_prefs({"camera_source": "ps4-stereo"})
        effective, profile = tracking._source_preferences(ROOT, requested)
        with patch.object(tracking, "open_camera_source", return_value=(object(), None)) as opened:
            tracking._open_effective_camera(ROOT, effective, profile)
        self.assertEqual(opened.call_args.kwargs["controls"],
                         {"exposure": -7, "gain": 8, "brightness": 1})
        self.assertFalse(opened.call_args.kwargs["strict_controls"])
        self.assertEqual(requested["exposure"], 13)

    def test_generic_requires_its_own_calibration_and_never_gets_ps3_controls(self):
        requested = tracking.normalize_prefs({"camera_source": "opencv-dshow"})
        with self.assertRaisesRegex(ValueError, "generic_configuration_required"):
            tracking._source_preferences(ROOT, requested)
        config = {"width": 800, "height": 600, "fps": 30,
                  "intrinsicsPath": "generic-intrinsics.json",
                  "rigPath": "generic-rig.json", "imu_rig": "generic-imu.json"}
        requested = tracking.normalize_prefs({"camera_source": "opencv-dshow",
                                              "camera_profiles": {"opencv-dshow": config}})
        effective, profile = tracking._source_preferences(ROOT, requested)
        self.assertEqual(effective["width"], 800)
        self.assertIsNone(effective["exposure"])
        self.assertIsNone(effective["gain"])
        with patch.object(tracking, "open_camera_source", return_value=(object(), None)) as opened:
            tracking._open_effective_camera(ROOT, effective, profile)
        self.assertNotIn("controls", opened.call_args.kwargs)

    def test_conflicting_namespaces_are_rejected_not_silently_prioritized(self):
        requested = tracking.normalize_prefs({"camera_source": "ps4-stereo",
            "source_preferences": {"ps4-stereo": {"exposure": -7}},
            "camera_profiles": {"ps4-stereo": {"controls": {"exposure": -8}}}})
        with self.assertRaisesRegex(ValueError, "conflicting"):
            tracking._source_preferences(ROOT, requested)

    def test_unknown_or_cross_source_control_keys_fail(self):
        for entries in ({"opencv-dshow": {"exposure": 13}},
                        {"pseye": {"brightness": 1}},
                        {"ps4-stereo": {"imu_rig": "ps3.json"}}):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                tracking.normalize_prefs({"source_preferences": entries})


class TransitionTests(unittest.TestCase):
    def exercise(self, *, release_complete=True, rollback=False, startup_error=None):
        events, opens, caps = [], [], []
        worker = None
        initial = tracking.normalize_prefs({"source_preferences": {
            "pseye": {"exposure": 21, "gain": 2}}})
        pending = tracking.normalize_prefs({"camera_source": "ps4-stereo"})

        class Capture:
            error, cleanup_pending, abandoned = None, False, False
            last_frame_monotonic = None
            def __init__(self, backend):
                self.backend = backend
                self.controls = []
                self.last_pair = None
                self.sequence = 0
            def set_manual_controls(self, exposure, gain):
                self.controls.append((exposure, gain))
            def controls_status(self):
                return {"origin": self.backend}
            def read(self):
                if len(opens) == 1:
                    worker._settings.put_nowait(pending)
                else:
                    worker._stop.set()
                frame = np.zeros((400 if self.backend == "ps4-stereo" else 480,
                                  640, 3), np.uint8)
                self.last_frame_monotonic = time.perf_counter()
                if self.backend == "ps4-stereo":
                    # A successful real Ps4SuiteSource.read() always publishes
                    # its atomic calibration-eye pair, even without fresh IMU.
                    # The worker needs that provenance for preview/session logs
                    # as well as the stereo solver; a plain mono fake is invalid.
                    self.sequence += 1
                    self.last_pair = {"left": frame, "right": frame.copy(),
                                      "sequence": self.sequence,
                                      "host_monotonic": self.last_frame_monotonic}
                return True, frame
            def release(self):
                events.append("release")
                self.cleanup_pending = not release_complete
                return release_complete

        def open_camera(index, **kwargs):
            events.append("open")
            opens.append(copy.deepcopy(kwargs))
            if rollback and kwargs["backend"] == "ps4-stereo":
                raise startup_error or ValueError("injected PS4 open failure")
            cap = Capture(kwargs["backend"])
            caps.append(cap)
            return cap, None

        tracker = Mock()
        tracker.search_region.return_value = (None, 1.)
        tracker.diagnostics.return_value = {}
        tracker.reset.side_effect = lambda: events.append("tracker_reset")
        tracker.alignment = None
        pipeline = Mock()
        pipeline.diagnostics.return_value = {}
        pipeline.reset.side_effect = lambda *args: events.append("pipeline_reset")
        detector = Mock()
        detector.detect.return_value = []
        detector.annotate.side_effect = lambda frame, candidates: frame
        detector.cv2 = None
        sender = Mock()
        sender.invalidate.side_effect = lambda: events.append("invalidate")
        sender.last_packet = None
        imu = Mock()
        imu.at.return_value = imu.latest.return_value = None
        def optical(root, prefs, profile):
            intrinsics = types.SimpleNamespace(image_size=(prefs["width"], prefs["height"]))
            return intrinsics, {}, np.zeros((5, 3)), {}, np.zeros((5, 3)), None, [], tracker

        profile = ps4_suite_source.load_suite_profile(ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            worker = tracking.TrackingWorker(tmp)
            with patch.object(tracking, "open_camera_source", side_effect=open_camera), \
                 patch.object(tracking, "_load_optical_setup", side_effect=optical), \
                 patch.object(ps4_suite_source, "load_suite_profile", return_value=profile), \
                 patch.object(tracking, "_load_kalman_table", return_value={}), \
                 patch.object(tracking, "_make_pipeline", return_value=pipeline), \
                 patch.object(tracking, "BlueLedDetector", return_value=detector), \
                 patch.object(tracking, "DriverImuSource", return_value=imu), \
                 patch.object(tracking, "DriverPositionSender", return_value=sender):
                worker._run(initial)
            rows = [json.loads(line) for line in Path(worker.status()["log"]).read_text().splitlines()]
        return worker, events, opens, caps, rows

    def test_switch_invalidates_before_release_and_never_sends_ps3_units_to_ps4(self):
        worker, events, opens, caps, rows = self.exercise()
        release = events.index("release")
        self.assertIn("invalidate", events[:release])
        self.assertIn("pipeline_reset", events[:release])
        self.assertIn("tracker_reset", events[:release])
        self.assertEqual(caps[0].controls, [(21, 2)])
        self.assertEqual(caps[1].controls, [])  # fake exposes method deliberately
        self.assertEqual(opens[1]["controls"], {"exposure": -7, "gain": 8, "brightness": 1})
        self.assertIsNone(worker.status()["camera_controls"])
        self.assertIsNone(worker.status()["estimated_position_m"])
        self.assertTrue(rows[-1]["ok"], rows[-1])
        self.assertEqual(rows[-1]["captured_frames"], 2)
        poses = [row for row in rows if row.get("event") == "pose"]
        stereo = [row for row in poses if row.get("camera_source") == "ps4-stereo"]
        self.assertEqual(len(stereo), 1)
        self.assertEqual(stereo[0]["stereo_sequence"], caps[1].last_pair["sequence"])
        self.assertEqual(stereo[0]["monotonic_s"], caps[1].last_pair["host_monotonic"])
        self.assertTrue(all(row["valid"] is False for row in poses))  # no fabricated IMU pose

    def test_pending_release_prohibits_reopen_rollback_and_restart(self):
        worker, events, opens, caps, rows = self.exercise(release_complete=False)
        self.assertEqual(len(opens), 1)
        self.assertIs(worker._retired_cap, caps[0])
        self.assertFalse(rows[-1]["ok"], rows[-1])
        self.assertIn("no reopen or rollback", rows[-1]["error"])
        with self.assertRaisesRegex(RuntimeError, "cleanup pending"):
            worker.start({})

    def test_rollback_reapplies_ps3_controls_on_a_reset_optical_context(self):
        worker, events, opens, caps, rows = self.exercise(rollback=True)
        self.assertEqual([kwargs["backend"] for kwargs in opens],
                         ["pseye", "ps4-stereo", "pseye"])
        self.assertEqual(caps[-1].controls, [(21, 2)])
        self.assertTrue(any(row.get("event") == "camera_rollback" for row in rows))
        self.assertTrue(rows[-1]["ok"], rows[-1])


if __name__ == "__main__":
    unittest.main()
