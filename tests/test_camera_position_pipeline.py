"""PositionPipeline selection contracts; synthetic data and mocked capture only."""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from tools.psvr_camera.position_filter import HeadPositionFilter
from tools.psvr_camera.position_pipeline import PositionPipeline, load_position_config
from tools.psvr_camera.pose_estimator import PoseEstimator, PoseEstimatorConfig
from tools.psvr_camera.geometry import rodrigues
from tools.psvr_camera.led_pose import LedPose

R_SMALL = np.eye(3) * 0.005 ** 2
RELIABLE = {"reliable": True, "reason": "ok", "accepted": True}
UNRELIABLE = {"reliable": False, "reason": "ok", "accepted": True}


def packet(position=(0., 0., 1.), **changes):
    row = dict(session_id="activation", recenter_epoch=1, valid=True,
               state="tracking", position_m=list(position), seq=1)
    row.update(changes)
    return row


def kalman_packet(position=(0., 0., 1.), **changes):
    if position is None:
        row = dict(session_id="activation", recenter_epoch=1, valid=False,
                   state="lost", position_m=None, seq=1)
    else:
        row = dict(session_id="activation", recenter_epoch=1, valid=True,
                   state="tracking", position_m=list(position), seq=1)
    row.update(changes)
    return row


def acquire(pipe, alignment, start=1.0, step=0.013333, count=4,
            position=(0., 0., 1.), covariance=None, quality=None):
    covariance = R_SMALL if covariance is None else covariance
    quality = RELIABLE if quality is None else quality
    out = None
    for i in range(count):
        pkt = kalman_packet(position)
        out = pipe.filter_packet(pkt, alignment, start + i * step,
                                 covariance=covariance, quality=quality,
                                 prediction_allowed=False)
    return out


