"""Opt-in bound position v2; deliberately independent of the ArUco v1 codec."""
import json
import socket
import time

import numpy as np

from .driver_imu_source import _identity, _number, MAX_AGE_S
from .protocol import validate_packet
from .geometry import rodrigues


def _rotation(value):
    matrix = np.asarray(value, float)
    if (matrix.shape != (3, 3) or not np.isfinite(matrix).all()
            or not np.allclose(matrix.T@matrix, np.eye(3), atol=1e-5)
            or not np.isclose(np.linalg.det(matrix), 1., atol=1e-5)):
        raise ValueError("invalid rotation")
    return matrix


def validate_driver_position_packet(packet):
    """Validate v2 metadata and every existing v1 field constraint."""
    if not isinstance(packet, dict) or type(packet.get("version")) is not int or packet["version"] != 2:
        raise ValueError("invalid position version")
    if packet.get("frame") != "psvr_imu_unrecentered":
        raise ValueError("invalid position frame")
    _identity(packet)
    if type(packet.get("imu_seq")) is not int or not 0 <= packet["imu_seq"] <= 2**64-1:
        raise ValueError("invalid imu_seq")
    if not _number(packet.get("imu_sample_time_s")) or packet["imu_sample_time_s"] < 0:
        raise ValueError("invalid imu_sample_time_s")
    # validate_packet raises ProtocolError (a ValueError subclass) carrying the
    # specific reason; let it propagate unwrapped to preserve detail
    # (fail-closed). No TypeError/OverflowError path exists here (B7-06:
    # dead except removed, no generic ValueError wrap).
    validate_packet({**packet, "version": 1, "frame": "psvr_world"})


def build_driver_position_packet(pose, imu_sample, alignment, seq,
                                 head_offset_model_m=(0, 0, 0), scale=1.0, now=None):
    """Return bound v2 or None without identity. Pose is LedPose (rvec/tvec).

    Invalid/stale observations contain position_m=None, never a synthetic origin.
    Alignment is camera_from_absolute_IMU_world, not the recentered rotation.
    """
    if not _number(scale) or scale <= 0:
        raise ValueError("scale must be positive and finite")
    offset = np.asarray(head_offset_model_m, float)
    if offset.shape != (3,) or not np.isfinite(offset).all():
        raise ValueError("head offset must be three finite metres")
    now = time.perf_counter() if now is None else now
    if not _number(now) or now < 0:
        raise ValueError("invalid now")
    if not isinstance(imu_sample, dict):
        return None
    try:
        _identity(imu_sample)
        stamp = imu_sample["sample_time_s"]
        if not _number(stamp) or stamp < 0:
            return None
    except (ValueError, KeyError):
        return None
    a = None if alignment is None else _rotation(alignment)
    mapped = imu_sample.get("monotonic_s")
    valid = (pose is not None and bool(pose.valid) and imu_sample.get("valid") is True
             and imu_sample.get("connected", True) is True and a is not None
             and _number(mapped) and 0 <= now-mapped <= MAX_AGE_S)
    position = None
    if valid:
        rvec = np.asarray(pose.rvec, float)
        t = np.asarray(pose.tvec, float).reshape(-1)
        if rvec.size != 3 or not np.isfinite(rvec).all() or t.shape != (3,) or not np.isfinite(t).all():
            raise ValueError("invalid optical pose")
        r = _rotation(rodrigues(rvec.reshape(3)))
        position = (a.T@(scale*t+r@offset)).tolist()
    packet = {"protocol": "psvr.position", "version": 2,
              "frame": "psvr_imu_unrecentered", "units": "m",
              "session_id": imu_sample["session_id"], "recenter_epoch": imu_sample["recenter_epoch"],
              "imu_seq": imu_sample["seq"], "imu_sample_time_s": stamp,
              "seq": seq, "monotonic_s": now, "host_time_ns": time.time_ns(),
              "valid": valid, "state": "tracking" if valid else "lost",
              "source": "blob_led", "position_m": position}
    validate_driver_position_packet(packet)
    return packet


class DriverPositionSender:
    """Loopback v2 sender. send(dict)->bool; close emits a final invalid packet.

    Socket errors return False, retain only an invalid identity for shutdown,
    and attempt invalidation immediately (driver watchdog covers delivery loss).
    Caller seq is a lower bound: every send attempt reserves a strictly larger
    wire sequence when needed, including invalidation and failed sends. Binding
    metadata is never rewritten; input dictionaries are not mutated.

    NOTE (B7-09, comments only): single-writer by construction in all current
    consumers, so no socket lock is held (implicit caller-thread
    serialization). close() emits a final invalid packet as a diagnostic
    shutdown marker; delivery loss remains covered by the driver watchdog.
    """
    def __init__(self, port=4271, host="127.0.0.1"):
        if host != "127.0.0.1" or type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("position endpoint must be IPv4 loopback")
        self.endpoint = (host, port)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setblocking(False)
        self.last_packet = None
        self.closed = False
        self.last_error = None
        self._last_seq = -1

    def _reserve_sequence(self, packet):
        wire = dict(packet)
        wire["seq"] = max(packet["seq"], self._last_seq+1)
        validate_driver_position_packet(wire)
        self._last_seq = wire["seq"]
        self.last_packet = wire
        return wire

    def send(self, packet):
        if packet is None or self.closed:
            return False
        validate_driver_position_packet(packet)
        packet = self._reserve_sequence(packet)
        try:
            self.socket.sendto(json.dumps(packet, allow_nan=False, separators=(",", ":")).encode(), self.endpoint)
            self.last_error = None
            return True
        except OSError as exc:
            self.last_error = str(exc)
            self.invalidate()
            return False

    def invalidate(self):
        if self.last_packet is None or self.closed:
            return False
        packet = {**self.last_packet, "seq": self.last_packet["seq"]+1,
                  "valid": False, "state": "lost", "position_m": None,
                  "monotonic_s": time.perf_counter(), "host_time_ns": time.time_ns()}
        packet = self._reserve_sequence(packet)
        try:
            self.socket.sendto(json.dumps(packet, allow_nan=False).encode(), self.endpoint)
            return True
        except OSError as exc:
            self.last_error = str(exc)
            return False

    def close(self):
        if not self.closed:
            self.invalidate()
            self.closed = True
            self.socket.close()
