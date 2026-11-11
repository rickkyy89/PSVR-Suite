"""Driver-owned absolute IMU over loopback; no HID or child process.

Public codecs raise ValueError. Source snapshots are independent dictionaries;
``at`` uses perf_counter timestamps and never crosses activation/recenter bounds.
"""
from collections import deque
import copy
import errno
import json
import math
import socket
import threading
import time

import numpy as np

from .imu_clock import ImuClockMapper

MAX_PACKET_BYTES = 2048
MAX_SESSION_BYTES = 128
MAX_AGE_S = .15


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _identity(row):
    session = row.get("session_id")
    if (not isinstance(session, str) or not session.strip()
            or len(session.encode("utf-8")) > MAX_SESSION_BYTES):
        raise ValueError("invalid session_id")
    for key in ("seq", "recenter_epoch"):
        if type(row.get(key)) is not int or not 0 <= row[key] <= 2**64-1:
            raise ValueError("invalid " + key)


def validate_driver_imu_packet(packet):
    """Validate a wire dictionary without mutating it."""
    if not isinstance(packet, dict):
        raise ValueError("IMU packet must be an object")
    for key, expected in (("protocol", "psvr.imu"), ("frame", "psvr_imu_unrecentered"),
                          ("clock", "driver_steady_seconds")):
        if packet.get(key) != expected:
            raise ValueError("invalid " + key)
    if type(packet.get("version")) is not int or packet["version"] != 1:
        raise ValueError("invalid version")
    _identity(packet)
    for key in ("sample_time_s", "sent_time_s"):
        if not _number(packet.get(key)) or packet[key] < 0:
            raise ValueError("invalid " + key)
    if packet["sample_time_s"] > packet["sent_time_s"]:
        raise ValueError("sample after send")
    for key in ("connected", "valid"):
        if type(packet.get(key)) is not bool:
            raise ValueError("invalid " + key)
    state = packet.get("state")
    if not isinstance(state, str) or not state or len(state) > 64:
        raise ValueError("invalid state")
    if packet["valid"] and (not packet["connected"] or state != "tracking"
                            or packet["sent_time_s"]-packet["sample_time_s"] > MAX_AGE_S):
        raise ValueError("valid requires fresh connected tracking")
    for key, size in (("quaternion_sensor_wxyz", 4), ("world_rotation_wxyz", 4),
                      ("corrected_gyro_rad_s", 3)):
        value = packet.get(key)
        if not isinstance(value, (list, tuple)) or len(value) != size or not all(map(_number, value)):
            raise ValueError("invalid " + key)
        if size == 4 and abs(math.hypot(*value)-1) > 1e-3:
            raise ValueError("quaternion must be normalized")


def parse_driver_imu_packet(payload):
    """Decode at most 2048 UTF-8 bytes and validate the IMU v1 contract."""
    try:
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, (bytes, bytearray)) or len(payload) > MAX_PACKET_BYTES:
            raise ValueError("invalid IMU payload size/type")
        def unique_object(pairs):
            row = {}
            for key, value in pairs:
                if key in row:
                    raise ValueError("duplicate JSON field: " + key)
                row[key] = value
            return row
        row = json.loads(payload, object_pairs_hook=unique_object)
        validate_driver_imu_packet(row)
        return row
    except (UnicodeError, TypeError, OverflowError, RecursionError) as exc:
        raise ValueError("invalid IMU payload") from exc