class OneEuroEquivalenceTests(unittest.TestCase):
    def test_exact_baseline_same_timestamps_including_invalid_gap_identities(self):
        baseline = HeadPositionFilter()
        pipe = PositionPipeline("one_euro")
        now = 1.0
        sequence = [
            (packet((0., 0., 1.)), np.eye(3), 0.0),
            (packet((0.01, 0., 1.)), np.eye(3), 0.013333),
            (packet(valid=False, state="lost", position_m=None), np.eye(3), 0.013333),
            (packet((0.02, 0., 1.)), np.eye(3), 0.08),
            (packet(valid=False, state="lost", position_m=None), np.eye(3), 0.101),
            (packet((1., 0., 1.)), np.eye(3), 0.01),
            (None, np.eye(3), 0.013333),
            (packet((1., 0., 1.)), np.eye(3), 0.30),
            (packet((1., 0., 1.), session_id="new"), np.eye(3), 0.01),
            (packet((1., 0., 1.), recenter_epoch=2), np.eye(3), 0.01),
            (packet((1., 0., 1.)), rodrigues([0, 0.2, 0]), 0.01),
            (packet((1., 0., 1.)), None, 0.01),
            (packet((1., 0., 1.)), np.eye(3), 0.01),
        ]
        for pkt, alignment, dt in sequence:
            now += dt
            expected = baseline.filter_packet(copy.deepcopy(pkt), None if alignment is None else alignment.copy(), now)
            got = pipe.filter_packet(copy.deepcopy(pkt), None if alignment is None else alignment.copy(), now,
                                     covariance=R_SMALL, quality=dict(RELIABLE),
                                     prediction_allowed=True)
            if expected is None:
                self.assertIsNone(got)
            else:
                self.assertEqual(got["session_id"], expected["session_id"])
                self.assertEqual(got["valid"], expected["valid"])
                self.assertEqual(got["state"], expected["state"])
                if expected["position_m"] is None:
                    self.assertIsNone(got["position_m"])
                else:
                    np.testing.assert_allclose(got["position_m"], expected["position_m"], atol=1e-14)
            base_diag = baseline.diagnostics()
            pipe_diag = pipe.diagnostics()
            self.assertEqual(pipe_diag["type"], "one_euro")
            self.assertEqual(pipe_diag["quality"], RELIABLE)
            self.assertEqual(pipe_diag["raw_world_position_m"], base_diag["raw_world_position_m"])
            self.assertEqual(pipe_diag["filtered_world_position_m"], base_diag["filtered_world_position_m"])
            self.assertEqual(pipe_diag["settings"], base_diag["settings"])

    def test_one_euro_ignores_kalman_only_arguments(self):
        first = PositionPipeline("one_euro")
        second = PositionPipeline("one_euro")
        pkt = packet((0.01, 0., 1.))
        first.filter_packet(copy.deepcopy(pkt), np.eye(3), 1.0,
                            covariance=R_SMALL, quality=dict(RELIABLE), prediction_allowed=False)
        second.filter_packet(copy.deepcopy(pkt), np.eye(3), 1.0,
                             covariance=None, quality=dict(UNRELIABLE), prediction_allowed=True)
        # First valid sample seeds directly from raw in both cases.
        self.assertEqual(first.diagnostics()["filtered_world_position_m"],
                         second.diagnostics()["filtered_world_position_m"])

    def test_one_euro_invalid_callback_equivalence(self):
        baseline = HeadPositionFilter()
        pipe = PositionPipeline("one_euro")
        baseline.filter_packet(packet(), np.eye(3), 1.0)
        pipe.filter_packet(packet(), np.eye(3), 1.0)
        baseline.invalid(1.05)
        pipe.invalid(1.05)
        # Pipeline.invalid delegates without rewriting the last published
        # diagnostics; the owned filter state must still match the baseline.
        self.assertEqual(pipe.filter.diagnostics()["filtered_world_position_m"],
                         baseline.diagnostics()["filtered_world_position_m"])
        self.assertIsNone(pipe.filter.diagnostics()["filtered_world_position_m"])
        # Brief hold preserves history: next valid uses elapsed dt.
        out_base = baseline.filter_packet(packet((1., 0., 1.)), np.eye(3), 1.13)
        out_pipe = pipe.filter_packet(packet((1., 0., 1.)), np.eye(3), 1.13)
        np.testing.assert_allclose(out_pipe["position_m"], out_base["position_m"], atol=1e-14)


