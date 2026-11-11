"""Owned diagnostic IMU capture; pose history shares Python's local clock.

Uses the existing tested C++ fusion, not a second implementation. The process
must be the sensor owner (SteamVR driver must be stopped for this trial).
"""

from collections import deque
import copy
import json
import math
from pathlib import Path
import subprocess
import threading
import time
import numpy as np

from .imu_clock import ImuClockMapper

MAX_AGE_S = .15


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


class DiagnosticImuSource:
    def __init__(self, executable, config):
        self.executable = str(Path(executable).resolve())
        if not Path(self.executable).is_file() or not Path(config).is_file():
            raise ValueError("IMU diagnostic executable/config not found")
        self.samples = deque(maxlen=200)
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)
        self.offset = None
        self.clock_mapper = ImuClockMapper()
        self.process = subprocess.Popen([self.executable,"--config",str(Path(config).resolve()),
            "--imu","--duration-ms","0","--json-stream","--output-interval-ms","20",
            "--raw-packets","0"],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
            text=True,encoding="utf-8",errors="replace",bufsize=1,
            creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        self.thread = threading.Thread(target=self._read,daemon=True)
        self.thread.start()

    def _read(self):
        for line in self.process.stdout:
            at = line.find("{")
            if at < 0:
                continue
            try:
                row = json.loads(line[at:])
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict) or row.get("event") != "imu":
                continue
            received = time.perf_counter()
            with self.lock:
                self._accept(row, received)

    def _accept(self, row, received):
        """Publish a diagnostic row under lock; elapsed reset starts new history."""
        elapsed = row.get("host_elapsed_s")
        try:
            valid_time = type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0
        except OverflowError:
            valid_time = False
        if not valid_time:
            return False
        for key in ("quaternion_wxyz", "quaternion_sensor_wxyz"):
            if key not in row:
                continue
            value = row[key]
            if (not isinstance(value, (list, tuple)) or len(value) != 4
                    or not all(map(_number, value))):
                return False
            if abs(math.hypot(*value)-1) > 1e-3:
                return False
        if self.samples:
            last = self.samples[-1]
            if elapsed < last["host_elapsed_s"]:
                self.clock_mapper.reset()
                self.samples.clear()
            elif row.get("recenter_count") != last.get("recenter_count"):
                self.samples.clear()
        row = copy.deepcopy(row)
        row["monotonic_s"] = self.clock_mapper.map(elapsed, elapsed, received)
        self.offset = self.clock_mapper.offset
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
            while (not self.samples or self.samples[-1]["monotonic_s"] < captured_at):
                remaining = deadline-time.perf_counter()
                if remaining <= 0:
                    break
                self.changed.wait(remaining)
            past = next((s for s in reversed(self.samples) if s["monotonic_s"] <= captured_at),None)
            future = next((s for s in self.samples if s["monotonic_s"] > captured_at),None)
            if past is None:
                return None
            result = copy.deepcopy(past)
            if captured_at-past["monotonic_s"] > MAX_AGE_S:
                result["valid"] = False
                return result
            if (future is not None and past.get("valid") and future.get("valid")
                    and past.get("recenter_count") == future.get("recenter_count")
                    and 0 < future["monotonic_s"]-past["monotonic_s"] <= .06):
                alpha = (captured_at-past["monotonic_s"])/(future["monotonic_s"]-past["monotonic_s"])
                for key in ("quaternion_wxyz","quaternion_sensor_wxyz"):
                    if key not in past or key not in future:
                        continue
                    q0,q1 = np.asarray(past[key],float),np.asarray(future[key],float)
                    if np.dot(q0,q1) < 0:
                        q1 = -q1
                    q = (1-alpha)*q0+alpha*q1
                    norm = np.linalg.norm(q)
                    if np.isfinite(norm) and norm > .5:
                        result[key] = (q/norm).tolist()
                        result["monotonic_s"] = captured_at
                        result["interpolated"] = True
            return result

    def close(self):
        if self.process.poll() is None:
            try:
                subprocess.run([self.executable,"--stop-imu",str(self.process.pid)],
                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=3,
                    creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
                self.process.wait(timeout=5)
            except (subprocess.TimeoutExpired,OSError):
                self.process.terminate()
                self.process.wait(timeout=5)
        self.thread.join(timeout=2)
        self.process.stdout.close()
