# SPDX-License-Identifier: MIT
"""End-to-end checks for tools/psvr_position_probe.

Offline part: malformed CLI must fail with exactly one JSON 'error' event and
no 'ready' line (arguments are validated before any socket exists), and a
port held by another socket must fail with exit 3 without ever reporting
ready.

Interop part: launches the real probe process on an ephemeral loopback port,
waits for the deterministic stdout 'ready' handshake, then sends real
psvr.position v1 datagrams through tools/psvr_camera/udp_sender: a seated
origin frame, +0.1 m frames, a silence longer than --timeout-ms (local
timeout tracking loss), a recovery frame and an invalid (valid:false) frame,
plus one malformed datagram. The JSON line stream and summary counters are
then verified exactly. No USB, no camera, no SteamVR.
"""
from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from psvr_camera.protocol import PositionSample  # noqa: E402
from psvr_camera.udp_sender import SequenceAllocator, UdpPositionSender  # noqa: E402

READY_TIMEOUT_S = 10.0


class LineCollector:
    """Daemon reader so process waits never block on an unread pipe."""

    def __init__(self, stream) -> None:
        self.stream = stream
        self.lines: list[str] = []
        self.eof = threading.Event()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        for line in self.stream:
            self.lines.append(line)
        self.stream.close()
        self.eof.set()

    def wait_for_count(self, count: int, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if len(self.lines) >= count:
                return True
            time.sleep(0.01)
        return len(self.lines) >= count


def run(expected_code, exe, *args):
    result = subprocess.run([str(exe), *map(str, args)], capture_output=True,
                            text=True, encoding="utf-8", timeout=15, check=False)
    assert result.returncode == expected_code, (args, result.returncode, result.stdout, result.stderr)
    if expected_code in (2, 3):
        # Failed runs must never reach the socket: only one JSON error line,
        # no ready handshake, on stdout.
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        assert len(lines) == 1, (args, lines)
        event = json.loads(lines[0])
        assert event["event"] == "error" and event["exit_code"] == expected_code, event
    return result


def check_cli(exe) -> None:
    help_text = run(0, exe, "--help").stdout
    for marker in ("psvr_position_probe", "Read-only", "psvr.position", "--port 0..65535",
                   "100..60000", "20..5000", "--json", "ready", "USB", "SteamVR",
                   "Exit: 0", "4271"):
        assert marker in help_text, (marker, help_text)

    bad = [
        ["--unknown"], ["--port"], ["--port", "abc"], ["--port", "-1"], ["--port", "65536"],
        ["--port", "1.0"], ["--port", "+80"], ["--port", "0", "--port", "0"],
        ["--duration-ms"], ["--duration-ms", "99"], ["--duration-ms", "60001"],
        ["--duration-ms", "0"], ["--duration-ms", "1e3"], ["--timeout-ms"],
        ["--timeout-ms", "19"], ["--timeout-ms", "5001"], ["--timeout-ms", "-20"],
        ["--json", "true"], ["--help", "--json"], ["--json", "--help"],
        ["--port", "0", "--duration-ms", "100", "--duration-ms", "200"],
        ["--port", "0", "--unknown"], ["--"], ["--json--"],
    ]
    for args in bad:
        run(2, exe, *args)

    # A port already held must fail at bind time (exit 3), not at CLI time,
    # and still without any ready event.
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    holder.bind(("127.0.0.1", 0))
    held_port = holder.getsockname()[1]
    try:
        run(3, exe, "--port", str(held_port), "--duration-ms", "100", "--timeout-ms", "20")
    finally:
        holder.close()


def send_tracking(sender, sequences, clock_start, position):
    sender.send_sample(PositionSample(
        seq=sequences.next(), monotonic_s=time.monotonic() - clock_start,
        valid=True, state="tracking", source="aruco", position_m=list(position)))


def check_interop(exe) -> None:
    process = subprocess.Popen(
        [str(exe), "--port", "0", "--duration-ms", "4000", "--timeout-ms", "300", "--json"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        stdout = LineCollector(process.stdout)
        stderr = LineCollector(process.stderr)
        assert stdout.wait_for_count(1, READY_TIMEOUT_S), (stdout.lines, stderr.lines)
        ready = json.loads(stdout.lines[0])
        assert ready["event"] == "ready" and ready["tool"] == "psvr_position_probe", ready
        assert ready["protocol"] == "psvr.position" and ready["version"] == 1, ready
        assert ready["port_option"] == 0 and 1 <= ready["bound_port"] <= 65535, ready
        port = ready["bound_port"]

        sequences = SequenceAllocator()
        clock_start = time.monotonic()
        with UdpPositionSender("127.0.0.1", port) as sender:
            # seq 0: seated origin. seqs 1..5: +0.1 m on X, 20 ms apart.
            send_tracking(sender, sequences, clock_start, [0.10, 0.0, -1.20])
            for _ in range(5):
                time.sleep(0.02)
                send_tracking(sender, sequences, clock_start, [0.20, 0.0, -1.20])
            # 700 ms silence > 300 ms freshness timeout: tracking loss must be
            # detected locally, with no packet in between.
            time.sleep(0.7)
            send_tracking(sender, sequences, clock_start, [0.20, 0.0, -1.20])  # seq 6: recovery
            time.sleep(0.05)
            sender.send_sample(PositionSample(  # seq 7: explicit invalid frame
                seq=sequences.next(), monotonic_s=time.monotonic() - clock_start,
                valid=False, state="lost", source="none", position_m=None))
            assert sender.sent == 8
            time.sleep(0.05)
            raw = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                raw.sendto(b"{not json", ("127.0.0.1", port))
            finally:
                raw.close()

        code = process.wait(timeout=30)
        assert stdout.eof.wait(5), stdout.lines
        events = [json.loads(line) for line in stdout.lines if line.strip()]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    assert code == 0, (code, [line for line in stdout.lines], stderr.lines)
    assert events[0]["event"] == "ready" and events[-1]["event"] == "summary", events
    summary = events[-1]

    # The wire is exactly what the driver-side parser accepted, in order:
    # a state transition belongs to the poll iteration that consumed its
    # datagram, and the tracking->lost with last_seq==5 has no packet
    # between seq 5 and seq 6 -> that loss came from the local timeout.
    stream = []
    for event in events:
        if event["event"] == "packet":
            stream.append(("packet", event["seq"]))
        elif event["event"] == "state":
            stream.append(("state", event["from"], event["to"], event["last_seq"]))
        else:
            stream.append((event["event"],))
    assert stream == [
        ("ready",),
        ("packet", 0), ("state", "waiting", "tracking", 0),
        ("packet", 1), ("packet", 2), ("packet", 3), ("packet", 4), ("packet", 5),
        ("state", "tracking", "lost", 5),
        ("packet", 6), ("state", "lost", "tracking", 6),
        ("packet", 7), ("state", "tracking", "lost", 7),
        ("summary",),
    ], stream

    packets = [e for e in events if e["event"] == "packet"]
    assert all(p["state"] == "tracking" and p["source"] == "aruco" for p in packets[:7]), packets
    assert packets[0]["position_m"] == [0.10, 0.0, -1.20], packets[0]
    assert packets[7]["valid"] is False and packets[7]["state"] == "lost", packets[7]
    assert packets[7]["position_m"] is None, packets[7]

    receiver = summary["receiver"]
    assert receiver == {"datagrams": 9, "parsed": 8, "malformed": 1,
                        "oversized": 0, "errors": 0, "fatal": 0}, receiver
    bridge = summary["bridge"]
    assert bridge == {"accepted": 7, "invalid": 1, "duplicates": 0, "out_of_order": 0,
                      "gaps": 0, "resyncs": 0, "rejected": 0}, bridge
    assert summary["first_sequence"] == 0 and summary["last_sequence"] == 7, summary
    assert summary["valid_frames_ingested"] == 7, summary
    assert summary["valid_snapshot_observations"] >= 7, summary
    assert 0 <= summary["first_valid_elapsed_ms"] < 1500, summary

    # Relative to the first seated origin: the moved frames are +0.1 m, all
    # through the shared parser/bridge, and the summary carries the extremes.
    assert abs(summary["max_displacement_m"] - 0.1) < 1e-9, summary
    assert abs(summary["final_displacement_m"] - 0.1) < 1e-9, summary

    assert summary["tracking_loss_observed"] is True, summary
    assert summary["tracking_recovery_count"] == 1, summary
    assert summary["final_view"]["state"] == "lost", summary
    assert summary["final_view"]["valid"] is False, summary
    assert summary["final_view"]["age_ms"] > summary["timeout_ms"], summary
    assert summary["port_option"] == 0 and summary["bound_port"] == port, summary
    assert summary["duration_ms"] == 4000 and summary["timeout_ms"] == 300, summary
    assert 4000 <= summary["elapsed_ms"] <= 6000, summary
    assert summary["exit_code"] == 0 and summary["reason"] == "valid_position_observed", summary
    assert summary["frame"] == "psvr_world" and summary["units"] == "m", summary
    assert not stderr.lines, stderr.lines


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: check_position_probe.py <psvr_position_probe.exe>")
    exe = Path(sys.argv[1]).resolve(strict=True)
    check_cli(exe)
    check_interop(exe)
    print("PASS psvr_position_probe CLI validation before socket, held-port bind failure and "
          "end-to-end psvr.position interop: seated origin, +0.1 m relative displacement, "
          "local-timeout tracking loss, recovery, invalid frame, malformed datagram counters")


if __name__ == "__main__":
    main()