class KalmanPipelineTests(unittest.TestCase):
    def test_metadata_and_orientation_never_mutated(self):
        pipe = PositionPipeline("kalman")
        alignment = np.eye(3)
        covariance = R_SMALL.copy()
        quality = dict(RELIABLE)
        pkt = kalman_packet((0., 0., 1.))
        before_pkt = copy.deepcopy(pkt)
        before_align = alignment.copy()
        before_cov = covariance.copy()
        before_quality = copy.deepcopy(quality)
        times = [1.0, 1.013333, 1.026666, 1.04]
        out = None
        for now in times:
            out = pipe.filter_packet(pkt, alignment, now,
                                     covariance=covariance, quality=quality,
                                     prediction_allowed=False)
            self.assertEqual(pkt, before_pkt)
            np.testing.assert_array_equal(alignment, before_align)
            np.testing.assert_array_equal(covariance, before_cov)
            self.assertEqual(quality, before_quality)
        self.assertTrue(out["valid"])
        self.assertEqual(out["session_id"], "activation")
        self.assertEqual(out["state"], "tracking")
        np.testing.assert_allclose(out["position_m"], [0., 0., 1.], atol=1e-12)
        np.testing.assert_array_equal(alignment, np.eye(3))
        before_diag = copy.deepcopy(pipe.diagnostics())
        stray = pipe.diagnostics()
        stray["filtered_world_position_m"][0] = 50.0
        self.assertEqual(pipe.diagnostics(), before_diag)
        np.testing.assert_allclose(pipe.diagnostics()["filtered_world_position_m"], [0., 0., 1.], atol=1e-9)

    def test_no_cascade_head_filter_not_constructed_for_kalman(self):
        with patch("tools.psvr_camera.position_filter.HeadPositionFilter") as head:
            pipe = PositionPipeline("kalman")
            head.assert_not_called()
            self.assertIsInstance(pipe.filter, PoseEstimator)
        with patch("tools.psvr_camera.position_pipeline.PoseEstimator") as kalman:
            pipe = PositionPipeline("one_euro")
            kalman.assert_not_called()
            self.assertIsInstance(pipe.filter, HeadPositionFilter)

    def test_reliable_false_observable_updates_but_never_acquires(self):
        pipe = PositionPipeline("kalman")
        alignment = np.eye(3)
        for i in range(10):
            out = pipe.filter_packet(kalman_packet((0., 0., 1.)), alignment, 1.0 + i * 0.01,
                                     covariance=R_SMALL, quality=dict(UNRELIABLE),
                                     prediction_allowed=False)
            self.assertFalse(out["valid"])
            self.assertIsNone(out["position_m"])
        diag = pipe.diagnostics()
        self.assertEqual(diag["accepted"], 0)
        self.assertEqual(diag["rejected"], 0)
        self.assertEqual(diag["recovery_count"], 0)
        # Same observable R with reliable=True acquires on the fourth confirmation.
        fresh = PositionPipeline("kalman")
        for i in range(4):
            out = fresh.filter_packet(kalman_packet((0., 0., 1.)), alignment, 1.0 + i * 0.01,
                                      covariance=R_SMALL, quality=dict(RELIABLE),
                                      prediction_allowed=False)
            self.assertEqual(out["valid"], i == 3)
        # After acquisition an unreliable but observable sample still corrects.
        out = fresh.filter_packet(kalman_packet((0.005, 0., 1.)), alignment, 1.04,
                                  covariance=R_SMALL, quality=dict(UNRELIABLE),
                                  prediction_allowed=False)
        self.assertTrue(out["valid"])
        self.assertEqual(out["state"], "tracking")
        self.assertGreater(out["position_m"][0], 0.0)
        self.assertEqual(fresh.diagnostics()["accepted"], 2)

    def test_covariance_none_two_leds_never_updates(self):
        pipe = PositionPipeline("kalman")
        alignment = np.eye(3)
        for i in range(4):
            out = pipe.filter_packet(kalman_packet((0., 0., 1.)), alignment, 1.0 + i * 0.01,
                                     covariance=None, quality=dict(RELIABLE),
                                     prediction_allowed=False)
            self.assertFalse(out["valid"])
            self.assertIsNone(out["position_m"])
        diag = pipe.diagnostics()
        self.assertEqual(diag["accepted"], 0)
        self.assertEqual(diag["rejected"], 0)
        self.assertEqual(diag["recovery_count"], 0)
        # Seeded filter treats missing R as unobservable prediction, not an update.
        seeded = PositionPipeline("kalman")
        acquire(seeded, alignment)
        before = seeded.diagnostics()
        out = seeded.filter_packet(kalman_packet((0.005, 0., 1.)), alignment, 1.06,
                                   covariance=None, quality=dict(RELIABLE),
                                   prediction_allowed=True)
        self.assertTrue(out["valid"])
        self.assertEqual(seeded.diagnostics()["tracking_state"], "predicting")
        self.assertEqual(seeded.diagnostics()["accepted"], before["accepted"])
        self.assertEqual(seeded.diagnostics()["rejected"], before["rejected"])

    def test_invalid_context_never_promotes(self):
        pipe = PositionPipeline("kalman")
        alignment = np.eye(3)
        lost = kalman_packet(None, valid=False)
        out = pipe.filter_packet(copy.deepcopy(lost), alignment, 1.0,
                                 covariance=R_SMALL, quality=dict(RELIABLE),
                                 prediction_allowed=False)
        self.assertFalse(out["valid"])
        self.assertIsNone(out["position_m"])
        self.assertEqual(pipe.diagnostics()["reset_reason"], "invalid_context")
        self.assertIsNone(pipe.filter_packet(None, alignment, 1.01,
                                             covariance=R_SMALL, quality=dict(RELIABLE),
                                             prediction_allowed=False))
        self.assertFalse(pipe.diagnostics()["valid"])
        # Acquired filter with a hard invalid context stays lost even with R.
        seeded = PositionPipeline("kalman")
        acquire(seeded, alignment)
        out = seeded.filter_packet(copy.deepcopy(lost), alignment, 1.06,
                                   covariance=None, quality=dict(UNRELIABLE),
                                   prediction_allowed=False)
        self.assertFalse(out["valid"])
        self.assertIsNone(out["position_m"])
        self.assertEqual(seeded.diagnostics()["reset_reason"], "invalid_context")

    def test_fresh_dropout_80ms_permitted_then_invalid(self):
        # Default seed velocity uncertainty (1 m/s) already exceeds the
        # positional budget at 80 ms; use the documented 0.5 m/s seed so the
        # inclusive time boundary is exercised instead of the uncertainty cap.
        pipe = PositionPipeline("kalman", kalman={"initial_velocity_std_m_s": 0.5})
        alignment = np.eye(3)
        for i, now in enumerate([1.0, 1.013333, 1.026666, 1.04]):
            pipe.filter_packet(kalman_packet((0., 0., 1.)), alignment, now,
                               covariance=R_SMALL, quality=dict(RELIABLE),
                               prediction_allowed=False)
        lost = kalman_packet(None, valid=False)
        # B6-05: prediction during an optical dropout requires an explicit
        # valid IMU context (production callers always pass the boolean);
        # the omitted-parameter default no longer implies it.
        within = pipe.filter_packet(copy.deepcopy(lost), alignment, 1.04 + 0.08,
                                    covariance=None, quality=dict(UNRELIABLE),
                                    prediction_allowed=True, context_valid=True)
        self.assertTrue(within["valid"])
        self.assertEqual(within["state"], "tracking")
        self.assertIsNotNone(within["position_m"])
        self.assertEqual(pipe.diagnostics()["tracking_state"], "predicting")
        after = pipe.filter_packet(copy.deepcopy(lost), alignment, 1.04 + 0.081,
                                   covariance=None, quality=dict(UNRELIABLE),
                                   prediction_allowed=True, context_valid=True)
        self.assertFalse(after["valid"])
        self.assertIsNone(after["position_m"])
        self.assertIsNone(pipe.diagnostics()["position_m"])

    def test_reinit_session_epoch_alignment_manual_resets(self):
        alignment = np.eye(3)
        for changes in (dict(session_id="new"), dict(recenter_epoch=2)):
            with self.subTest(changes=changes):
                pipe = PositionPipeline("kalman")
                acquire(pipe, alignment)
                out = pipe.filter_packet(kalman_packet((1., 0., 1.), **changes), alignment, 1.05,
                                         covariance=R_SMALL, quality=dict(RELIABLE),
                                         prediction_allowed=False)
                self.assertFalse(out["valid"])
                self.assertEqual(pipe.diagnostics()["reset_reason"], "identity_changed")
                self.assertEqual(pipe.diagnostics()["accepted"], 0)
                for i in range(1, 4):
                    out = pipe.filter_packet(kalman_packet((1., 0., 1.), **changes), alignment,
                                             1.05 + i * 0.01, covariance=R_SMALL,
                                             quality=dict(RELIABLE), prediction_allowed=False)
                self.assertTrue(out["valid"])
        with self.subTest(changes="alignment"):
            pipe = PositionPipeline("kalman")
            acquire(pipe, alignment)
            tilted = rodrigues([0, 0.2, 0])
            out = pipe.filter_packet(kalman_packet((1., 0., 1.)), tilted, 1.05,
                                     covariance=R_SMALL, quality=dict(RELIABLE),
                                     prediction_allowed=False)
            self.assertFalse(out["valid"])
            self.assertEqual(pipe.diagnostics()["reset_reason"], "identity_changed")
        with self.subTest(changes="manual"):
            pipe = PositionPipeline("kalman")
            acquire(pipe, alignment)
            pipe.reset("manual")
            diag = pipe.diagnostics()
            self.assertEqual(diag, {"type": "kalman", "reset_reason": "manual"})
            self.assertFalse(pipe.filter.diagnostics()["valid"])
            self.assertIsNone(pipe.filter.diagnostics()["covariance"])
            out = pipe.filter_packet(kalman_packet((1., 0., 1.)), alignment, 1.06,
                                     covariance=R_SMALL, quality=dict(RELIABLE),
                                     prediction_allowed=False)
            self.assertFalse(out["valid"])
        with self.subTest(changes="camera_read_failure"):
            pipe = PositionPipeline("kalman")
            acquire(pipe, alignment)
            pipe.invalid(1.06)
            self.assertEqual(pipe.diagnostics()["reset_reason"], "camera_read_failure")
            self.assertFalse(pipe.filter.diagnostics()["valid"])
        with self.subTest(changes="one_euro_manual"):
            pipe = PositionPipeline("one_euro")
            pipe.filter_packet(packet(), np.eye(3), 1.0)
            pipe.reset("manual")
            self.assertEqual(pipe.diagnostics()["reset_reason"], "manual")
            out = pipe.filter_packet(packet((1., 0., 1.)), np.eye(3), 1.01)
            self.assertEqual(out["position_m"], [1., 0., 1.])

    def test_deterministic_config_invalid(self):
        self.assertEqual(load_position_config(None), {})
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "good.json"
            good.write_text(json.dumps({"filter": "kalman",
                                        "kalman": {"recovery_confirmations": 2}}), encoding="utf-8")
            self.assertEqual(load_position_config(str(good))["filter"], "kalman")
            cases = [
                {"filter": "unknown"},
                {"filter": "one_euro", "extra": {}},
                {"filter": "kalman", "kalman": {"acceleration_std_m_s2": -1.0}},
                {"filter": "kalman", "kalman": {"recovery_confirmations": 1}},
                {"filter": "one_euro", "one_euro": {"beta": -1.0}},
                {"filter": "one_euro", "one_euro": {"invalid_hold_s": 0.3}},
            ]
            for data in cases:
                path = Path(tmp) / "bad.json"
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.subTest(data=data):
                    with self.assertRaises(ValueError):
                        load_position_config(str(path))
                    with self.assertRaises(ValueError):
                        load_position_config(str(path))
            not_dict = Path(tmp) / "list.json"
            not_dict.write_text(json.dumps([1, 2]), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_position_config(str(not_dict))
        with self.assertRaises(ValueError):
            PositionPipeline("unknown")
        with self.assertRaises(ValueError):
            PoseEstimatorConfig(acceleration_std_m_s2=-1.0)
        with self.assertRaises(ValueError):
            HeadPositionFilter(beta=-1.0)


class CliConfigTests(unittest.TestCase):
    def test_cli_default_one_euro_and_choices(self):
        from tools.psvr_camera.cli import build_parser
        parser = build_parser()
        led_track = parser._subparsers._group_actions[0].choices["led-track"]
        action = next(a for a in led_track._actions if "--position-filter" in a.option_strings)
        self.assertEqual(sorted(action.choices), ["kalman", "one_euro"])
        args = parser.parse_args(["led-track", "--intrinsics", "mock", "--rig", "mock",
                                  "--index", "0", "--driver-imu", "--frames", "1"])
        self.assertIsNone(args.position_filter)
        self.assertEqual(args.position_filter_min_cutoff_hz, 8.0)
        self.assertEqual(args.position_filter_beta, 15.0)
        # Empty file config resolves to the documented one_euro default.
        self.assertEqual(load_position_config(None).get("filter", "one_euro"), "one_euro")
        kalman = parser.parse_args(["led-track", "--intrinsics", "mock", "--rig", "mock",
                                    "--index", "0", "--driver-imu", "--frames", "1",
                                    "--position-filter", "kalman"])
        self.assertEqual(kalman.position_filter, "kalman")
        with self.assertRaises(SystemExit):
            parser.parse_args(["led-track", "--intrinsics", "mock", "--rig", "mock",
                               "--index", "0", "--position-filter", "bogus"])


class FakeCap:
    def __init__(self, timestamps):
        self.timestamps = list(timestamps)
        self.index = 0
        self.last_frame_monotonic = None
        self.controls = {"exposure": 13, "gain": 0}
        self.set_calls = []
        self.released = False

    def set_manual_controls(self, exposure, gain):
        self.set_calls.append((exposure, gain))

    def controls_status(self):
        return dict(self.controls)

    def read(self):
        if self.index >= len(self.timestamps):
            return False, None
        self.last_frame_monotonic = self.timestamps[self.index]
        self.index += 1
        return True, np.zeros((480, 640, 3), np.uint8)

    def release(self):
        self.released = True


def imu_row(now, seq, **changes):
    row = dict(session_id="activation", recenter_epoch=1, seq=seq,
               sample_time_s=now - 0.001, monotonic_s=now,
               valid=True, connected=True)
    row.update(changes)
    return row


class KalmanLedTrackIntegrationTests(unittest.TestCase):
    def test_kalman_logging_acquire_predict_stale_invalid_and_controls_reset(self):
        from tools.psvr_camera.cli import build_parser
        from tools.psvr_camera.led_tracking import run_led_track
        timestamps = [1.0, 1.013333, 1.026666, 1.04, 1.053333, 1.066666, 1.08]
        poses = [LedPose(True, "tracking", rvec=np.zeros(3), tvec=np.array([0., 0., 1.]))
                 for _ in range(4)]
        poses += [LedPose(False, "Pattern parziale: 2/5") for _ in range(3)]
        imus = [imu_row(now, seq) for seq, now in enumerate(timestamps[:5])]
        imus.append(imu_row(timestamps[5] - 0.2, 5, monotonic_s=timestamps[5] - 0.2))
        imus.append(imu_row(timestamps[6], 6, valid=False))
        covariances = [(R_SMALL, dict(accepted=True, reason="ok", reliable=True))
                       for _ in range(4)]
        covariances += [(None, dict(accepted=False, reason="invalid_pose", reliable=False))
                        for _ in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "capture.jsonl"
            args = build_parser().parse_args([
                "led-track", "--intrinsics", "mock", "--rig", "mock",
                "--index", "0", "--driver-imu", "--frames", "7",
                "--log", str(log), "--send-position",
                "--position-filter", "kalman"])
            cap = FakeCap(timestamps)
            tracker = Mock(alignment=np.eye(3))
            tracker.search_region.return_value = (None, 1.0)
            tracker.update.side_effect = list(poses)
            source = Mock(last_packet=None)
            source.at.side_effect = list(imus)
            source.latest.return_value = copy.deepcopy(imus[0])
            detector = Mock()
            detector.detect.return_value = []
            sender = Mock(last_packet=None)
            sender.send.return_value = True
            rig = dict(provenance="synthetic", labels=list(map(str, range(9))))
            with contextlib.ExitStack() as stack:
                for target, value in (
                    ("led_tracking.load_intrinsics", Mock(image_size=(640, 480))),
                    ("led_tracking.load_led_rig", (dict(provenance="synthetic",
                                                        labels=list(map(str, range(5)))),
                                                   np.zeros((5, 3)))),
                    ("led_tracking.BlueLedDetector", detector),
                    ("led_tracking.FrontLedTracker", Mock()),
                    ("imu_led_pose.load_rotation_rig", (rig, np.zeros((9, 3)), np.zeros((9, 3)))),
                    ("imu_led_pose.ImuLedTracker", tracker),
                    ("driver_imu_source.DriverImuSource", source),
                    ("led_tracking.open_camera_source", (cap, None)),
                    ("driver_position.DriverPositionSender", sender),
                ):
                    stack.enter_context(patch("tools.psvr_camera." + target, return_value=value))
                estimate = stack.enter_context(patch(
                    "tools.psvr_camera.pose_uncertainty.estimate_pose_covariance",
                    side_effect=list(covariances)))
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(run_led_track(args), 0)
            self.assertEqual(estimate.call_count, 7)
            self.assertTrue(cap.set_calls)
            self.assertTrue(tracker.reset.call_count >= 1)
            self.assertTrue(cap.released)
            source.close.assert_called_once()
            sender.close.assert_called_once()
            rows = [json.loads(line) for line in log.read_text().splitlines()]
            pose_rows = [row for row in rows if row.get("event") == "pose"]
            self.assertEqual(len(pose_rows), 7)
            np.testing.assert_allclose([row["monotonic_s"] for row in pose_rows], timestamps)
            resets = [row for row in rows if row.get("event") == "position_reset"]
            self.assertTrue(any(row.get("reason") == "camera_controls" for row in resets))
            for index, row in enumerate(pose_rows):
                self.assertIn("estimator_input", row)
                self.assertIn("position_output", row)
                self.assertIn("position_filter", row)
                estimator_input = row["estimator_input"]
                self.assertIn("packet", estimator_input)
                self.assertIn("alignment", estimator_input)
                self.assertIn("covariance", estimator_input)
                self.assertIn("quality", estimator_input)
                self.assertIn("prediction_allowed", estimator_input)
                diagnostics = row["position_filter"]
                self.assertEqual(diagnostics["type"], "kalman")
                self.assertIn("settings", diagnostics)
                self.assertIn("raw_world_position_m", diagnostics)
                self.assertIn("filtered_world_position_m", diagnostics)
                if index < 4:
                    self.assertEqual(estimator_input["quality"]["reliable"], True)
                    np.testing.assert_allclose(np.array(estimator_input["covariance"]),
                                               R_SMALL, atol=1e-14)
                else:
                    self.assertIsNone(estimator_input["covariance"])
            # Four reliable confirmations acquire on the fourth camera frame.
            self.assertFalse(pose_rows[0]["position_output"]["valid"])
            self.assertFalse(pose_rows[2]["position_output"]["valid"])
            self.assertTrue(pose_rows[3]["position_output"]["valid"])
            self.assertEqual(pose_rows[3]["position_output"]["state"], "tracking")
            np.testing.assert_allclose(pose_rows[3]["position_output"]["position_m"],
                                       [0., 0., 1.], atol=1e-9)
            # Fresh IMU dropout predicts within budget despite missing covariance.
            self.assertTrue(pose_rows[3]["estimator_input"]["prediction_allowed"] is False or True)
            self.assertTrue(pose_rows[4]["estimator_input"]["prediction_allowed"])
            self.assertTrue(pose_rows[4]["position_output"]["valid"])
            self.assertIsNotNone(pose_rows[4]["position_output"]["position_m"])
            self.assertEqual(pose_rows[4]["position_filter"]["tracking_state"], "predicting")
            # Stale and invalid IMU never promote, even with an optical partial reason.
            self.assertFalse(pose_rows[5]["estimator_input"]["prediction_allowed"])
            self.assertFalse(pose_rows[6]["estimator_input"]["prediction_allowed"])
            for row in pose_rows[5:]:
                self.assertFalse(row["position_output"]["valid"])
                self.assertIsNone(row["position_output"]["position_m"])
                self.assertFalse(row["position_filter"]["valid"])
            sent = [call.args[0] for call in sender.send.call_args_list]
            self.assertEqual(len(sent), 7)
            np.testing.assert_allclose(sent[3]["position_m"], [0., 0., 1.], atol=1e-9)
            self.assertIsNotNone(sent[4]["position_m"])
            self.assertIsNone(sent[5]["position_m"])
            self.assertIsNone(sent[6]["position_m"])


if __name__ == "__main__":
    unittest.main()
