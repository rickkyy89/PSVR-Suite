"""Additional Python bridge checks, separate from the shared QA link suite.

Only synthetic geometry, mocked camera lifecycle and ephemeral loopback sockets.
"""
import json
import socket
import threading
import time
import unittest
from unittest.mock import Mock, patch
import contextlib
import io
from pathlib import Path

import numpy as np

from tools.psvr_camera.cli import build_parser, cmd_led_track
from tools.psvr_camera.driver_imu_source import (
    DriverImuSource, parse_driver_imu_packet, validate_driver_imu_packet)
from tools.psvr_camera.driver_position import (
    DriverPositionSender, build_driver_position_packet, validate_driver_position_packet)
from tools.psvr_camera.led_pose import LedPose


def imu(**updates):
    row = dict(protocol="psvr.imu", version=1, frame="psvr_imu_unrecentered",
               clock="driver_steady_seconds", session_id="activation-a", seq=1,
               recenter_epoch=1, sample_time_s=10., sent_time_s=10.,
               connected=True, valid=True, state="tracking",
               quaternion_sensor_wxyz=[1, 0, 0, 0], world_rotation_wxyz=[1, 0, 0, 0],
               corrected_gyro_rad_s=[0, 0, 0])
    row.update(updates)
    return row


class PythonDriverBridgeTests(unittest.TestCase):
    def test_codec_strict_types_and_finiteness(self):
        self.assertEqual(parse_driver_imu_packet(json.dumps(imu()).encode()), imu())
        for changes in ({"seq": True}, {"version": True}, {"recenter_epoch": -1},
                        {"sample_time_s": 11}, {"connected": False},
                        {"session_id": "x"*129}, {"sent_time_s": float("nan")},
                        {"quaternion_sensor_wxyz": [2, 0, 0, 0]},
                        {"corrected_gyro_rad_s": [True, 0, 0]},
                        {"sent_time_s": 10**1000}, {"state": []}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_driver_imu_packet(imu(**changes))
        for payload in (b"\xff", b"{" , b" "*2049, b"[]"):
            with self.assertRaises(ValueError):
                parse_driver_imu_packet(payload)
        duplicate = json.dumps(imu())[:-1]+',"seq":1}'
        with self.assertRaises(ValueError):
            parse_driver_imu_packet(duplicate)

    def test_loopback_retirement_epoch_future_and_close(self):
        source = DriverImuSource(port=0)
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        def send(row):
            tx.sendto(json.dumps(row).encode(), ("127.0.0.1", source.local_port))
        def await_seq(seq):
            end = time.perf_counter()+1
            while time.perf_counter() < end:
                row = source.latest()
                if row and row["seq"] == seq:
                    return row
                time.sleep(.002)
            self.fail("receiver did not advance")
        try:
            send(imu())
            initial = await_seq(1)
            send(imu(seq=2, sample_time_s=10.001, sent_time_s=10.001, recenter_epoch=2))
            row = await_seq(2)
            self.assertEqual(row["recenter_count"], 2)
            self.assertEqual(len(source.samples), 1)
            for rejected in (imu(seq=2), imu(seq=3, recenter_epoch=1),
                             imu(seq=3, sample_time_s=100, sent_time_s=100)):
                send(rejected)
            time.sleep(.03)
            self.assertEqual(source.latest()["seq"], 2)
            send(imu(session_id="activation-b", seq=4))
            await_seq(4)
            send(imu(seq=5))
            time.sleep(.03)
            self.assertEqual(source.latest()["session_id"], "activation-b")
            self.assertIsNone(source.at(initial["monotonic_s"]-.5, wait_s=0))
            waiter = threading.Thread(target=lambda: source.at(time.perf_counter()+100, wait_s=100))
            waiter.start()
            started = time.perf_counter()
            source.close()
            waiter.join(.5)
            self.assertFalse(waiter.is_alive())
            self.assertFalse(source.thread.is_alive())
            self.assertLess(time.perf_counter()-started, .6)
        finally:
            source.close()
            tx.close()

    def test_interpolation_uses_absolute_quaternion_and_driver_time(self):
        source = DriverImuSource(port=0)
        try:
            with source.changed:
                source.samples.extend([
                    imu(monotonic_s=1., quaternion_sensor_wxyz=[1, 0, 0, 0]),
                    imu(monotonic_s=1.04, seq=2, sample_time_s=10.04, sent_time_s=10.04,
                        quaternion_sensor_wxyz=[np.cos(.2), 0, np.sin(.2), 0])])
            row = source.at(1.02, wait_s=0)
            np.testing.assert_allclose(row["quaternion_sensor_wxyz"], [np.cos(.1), 0, np.sin(.1), 0])
            self.assertAlmostEqual(row["sample_time_s"], 10.02)
            self.assertEqual(row["monotonic_s"], 1.02)
            validate_driver_imu_packet(row)
            self.assertFalse(source.at(2., wait_s=0)["valid"])
        finally:
            source.close()

    def test_transform_scale_does_not_scale_head_offset(self):
        from tools.psvr_camera.geometry import rodrigues
        a = rodrigues([0., .7, 0.])
        rvec = np.array([.2, 0., -.3])
        r = rodrigues(rvec)
        t = np.array([.1, -.2, 1.])
        offset = np.array([.03, .04, -.08])
        sample = imu(monotonic_s=1.)
        pose = LedPose(True, "tracking", rvec=rvec, tvec=t)
        packet = build_driver_position_packet(pose, sample, a, 1, offset, 2., now=1.)
        np.testing.assert_allclose(packet["position_m"], a.T@(2*t+r@offset))
        validate_driver_position_packet(packet)
        for scale in (0, -1, float("inf")):
            with self.assertRaises(ValueError):
                build_driver_position_packet(pose, sample, a, 1, scale=scale, now=1.)
        with self.assertRaises(ValueError):
            build_driver_position_packet(pose, sample, np.zeros((3, 3)), 1, now=1.)
        self.assertIsNone(build_driver_position_packet(pose, None, a, 1, now=1.))
        for observation, now in ((LedPose(False,"lost"), 1.), (pose, 2.)):
            packet = build_driver_position_packet(observation, sample, a, 2, now=now)
            self.assertFalse(packet["valid"])
            self.assertIsNone(packet["position_m"])

    def test_sender_shutdown_is_bound_invalid(self):
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(("127.0.0.1", 0))
        rx.settimeout(1.)
        sender = DriverPositionSender(port=rx.getsockname()[1])
        try:
            pose = LedPose(True,"tracking", rvec=np.zeros(3), tvec=np.array([0.,0.,1.]))
            packet = build_driver_position_packet(pose, imu(monotonic_s=1.), np.eye(3), 1, now=1.)
            self.assertTrue(sender.send(packet))
            self.assertTrue(json.loads(rx.recv(2048))["valid"])
            sender.close()
            end = json.loads(rx.recv(2048))
            self.assertFalse(end["valid"])
            self.assertIsNone(end["position_m"])
            self.assertEqual(end["session_id"], packet["session_id"])
            self.assertGreater(end["seq"], packet["seq"])
        finally:
            sender.close()
            rx.close()

    def test_sender_reserves_wire_sequence_across_errors_and_invalidations(self):
        transport = Mock()
        transport.sendto.side_effect = [1,OSError("send failed"),1,1,
                                       OSError("send failed"),OSError("loss failed"),1,1]
        with patch("tools.psvr_camera.driver_position.socket.socket",return_value=transport):
            sender = DriverPositionSender()
        packet = build_driver_position_packet(None,imu(monotonic_s=1.),np.eye(3),7,now=1.)
        packet.update(valid=True,state="tracking",position_m=[.1,.2,1.])
        binding_keys = ("session_id","recenter_epoch","imu_seq","imu_sample_time_s")
        try:
            self.assertTrue(sender.send(packet))
            self.assertFalse(sender.send({**packet,"seq":8}))  # reserves 8, loss reserves 9
            self.assertTrue(sender.send({**packet,"seq":9}))  # caller reuses loss seq
            self.assertFalse(sender.send({**packet,"seq":10}))  # both transport attempts fail
            self.assertTrue(sender.send({**packet,"seq":11}))
            sender.close()
            wire = [json.loads(call.args[0]) for call in transport.sendto.call_args_list]
            self.assertEqual([row["seq"] for row in wire],list(range(7,15)))
            for row in wire:
                self.assertEqual({key:row[key] for key in binding_keys},
                                 {key:packet[key] for key in binding_keys})
            for index in (2,5,7):
                self.assertFalse(wire[index]["valid"])
                self.assertIsNone(wire[index]["position_m"])
            self.assertEqual(packet["seq"],7)  # caller-owned input stays unchanged
        finally:
            sender.close()

    def test_sender_explicit_invalidation_then_same_caller_sequence(self):
        rx = socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        rx.bind(("127.0.0.1",0))
        rx.settimeout(1.)
        sender = DriverPositionSender(port=rx.getsockname()[1])
        try:
            packet = build_driver_position_packet(None,imu(monotonic_s=1.),np.eye(3),7,now=1.)
            self.assertTrue(sender.send(packet))
            self.assertTrue(sender.invalidate())
            self.assertTrue(sender.send({**packet,"seq":8}))
            wire = [json.loads(rx.recv(2048)) for _ in range(3)]
            self.assertEqual([row["seq"] for row in wire],[7,8,9])
            self.assertEqual(wire[-1]["session_id"],packet["session_id"])
        finally:
            sender.close()
            rx.close()

    def test_cli_modes_defaults_and_opt_in(self):
        base = ["led-track", "--intrinsics", "unused", "--rig", "unused", "--index", "0"]
        parser = build_parser()
        args = parser.parse_args(base+["--driver-imu"])
        self.assertFalse(args.imu)
        self.assertFalse(args.send_position)
        self.assertEqual((args.value_min,args.min_area,args.exposure,args.gain), (60,6,13,0))
        self.assertEqual(args.position_scale, 1.)
        self.assertEqual(tuple(args.head_offset_model_m), (0.,0.,0.))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(base+["--driver-imu", "--imu"])
        with self.assertRaises(ValueError):
            cmd_led_track(parser.parse_args(base+["--send-position"]))

    def test_recenter_preserves_absolute_alignment_new_session_resets(self):
        from tools.psvr_camera.imu_led_pose import ImuLedTracker
        tracker = ImuLedTracker.__new__(ImuLedTracker)
        tracker.front = Mock()
        tracker.reset()
        tracker.set_imu(imu(monotonic_s=1.))
        tracker.alignment = np.eye(3)
        tracker.translation = np.array([0,0,1.])
        tracker.set_imu(imu(monotonic_s=1.,recenter_epoch=2,seq=2))
        np.testing.assert_equal(tracker.alignment, np.eye(3))
        self.assertIsNotNone(tracker.rotation(1.))
        tracker.set_imu(imu(monotonic_s=1.,session_id="new"))
        self.assertIsNone(tracker.alignment)
        self.assertIsNone(tracker.translation)

    def test_driver_dropout_reacquisition_preserves_absolute_coordinates(self):
        try:
            import cv2
        except ImportError:
            self.skipTest("OpenCV unavailable; optical reacquisition requires cv2")
        from dataclasses import replace
        from tools.psvr_camera.calibration import Intrinsics
        from tools.psvr_camera.imu_led_pose import ImuLedTracker,load_rotation_rig,MODEL_TO_IMU
        from tools.psvr_camera.protocol import quaternion_from_rotation
        rig,points,normals = load_rotation_rig(Path(__file__).resolve().parents[1]/
                                              "configurations/psvr_nine_leds_rotation_trial.json")
        k = np.array([[500.,0,320],[0,500.,240],[0,0,1.]])
        tracker = ImuLedTracker(Intrinsics(k,np.zeros(5),(640,480),True),
                                points,normals,rig["labels"])
        rvec = np.array([.05,.10,-.03])
        rotation = cv2.Rodrigues(rvec)[0]
        absolute = cv2.Rodrigues(np.array([.1,.25,.03]))[0]
        q = quaternion_from_rotation(absolute)
        t = np.array([[.02],[-.01],[1.]])
        image = cv2.projectPoints(points[:5],rvec,t,k,np.zeros(5))[0].reshape(-1,2)
        candidates = [{"center_px":xy.tolist()} for xy in image[::-1]]
        for index in range(4):
            now = 1.+index*.02
            sample = imu(monotonic_s=now-.001,quaternion_sensor_wxyz=q,recenter_count=1)
            tracker.set_imu(sample)
            pose = tracker.update(candidates,now)
        self.assertTrue(pose.valid,pose.reason)
        stable_a = tracker.alignment
        before = build_driver_position_packet(pose,sample,stable_a,1,now=now)
        tracker.set_imu(imu(monotonic_s=4.,quaternion_sensor_wxyz=q,recenter_count=1))
        self.assertFalse(tracker.update([],4.).valid)
        self.assertIs(tracker.alignment,stable_a)
        self.assertIsNone(tracker.translation)

        frontend_update = tracker.front.update
        def biased_frontend(candidates,now):
            front = frontend_update(candidates,now)
            # Correspondences remain correct, but optical orientation/seed are
            # deliberately biased: neither may redefine the activation's A.
            return (replace(front,rvec=front.rvec+np.array([[.08],[0.],[0.]]),
                            tvec=front.tvec+np.array([[.1],[0.],[0.]])) if front.valid else front)
        with patch.object(tracker.front,"update",side_effect=biased_frontend):
            for index in range(4):
                now = 4.1+index*.02
                sample = imu(monotonic_s=now-.001,quaternion_sensor_wxyz=q,
                             recenter_epoch=2,recenter_count=2)
                tracker.set_imu(sample)
                pose = tracker.update(candidates,now)
                self.assertIs(tracker.alignment,stable_a)
        self.assertTrue(pose.valid,pose.reason)
        np.testing.assert_allclose(pose.tvec,t,atol=1e-7)
        np.testing.assert_allclose(cv2.Rodrigues(pose.rvec)[0],stable_a@absolute@MODEL_TO_IMU,atol=1e-10)
        after = build_driver_position_packet(pose,sample,tracker.alignment,2,now=now)
        np.testing.assert_allclose(after["position_m"],before["position_m"],atol=1e-7)
        tracker.reset()  # explicit manual realign is still allowed to discard A
        self.assertIsNone(tracker.alignment)

    def test_diagnostic_dropout_still_discards_alignment(self):
        from tools.psvr_camera.imu_led_pose import ImuLedTracker
        tracker = ImuLedTracker.__new__(ImuLedTracker)
        tracker.front = Mock()
        tracker.reset()
        tracker.set_imu({"valid":True,"monotonic_s":4.,"recenter_count":1,
                         "quaternion_sensor_wxyz":[1,0,0,0]})
        tracker.alignment = np.eye(3)
        tracker.translation = np.array([[0.],[0.],[1.]])
        tracker.epoch = 1
        tracker.last_match = 1.
        self.assertFalse(tracker.update([],4.).valid)
        self.assertIsNone(tracker.alignment)
        self.assertIsNone(tracker.translation)

    def test_driver_capture_lifecycle_never_uses_process_or_diagnostic(self):
        from tools.psvr_camera.led_tracking import run_led_track
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(("127.0.0.1",0))
        rx.settimeout(1.)
        args = build_parser().parse_args([
            "led-track","--intrinsics","mock","--rig","mock","--index","0",
            "--driver-imu","--send-position","--frames","2",
            "--position-port",str(rx.getsockname()[1])])
        # Deliberately no process attribute on the driver source.
        class Source:
            def at(self, captured_at):
                return imu(monotonic_s=captured_at-.001)
            def close(self):
                self.closed = True
        source = Source()
        cap = Mock()
        cap.last_frame_monotonic = None
        cap.read.return_value = (True,np.zeros((480,640,3),np.uint8))
        cap.controls_status.return_value = {}
        detector = Mock()
        detector.detect.return_value = []
        tracker = Mock()
        tracker.alignment = np.eye(3)
        tracker.search_region.return_value = (None,1.)
        tracker.update.side_effect = [LedPose(True,"tracking",rvec=np.zeros(3),tvec=np.array([0.,0.,1.])),
                                      LedPose(False,"lost")]
        rig = {"provenance":"synthetic","labels":list(map(str,range(9)))}
        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch("tools.psvr_camera.led_tracking.load_intrinsics",return_value=Mock(image_size=(640,480))))
                stack.enter_context(patch("tools.psvr_camera.led_tracking.load_led_rig",return_value=(rig,np.zeros((5,3)))))
                stack.enter_context(patch("tools.psvr_camera.led_tracking.BlueLedDetector",return_value=detector))
                stack.enter_context(patch("tools.psvr_camera.led_tracking.FrontLedTracker"))
                stack.enter_context(patch("tools.psvr_camera.imu_led_pose.load_rotation_rig",return_value=(rig,np.zeros((9,3)),np.zeros((9,3)))))
                stack.enter_context(patch("tools.psvr_camera.imu_led_pose.ImuLedTracker",return_value=tracker))
                stack.enter_context(patch("tools.psvr_camera.driver_imu_source.DriverImuSource",return_value=source))
                diagnostic = stack.enter_context(patch("tools.psvr_camera.imu_source.DiagnosticImuSource",side_effect=AssertionError("HID diagnostic forbidden")))
                stack.enter_context(patch("tools.psvr_camera.led_tracking.open_camera_source",return_value=(cap,None)))
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(run_led_track(args),0)
                diagnostic.assert_not_called()
            packets = [json.loads(rx.recv(2048)) for _ in range(3)]
            self.assertTrue(packets[0]["valid"])
            self.assertFalse(packets[1]["valid"])
            self.assertFalse(packets[2]["valid"])
            self.assertIsNone(packets[1]["position_m"])
            self.assertIsNone(packets[2]["position_m"])
            self.assertTrue(source.closed)
            cap.release.assert_called_once()
        finally:
            rx.close()
