"""Offline HEAD smoothing contracts; synthetic data and mocked capture only."""
import contextlib
import copy
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np

from tools.psvr_camera.position_filter import HeadPositionFilter
from tools.psvr_camera.driver_position import build_driver_position_packet
from tools.psvr_camera.geometry import rodrigues
from tools.psvr_camera.led_pose import LedPose


def sample(now=1., **changes):
    row = dict(session_id="activation", recenter_epoch=1, seq=1,
               sample_time_s=10., monotonic_s=now, valid=True, connected=True)
    row.update(changes)
    return row


def packet(position=(0., 0., 1.), **changes):
    row = dict(session_id="activation", recenter_epoch=1, valid=True,
               state="tracking", position_m=list(position), seq=1)
    row.update(changes)
    return row


class HeadFilterTests(unittest.TestCase):
    def test_defaults_and_invalid_settings(self):
        f = HeadPositionFilter()
        self.assertEqual(f.settings, dict(min_cutoff_hz=8., beta=15.,
            max_cutoff_hz=40., derivative_cutoff_hz=4., max_gap_s=.25,
            invalid_hold_s=.1))
        for change in (dict(beta=-1), dict(min_cutoff_hz=0),
                       dict(max_cutoff_hz=7), dict(derivative_cutoff_hz=0),
                       dict(max_gap_s=0), dict(invalid_hold_s=.3),
                       dict(beta=float("nan"))):
            with self.subTest(change=change), self.assertRaises(ValueError):
                HeadPositionFilter(**change)

    def test_rest_noise_suppression(self):
        rng = np.random.default_rng(42)
        raw = rng.normal(0, .001, (1200, 3)) + [0., 0., 1.]
        f = HeadPositionFilter()
        filtered = np.array([f.filter_packet(packet(p), np.eye(3),
                              1.+i/120.)["position_m"] for i, p in enumerate(raw)])
        ratio = np.std(filtered[100:], axis=0)/np.std(raw[100:], axis=0)
        self.assertTrue(np.all(ratio < .55), ratio)
        np.testing.assert_allclose(filtered[100:].mean(axis=0), [0, 0, 1], atol=.00015)

    def test_light_rest_profile_reduces_depth_noise_and_preserves_step_response(self):
        rng = np.random.default_rng(43)
        positions = np.zeros((750, 3)) + [0., 0., 1.]
        positions[:, 2] += rng.normal(0, .003, len(positions))
        outputs=[]
        for settings in ({}, dict(min_cutoff_hz=2.5, beta=8.)):
            f=HeadPositionFilter(**settings)
            output=np.array([f.filter_packet(packet(p), np.eye(3), 1.+i/75.)["position_m"]
                             for i,p in enumerate(positions)])
            outputs.append(output[100:,2])
        self.assertLess(np.std(outputs[1]), .8*np.std(outputs[0]))
        self.assertAlmostEqual(np.mean(outputs[1]), 1., delta=.0003)
        f=HeadPositionFilter(min_cutoff_hz=2.5, beta=8.)
        f.filter_packet(packet(), np.eye(3), 1.)
        values=[f.filter_packet(packet((0.,0.,1.1)), np.eye(3), 1.+i/75.)["position_m"][2]
                for i in range(1,7)]
        self.assertGreater(values[0], 1.)
        self.assertGreater(values[-1], 1.095)  # >95% in 80ms, no queued frames
        self.assertTrue(np.all(np.diff(values) >= 0))

    def test_step_immediate_bounded_and_fast_without_outlier_rejection(self):
        f = HeadPositionFilter()
        f.filter_packet(packet(), np.eye(3), 1.)
        values = [f.filter_packet(packet((1., 0., 1.)), np.eye(3),
                  1.+i/120.)["position_m"][0] for i in range(1, 13)]
        self.assertGreater(values[0], 0.)  # no queued frame delay
        self.assertLess(values[0], 1.)
        self.assertTrue(np.all(np.diff(values) >= 0))
        self.assertTrue(all(0 <= v <= 1 for v in values))
        self.assertGreater(values[5], .99)
        self.assertLessEqual(f.cutoff_hz, 40.)

    def test_variable_actual_dt_matches_independent_recurrence(self):
        f = HeadPositionFilter()
        previous_raw = np.array([0., 0., 1.])
        expected = previous_raw.copy()
        derivative = np.zeros(3)
        now = 1.
        f.filter_packet(packet(previous_raw), np.eye(3), now)
        for dt, raw in zip((.007, .021, .009, .08),
                           ([.01, .02, 1], [.03, 0, 1], [.02, -.01, 1], [.1, .02, 1])):
            now += dt
            raw = np.array(raw)
            ad = 2*math.pi*4*dt/(1+2*math.pi*4*dt)
            derivative += ad*((raw-previous_raw)/dt-derivative)
            cutoff = min(40., 8.+15.*np.linalg.norm(derivative))
            alpha = 2*math.pi*cutoff*dt/(1+2*math.pi*cutoff*dt)
            expected += alpha*(raw-expected)
            out = f.filter_packet(packet(raw), np.eye(3), now)
            np.testing.assert_allclose(out["position_m"], expected, atol=1e-14)
            previous_raw = raw

    def test_brief_loss_80_to_100ms_preserves_history_and_elapsed_dt(self):
        for dt in (.08, .1):
            with self.subTest(dt=dt):
                f = HeadPositionFilter(beta=0)
                f.filter_packet(packet(), np.eye(3), 1.)
                lost = packet(valid=False, state="lost", position_m=None)
                self.assertEqual(f.filter_packet(lost, np.eye(3), 1.+dt), lost)
                self.assertIsNone(f.diagnostics()["filtered_world_position_m"])
                out = f.filter_packet(packet((1., 0., 1.)), np.eye(3), 1.+dt)
                alpha = 2*math.pi*8*dt/(1+2*math.pi*8*dt)
                self.assertAlmostEqual(out["position_m"][0], alpha)

    def test_long_loss_none_and_invalid_callback_reset_history(self):
        for mode in ("packet", "none", "callback"):
            with self.subTest(mode=mode):
                f = HeadPositionFilter()
                f.filter_packet(packet(), np.eye(3), 1.)
                if mode == "callback":
                    f.invalid(1.101)
                else:
                    lost = None if mode == "none" else packet(valid=False, position_m=None)
                    out = f.filter_packet(lost, np.eye(3), 1.101)
                    self.assertEqual(out, lost)
                self.assertIsNone(f.diagnostics()["raw_world_position_m"])
                out = f.filter_packet(packet((1., 0., 1.)), np.eye(3), 1.11)
                self.assertEqual(out["position_m"], [1., 0., 1.])

    def test_gap_identity_alignment_manual_reset_and_clock_boundaries(self):
        cases = [(dict(), np.eye(3), 1.25, False),
                 (dict(), np.eye(3), 1.3, False),
                 (dict(session_id="new"), np.eye(3), 1.01, False),
                 (dict(recenter_epoch=2), np.eye(3), 1.01, False),
                 (dict(), rodrigues([0, .2, 0]), 1.01, False),
                 (dict(), None, 1.01, False),
                 (dict(), np.eye(3), 1.01, True),
                 (dict(), np.eye(3), 1., False),
                 (dict(), np.eye(3), .9, False)]
        for changes, a, now, reset in cases:
            with self.subTest(changes=changes, now=now, reset=reset):
                f = HeadPositionFilter()
                f.filter_packet(packet(), np.eye(3), 1.)
                if reset:
                    f.reset()
                out = f.filter_packet(packet((1., 0., 1.), **changes), a, now)
                self.assertEqual(out["position_m"], [1., 0., 1.])

    def test_inputs_and_metadata_not_mutated(self):
        f = HeadPositionFilter()
        a = np.eye(3)
        p = packet()
        p["imu"] = {"quaternion_sensor_wxyz": [1, 0, 0, 0]}
        before = copy.deepcopy(p)
        out = f.filter_packet(p, a, 1.)
        out["position_m"][0] = 50
        self.assertEqual(p, before)
        self.assertEqual(f.diagnostics()["filtered_world_position_m"], [0., 0., 1.])
        np.testing.assert_equal(a, np.eye(3))
        for now in (-1., float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                f.filter_packet(p, a, now)
        for position in ([0, 1], [0, float("nan"), 1]):
            with self.assertRaises(ValueError):
                f.filter_packet(packet(position), a, 1.01)

    def test_250ms_gap_boundary_across_timestamp_rounding(self):
        f = HeadPositionFilter()
        f.filter_packet(packet(), np.eye(3), 1.8)
        out = f.filter_packet(packet((1., 0., 1.)), np.eye(3), 2.05)
        self.assertEqual(out["position_m"], [1., 0., 1.])

    def test_final_head_transform_physical_offset_and_staleness(self):
        a = rodrigues([0., .7, 0.])
        rvec = np.array([.2, 0., -.3])
        t = np.array([.1, -.2, 1.])
        offset = np.array([.03, .04, -.08])
        pose = LedPose(True, "tracking", rvec=rvec, tvec=t)
        f = HeadPositionFilter()
        for scale in (1., 2.5):
            f.reset()
            raw = build_driver_position_packet(pose, sample(), a, 1, offset, scale, now=1.)
            original = copy.deepcopy(raw)
            out = f.filter_packet(raw, a, 1.)
            np.testing.assert_allclose(out["position_m"], a.T@(scale*t+rodrigues(rvec)@offset))
            self.assertEqual(raw, original)
            np.testing.assert_allclose(f.diagnostics()["raw_world_position_m"], out["position_m"])
        for changes in (dict(valid=False), dict(connected=False), dict(monotonic_s=0.),
                        dict(monotonic_s=2.)):
            raw = build_driver_position_packet(pose, sample(**changes), a, 2, now=1.)
            out = f.filter_packet(raw, a, 1.)
            self.assertFalse(out["valid"])
            self.assertIsNone(out["position_m"])
        self.assertIsNone(f.filter_packet(None, a, 1.01))


class CaptureWiringTests(unittest.TestCase):
    def test_opt_in_final_point_logging_and_stop_reset(self):
        from tools.psvr_camera.led_tracking import run_led_track
        from tools.psvr_camera.cli import build_parser
        for enabled, manual_realign in ((False, False), (True, False), (True, True)):
            with self.subTest(send_position=enabled, realign=manual_realign), tempfile.TemporaryDirectory() as tmp:
                log = Path(tmp)/"capture.jsonl"
                args = build_parser().parse_args(["led-track", "--intrinsics", "mock",
                    "--rig", "mock", "--index", "0", "--driver-imu", "--frames", "2",
                    "--log", str(log)] + (["--send-position"] if enabled else []))
                args.head_offset_model_m = (.03, .04, -.08)
                args.position_scale = 2.5
                poses = [LedPose(True, "tracking", rvec=np.array([.2, 0, -.3]),
                                tvec=np.array([.1, -.2, 1.])), LedPose(False, "lost")]
                if manual_realign:
                    poses[1] = LedPose(True, "tracking", rvec=np.zeros(3),
                                       tvec=np.array([.4, 0., 1.]))
                a = rodrigues([0, .7, 0])
                tracker = Mock(alignment=a)
                tracker.search_region.return_value = (None, 1.)
                tracker.update.side_effect = poses
                source = Mock()
                source.at.side_effect = lambda now: sample(now)
                cap = Mock(last_frame_monotonic=None)
                cap.read.return_value = (True, np.zeros((480, 640, 3), np.uint8))
                cap.controls_status.return_value = {}
                detector = Mock()
                detector.detect.return_value = []
                sender = Mock(last_packet=None)
                sender.send.return_value = True
                f = HeadPositionFilter()
                rig = dict(provenance="synthetic", labels=list(map(str, range(9))))
                with contextlib.ExitStack() as stack:
                    if manual_realign:
                        import threading
                        events = [threading.Event() for _ in range(4)]
                        events[2] = Mock()
                        events[2].is_set.side_effect = [False, True]
                        stack.enter_context(patch("tools.psvr_camera.led_tracking.threading.Event",
                                                  side_effect=events))
                    for target, value in (
                        ("led_tracking.load_intrinsics", Mock(image_size=(640, 480))),
                        ("led_tracking.load_led_rig", (rig, np.zeros((5, 3)))),
                        ("led_tracking.BlueLedDetector", detector),
                        ("led_tracking.FrontLedTracker", Mock()),
                        ("imu_led_pose.load_rotation_rig", (rig, np.zeros((9, 3)), np.zeros((9, 3)))),
                        ("imu_led_pose.ImuLedTracker", tracker),
                        ("driver_imu_source.DriverImuSource", source),
                        ("led_tracking.open_camera_source", (cap, None))):
                        stack.enter_context(patch("tools.psvr_camera."+target, return_value=value))
                    factory = stack.enter_context(patch("tools.psvr_camera.position_filter.HeadPositionFilter", return_value=f))
                    sender_factory = stack.enter_context(patch("tools.psvr_camera.driver_position.DriverPositionSender", return_value=sender))
                    reset = stack.enter_context(patch.object(f, "reset", wraps=f.reset))
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(run_led_track(args), 0)
                    self.assertEqual(factory.call_count, int(enabled))
                    self.assertEqual(sender_factory.call_count, int(enabled))
                    self.assertEqual(reset.call_count, int(enabled)+int(manual_realign))
                rows = [json.loads(line) for line in log.read_text().splitlines()]
                pose_rows = [row for row in rows if row["event"] == "pose"]
                self.assertEqual("position_filter" in pose_rows[0], enabled)
                if enabled:
                    sent = [call.args[0] for call in sender.send.call_args_list]
                    expected = a.T@(2.5*poses[0].tvec+rodrigues(poses[0].rvec)@args.head_offset_model_m)
                    np.testing.assert_allclose(sent[0]["position_m"], expected)
                    diag = pose_rows[0]["position_filter"]
                    np.testing.assert_allclose(diag["raw_world_position_m"], expected)
                    np.testing.assert_allclose(diag["filtered_world_position_m"], expected)
                    self.assertEqual(diag["settings"], f.settings)
                    if manual_realign:
                        np.testing.assert_allclose(sent[1]["position_m"],
                            a.T@(2.5*poses[1].tvec+args.head_offset_model_m))
                        events[2].clear.assert_called_once()
                    else:
                        self.assertFalse(sent[1]["valid"])
                        self.assertIsNone(sent[1]["position_m"])
                    sender.close.assert_called_once()
                    self.assertIsNone(f.diagnostics()["filtered_world_position_m"])
                np.testing.assert_allclose(poses[0].tvec, [.1, -.2, 1.])
                np.testing.assert_allclose(tracker.set_imu.call_args_list[0].args[0]["monotonic_s"],
                                           pose_rows[0]["monotonic_s"])
                cap.release.assert_called_once()
                source.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
