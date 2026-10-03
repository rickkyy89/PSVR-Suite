"""PSVR diagnostic monitor. USB access belongs exclusively to psvr_diag.exe.

Only the Tk/main-thread pump mutates telemetry from queued worker messages.
HTTP handlers consume locked snapshots, never Tk objects or subprocess commands.

Native exit 6 on a power/mode one-shot means "stato non verificato", not
necessarily failed transport (redundant power-on may produce no new F0).
An IMU exit 0 means stopped, never an M3 measurement/profile pass.
Discovery uses an OS-held byte/file lock: a second app cannot replace it or
start USB work. The inert .lock file remains; process exit releases its lock.
"""
from __future__ import annotations

import argparse
from collections import deque
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[2]
STATES = {"warming", "calibrating", "tracking", "calibration-failed",
          "tracking-lost", "stopped"}
FRESH_SECONDS = 1.5
MAX_HISTORY = 200
STATIONARY_MEASUREMENT_MS = 30000
STATIONARY_MARGIN_MS = 1000  # Device-clock window vs. host-clock capture deadline.
NATIVE_READY_TIMEOUT_S = 8.0
CALIBRATION_GUIDANCE = (
    "Appoggia il visore immobile sul tavolo, mani staccate durante calibrazione; "
    "dopo tracking indossa e Ricentra."
)
RECENTER_GUIDANCE = "Ricentra azzera riferimento, NON corregge drift né ricalcola bias."
HISTORY_LEGEND = "Angoli · yaw(Y) ROSSO / pitch(X) VERDE / roll(Z) BLU · ±180°"
BODY_AXES_LEGEND = "Assi corpo 3D: +X rosso · +Y verde · −Z blu (non colori degli angoli)"


class TelemetryError(ValueError):
    pass


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _vector(value, size):
    return isinstance(value, list) and len(value) == size and all(_number(v) for v in value)


def stationary_minimum_ms(root):
    """Minimum CLI duration: config calibration + startup + 30000 + 1000 ms.

    With 8000/200 ms this is 39200 ms; 42000 ms is a convenient launch value.
    Read only these two timing settings. Native remains the full config validator.
    """
    config_path = Path(root) / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("config deve essere un oggetto")
        tracking = config.get("tracking", {})
        if not isinstance(tracking, dict):
            raise ValueError("tracking deve essere un oggetto")
        calibration = tracking.get("calibration_ms", 8000)
        startup = tracking.get("startup_discard_ms", 200)
        if type(calibration) is not int or not 500 <= calibration <= 10000:
            raise ValueError("tracking.calibration_ms deve essere 500..10000 ms")
        if type(startup) is not int or not 0 <= startup <= 2000:
            raise ValueError("tracking.startup_discard_ms deve essere 0..2000 ms")
    except (OSError, ValueError, RecursionError) as exc:
        raise ValueError(f"Impossibile verificare durata test statico da {config_path}: {exc}") from exc
    return calibration + startup + STATIONARY_MEASUREMENT_MS + STATIONARY_MARGIN_MS


def validate_imu_options(duration_ms, stationary_test, root=ROOT):
    if type(duration_ms) is not int or not 0 <= duration_ms <= 180000:
        raise ValueError("--imu-duration-ms deve essere un intero tra 0 e 180000")
    if type(stationary_test) is not bool:
        raise ValueError("stationary_test deve essere booleano")
    if stationary_test:
        if duration_ms == 0:
            raise ValueError("--stationary-test richiede durata finita, non 0")
        minimum = stationary_minimum_ms(root)
        if duration_ms < minimum:
            raise ValueError(f"--stationary-test richiede --imu-duration-ms >= {minimum} "
                             "(config: calibrazione + startup + 30000 ms misura + 1000 ms margine)")


def _require(condition, message):
    if not condition:
        raise TelemetryError(message)


