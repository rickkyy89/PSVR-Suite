"""Versioned UDP position protocol shared with the driver agent.

Single source of truth for the M9 webcam-to-driver wire format. ``PROTOCOL.md``
and ``protocol.json`` are the human/machine readable contract; a test asserts
that ``protocol.json`` equals :func:`protocol_document` so the files cannot
drift from the code.

Coordinate frame (driver frame, matching ``docs/AXIS_CONVENTIONS.md`` and
``driver/pose_bridge.hpp``): right-handed, metres, **+X right, +Y up, -Z
forward**. ``orientation_wxyz`` is ``world_from_body`` (maps body vectors into
the world frame), WXYZ order, and is optional: the M9 driver already owns
orientation from the IMU, so position is the primary payload.

The packet is intentionally small and self-describing. Unknown fields are not
sent; a receiver that only knows version 1 must reject other versions rather
than guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import time
from typing import Optional

PROTOCOL_NAME = "psvr.position"
PROTOCOL_VERSION = 1

COORDINATE_FRAME = "psvr_world"
UNITS = "m"
AXES = {"right": "+X", "up": "+Y", "forward": "-Z"}
HANDEDNESS = "right"
ORIENTATION_ORDER = "WXYZ"
ORIENTATION_CONVENTION = "world_from_body"
DEFAULT_UDP_HOST = "127.0.0.1"
DEFAULT_UDP_PORT = 4271  # Avoids AITrack 4242 and the monitor HTTP API.

VALID_STATES = frozenset({"tracking", "lost", "calibrating", "ambiguous", "error"})
VALID_SOURCES = frozenset({"aruco", "blob_led", "none"})

_NORM_TOLERANCE = 1e-3


class ProtocolError(ValueError):
    """Raised when a packet does not satisfy the versioned contract."""


def _finite(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _finite_vector(value, size: int) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) == size
        and all(_finite(v) for v in value)
    )


def quaternion_from_rotation(matrix) -> list:
    """Convert a 3x3 rotation matrix to a WXYZ quaternion (no NumPy needed)."""
    m = [[float(matrix[r][c]) for c in range(3)] for r in range(3)]
    trace = m[0][0] + m[1][1] + m[2][2]
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        w = 0.25 * s
        x = (m[2][1] - m[1][2]) / s
        y = (m[0][2] - m[2][0]) / s
        z = (m[1][0] - m[0][1]) / s
    elif m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        s = math.sqrt(1.0 + m[0][0] - m[1][1] - m[2][2]) * 2.0
        w = (m[2][1] - m[1][2]) / s
        x = 0.25 * s
        y = (m[0][1] + m[1][0]) / s
        z = (m[0][2] + m[2][0]) / s
    elif m[1][1] > m[2][2]:
        s = math.sqrt(1.0 + m[1][1] - m[0][0] - m[2][2]) * 2.0
        w = (m[0][2] - m[2][0]) / s
        x = (m[0][1] + m[1][0]) / s
        y = 0.25 * s
        z = (m[1][2] + m[2][1]) / s
    else:
        s = math.sqrt(1.0 + m[2][2] - m[0][0] - m[1][1]) * 2.0
        w = (m[1][0] - m[0][1]) / s
        x = (m[0][2] + m[2][0]) / s
        y = (m[1][2] + m[2][1]) / s
        z = 0.25 * s
    norm = math.sqrt(w * w + x * x + y * y + z * z)
    if norm <= 0.0 or not math.isfinite(norm):
        raise ProtocolError("degenerate rotation matrix")
    return [w / norm, x / norm, y / norm, z / norm]


@dataclass
class PositionSample:
    """A single validated metric position observation."""

    seq: int
    monotonic_s: float
    valid: bool
    state: str
    source: str = "none"
    position_m: Optional[list] = None
    orientation_wxyz: Optional[list] = None
    reprojection_px: Optional[float] = None
    inliers: Optional[int] = None
    ambiguity_ratio: Optional[float] = None
    note: Optional[str] = None
    host_time_ns: int = field(default_factory=time.time_ns)

    def to_packet(self) -> dict:
        packet = {
            "protocol": PROTOCOL_NAME,
            "version": PROTOCOL_VERSION,
            "seq": self.seq,
            "host_time_ns": self.host_time_ns,
            "monotonic_s": float(self.monotonic_s),
            "valid": bool(self.valid),
            "state": self.state,
            "source": self.source,
            "frame": COORDINATE_FRAME,
            "units": UNITS,
            "position_m": None if self.position_m is None else [float(v) for v in self.position_m],
            "orientation_wxyz": (
                None if self.orientation_wxyz is None
                else [float(v) for v in self.orientation_wxyz]
            ),
            "reprojection_px": (
                None if self.reprojection_px is None else float(self.reprojection_px)
            ),
            "inliers": None if self.inliers is None else int(self.inliers),
            "ambiguity_ratio": (
                None if self.ambiguity_ratio is None else float(self.ambiguity_ratio)
            ),
            "note": self.note,
        }
        validate_packet(packet)
        return packet


def validate_packet(packet: dict) -> None:
    """Strictly validate a decoded packet. Raises :class:`ProtocolError`."""
    if not isinstance(packet, dict):
        raise ProtocolError("packet must be a JSON object")
    if packet.get("protocol") != PROTOCOL_NAME:
        raise ProtocolError(f"unexpected protocol {packet.get('protocol')!r}")
    if packet.get("version") != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol version {packet.get('version')!r}")
    if packet.get("frame") != COORDINATE_FRAME:
        raise ProtocolError(f"unexpected frame {packet.get('frame')!r}")
    if packet.get("units") != UNITS:
        raise ProtocolError(f"unexpected units {packet.get('units')!r}")
    seq = packet.get("seq")
    if type(seq) is not int or seq < 0:
        raise ProtocolError("seq must be a non-negative integer")
    if not _finite(packet.get("monotonic_s")) or packet["monotonic_s"] < 0:
        raise ProtocolError("monotonic_s must be a finite non-negative number")
    if type(packet.get("valid")) is not bool:
        raise ProtocolError("valid must be a boolean")
    if packet.get("state") not in VALID_STATES:
        raise ProtocolError(f"unknown state {packet.get('state')!r}")
    if packet.get("source") not in VALID_SOURCES:
        raise ProtocolError(f"unknown source {packet.get('source')!r}")
    host = packet.get("host_time_ns")
    if type(host) is not int or host < 0:
        raise ProtocolError("host_time_ns must be a non-negative integer")
    position = packet.get("position_m")
    orientation = packet.get("orientation_wxyz")
    if packet["valid"]:
        if not _finite_vector(position, 3):
            raise ProtocolError("valid packet requires a finite position_m [x, y, z]")
        if orientation is not None:
            if not _finite_vector(orientation, 4):
                raise ProtocolError("orientation_wxyz must be four finite numbers")
            norm = math.sqrt(sum(float(v) * float(v) for v in orientation))
            if abs(norm - 1.0) > _NORM_TOLERANCE:
                raise ProtocolError("orientation_wxyz must be a unit quaternion")
    else:
        if position is not None and not _finite_vector(position, 3):
            raise ProtocolError("position_m must be null or finite [x, y, z]")
        if orientation is not None and not _finite_vector(orientation, 4):
            raise ProtocolError("orientation_wxyz must be null or finite [w, x, y, z]")
    for key in ("reprojection_px", "ambiguity_ratio"):
        value = packet.get(key)
        if value is not None and (not _finite(value) or value < 0):
            raise ProtocolError(f"{key} must be null or a finite non-negative number")
    if packet.get("inliers") is not None and (
        type(packet["inliers"]) is not int or packet["inliers"] < 0
    ):
        raise ProtocolError("inliers must be null or a non-negative integer")
    note = packet.get("note")
    if note is not None and not isinstance(note, str):
        raise ProtocolError("note must be null or a string")


def encode_packet(packet: dict) -> bytes:
    """Validate and encode a packet as compact, sorted, UTF-8 JSON bytes."""
    validate_packet(packet)
    return (json.dumps(packet, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def decode_packet(data: bytes) -> dict:
    """Decode and strictly validate JSON bytes into a packet dict."""
    try:
        packet = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"not valid UTF-8 JSON: {exc}") from exc
    validate_packet(packet)
    return packet


def protocol_document() -> dict:
    """Machine-readable contract; mirrors ``protocol.json`` exactly."""
    return {
        "protocol": PROTOCOL_NAME,
        "version": PROTOCOL_VERSION,
        "transport": "udp",
        "encoding": "utf-8 json, one object per datagram",
        "default_endpoint": {"host": DEFAULT_UDP_HOST, "port": DEFAULT_UDP_PORT},
        "frame": COORDINATE_FRAME,
        "units": UNITS,
        "handedness": HANDEDNESS,
        "axes": dict(AXES),
        "orientation_order": ORIENTATION_ORDER,
        "orientation_convention": ORIENTATION_CONVENTION,
        "states": sorted(VALID_STATES),
        "sources": sorted(VALID_SOURCES),
        "position_key": "position_m",
        "rotation_key": "orientation_wxyz",
        "fields": {
            "protocol": "constant string",
            "version": "integer, this document's version",
            "seq": "monotonic non-negative integer, per sender",
            "host_time_ns": "sender wall clock in nanoseconds since epoch",
            "monotonic_s": "sender monotonic seconds at capture time",
            "valid": "boolean; false means pose not usable this frame",
            "state": "one of states",
            "source": "one of sources (measurement origin)",
            "frame": "constant frame name",
            "units": "constant unit string, metres",
            "position_m": "[x, y, z] metres in frame, null when invalid",
            "orientation_wxyz": "[w, x, y, z] world_from_body, null when unavailable",
            "reprojection_px": "RMS reprojection error in pixels, null when unavailable",
            "inliers": "number of accepted correspondences, null when unavailable",
            "ambiguity_ratio": "planar PnP ambiguity metric, null when unavailable",
            "note": "optional short human-readable reason, null when absent",
        },
        "driver_agent_todo": (
            "The driver agent must confirm it listens on this endpoint and agrees "
            "with the psvr_world axes (+X right, +Y up, -Z forward) before the "
            "position stream is enabled. Position from the camera is a separate "
            "metric source from IMU orientation; neither is assumed calibrated."
        ),
    }
