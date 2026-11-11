# SPDX-License-Identifier: MIT
"""CLI: python tests/check_driver_camera_interop.py <fixture.exe>; real UDP only."""
import json
import math
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from psvr_camera.driver_imu_source import DriverImuSource
from psvr_camera.driver_position import build_driver_position_packet, DriverPositionSender
from psvr_camera.led_pose import LedPose


def check(executable):
    source = DriverImuSource(port=0)
    process = sender = reader = None
    rows = queue.Queue()
    try:
        process = subprocess.Popen([str(Path(executable).resolve()), str(source.local_port)],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8")
        def read_stdout():
            for line in process.stdout:
                rows.put(line)
            rows.put(None)
        reader = threading.Thread(target=read_stdout, daemon=True)
        reader.start()
        deadline = time.perf_counter() + 8

        def reply(event):
            line = rows.get(timeout=max(.001, min(2., deadline - time.perf_counter())))
            if line is None:
                raise AssertionError("fixture exited before " + event)
            row = json.loads(line)
            if row.get("event") != event:
                raise AssertionError("unexpected fixture reply: " + repr(row))
            return row

        ready = reply("ready")
        sender = DriverPositionSender(port=ready["port"])
        alignment = np.array([[0., 0., 1.], [0., 1., 0.], [-1., 0., 0.]])
        world_yaw = alignment.copy()  # independent exact +90-degree Y matrix
        offset = np.array([.1, .02, -.03])
        model_rotation = np.array([[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]])
        start_t = np.array([.2, .3, 1.])
        move_t = start_t + [.12, -.04, .08]

        def packet(seq, translation=move_t, lost=False):
            while time.perf_counter() < deadline:
                sample = source.latest()
                if sample and sample["valid"] and time.perf_counter()-sample["monotonic_s"] < .1:
                    break
                time.sleep(.002)
            else:
                raise AssertionError("no fresh real exported IMU packet")
            assert sample["session_id"] == ready["session"]
            assert sample["recenter_epoch"] == ready["epoch"]
            np.testing.assert_allclose(sample["quaternion_sensor_wxyz"], [1, 0, 0, 0])
            np.testing.assert_allclose(sample["world_rotation_wxyz"],
                                       [math.sqrt(.5), 0, math.sqrt(.5), 0])
            pose = LedPose(not lost, "lost" if lost else "tracking",
                           rvec=np.array([0., 0., math.pi/2]), tvec=translation.reshape(3, 1))
            result = build_driver_position_packet(pose, sample, alignment, seq,
                                                  head_offset_model_m=offset, scale=1.7)
            assert result is not None and result["valid"] == (not lost)
            return result

        def send(value, accepted, valid, expected=None):
            assert sender.send(value), "real position UDP send failed"
            row = reply("pose")
            assert row["seq"] == value["seq"], row
            assert row["accepted"] == accepted and row["pose_valid"] == valid, row
            assert row["connected"], row
            if expected is not None:
                np.testing.assert_allclose(row["position"], expected, atol=1e-9)
            if valid:
                np.testing.assert_allclose(row["orientation_wxyz"],
                                           [math.sqrt(.5), 0, math.sqrt(.5), 0], atol=1e-9)

        origin = packet(1, start_t)
        np.testing.assert_allclose(origin["position_m"],
                                   alignment.T @ (1.7*start_t + model_rotation@offset), atol=1e-9)
        send(origin, True, True, [0, 0, 0])
        moved = packet(2)
        np.testing.assert_allclose(moved["position_m"],
                                   alignment.T @ (1.7*move_t + model_rotation@offset), atol=1e-9)
        displacement = world_yaw @ (alignment.T @ (1.7*(move_t-start_t)))
        send(moved, True, True, displacement)
        send({**packet(3), "session_id": "wrong-activation"}, False, True, displacement)
        send({**packet(4), "recenter_epoch": ready["epoch"]+1}, False, True, displacement)
        send({**packet(5), "imu_sample_time_s": 0.}, False, True, displacement)
        send({**packet(6, lost=True), "imu_sample_time_s": 0.}, True, False, [0, 0, 0])
        send(packet(7), True, True, displacement)  # loss must not reset seated origin
        sender.close()  # eighth datagram: real terminating invalid
        terminal = reply("pose")
        assert terminal["seq"] == 8 and terminal["accepted"] and not terminal["pose_valid"], terminal
        assert reply("done")["replies"] == 8
        assert process.wait(timeout=1) == 0
        print("PASS: C++ exporter -> Python IMU -> Python position -> C++ receiver/bridge/6DoF")
    finally:
        if sender is not None:
            sender.close()
        source.close()
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
            if reader is not None:
                reader.join(timeout=1)
            process.stdout.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: check_driver_camera_interop.py <fixture.exe>")
    check(sys.argv[1])