def parse_data_line(line):
    """Return validated telemetry or None for a human log; never repair bad data."""
    match = re.match(r"^.+?\s+\[DATA\]\s+(.*)$", line.strip())
    if not match:
        return None
    _require(len(line) <= 65536, "riga DATA troppo lunga")
    try:
        data = json.loads(match.group(1), parse_constant=lambda x: (_ for _ in ()).throw(
            TelemetryError("JSON non finito: " + x)))
    except (ValueError, TypeError, RecursionError) as exc:
        raise TelemetryError(str(exc)) from exc
    _require(isinstance(data, dict), "DATA deve essere un oggetto")
    _require(type(data.get("schema_version")) is int and data["schema_version"] == 1,
             "schema_version non supportata")
    event = data.get("event")
    _require(isinstance(event, str) and event in {"usb_device", "imu_state", "imu", "control_status"}, "evento non supportato")
    if event == "usb_device":
        for key in ("instance_id", "name", "service"):
            _require(isinstance(data.get(key), str), key + " non valido")
        _require(type(data.get("problem_code")) is int, "problem_code non valido")
        _require(type(data.get("is_psvr")) is bool, "is_psvr non valido")
    elif event == "control_status":
        for key in ("power_on", "worn"):
            _require(type(data.get(key)) is bool, key + " non valido")
        _require(isinstance(data.get("mode"), str), "mode non valido")
        for key in ("vr_active", "status_mask"):
            _require(type(data.get(key)) is int and 0 <= data[key] <= 255, key + " non valido (byte 0..255)")
    else:
        _require(isinstance(data.get("state"), str) and data["state"] in STATES, "stato IMU non valido")
        _require(type(data.get("valid")) is bool, "valid non valido")
        _require("reason" not in data or isinstance(data["reason"], str), "reason non valido")
        if event == "imu":
            for key in ("host_elapsed_s", "device_elapsed_s"):
                _require(_number(data.get(key)) and data[key] >= 0, key + " non valido")
            _require(type(data.get("tick24")) is int and 0 <= data["tick24"] < 2**24,
                     "tick24 non valido")
            _require(type(data.get("recenter_count")) is int and data["recenter_count"] >= 0,
                     "recenter_count non valido")
            angles = data.get("angles_deg")
            _require(isinstance(angles, dict) and all(_number(angles.get(k)) for k in
                     ("yaw", "pitch", "roll")), "angles_deg non valido")
            for key, size in (("quaternion_wxyz", 4), ("gyro_rad_s", 3),
                              ("corrected_gyro_rad_s", 3), ("accel_mps2", 3)):
                _require(_vector(data.get(key), size), key + " non valido")
            norm = sum(v*v for v in data["quaternion_wxyz"])
            _require(0.5 <= norm <= 1.5, "quaternione non unitario")
            sensor = data.get("sensor")
            _require(isinstance(sensor, dict), "sensor non valido")
            for key in ("worn", "display_flag_raw"):
                _require(type(sensor.get(key)) is bool, key + " non valido")
            for key, maximum in (("status_raw", 255), ("ir_raw", 65535)):
                _require(type(sensor.get(key)) is int and 0 <= sensor[key] <= maximum,
                         key + " fuori range raw")
            cal = data.get("calibration")
            _require(isinstance(cal, dict), "calibration non valido")
            for key in ("samples", "target_ms"):
                _require(type(cal.get(key)) is int and cal[key] >= 0, key + " non valido")
            _require(500 <= cal["target_ms"] <= 10000, "calibration.target_ms fuori range 500..10000")
            if "elapsed_ms" in cal:
                # The last accepted sample may overshoot the target by up to
                # native max_sample_gap_us (maximum allowed value: 20000 us).
                _require(_number(cal["elapsed_ms"]) and 0 <= cal["elapsed_ms"] <= cal["target_ms"] + 20,
                         "calibration.elapsed_ms non valido")
            for key in ("gyro_mean_dps", "gyro_std_dps", "accel_std_mps2"):
                _require(_vector(cal.get(key), 3), key + " non valido")
                if key != "gyro_mean_dps":
                    _require(all(v >= 0 for v in cal[key]), key + " deve essere non negativo")
            if "gyro_halves_mean_shift_dps" in cal:
                _require(_vector(cal["gyro_halves_mean_shift_dps"], 3),
                         "gyro_halves_mean_shift_dps non valido")
            for key, low, high in (("gyro_std_limit_dps", .01, 5), ("gyro_mean_shift_limit_dps", .001, 1)):
                if key in cal:
                    _require(_number(cal[key]) and low <= cal[key] <= high, key + " fuori range nativo")
            rate = data.get("rate")
            _require(isinstance(rate, dict), "rate non valido")
            for key in ("reports", "samples"):
                _require(type(rate.get(key)) is int and rate[key] >= 0, key + " non valido")
            for key in ("report_hz", "sample_hz"):
                _require(_number(rate.get(key)) and rate[key] >= 0, key + " non valido")
    # Also reject overflow/NaN in unknown extension fields, including nested ones.
    def finite_tree(value, depth=0):
        _require(depth <= 32, "DATA troppo annidato")
        if isinstance(value, float):
            _require(math.isfinite(value), "valore non finito")
        elif isinstance(value, dict):
            for child in value.values():
                finite_tree(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                finite_tree(child, depth + 1)
    finite_tree(data)
    return data


class MonitorState:
    def __init__(self, clock=time.monotonic):
        self.lock = threading.RLock()
        self.clock = clock
        self.started = clock()
        self.worker_pid = None
        self.action_pid = None
        self.running = False
        self.native_ready = False
        self.readiness_reason = None
        self.readiness_timeout_s = NATIVE_READY_TIMEOUT_S
        self.worker_generation = 0
        self.pending_stop = False
        self.stop_status = None
        self.stop_error = None
        self.measurement_profile = None
        self.imu_duration_ms = 0
        self.busy = False
        self.action = "Pronto — rilevamento in sola lettura"
        self.phase = "stopped"
        self.latest_data = None
        self.pose = None
        self.pose_gate = None
        self.last_data_at = None
        self.usb_devices = {}
        self.control_status = None
        self.last_control_result = None
        self.last_error = None
        self.last_calibration_error = None
        self.counters = {"data": 0, "imu": 0, "parse_errors": 0, "eof": 0, "errors": 0}
        self.logs = deque(maxlen=MAX_HISTORY)
        self.samples = deque(maxlen=MAX_HISTORY)

    def log(self, text, level="info"):
        with self.lock:
            self.logs.append({"elapsed_s": self.clock() - self.started,
                              "level": level, "text": str(text)[:8192]})

    def invalidate(self, phase=None):
        with self.lock:
            self.pose = None
            if phase:
                self.phase = phase

    def error(self, text, invalidate=True):
        with self.lock:
            self.last_error = str(text)
            self.counters["errors"] += 1
            if invalidate:
                self.invalidate("tracking-lost")
            self.log(text, "error")

    def accept(self, data, received_at=None):
        with self.lock:
            received_at = self.clock() if received_at is None else received_at
            self.counters["data"] += 1
            event = data["event"]
            if event == "usb_device":
                if data["is_psvr"]:
                    node = copy.deepcopy(data)
                    instance = node["instance_id"].upper()
                    node["vid_pid"] = "054C:09AF" if "VID_054C&PID_09AF" in instance else None
                    node["interface"] = (4 if "MI_04" in instance else
                                         5 if "MI_05" in instance else None)
                    expected = {4: "HidUsb", 5: "WinUSB"}.get(node["interface"])
                    node["expected_service"] = expected
                    node["binding_ok"] = (node["problem_code"] == 0 and
                        node["service"].lower() == expected.lower()) if expected else None
                    self.usb_devices[node["instance_id"]] = node
            elif event == "control_status":
                self.control_status = copy.deepcopy(data)
            elif event == "imu_state":
                self.phase = data["state"]
                if self.phase == "calibration-failed" and data.get("reason"):
                    self.last_calibration_error = data["reason"]
                    self.error(data["reason"], invalidate=False)
                if self.pose_gate == "calibration" and self.phase in {"warming", "calibrating"}:
                    self.pose_gate = None
                if self.phase != "tracking" or not data["valid"]:
                    self.invalidate()
            elif event == "imu":
                self.counters["imu"] += 1
                self.phase = data["state"]
                self.latest_data = copy.deepcopy(data)
                self.last_data_at = received_at
                if self.pose_gate == "calibration" and self.phase in {"warming", "calibrating"}:
                    self.pose_gate = None
                valid = self.phase == "tracking" and data["valid"] and self.pose_gate is None
                self.pose = {"angles_deg": copy.deepcopy(data["angles_deg"]),
                             "quaternion_wxyz": list(data["quaternion_wxyz"])} if valid else None
                self.samples.append({"received_elapsed_s": received_at - self.started,
                                     "valid": valid, "data": copy.deepcopy(data)})

    def snapshot(self):
        with self.lock:
            age = None if self.last_data_at is None else max(0, self.clock() - self.last_data_at)
            fresh = age is not None and age <= FRESH_SECONDS and self.running
            valid = fresh and self.phase == "tracking" and self.pose is not None and self.pose_gate is None
            return copy.deepcopy({"schema_version": 1, "pid": os.getpid(),
                "worker_pid": self.worker_pid, "running": self.running, "busy": self.busy,
                "worker_generation": self.worker_generation, "native_ready": self.native_ready,
                "startup_state": ("ready" if self.native_ready else "waiting-native") if self.running else "idle",
                "readiness_reason": self.readiness_reason, "readiness_timeout_s": self.readiness_timeout_s,
                "pending_stop": self.pending_stop, "stop_status": self.stop_status, "stop_error": self.stop_error,
                "measurement_profile": self.measurement_profile, "imu_duration_ms": self.imu_duration_ms,
                "action_pid": self.action_pid,
                "phase": self.phase, "action": self.action, "pending_transition": self.pose_gate,
                "latest_data": self.latest_data,
                "latest_pose": self.pose if valid else None, "pose_valid": valid,
                "age_s": age, "fresh": fresh, "stale_after_s": FRESH_SECONDS,
                "usb_devices": list(self.usb_devices.values()), "control_status": self.control_status,
                "last_control_result": self.last_control_result,
                "last_calibration_error": self.last_calibration_error,
                "last_error": self.last_error, "counters": self.counters, "logs": list(self.logs)})

    def history(self, name, limit):
        with self.lock:
            return copy.deepcopy(list(getattr(self, name))[-limit:] if limit else [])


class CaptureSession:
    """Immutable process/generation ownership; readiness is not inferred from Popen."""
    def __init__(self, process, generation, timeout_s=NATIVE_READY_TIMEOUT_S):
        self.process = process
        self.generation = generation
        self.ready = threading.Event()
        self.exited = threading.Event()
        self.stop_done = threading.Event()
        self.stop_started = False  # protected by Backend.guard
        self.timeout_s = timeout_s
        self.deadline = time.monotonic() + timeout_s
        self.termination_lock = threading.Lock()


class Backend:
    """Workers enqueue messages; pump is the only consumer (Tk or headless)."""
    def __init__(self, root=ROOT, state=None, popen=None, run=None):
        self.root = Path(root)
        self.exe = self.root / "out" / "diag" / "psvr_diag.exe"
        self.config = self.root / "config.json"
        self.state = state or MonitorState()
        self.messages = queue.Queue(maxsize=4096)
        self.popen = popen or subprocess.Popen
        self.run = run or subprocess.run
        self.guard = threading.RLock()
        self.worker = None
        self.capture = None
        self.native_ready_timeout_s = NATIVE_READY_TIMEOUT_S
        self.generation = 0
        self.closing = False
        self.threads = []

    def command(self, *args):
        return [str(self.exe), "--config", str(self.config), *args, "--json-stream"]

    def _emit(self, kind, value, generation):
        if kind in {"line", "worker_line"}:
            # Freshness must reflect reception, not a delayed Tk queue drain.
            value = (value, self.state.clock())
        while not self.closing or kind in {"worker_line", "stream_end", "eof", "error", "done", "stop_status", "stop_error", "stop_done"}:
            try:
                self.messages.put((kind, value, generation), timeout=0.1)
                return
            except queue.Full:
                if self.closing:
                    return

    def pump(self):
        for _ in range(1000):
            try:
                kind, value, generation = self.messages.get_nowait()
            except queue.Empty:
                break
            if generation != self.generation:
                continue
            with self.state.lock:
                if kind in {"line", "worker_line"}:
                    received_at = None
                    if isinstance(value, tuple):
                        value, received_at = value
                    self.state.log(value.rstrip())
                    try:
                        data = parse_data_line(value)
                        if data:
                            self.state.accept(data, received_at)
                            capture = self.capture
                            if (kind == "worker_line" and capture is not None and
                                capture.generation == generation and self.worker is capture.process and
                                self.state.running and (data["event"] == "imu" or
                                (data["event"] == "imu_state" and data.get("reason") == "capture-started"))):
                                self.state.native_ready = True
                                self.state.readiness_reason = "imu" if data["event"] == "imu" else "capture-started"
                                capture.ready.set()
                    except TelemetryError as exc:
                        self.state.counters["parse_errors"] += 1
                        self.state.error("Telemetria rifiutata: " + str(exc))
                elif kind == "error":
                    self.state.error(value)
                elif kind == "stop_error":
                    self.state.stop_error = value
                    self.state.error(value, invalidate=False)
                elif kind == "stop_status":
                    self.state.stop_status = value
                    if self.state.running:
                        self.state.action = "Stop: " + value
                elif kind == "stop_done":
                    self.state.pending_stop = False
                    self.state.busy = False
                    self.state.stop_status = "completed" if value else "failed"
                    if not value:
                        self.state.pose_gate = None
                        self.state.invalidate("tracking-lost")
                    if not self.state.running:
                        self.state.action = "IMU arrestata con errore" if self.state.last_error else "IMU arrestata"
                elif kind == "control_result":
                    self.state.last_control_result = value
                    if value["exit_code"] == 6:
                        self.state.log(value["message"], "warning")
                elif kind == "stream_end":
                    self.state.invalidate(None if self.state.phase == "calibration-failed" else "tracking-lost")
                    self.state.action = "Stream IMU chiuso — attesa uscita worker"
                elif kind == "eof":
                    self.state.counters["eof"] += 1
                    self.state.running = False
                    self.state.native_ready = False
                    self.state.pending_stop = False
                    self.state.measurement_profile = None
                    self.state.pose_gate = None
                    self.state.worker_pid = None
                    self.state.invalidate("stopped")
                    if value == 7 and self.state.last_calibration_error:
                        # Do not replace the precise native rejection with "exit 7".
                        self.state.phase = "calibration-failed"
                        self.state.last_error = self.state.last_calibration_error
                        self.state.log("Worker terminato con codice 7: " + self.state.last_calibration_error, "error")
                    elif value and self.state.stop_error:
                        self.state.last_error = self.state.stop_error
                        self.state.log("Worker terminato con codice " + str(value) + ": " + self.state.stop_error, "error")
                    elif value:
                        self.state.error("Worker terminato con codice " + str(value))
                    self.state.action = "IMU arrestata" if not value else "Errore worker IMU"
                elif kind == "done":
                    self.state.busy = False
                    self.state.action_pid = None
                    self.state.action = value

    def _thread(self, target):
        thread = threading.Thread(target=target, daemon=True)
        self.threads.append(thread)
        thread.start()

    def _reserve(self, action):
        # Guard is also held across Popen, so stop/shutdown cannot miss a new worker.
        with self.state.lock:
            alive = self.worker is not None and self.worker.poll() is None
            if self.closing or self.state.busy or self.state.running or alive:
                self.state.log("Azione rifiutata: arrestare prima il worker / attendere l'azione", "warning")
                return False
            self.generation += 1
            self.state.busy = True
            self.state.action = action
            self.state.last_error = None
            return True

    @staticmethod
    def _process_options():
        return {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT, "text": True, "encoding": "utf-8",
                "errors": "replace", "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}

    def start_imu(self, duration_ms=0, stationary_test=False):
        validate_imu_options(duration_ms, stationary_test, self.root)
        with self.guard:
            action = (f"Test statico su tavolo — mani staccate, durata {duration_ms / 1000:.1f} s; attendi l'esito"
                      if stationary_test else "Avvio IMU — tenere il visore fermo")
            if not self._reserve(action):
                return False
            generation = self.generation
            try:
                arguments = ["--imu", "--duration-ms", str(duration_ms), "--output-interval-ms", "100"]
                if stationary_test:
                    arguments.append("--stationary-test")
                self.worker = self.popen(self.command(*arguments), **self._process_options())
            except (OSError, ValueError) as exc:
                self.state.error("Avvio non riuscito: " + str(exc))
                with self.state.lock:
                    self.state.busy = False
                return False
            proc = self.worker
            capture = CaptureSession(proc, generation, self.native_ready_timeout_s)
            self.capture = capture
            with self.state.lock:
                self.state.running = True
                self.state.native_ready = False
                self.state.readiness_reason = None
                self.state.readiness_timeout_s = self.native_ready_timeout_s
                self.state.worker_generation = generation
                self.state.pending_stop = False
                self.state.stop_status = None
                self.state.stop_error = None
                self.state.measurement_profile = "stationary" if stationary_test else None
                self.state.imu_duration_ms = duration_ms
                self.state.busy = False
                self.state.worker_pid = proc.pid
                self.state.latest_data = None
                self.state.last_data_at = None
                self.state.last_calibration_error = None
                self.state.pose_gate = None
                self.state.invalidate("warming")

            def read_worker():
                try:
                    for line in proc.stdout:
                        self._emit("worker_line", line, generation)
                    self._emit("stream_end", None, generation)
                    code = proc.wait()
                except (OSError, ValueError) as exc:
                    self._emit("error", "Lettura worker: " + str(exc), generation)
                    code = proc.poll()
                    if code is None:
                        # Keep ownership/running until the process actually exits.
                        code = proc.wait()
                finally:
                    if proc.stdout:
                        proc.stdout.close()
                capture.exited.set()
                self._emit("eof", code, generation)
            self._thread(read_worker)
            def watch_startup():
                outcome = self._wait_ready(capture)
                if outcome == "timeout":
                    # Same single stop task as an early Stop/Close; no IPC retries.
                    self._ensure_stop(capture)
            self._thread(watch_startup)
            return True

    def one_shot(self, flag):
        if flag not in {"--enumerate-only", "--power-on", "--power-off", "--vr-mode", "--cinema-mode"}:
            raise ValueError("Comando non autorizzato")
        with self.guard:
            if not self._reserve("In corso: " + flag):
                return False
            generation = self.generation
            if flag == "--enumerate-only":
                with self.state.lock:
                    self.state.usb_devices.clear()

            def execute():
                proc = None
                completion = "Comando concluso: " + flag
                try:
                    # One-shot children are also owned and tracked for bounded shutdown.
                    with self.guard:
                        if self.closing:
                            return
                        proc = self.popen(self.command(flag), **self._process_options())
                        self.worker = proc
                        with self.state.lock:
                            self.state.action_pid = proc.pid
                    try:
                        output, _ = proc.communicate(timeout=15)
                    except subprocess.TimeoutExpired:
                        self._emit("error", "Timeout comando " + flag +
                            ": attesa uscita; chiudere l'app se resta bloccato", generation)
                        # Do not abandon ownership or kill on an ordinary action.
                        # Shutdown may terminate this exact handle after a bounded wait.
                        output, _ = proc.communicate()
                    for line in (output or "").splitlines():
                        self._emit("line", line, generation)
                    if flag != "--enumerate-only":
                        # Code 6 does not prove transport failure or command rejection.
                        message = ("Stato non verificato (exit 6): " + flag +
                                   "; il comando può essere stato accettato senza nuovo stato F0")
                        self._emit("control_result", {"command": flag, "exit_code": proc.returncode,
                            "state_verified": proc.returncode == 0,
                            "message": message if proc.returncode == 6 else completion}, generation)
                        if proc.returncode == 6:
                            completion = "Stato non verificato (exit 6): " + flag
                    if proc.returncode and not (proc.returncode == 6 and flag != "--enumerate-only"):
                        self._emit("error", "Comando fallito: " + str(proc.returncode), generation)
                except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                    self._emit("error", str(exc), generation)
                finally:
                    self._emit("done", completion, generation)
            self._thread(execute)
            return True

    def signal_worker(self, flag):
        if flag not in {"--stop-imu", "--recenter", "--recalibrate"}:
            raise ValueError("Segnale non autorizzato")
        with self.guard, self.state.lock:
            proc = self.worker
            capture = self.capture
            if self.closing or not self.state.running or self.state.busy or proc is None or proc.poll() is not None:
                return False
            if capture is None or capture.process is not proc or capture.generation != self.generation:
                return False
            if capture.exited.is_set():
                return False
            if self.state.measurement_profile and flag != "--stop-imu":
                self.state.log("Profilo di misura attivo: Ricentra e Ricalibra bias non consentiti", "warning")
                return False
            if flag == "--stop-imu":
                return self._ensure_stop(capture)
            if not capture.ready.is_set() or not self.state.native_ready:
                self.state.log("IPC nativa non ancora pronta: attendere capture-started / dati IMU", "warning")
                return False
            if flag == "--recenter" and not self.state.snapshot()["pose_valid"]:
                self.state.log("Ricentra richiede tracking valido e fresco", "warning")
                return False
            generation = self.generation
            self.state.busy = True
            self.state.action = "In corso: " + flag
            if flag == "--recalibrate":
                self.state.last_calibration_error = None
                self.state.last_error = None
                self.state.pose_gate = "calibration"
                self.state.invalidate("calibrating")

            def signal():
                try:
                    result = self.run(self.command(flag, str(proc.pid)), timeout=4,
                                      **self._process_options())
                    for line in (result.stdout or "").splitlines():
                        self._emit("line", line, generation)
                    if result.returncode:
                        raise RuntimeError("Segnale fallito: " + str(result.returncode))
                except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                    self._emit("error", str(exc), generation)
                finally:
                    self._emit("done", "Segnale concluso: " + flag, generation)
            self._thread(signal)
            return True

    def _wait_ready(self, capture):
        """Poll owned-process liveness, not named IPC. One deadline per generation."""
        while True:
            if capture.exited.is_set() or capture.process.poll() is not None:
                return "exited"
            if capture.ready.is_set():
                return "ready"
            remaining = capture.deadline - time.monotonic()
            if remaining <= 0:
                return "timeout"
            capture.ready.wait(min(.05, remaining))

    def _terminate_owned(self, capture):
        # Only the immutable Popen handle, never taskkill / PID-based termination.
        with capture.termination_lock:
            proc = capture.process
            if capture.exited.is_set() or proc.poll() is not None:
                return
            self.state.log("Fallback shutdown del solo worker posseduto", "warning")
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
            capture.exited.set()

    def _ensure_stop(self, capture):
        with self.guard, self.state.lock:
            if capture is not self.capture or capture.generation != self.generation or capture.stop_started:
                return False
            capture.stop_started = True
            self.state.busy = True
            self.state.pending_stop = True
            self.state.stop_status = "waiting-ready" if not capture.ready.is_set() else "sending"
            self.state.action = "Stop richiesto: attesa readiness nativa" if not capture.ready.is_set() else "Stop richiesto"
            self.state.pose_gate = "stop"
            self.state.invalidate("stopped")

            def stop():
                try:
                    outcome = self._wait_ready(capture)
                    if outcome == "exited":
                        return
                    if outcome == "timeout":
                        self._emit("stop_error", "Readiness nativa non ricevuta entro " +
                            f"{capture.timeout_s:g} s; shutdown limitato al worker posseduto", capture.generation)
                    else:
                        self._emit("stop_status", "sending", capture.generation)
                        try:
                            # Exactly one IPC invocation, after readiness and a final
                            # owned-handle liveness check. Exit racing IPC is benign.
                            if capture.process.poll() is not None or capture.exited.is_set():
                                return
                            result = self.run(self.command("--stop-imu", str(capture.process.pid)),
                                timeout=4, **self._process_options())
                            for text in (result.stdout or "").splitlines():
                                self._emit("line", text, capture.generation)
                            if result.returncode:
                                raise RuntimeError("Segnale Stop fallito: " + str(result.returncode))
                            self._emit("stop_status", "waiting-exit", capture.generation)
                            capture.process.wait(timeout=4)
                            capture.exited.set()
                            return
                        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                            if capture.process.poll() is not None or capture.exited.is_set():
                                return
                            self._emit("stop_error", "Stop non confermato: " + str(exc) +
                                       "; shutdown limitato al worker posseduto", capture.generation)
                    self._emit("stop_status", "owned-fallback", capture.generation)
                    self._terminate_owned(capture)
                except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                    self._emit("stop_error", "Shutdown worker posseduto non confermato: " + str(exc) +
                               "; chiudere l'app per riprovare il solo shutdown posseduto", capture.generation)
                finally:
                    self._emit("stop_done", capture.exited.is_set() or capture.process.poll() is not None, capture.generation)
                    capture.stop_done.set()
            self._thread(stop)
            return True

    def shutdown(self):
        """Called off the Tk thread. Only our Popen handle can be terminated."""
        with self.guard:
            self.closing = True
            proc = self.worker
            capture = self.capture
            with self.state.lock:
                is_imu = self.state.running
                self.state.pose_gate = "stop"
                self.state.invalidate("stopped")
        if proc is not None and proc.poll() is None:
            if is_imu and capture is not None and capture.process is proc:
                self._ensure_stop(capture)
                # Shared task, including pending startup Stop. Keep Tk pumping
                # worker lines during Close so readiness can still be observed.
                if not capture.stop_done.wait(timeout=capture.timeout_s + 13):
                    raise RuntimeError("Timeout shutdown del worker posseduto")
                self._terminate_owned(capture)
            else:
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.state.log("Chiusura: fallback terminate del solo worker posseduto", "warning")
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2)
        with self.state.lock:
            self.state.running = False
            self.state.native_ready = False
            self.state.pending_stop = False
            self.state.measurement_profile = None
            self.state.pose_gate = None
            self.state.worker_pid = None
            self.state.action_pid = None


class DiscoveryLease:
    """OS lock, not a stale-PID check: atomic exclusion and crash-safe release."""
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def acquire(self):
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError("Monitor già attivo o discovery occupata; chiudere l'altra istanza") from exc
        self.file = handle

    def close(self):
        if self.file is not None:
            # Closing releases the lock, including on Windows. Do not unlink the
            # lock inode: that would allow two owners locking different files.
            self.file.close()
            self.file = None


class MonitorAPI:
    def __init__(self, state, discovery_path=None):
        self.state = state
        self.discovery_path = Path(discovery_path) if discovery_path else ROOT / "logs" / "monitor_api.json"
        self.lease = DiscoveryLease(self.discovery_path.with_suffix(".lock"))
        self.owns_discovery = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(2)

            def log_message(self, *_):
                pass

            def _reply(self, status, value):
                payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                if status == 405:
                    self.send_header("Allow", "GET")
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                # No wildcard CORS; reject browser origins and DNS-rebinding hosts.
                host = self.headers.get("Host", "")
                if host not in {f"127.0.0.1:{owner.server.server_port}", f"localhost:{owner.server.server_port}"} or self.headers.get("Origin"):
                    self._reply(403, {"error": "loopback clients only"})
                    return
                url = urlsplit(self.path)
                if url.path == "/health":
                    self._reply(200, {"schema_version": 1, "ok": True, "pid": os.getpid(),
                        "uptime_s": state.clock() - state.started})
                elif url.path == "/state":
                    self._reply(200, state.snapshot())
                elif url.path in {"/samples", "/logs"}:
                    try:
                        values = parse_qs(url.query, keep_blank_values=True, max_num_fields=8)
                        limit = int(values.get("limit", ["100"])[0])
                        if limit < 0:
                            raise ValueError()
                    except ValueError:
                        self._reply(400, {"error": "limit must be a nonnegative integer"})
                        return
                    limit = min(MAX_HISTORY, limit)
                    self._reply(200, {"schema_version": 1, "items": state.history(url.path[1:], limit)})
                else:
                    self._reply(404, {"error": "not found"})

            def reject_write(self):
                self._reply(405, {"error": "read-only API"})

            do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = reject_write
            do_TRACE = do_CONNECT = reject_write

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        try:
            self.discovery_path.parent.mkdir(parents=True, exist_ok=True)
            self.lease.acquire()
            metadata = {"schema_version": 1, "pid": os.getpid(), "base_url": self.base_url,
                        "endpoints": ["/health", "/state", "/samples?limit=N", "/logs?limit=N"]}
            temporary = self.discovery_path.with_name(self.discovery_path.name + f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
            temporary.replace(self.discovery_path)
            self.owns_discovery = True
        except (OSError, RuntimeError):
            self.lease.close()
            self.server.server_close()
            raise
        self.thread.start()
        self.state.log("API locale sola lettura: " + self.base_url)
        return self.base_url

    def close(self):
        if self.thread.is_alive():
            self.server.shutdown()
            self.thread.join(timeout=2)
        self.server.server_close()
        try:
            if self.owns_discovery:
                metadata = json.loads(self.discovery_path.read_text(encoding="utf-8"))
                if metadata.get("pid") == os.getpid() and metadata.get("base_url") == self.base_url:
                    self.discovery_path.unlink()
        except (OSError, ValueError):
            pass
        finally:
            self.owns_discovery = False
            self.lease.close()


class MonitorWindow:
    def __init__(self, backend, api, start_imu=False, imu_duration_ms=0, stationary_test=False):
        validate_imu_options(imu_duration_ms, stationary_test, backend.root)
        if (stationary_test or imu_duration_ms != 0) and not start_imu:
            raise ValueError("--imu-duration-ms / --stationary-test richiedono --start-imu")
        # Lazy import keeps headless smoke tests independent of a display/Tk.
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.backend, self.api = backend, api
        self.root = tk.Tk()
        self.root.title("PSVR · Diagnostica IMU")
        self.root.geometry("1150x840")
        self.root.minsize(850, 680)
        self.root.configure(bg="#101b2d")
        self.closing = False
        self.closed = threading.Event()
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background="#101b2d")
        style.configure("Card.TFrame", background="#1a2940")
        style.configure("TLabel", background="#101b2d", foreground="#e1eaf6", font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI", 20, "bold"))
        style.configure("Card.TLabel", background="#1a2940", foreground="#e1eaf6")
        style.configure("TButton", padding=(10, 7), font=("Segoe UI", 10))
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(5, weight=2)
        outer.rowconfigure(6, weight=1)
        ttk.Label(outer, text="PSVR / banco di prova", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        startup_label = "avvio IMU esplicito" if start_imu else "all'avvio solo rilevamento"
        ttk.Label(outer, text="Nessuna USB diretta · " + startup_label + " · API: " + api.base_url).grid(row=1, column=0, sticky="w", pady=(0, 12))
        controls = ttk.Frame(outer)
        controls.grid(row=2, column=0, sticky="ew")
        self.buttons = {}
        actions = [("refresh", "Rileva", lambda: backend.one_shot("--enumerate-only")),
            ("on", "Accendi", lambda: backend.one_shot("--power-on")),
            ("off", "Spegni", lambda: backend.one_shot("--power-off")),
            ("vr", "Modo VR", lambda: backend.one_shot("--vr-mode")),
            ("cinema", "Modo cinema", lambda: backend.one_shot("--cinema-mode")),
            ("start", "Avvia IMU", backend.start_imu),
            ("stop", "Stop", lambda: backend.signal_worker("--stop-imu")),
            ("cal", "Ricalibra bias", lambda: backend.signal_worker("--recalibrate")),
            ("center", "Ricentra", lambda: backend.signal_worker("--recenter"))]
        for index, (key, text, command) in enumerate(actions):
            button = ttk.Button(controls, text=text, command=command)
            button.grid(row=index // 5, column=index % 5, padx=(0, 6), pady=3, sticky="ew")
            controls.columnconfigure(index % 5, weight=1)
            self.buttons[key] = button
        self.instruction = tk.StringVar()
        ttk.Label(outer, textvariable=self.instruction, wraplength=1050, foreground="#80d8ff").grid(row=3, column=0, sticky="ew", pady=10)
        cards = ttk.Frame(outer)
        cards.grid(row=4, column=0, sticky="ew")
        self.labels = {}
        for i, title in enumerate(("Hardware / pannello", "IMU / calibrazione", "Orientamento / sensori")):
            cards.columnconfigure(i, weight=1, uniform="cards")
            card = ttk.Frame(cards, style="Card.TFrame", padding=12)
            card.grid(row=0, column=i, sticky="nsew", padx=(0, 8 if i < 2 else 0))
            ttk.Label(card, text=title, style="Card.TLabel", font=("Segoe UI", 11, "bold")).pack(anchor="w")
            label = ttk.Label(card, text="—", style="Card.TLabel", justify="left", wraplength=340)
            label.pack(anchor="w", fill="x", pady=(7, 0))
            self.labels[i] = label
        plots = ttk.Frame(outer)
        plots.grid(row=5, column=0, sticky="nsew", pady=12)
        plots.columnconfigure(0, weight=1)
        plots.columnconfigure(1, weight=2)
        plots.rowconfigure(0, weight=1)
        self.sketch = tk.Canvas(plots, bg="#142239", highlightthickness=0, height=220)
        self.history_canvas = tk.Canvas(plots, bg="#142239", highlightthickness=0, height=220)
        self.sketch.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self.history_canvas.grid(row=0, column=1, sticky="nsew")
        log_frame = ttk.Frame(outer)
        log_frame.grid(row=6, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)
        ttk.Label(log_frame, text="Console diagnostica · ultime 200 righe · nessuna telemetria simulata").grid(row=0, column=0, sticky="w")
        self.console = tk.Text(log_frame, height=7, bg="#0b1422", fg="#b8c9dd", font=("Consolas", 9), wrap="word", state="disabled")
        self.console.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.console.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.console.configure(yscrollcommand=scroll.set)
        self.last_logs = None
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(0, self.update)
        self.root.after(100, lambda: self.startup(start_imu, imu_duration_ms, stationary_test))

    def startup(self, start_imu=False, duration_ms=0, stationary_test=False):
        # One scheduled action only: the explicit native IMU launch emits USB
        # nodes itself, so enumeration cannot race capture and no retries occur.
        if self.closing:
            return
        if start_imu:
            self.backend.start_imu(duration_ms=duration_ms, stationary_test=stationary_test)
        else:
            self.backend.one_shot("--enumerate-only")

    @staticmethod
    def vector(values, decimals=3):
        return "  ".join(f"{v:+.{decimals}f}" for v in values) if values is not None else "—"

    @staticmethod
    def frequency(value):
        return f"{value:.1f} Hz" if _number(value) else "— Hz"

    @staticmethod
    def limit(value):
        return f"{value:.3f}" if _number(value) else "—"

    @staticmethod
    def calibration_progress(cal):
        elapsed, target = cal.get("elapsed_ms"), cal.get("target_ms")
        if _number(elapsed) and _number(target) and target > 0:
            return f"{elapsed:.0f} / {target:.0f} ms ({min(100, elapsed / target * 100):.0f}%, clock dispositivo)"
        # Older native versions: report actual samples, do not invent progress.
        return f"{cal.get('samples', '—')} campioni / finestra {target if target is not None else '—'} ms"

    def update(self):
        self.backend.pump()
        if self.closing:
            if self.closed.is_set():
                self.root.destroy()
            else:
                self.root.after(100, self.update)
            return
        state = self.backend.state.snapshot()
        running, busy = state["running"], state["busy"]
        for key, button in self.buttons.items():
            enabled = not busy and (running if key in {"stop", "cal", "center"} else not running)
            if key in {"cal", "center"} and state["measurement_profile"]:
                enabled = False
            if key in {"cal", "center"} and not state["native_ready"]:
                enabled = False
            if key == "center" and not state["pose_valid"]:
                enabled = False
            button.configure(state="normal" if enabled else "disabled")
        phase = state["phase"]
        if state["pending_stop"]:
            instruction = "Stop pendente: attendo readiness/uscita del solo worker posseduto; nessun retry IPC alla cieca."
        elif running and not state["native_ready"]:
            instruction = f"Avvio nativo: attendo capture-started / dati IMU (limite {state['readiness_timeout_s']:g} s). Stop è disponibile."
        elif running and state["measurement_profile"] == "stationary":
            instruction = "Test statico: lascia il visore sul tavolo, immobile e mani staccate fino all'esito. " + \
                          "Non indossare né ricentrare durante la misura. Stop interrompe, NON certifica un PASS."
        elif state["pose_valid"]:
            instruction = "Tracking valido: indossa il visore e Ricentra, senza ricalibrare il bias."
        elif running:
            instruction = CALIBRATION_GUIDANCE
        else:
            instruction = "Avvia IMU per calibrare. " + CALIBRATION_GUIDANCE
        data = state["latest_data"] or {}
        target_ms = data.get("calibration", {}).get("target_ms")
        if not state["pose_valid"] and _number(target_ms) and target_ms > 0:
            instruction += f" Finestra di calibrazione richiesta: {target_ms / 1000:.1f} s; attendi l'esito."
        self.instruction.set(state["action"] + " · " + instruction + "\n" + RECENTER_GUIDANCE +
                             ("\nERRORE: " + state["last_error"] if state["last_error"] else ""))
        sensor = data.get("sensor", {}) if state["fresh"] else {}
        panel = state["control_status"] or {}
        worn = sensor.get("worn", panel.get("worn"))
        binding = []
        for node in state["usb_devices"]:
            interface = node["interface"]
            binding.append(f"{node['vid_pid'] or 'PSVR'} / {('IF' + str(interface)) if interface is not None else 'USB'}: "
                           f"{node['service'] or 'nessun driver'} (errore {node['problem_code']})" +
                           (" [driver inatteso]" if node["binding_ok"] is False else ""))
        self.labels[0].configure(text=("\n".join(binding) or "PSVR: nessun nodo rilevato") +
            "\nIndossato: " + ({True: "sì", False: "no"}.get(worn, "sconosciuto")) +
            " · IR RAW: " + str(sensor.get("ir_raw", "—")) +
            "\nPannello (ultimo controllo): " + ({True: "acceso", False: "spento"}.get(panel.get("power_on"), "sconosciuto")) +
            "\nModo (ultimo controllo): " + panel.get("mode", "sconosciuto") +
            ("\nUltimo comando: stato non verificato (exit 6)" if
             (state["last_control_result"] or {}).get("exit_code") == 6 else ""))
        cal, rate = data.get("calibration", {}), data.get("rate", {})
        age = state["age_s"]
        self.labels[1].configure(text=f"Fase: {phase} · {'VALIDO' if state['pose_valid'] else 'NON VALIDO'}\n"
            f"Worker: {state['worker_pid'] or '—'} · IPC: {state['startup_state']}\n"
            f"Stop pendente: {state['stop_status'] if state['pending_stop'] else 'no'}\n"
            f"Calibrazione: {self.calibration_progress(cal)}\n"
            f"Campioni: {cal.get('samples', '—')}\n"
            f"Bias stimato gyro °/s (X Y Z): {self.vector(cal.get('gyro_mean_dps'))}\n"
            f"σ gyro °/s: {self.vector(cal.get('gyro_std_dps'))} · limite {self.limit(cal.get('gyro_std_limit_dps'))}\n"
            f"Δ medie metà °/s: {self.vector(cal.get('gyro_halves_mean_shift_dps'))} · limite {self.limit(cal.get('gyro_mean_shift_limit_dps'))}\n"
            f"Report: {self.frequency(rate.get('report_hz'))} · campioni: {self.frequency(rate.get('sample_hz'))}\n"
            f"Età: {f'{age:.2f} s' if age is not None else '—'} · {'fresco' if state['fresh'] else 'obsoleto/assente'}")
        pose = state["latest_pose"]
        angles = pose["angles_deg"] if pose else {}
        self.labels[2].configure(text="Yaw / pitch / roll (°):\n" +
            (self.vector([angles[k] for k in ("yaw", "pitch", "roll")], 1) if pose else "— / — / —") +
            "\nQuaternion wxyz: " + self.vector(pose["quaternion_wxyz"] if pose else None) +
            "\nGyro rad/s: " + self.vector(data.get("gyro_rad_s") if state["fresh"] else None) +
            "\nAccel m/s²: " + self.vector(data.get("accel_mps2") if state["fresh"] else None) +
            "\nFlag display RAW: " + str(sensor.get("display_flag_raw", "—")) + " (non è potenza)")
        self.draw_sketch(pose)
        self.draw_history(self.backend.state.history("samples", MAX_HISTORY))
        logs = state["logs"]
        if logs != self.last_logs:
            at_bottom = self.console.yview()[1] >= 0.98
            self.console.configure(state="normal")
            self.console.delete("1.0", "end")
            self.console.insert("end", "\n".join(item["text"] for item in logs))
            self.console.configure(state="disabled")
            if at_bottom:
                self.console.see("end")
            self.last_logs = logs
        self.root.after(100, self.update)

    def draw_sketch(self, pose):
        canvas = self.sketch
        canvas.delete("all")
        w, h = canvas.winfo_width(), canvas.winfo_height()
        canvas.create_text(12, 12, anchor="nw", text="Orientamento illustrativo · non rendering VR", fill="#b8c9dd")
        canvas.create_text(12, h-28, anchor="sw", text=BODY_AXES_LEGEND, fill="#b8c9dd", width=max(1, w-24))
        canvas.create_text(12, h-10, anchor="sw", text="+X destra · +Y alto · −Z avanti", fill="#b8c9dd")
        if not pose:
            canvas.create_text(w/2, h/2, text="Pose assente / non valida", fill="#f4bc6b")
            return
        qw, qx, qy, qz = pose["quaternion_wxyz"]
        norm = math.sqrt(qw*qw+qx*qx+qy*qy+qz*qz)
        qw, qx, qy, qz = (v/norm for v in (qw, qx, qy, qz))
        matrix = ((1-2*(qy*qy+qz*qz), 2*(qx*qy-qz*qw), 2*(qx*qz+qy*qw)),
                  (2*(qx*qy+qz*qw), 1-2*(qx*qx+qz*qz), 2*(qy*qz-qx*qw)),
                  (2*(qx*qz-qy*qw), 2*(qy*qz+qx*qw), 1-2*(qx*qx+qy*qy)))
        scale = min(w/4, h/3)
        def project(point):
            x, y, z = [sum(matrix[i][j]*point[j] for j in range(3)) for i in range(3)]
            return w/2+scale*(x+0.4*z), h/2-scale*(y-0.22*z)
        vertices = [(-.8,-.4,-.5), (.8,-.4,-.5), (.8,.4,-.5), (-.8,.4,-.5),
                    (-.8,-.4,.4), (.8,-.4,.4), (.8,.4,.4), (-.8,.4,.4)]
        for a, b in ((0,1),(1,2),(2,3),(3,0),(4,5),(5,6),(6,7),(7,4),(0,4),(1,5),(2,6),(3,7)):
            canvas.create_line(*project(vertices[a]), *project(vertices[b]), fill="#80d8ff", width=2)
        for point, color, title in (((1.2,0,0), "#ff8787", "+X"), ((0,1.2,0), "#80e5aa", "+Y"), ((0,0,-1.2), "#8caaff", "−Z")):
            tip = project(point)
            canvas.create_line(*project((0,0,0)), *tip, fill=color, arrow="last", width=2)
            canvas.create_text(*tip, text=title, fill=color, anchor="sw")

    def draw_history(self, samples):
        canvas = self.history_canvas
        canvas.delete("all")
        w, h = canvas.winfo_width(), canvas.winfo_height()
        canvas.create_text(12, 12, anchor="nw", text=HISTORY_LEGEND, fill="#b8c9dd")
        canvas.create_text(12, h-10, anchor="sw", text="Destra: yaw − · Alto: pitch + · Roll destra −", fill="#b8c9dd")
        for degrees in (-180, -90, 0, 90, 180):
            y = 38+(180-degrees)/360*max(1, h-70)
            canvas.create_line(30,y,w-10,y,fill="#263a55")
        if len(samples) < 2:
            return
        start, end = samples[0]["received_elapsed_s"], samples[-1]["received_elapsed_s"]
        for key, color in (("yaw", "#ff8787"), ("pitch", "#80e5aa"), ("roll", "#8caaff")):
            previous = None
            previous_angle = None
            for sample in samples:
                if not sample["valid"]:
                    previous = previous_angle = None
                    continue
                angle = sample["data"]["angles_deg"][key]
                point = (30+(sample["received_elapsed_s"]-start)/max(.01,end-start)*max(1,w-40),
                         38+(180-max(-180,min(180,angle)))/360*max(1,h-70))
                if previous and abs(angle-previous_angle) < 180:
                    canvas.create_line(*previous,*point,fill=color,width=2)
                previous, previous_angle = point, angle

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.instruction.set("Chiusura: richiesta Stop, attesa limitata del solo worker posseduto…")
        for button in self.buttons.values():
            button.configure(state="disabled")
        def cleanup():
            try:
                self.backend.shutdown()
            finally:
                try:
                    self.api.close()
                finally:
                    # Even cleanup errors must release the main-thread destroy poll.
                    self.closed.set()
        threading.Thread(target=cleanup, daemon=True).start()

    def run(self):
        self.root.mainloop()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Monitor PSVR Windows, API locale sola lettura")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--headless", action="store_true", help="Solo API; nessun accesso hardware automatico")
    parser.add_argument("--headless-seconds", type=float, default=None, help="Durata smoke test senza GUI")
    parser.add_argument("--start-imu", action="store_true", help="Avvio IMU esplicito nella GUI; default solo rilevamento")
    parser.add_argument("--imu-duration-ms", type=int, default=0, help="0 = indefinito; durata massima 180000 ms")
    parser.add_argument("--stationary-test", action="store_true", help="Test statico: --start-imu e durata >= config calibrazione + startup + 30000 ms misura + 1000 ms margine (default 39200)")
    args = parser.parse_args(argv)
    try:
        validate_imu_options(args.imu_duration_ms, args.stationary_test, args.root)
    except ValueError as exc:
        parser.error(str(exc))
    if (args.stationary_test or args.imu_duration_ms != 0) and not args.start_imu:
        parser.error("--imu-duration-ms / --stationary-test richiedono --start-imu")
    if args.start_imu and args.headless:
        parser.error("--start-imu richiede GUI; --headless resta senza accesso hardware")
    if args.headless_seconds is not None and (not _number(args.headless_seconds) or args.headless_seconds <= 0 or not args.headless):
        parser.error("--headless-seconds richiede --headless e durata positiva finita")
    backend = Backend(args.root)
    api = MonitorAPI(backend.state, args.root / "logs" / "monitor_api.json")
    try:
        api.start()
        if args.headless:
            print("PSVR monitor API: " + api.base_url, flush=True)
            deadline = time.monotonic() + args.headless_seconds if args.headless_seconds else None
            while deadline is None or time.monotonic() < deadline:
                backend.pump()
                time.sleep(.05)
        else:
            MonitorWindow(backend, api, start_imu=args.start_imu,
                          imu_duration_ms=args.imu_duration_ms, stationary_test=args.stationary_test).run()
    except KeyboardInterrupt:
        pass
    except (OSError, RuntimeError) as exc:
        print("PSVR monitor: " + str(exc), file=sys.stderr)
        if not args.headless:
            from tkinter import messagebox
            messagebox.showerror("PSVR monitor — avvio non riuscito", str(exc))
        return 1
    finally:
        backend.shutdown()
        api.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
