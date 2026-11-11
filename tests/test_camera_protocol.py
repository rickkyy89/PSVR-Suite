# SPDX-License-Identifier: MIT
"""Tests for the versioned M9 position protocol and coordinate transform.

Pure stdlib plus NumPy (already a project dependency). No camera, no OpenCV, no
network: the UDP socket is faked.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from psvr_camera import protocol as P  # noqa: E402
from psvr_camera.estimator import optical_to_world, optical_rotation_to_world  # noqa: E402
from psvr_camera.udp_sender import SequenceAllocator, UdpPositionSender  # noqa: E402


def sample(**overrides):
    data = dict(
        seq=0,
        monotonic_s=1.0,
        valid=True,
        state="tracking",
        source="aruco",
        position_m=[0.1, 0.2, -0.5],
        orientation_wxyz=[1.0, 0.0, 0.0, 0.0],
        reprojection_px=0.4,
        inliers=4,
        ambiguity_ratio=0.0,
    )
    data.update(overrides)
    return P.PositionSample(**data)


class ProtocolDocumentTest(unittest.TestCase):
    def test_json_file_matches_code(self):
        documented = json.loads(
            (ROOT / "tools/psvr_camera/protocol.json").read_text(encoding="utf-8")
        )
        self.assertEqual(documented, P.protocol_document())

    def test_frame_and_axes_are_driver_frame(self):
        self.assertEqual(P.COORDINATE_FRAME, "psvr_world")
        self.assertEqual(P.AXES, {"right": "+X", "up": "+Y", "forward": "-Z"})
        self.assertEqual(P.ORIENTATION_CONVENTION, "world_from_body")

    def test_default_port_avoids_known_services(self):
        self.assertNotEqual(P.DEFAULT_UDP_PORT, 4242)
        self.assertEqual(P.DEFAULT_UDP_HOST, "127.0.0.1")


class PacketValidationTest(unittest.TestCase):
    def test_round_trip(self):
        packet = sample().to_packet()
        encoded = P.encode_packet(packet)
        self.assertTrue(encoded.endswith(b"\n"))
        self.assertEqual(P.decode_packet(encoded), packet)

    def test_compact_and_sorted(self):
        encoded = P.encode_packet(sample().to_packet())
        text = encoded.decode("utf-8")
        self.assertIn('"protocol":"psvr.position"', text)
        self.assertEqual(text, json.dumps(json.loads(text), sort_keys=True, separators=(",", ":")) + "\n")

    def test_wrong_version_rejected(self):
        packet = sample().to_packet()
        packet["version"] = 2
        with self.assertRaises(P.ProtocolError):
            P.validate_packet(packet)

    def test_wrong_frame_rejected(self):
        packet = sample().to_packet()
        packet["frame"] = "opencv"
        with self.assertRaises(P.ProtocolError):
            P.validate_packet(packet)

    def test_valid_requires_finite_position(self):
        packet = sample().to_packet()
        packet["position_m"] = None
        with self.assertRaises(P.ProtocolError):
            P.validate_packet(packet)

    def test_non_finite_position_rejected(self):
        packet = sample().to_packet()
        packet["position_m"] = [0.0, float("nan"), 0.0]
        with self.assertRaises(P.ProtocolError):
            P.validate_packet(packet)

    def test_invalid_packet_may_carry_null_position(self):
        packet = sample(valid=False, position_m=None, state="lost").to_packet()
        self.assertIsNone(packet["position_m"])
        P.validate_packet(packet)

    def test_non_unit_quaternion_rejected(self):
        packet = sample().to_packet()
        packet["orientation_wxyz"] = [2.0, 0.0, 0.0, 0.0]
        with self.assertRaises(P.ProtocolError):
            P.validate_packet(packet)

    def test_negative_sequence_rejected(self):
        packet = sample().to_packet()
        packet["seq"] = -1
        with self.assertRaises(P.ProtocolError):
            P.validate_packet(packet)

    def test_decode_garbage_rejected(self):
        with self.assertRaises(P.ProtocolError):
            P.decode_packet(b"not json")
        with self.assertRaises(P.ProtocolError):
            P.decode_packet(b"\xff\xfe")

    def test_unknown_state_rejected(self):
        packet = sample().to_packet()
        packet["state"] = "teleporting"
        with self.assertRaises(P.ProtocolError):
            P.validate_packet(packet)


class CoordinateTransformTest(unittest.TestCase):
    def test_forward_is_negative_z(self):
        # A point 0.5 m in front of the OpenCV camera (+Z) is -Z in the driver frame.
        self.assertEqual(list(optical_to_world([0.0, 0.0, 0.5])), [0.0, -0.0, -0.5])
        self.assertEqual(list(optical_to_world([0.2, 0.1, 0.0])), [0.2, -0.1, -0.0])

    def test_rotation_transform_is_proper(self):
        import numpy as np

        rotation = np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
        world = optical_rotation_to_world(rotation)
        self.assertAlmostEqual(float(np.linalg.det(world)), 1.0, places=12)


class SenderTest(unittest.TestCase):
    class _FakeSocket:
        def __init__(self):
            self.sent = []
            self.timeout = None
            self.closed = False

        def settimeout(self, value):
            self.timeout = value

        def sendto(self, data, address):
            self.sent.append((data, address))
            return len(data)

        def close(self):
            self.closed = True

    def test_sends_validated_bytes_to_endpoint(self):
        fake = self._FakeSocket()
        sender = UdpPositionSender("127.0.0.1", 4271, socket_factory=lambda: fake)
        written = sender.send_sample(sample())
        self.assertGreater(written, 0)
        self.assertEqual(sender.sent, 1)
        data, address = fake.sent[0]
        self.assertEqual(address, ("127.0.0.1", 4271))
        self.assertEqual(P.decode_packet(data)["seq"], 0)
        sender.close()
        self.assertTrue(fake.closed)

    def test_invalid_packet_not_sent(self):
        fake = self._FakeSocket()
        sender = UdpPositionSender(socket_factory=lambda: fake)
        bad = sample().to_packet()
        bad["version"] = 9
        with self.assertRaises(P.ProtocolError):
            sender.send_packet(bad)
        self.assertEqual(fake.sent, [])
        self.assertEqual(sender.sent, 0)

    def test_bad_endpoint_rejected(self):
        with self.assertRaises(ValueError):
            UdpPositionSender(port=0)
        with self.assertRaises(ValueError):
            UdpPositionSender(host="")


class SequenceTest(unittest.TestCase):
    def test_monotonic_from_zero(self):
        allocator = SequenceAllocator()
        self.assertEqual([allocator.next() for _ in range(3)], [0, 1, 2])


if __name__ == "__main__":
    unittest.main()
