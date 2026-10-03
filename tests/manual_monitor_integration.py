"""Opt-in real PSVR integration; no GUI interaction or power/mode writes.

Run only with the connected headset resting on a stable surface and no active
monitor capture. This does not certify physical axis directions or drift.
"""
from pathlib import Path
import argparse
import json
import sys
import time
from urllib.request import urlopen
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "psvr_monitor"))
from monitor import Backend  # noqa: E402


def wait_for(backend, predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        backend.pump()
        state = backend.state.snapshot()
        if predicate(state):
            return state
        if state["last_error"]:
            raise RuntimeError(state["last_error"])
        time.sleep(0.02)
    raise RuntimeError("bounded wait expired: " + json.dumps(backend.state.snapshot(), ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hardware-check", action="store_true", required=True)
    args = parser.parse_args()
    assert args.hardware_check
    discovery = ROOT / "logs" / "monitor_api.json"
    if discovery.exists():
        meta = json.loads(discovery.read_text(encoding="utf-8"))
        if urlsplit(meta["base_url"]).hostname != "127.0.0.1":
            raise RuntimeError("unexpected monitor API host")
        with urlopen(meta["base_url"] + "/state", timeout=3) as response:
            current = json.load(response)
        if current["running"] or current["busy"]:
            raise RuntimeError("stop the existing monitor capture before this check")
    backend = Backend(ROOT)
    try:
        if not backend.one_shot("--enumerate-only"):
            raise RuntimeError("could not start read-only enumeration")
        state = wait_for(backend, lambda s: not s["busy"])
        nodes = {node["interface"]: node for node in state["usb_devices"]}
        if not all(nodes.get(i, {}).get("binding_ok") for i in (4, 5)):
            raise RuntimeError("PSVR requires healthy HID IF4 and WinUSB IF5")

        if not backend.start_imu() or not backend.signal_worker("--stop-imu"):
            raise RuntimeError("startup Stop could not be queued")
        stopped = wait_for(backend, lambda s: not s["running"] and not s["busy"])
        if stopped["pose_valid"] or backend.worker.poll() != 0:
            raise RuntimeError("startup Stop did not finish cleanly")
        print("PASS real native startup -> immediate Stop; owned process exited 0", flush=True)

        if not backend.start_imu():
            raise RuntimeError("could not start IMU")
        live = wait_for(backend, lambda s: s["pose_valid"], seconds=15)
        if live["counters"]["parse_errors"]:
            raise RuntimeError("native DATA contract rejected")
        old_count = live["latest_data"]["recenter_count"]
        if not backend.signal_worker("--recenter"):
            raise RuntimeError("recenter request refused")
        centered = wait_for(backend, lambda s: not s["busy"] and s["latest_data"] is not None and
                            s["latest_data"]["recenter_count"] > old_count)
        if max(abs(v) for v in centered["latest_data"]["angles_deg"].values()) > 1:
            raise RuntimeError("stationary recenter did not produce a near-neutral pose")
        print("PASS real DATA contract, calibrated pose and native recenter event", flush=True)

        if not backend.signal_worker("--recalibrate"):
            raise RuntimeError("recalibration request refused")
        wait_for(backend, lambda s: not s["busy"] and s["phase"] in {"warming", "calibrating"} and not s["pose_valid"] and
                 s["latest_data"] is not None and s["latest_data"]["state"] in {"warming", "calibrating"})
        if not backend.signal_worker("--stop-imu"):
            raise RuntimeError("Stop after recalibration refused")
        final = wait_for(backend, lambda s: not s["running"] and not s["busy"])
        if final["pose_valid"] or backend.worker.poll() != 0:
            raise RuntimeError("final Stop did not invalidate pose/exit cleanly")
        print("PASS real recalibration invalidation -> Stop/EOF; no valid stale pose", flush=True)
        return 0
    finally:
        backend.shutdown()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError) as error:
        print("FAIL integration:", error, file=sys.stderr)
        raise SystemExit(1)
