# SPDX-License-Identifier: MIT
"""Offline contract tests: no HID, camera, GUI, diagnostic process or SteamVR.

Only documented public APIs are used. UDP receive polling has a deadline; port
zero avoids clashes with an operator's bridge. Position-v1 tests stay separate.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import socket
import sys
import time
import unittest
from unittest.mock import Mock, patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from psvr_camera.driver_imu_source import (  # noqa: E402
    DriverImuSource, parse_driver_imu_packet, validate_driver_imu_packet,
)
from psvr_camera.driver_position import (  # noqa: E402
    DriverPositionSender, build_driver_position_packet,
)
from psvr_camera.led_pose import LedPose  # noqa: E402


def imu_packet(**changes):
    packet = dict(protocol="psvr.imu", version=1,
                  frame="psvr_imu_unrecentered", clock="driver_steady_seconds",
                  session_id="activation-a", seq=1, recenter_epoch=1,
                  sample_time_s=100.0, sent_time_s=100.001,
                  connected=True, valid=True, state="tracking",
                  quaternion_sensor_wxyz=[1., 0., 0., 0.],
                  world_rotation_wxyz=[1., 0., 0., 0.],
                  corrected_gyro_rad_s=[0., 0., 0.])
    packet.update(changes)
    return packet


def encode(packet):
    return json.dumps(packet, separators=(",", ":")).encode("utf-8")


class DriverImuCodecTests(unittest.TestCase):
    def test_wire_roundtrip_and_invalid_status_is_legal(self):
        packet = imu_packet()
        validate_driver_imu_packet(packet)
        parsed = parse_driver_imu_packet(encode(packet))
        for key, value in packet.items():
            self.assertEqual(parsed[key], value)
        validate_driver_imu_packet(imu_packet(valid=False, connected=False, state="lost"))

    def test_every_mandatory_field_is_required(self):
        for key in imu_packet():
            with self.subTest(field=key), self.assertRaises(ValueError):
                packet = imu_packet()
                del packet[key]
                validate_driver_imu_packet(packet)

    def test_bad_types_values_and_timing(self):
        cases = {
            "protocol": ["psvr.position", None], "version": [True, 1.0, 2],
            "frame": ["psvr_world"], "clock": ["unix_seconds"],
            "session_id": ["", None, 1, "x" * 4096],
            "seq": [True, -1, 1.5, "1"],
            "recenter_epoch": [False, -1, 1.5],
            "sample_time_s": [True, "100", float("nan"), float("inf"), 101.],
            "sent_time_s": [False, None, float("inf"), 99.],
            "connected": [1, "true"], "valid": [1, "true"],
            "state": [None, 1, "lost"],
            "quaternion_sensor_wxyz": [[0, 0, 0, 0], [2, 0, 0, 0],
                                        [1, 0, 0], [True, 0, 0, 0],
                                        [float("nan"), 0, 0, 0]],
            "world_rotation_wxyz": [[1.01, 0, 0, 0], [1, 0, 0, float("inf")]],
            "corrected_gyro_rad_s": [[0, 0], [0, 0, float("nan")], [True, 0, 0]],
        }
        for key, values in cases.items():
            for value in values:
                with self.subTest(field=key, value=value), self.assertRaises(ValueError):
                    validate_driver_imu_packet(imu_packet(**{key: value}))
        with self.assertRaises(ValueError):
            validate_driver_imu_packet(imu_packet(connected=False))
        with self.assertRaises(ValueError):
            validate_driver_imu_packet(imu_packet(sent_time_s=110.))
        for value in (None, [], 12):
            with self.subTest(root=value), self.assertRaises(ValueError):
                validate_driver_imu_packet(value)

    def test_unit_quaternion_tolerance_and_negative_equivalent(self):
        for q in ([1.0005, 0, 0, 0], [-1, 0, 0, 0]):
            validate_driver_imu_packet(imu_packet(quaternion_sensor_wxyz=q))
        with self.assertRaises(ValueError):
            validate_driver_imu_packet(imu_packet(quaternion_sensor_wxyz=[1.002, 0, 0, 0]))

    def test_parser_rejects_duplicates_oversize_and_malformed_json(self):
        good = encode(imu_packet())
        cases = [b"", b"[]", b"null", b"{", b"\xff", good + b"{}",
                 good + b" " * (2049 - len(good))]
        # Equal duplicates must fail too: last-key-wins is not a safe codec.
        for key in ("seq", "session_id", "valid"):
            value = json.dumps(imu_packet()[key]).encode()
            cases.append(good[:-1] + b',"' + key.encode() + b'":' + value + b"}")
        cases.append(good.replace(b"100.0", b"NaN"))
        for index, payload in enumerate(cases):
            with self.subTest(case=index, payload=payload[-80:]), self.assertRaises(ValueError):
                parse_driver_imu_packet(payload)


class DriverImuLoopbackTests(unittest.TestCase):
    def setUp(self):
        self.source = DriverImuSource(port=0)
        self.addCleanup(self.source.close)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(self.socket.close)
        self.assertGreater(self.source.local_port, 0)

    def send(self, packet):
        self.socket.sendto(encode(packet), ("127.0.0.1", self.source.local_port))

    def await_sample(self, seq, session="activation-a", epoch=1):
        deadline = time.perf_counter() + 1.0
        while time.perf_counter() < deadline:
            sample = self.source.latest()
            if (sample is not None and sample.get("seq") == seq
                    and sample.get("session_id") == session
                    and sample.get("recenter_epoch") == epoch):
                return sample
            time.sleep(.002)
        self.fail("receiver did not publish identity %r" % ((session, epoch, seq),))

    def assert_stays(self, seq, session="activation-a", epoch=1):
        deadline = time.perf_counter() + .08
        while time.perf_counter() < deadline:
            sample = self.source.latest()
            self.assertIsNotNone(sample)
            self.assertEqual((sample["session_id"], sample["recenter_epoch"], sample["seq"]),
                             (session, epoch, seq))
            time.sleep(.002)

    def test_receive_metadata_duplicate_order_and_malformed_fresh_packet(self):
        self.send(imu_packet(seq=10))
        sample = self.await_sample(10)
        self.assertTrue(math.isfinite(sample["monotonic_s"]))
        self.assertLess(abs(time.perf_counter() - sample["monotonic_s"]), .5)
        self.assertEqual(sample["recenter_count"], 1)
        self.send(imu_packet(seq=10, quaternion_sensor_wxyz=[0, 1, 0, 0]))
        self.send(imu_packet(seq=9))
        self.send(imu_packet(seq=999, quaternion_sensor_wxyz=[0, 0, 0, 0]))
        self.assert_stays(10)
        self.assertEqual(self.source.latest()["quaternion_sensor_wxyz"], [1, 0, 0, 0])
        self.send(imu_packet(seq=11))
        self.await_sample(11)  # malformed high seq must not poison accepted order

    def test_valid_false_is_fresh_loss_not_reused_tracking(self):
        self.send(imu_packet())
        self.await_sample(1)
        self.send(imu_packet(seq=2, valid=False, state="lost"))
        sample = self.await_sample(2)
        self.assertFalse(sample["valid"])
        at = self.source.at(sample["monotonic_s"], wait_s=0)
        self.assertTrue(at is None or not at["valid"])

    def test_epoch_session_boundaries_and_retired_session(self):
        self.send(imu_packet(seq=10))
        old = self.await_sample(10)
        time.sleep(.02)
        self.send(imu_packet(seq=11, recenter_epoch=2, sample_time_s=100.02, sent_time_s=100.021))
        self.await_sample(11, epoch=2)
        self.assertIsNone(self.source.at(old["monotonic_s"] - .001, wait_s=0))
        self.assertIsNone(self.source.at(old["monotonic_s"] + .005, wait_s=0))
        self.send(imu_packet(seq=12, recenter_epoch=1))
        self.assert_stays(11, epoch=2)
        time.sleep(.02)
        self.send(imu_packet(seq=0, session_id="activation-b", recenter_epoch=0,
                             sample_time_s=1., sent_time_s=1.001))
        self.await_sample(0, "activation-b", 0)
        boundary = self.source.at(old["monotonic_s"] + .005, wait_s=0)
        self.assertIsNone(boundary)
        self.send(imu_packet(seq=999, recenter_epoch=999))
        self.assert_stays(0, "activation-b", 0)
        self.send(imu_packet(seq=1, session_id="activation-b", recenter_epoch=0,
                             sample_time_s=1.01, sent_time_s=1.011))
        self.await_sample(1, "activation-b", 0)

    def test_same_epoch_interpolation_retains_driver_clock_and_stale_is_unusable(self):
        self.send(imu_packet())
        self.await_sample(1)
        time.sleep(.04)
        self.send(imu_packet(seq=2, sample_time_s=100.04, sent_time_s=100.041,
                             quaternion_sensor_wxyz=[math.cos(.1), 0, math.sin(.1), 0]))
        last = self.await_sample(2)
        target = last["monotonic_s"] - .02
        sample = self.source.at(target, wait_s=0)
        self.assertIsNotNone(sample)
        self.assertTrue(sample.get("interpolated"))
        self.assertAlmostEqual(sample["sample_time_s"], 100.02, places=4)
        self.assertAlmostEqual(sample["monotonic_s"], target, places=6)
        np.testing.assert_allclose(sample["quaternion_sensor_wxyz"],
                                   [math.cos(.05), 0, math.sin(.05), 0], atol=2e-4)
        self.assertEqual(sample["session_id"], "activation-a")
        self.assertEqual(sample["recenter_epoch"], 1)
        self.assertIsNone(self.source.at(target - 10, wait_s=0))
        stale = self.source.at(last["monotonic_s"] + 10, wait_s=0)
        self.assertTrue(stale is None or not stale.get("valid"), "stale IMU cannot remain usable")

    def test_empty_wait_and_close_are_bounded_and_release_port(self):
        start = time.perf_counter()
        self.assertIsNone(self.source.at(start, wait_s=.025))
        self.assertLess(time.perf_counter() - start, .5)
        port = self.source.local_port
        start = time.perf_counter()
        self.source.close()
        self.source.close()  # cleanup and caller shutdown may both close
        self.assertLess(time.perf_counter() - start, 1.)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as replacement:
            replacement.bind(("127.0.0.1", port))


class DriverPositionMathTests(unittest.TestCase):
    def setUp(self):
        self.sample = imu_packet(monotonic_s=20., recenter_count=1)
        self.pose = LedPose(True, "tracking", rvec=np.zeros((3, 1)),
                            tvec=np.array([[.2], [.3], [1.]]))

    def build(self, **kwargs):
        args = dict(pose=self.pose, imu_sample=self.sample, alignment=np.eye(3), seq=7, now=20.)
        args.update(kwargs)
        return build_driver_position_packet(**args)

    def test_v2_identity_and_unflipped_led_origin(self):
        packet = self.build()
        expected = dict(protocol="psvr.position", version=2, frame="psvr_imu_unrecentered",
                        units="m", session_id="activation-a", recenter_epoch=1,
                        imu_seq=1, imu_sample_time_s=100., seq=7, monotonic_s=20.,
                        valid=True, state="tracking", source="blob_led")
        for key, value in expected.items():
            self.assertEqual(packet[key], value, key)
        self.assertIs(type(packet["host_time_ns"]), int)
        self.assertGreater(packet["host_time_ns"], 0)
        np.testing.assert_allclose(packet["position_m"], [.2, .3, 1.])

    def test_transpose_scale_and_rotated_physical_offset(self):
        # Independent, exact +90-degree rotations around different axes.
        a = np.array([[0., 0., 1.], [0., 1., 0.], [-1., 0., 0.]])
        r = np.array([[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]])
        self.pose.rvec = np.array([[0.], [0.], [math.pi / 2]])
        offset = np.array([.1, .02, -.03])
        expected = a.T @ (2.5 * self.pose.tvec.reshape(3) + r @ offset)
        packet = self.build(alignment=a, scale=2.5, head_offset_model_m=offset)
        np.testing.assert_allclose(packet["position_m"], expected, atol=1e-10)
        # Physical offset must not grow when the nominal LED model scale changes.
        zero = self.build(alignment=a, scale=2.5)
        np.testing.assert_allclose(np.array(packet["position_m"]) - zero["position_m"], a.T @ r @ offset)

    def test_recenter_world_yaw_not_applied_in_camera_packet(self):
        first = self.build()["position_m"]
        self.sample["world_rotation_wxyz"] = [math.cos(.7), 0., math.sin(.7), 0.]
        np.testing.assert_allclose(self.build()["position_m"], first)

    def test_lost_no_pose_and_invalid_imu_publish_bound_loss(self):
        for pose in (None, LedPose(False, "lost")):
            with self.subTest(pose=pose):
                packet = self.build(pose=pose)
                self.assertIsNotNone(packet)
                self.assertFalse(packet["valid"])
                self.assertEqual(packet["session_id"], "activation-a")
                self.assertEqual(packet["version"], 2)
                self.assertNotEqual(packet["state"], "tracking")
        self.sample.update(valid=False, state="lost")
        self.assertFalse(self.build()["valid"])

    def test_stale_future_imu_and_missing_alignment_publish_loss(self):
        for changes in (dict(now=30.), dict(now=19.), dict(alignment=None)):
            with self.subTest(changes=changes):
                packet = self.build(**changes)
                self.assertFalse(packet["valid"])
                self.assertIsNone(packet["position_m"])

    def test_no_identity_cannot_emit_unbound_packet(self):
        self.assertIsNone(self.build(imu_sample=None))
        for key in ("session_id", "recenter_epoch", "seq", "sample_time_s"):
            sample = dict(self.sample)
            del sample[key]
            with self.subTest(missing=key):
                self.assertIsNone(self.build(imu_sample=sample))

    def test_invalid_transform_parameters_are_rejected(self):
        cases = [dict(scale=value) for value in (0, -1, float("nan"), float("inf"))]
        cases += [dict(head_offset_model_m=value) for value in
                  ([0, 0], [0, 0, float("nan")], [0, 0, float("inf")])]
        cases += [dict(alignment=value) for value in
                  (np.zeros((3, 3)), np.diag([1., 1., -1.]), np.eye(2), np.eye(3) * 2)]
        for params in cases:
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.build(**params)


class DriverPositionSenderTests(unittest.TestCase):
    def setUp(self):
        self.receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.receiver.bind(("127.0.0.1", 0))
        self.receiver.settimeout(.5)
        self.addCleanup(self.receiver.close)
        self.sender = DriverPositionSender(port=self.receiver.getsockname()[1])
        self.addCleanup(self.sender.close)
        # A wire fixture, not a replacement for the transform implementation.
        self.packet = build_driver_position_packet(
            LedPose(False, "lost"), imu_packet(monotonic_s=20.), np.eye(3), 7, now=20.)
        self.packet.update(valid=True, state="tracking", position_m=[.2, .3, 1.])

    def receive(self):
        return json.loads(self.receiver.recvfrom(2048)[0])

    def test_loopback_send_and_terminating_invalid_identity(self):
        self.assertTrue(self.sender.send(self.packet))
        self.assertEqual(self.receive(), self.packet)
        start = time.perf_counter()
        self.sender.close()
        self.assertLess(time.perf_counter() - start, .5)
        terminal = self.receive()
        self.assertFalse(terminal["valid"])
        self.assertIsNone(terminal["position_m"])
        self.assertGreater(terminal["seq"], self.packet["seq"])
        for key in ("session_id", "recenter_epoch", "imu_seq", "imu_sample_time_s"):
            self.assertEqual(terminal[key], self.packet[key])
        self.sender.close()
        self.assertFalse(self.sender.send(self.packet))

    def test_socket_error_returns_false_and_attempts_loss(self):
        # Constructor injection only; no implementation state is modified.
        transport = Mock()
        transport.sendto.side_effect = [OSError("offline test error"), 1, 1]
        with patch("psvr_camera.driver_position.socket.socket", return_value=transport):
            sender = DriverPositionSender(port=self.receiver.getsockname()[1])
        self.addCleanup(sender.close)
        self.assertFalse(sender.send(self.packet))
        self.assertGreaterEqual(transport.sendto.call_count, 2)
        attempted_loss = json.loads(transport.sendto.call_args_list[1].args[0])
        self.assertFalse(attempted_loss["valid"])
        self.assertIsNone(attempted_loss["position_m"])
        self.assertEqual(attempted_loss["session_id"], "activation-a")

    def test_without_identity_close_emits_nothing(self):
        self.assertFalse(self.sender.send(None))
        self.sender.close()
        self.receiver.settimeout(.05)
        with self.assertRaises(socket.timeout):
            self.receiver.recvfrom(2048)


if __name__ == "__main__":
    unittest.main()
