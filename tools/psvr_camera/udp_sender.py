"""UDP emitter for the versioned M9 position protocol.

Thin, testable wrapper around a datagram socket. The socket factory is
injectable so the unit tests never touch the network. The sender is the only
component that knows about the wire; it validates every packet before it leaves
the process.
"""

from __future__ import annotations

import socket
from typing import Callable, Optional

from .protocol import (
    DEFAULT_UDP_HOST,
    DEFAULT_UDP_PORT,
    PositionSample,
    encode_packet,
    validate_packet,
)


class UdpSendError(OSError):
    pass


class UdpPositionSender:
    """Sends validated ``psvr.position`` packets to one UDP endpoint."""

    def __init__(
        self,
        host: str = DEFAULT_UDP_HOST,
        port: int = DEFAULT_UDP_PORT,
        socket_factory: Optional[Callable[[], socket.socket]] = None,
        send_timeout_s: float = 0.5,
    ) -> None:
        if not isinstance(host, str) or not host:
            raise ValueError("host must be a non-empty string")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("port must be an integer in 1..65535")
        self.host = host
        self.port = port
        self.send_timeout_s = float(send_timeout_s)
        factory = socket_factory or (lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
        self._socket = factory()
        self._socket.settimeout(self.send_timeout_s)
        self.sent = 0
        self.errors = 0
        self.last_error: Optional[str] = None

    def send_sample(self, sample: PositionSample) -> int:
        return self.send_packet(sample.to_packet())

    def send_packet(self, packet: dict) -> int:
        validate_packet(packet)
        data = encode_packet(packet)
        try:
            written = self._socket.sendto(data, (self.host, self.port))
        except OSError as exc:
            self.errors += 1
            self.last_error = str(exc)
            raise UdpSendError(f"UDP send to {self.host}:{self.port} failed: {exc}") from exc
        self.sent += 1
        return written

    def close(self) -> None:
        try:
            self._socket.close()
        except OSError as exc:  # pragma: no cover - close errors are non-fatal
            self.last_error = str(exc)

    def __enter__(self) -> "UdpPositionSender":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class SequenceAllocator:
    """Monotonic per-sender sequence numbers, starting at zero."""

    def __init__(self, start: int = 0) -> None:
        if type(start) is not int or start < 0:
            raise ValueError("start must be a non-negative integer")
        self._next = start

    def next(self) -> int:
        value = self._next
        self._next += 1
        return value