class DriverImuSource:
    """Bounded receiver; port=0 selects an ephemeral offline-test endpoint."""
    def __init__(self, port=4272):
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("invalid UDP port")
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.socket.bind(("127.0.0.1", port))
            self.socket.settimeout(.05)
        except BaseException:
            self.socket.close()
            raise
        self.local_port = self.socket.getsockname()[1]
        self.samples = deque(maxlen=200)
        # NOTE (B7-09, comments only): implicit Condition lock (no separate
        # mutex object): all _accept/at/close paths synchronize via `changed`.
        self.changed = threading.Condition()
        self.offset = None
        self.clock_mapper = ImuClockMapper()
        self.session_id = None
        # NOTE (B7-09, comments only): retired_sessions is intentionally
        # unbounded with no eviction: evicting an old session id would risk
        # re-accepting its stale packets as fresh. Growth is bounded in
        # practice by the number of activations in one process lifetime.
        self.retired_sessions = set()
        self._closed = threading.Event()
        self.thread = threading.Thread(target=self._read, daemon=True, name="driver-imu")
        self.thread.start()

    def _read(self):
        while not self._closed.is_set():
            try:
                payload, peer = self.socket.recvfrom(MAX_PACKET_BYTES+1)
                received = time.perf_counter()
                if peer[0] != "127.0.0.1":
                    continue
                row = parse_driver_imu_packet(payload)
            except socket.timeout:
                continue
            except OSError as exc:
                # Winsock reports an oversized datagram instead of truncating it.
                if not self._closed.is_set() and (getattr(exc,"winerror",None) == 10040
                                                  or exc.errno in (errno.EMSGSIZE, 10040)):
                    continue
                break
            except ValueError:
                continue
            with self.changed:
                self._accept(row, received)

    def _accept(self, row, received):
        """Publish a validated packet under changed; also used by offline replay."""
        if self._closed.is_set() or row["session_id"] in self.retired_sessions:
            return False
        same = row["session_id"] == self.session_id
        if same and self.samples:
            last = self.samples[-1]
            # The frozen offset must not permanently reject supported clock
            # drift accumulated while no packets arrived. Rejected packets do
            # not advance this anchor or mutate the mapper/history.
            future_budget = MAX_AGE_S + self.clock_mapper.MAX_DRIFT * max(
                0., received-self.clock_mapper.last_received)
            if (row["seq"] <= last["seq"] or row["recenter_epoch"] < last["recenter_epoch"]
                    or row["sample_time_s"] < last["sample_time_s"]
                    or row["sent_time_s"] < last["sent_time_s"]
                    or row["sent_time_s"]+self.offset > received+future_budget):
                return False
        if not same:
            if self.session_id is not None:
                self.retired_sessions.add(self.session_id)
            self.session_id = row["session_id"]
            self.samples.clear()
            self.clock_mapper.reset()
        elif self.samples and row["recenter_epoch"] != self.samples[-1]["recenter_epoch"]:
            self.samples.clear()
        row = copy.deepcopy(row)
        row["monotonic_s"] = self.clock_mapper.map(
            row["sample_time_s"], row["sent_time_s"], received)
        self.offset = self.clock_mapper.offset
        row["recenter_count"] = row["recenter_epoch"]
        self.samples.append(row)
        self.changed.notify_all()
        return True

    def latest(self):
        with self.changed:
            return copy.deepcopy(self.samples[-1]) if self.samples else None

    def at(self, captured_at, wait_s=.025):
        if not _number(captured_at) or not _number(wait_s) or wait_s < 0:
            raise ValueError("invalid capture/wait time")
        deadline = time.perf_counter()+wait_s
        with self.changed:
            while not self._closed.is_set() and (not self.samples or self.samples[-1]["monotonic_s"] < captured_at):
                remaining = deadline-time.perf_counter()
                if remaining <= 0:
                    break
                self.changed.wait(remaining)
            past = next((s for s in reversed(self.samples) if s["monotonic_s"] <= captured_at), None)
            future = next((s for s in self.samples if s["monotonic_s"] > captured_at), None)
            if past is None or self._closed.is_set():
                return None
            result = copy.deepcopy(past)
            if captured_at-past["monotonic_s"] > MAX_AGE_S:
                result["valid"] = False
                return result
            if (future and past["valid"] and future["valid"]
                    and past["session_id"] == future["session_id"]
                    and past["recenter_epoch"] == future["recenter_epoch"]):
                gap = future["monotonic_s"]-past["monotonic_s"]
                if 0 < gap <= .06:
                    alpha = (captured_at-past["monotonic_s"])/gap
                    q0 = np.array(past["quaternion_sensor_wxyz"], float)
                    q1 = np.array(future["quaternion_sensor_wxyz"], float)
                    dot = float(np.dot(q0, q1))
                    if dot < 0:
                        q1, dot = -q1, -dot
                    theta = math.acos(min(1., dot))
                    # NOTE (B7-09, comments only): nlerp below vs slerp above
                    # differ by ~6.4e-11 at 10 deg, negligible at gaps <=0.06 s;
                    # the branch below is a small-angle fast path, not a bias.
                    q = ((1-alpha)*q0+alpha*q1 if theta < 1e-5 else
                         (math.sin((1-alpha)*theta)*q0+math.sin(alpha*theta)*q1)/math.sin(theta))
                    result["quaternion_sensor_wxyz"] = (q/np.linalg.norm(q)).tolist()
                    result["sample_time_s"] = past["sample_time_s"]+alpha*(future["sample_time_s"]-past["sample_time_s"])
                    result["sent_time_s"] = future["sent_time_s"]
                    result["monotonic_s"] = captured_at
                    result["interpolated"] = True
            return result

    def close(self):
        self._closed.set()
        self.socket.close()
        with self.changed:
            self.samples.clear()
            self.changed.notify_all()
        self.thread.join(timeout=.5)
