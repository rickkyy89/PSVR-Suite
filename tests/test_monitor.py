"""Software-only PSVR monitor tests: no Tk windows and no native hardware calls."""
import importlib.util
import io
from contextlib import redirect_stderr, redirect_stdout
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("psvr_monitor", ROOT / "tools/psvr_monitor/monitor.py")
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)


def imu():
    return {"event": "imu", "schema_version": 1, "host_elapsed_s": 2.0,
        "device_elapsed_s": 1.9, "tick24": 100, "state": "tracking", "valid": True,
        "angles_deg": {"yaw": -10., "pitch": 2., "roll": -3.},
        "quaternion_wxyz": [1., 0., 0., 0.], "gyro_rad_s": [0., 0., 0.],
        "corrected_gyro_rad_s": [0., 0., 0.], "accel_mps2": [0., 9.81, 0.],
        "recenter_count": 0, "sensor": {"worn": True, "status_raw": 1,
        "ir_raw": 2, "display_flag_raw": True}, "calibration": {"samples": 400,
        "target_ms": 2000, "gyro_mean_dps": [0., 0., 0.],
        "gyro_std_dps": [.01, .01, .01], "accel_std_mps2": [.01, .01, .01]},
        "rate": {"reports": 200, "report_hz": 100., "samples": 400, "sample_hz": 200.}}


def line(data):
    return "2026-10-03 12:00:00 [DATA] " + json.dumps(data)


def attach_ready_capture(backend, proc):
    """Legacy command-unit tests explicitly model an already-ready owned session."""
    backend.worker = proc
    backend.capture = monitor.CaptureSession(proc, backend.generation)
    backend.capture.ready.set()
    backend.state.running = True
    backend.state.worker_pid = proc.pid
    backend.state.native_ready = True


class FakePipe:
    def __init__(self):
        self.lines = queue.Queue()
        self.closed = False

    def __iter__(self):
        while True:
            value = self.lines.get()
            if value is None:
                return
            yield value

    def close(self):
        self.closed = True


class FakeChild:
    """A blocking fake Popen, not a process. No native executable is invoked."""
    def __init__(self, pid=6001):
        self.pid = pid
        self.stdout = FakePipe()
        self.exit_event = threading.Event()
        self.lock = threading.Lock()
        self.returncode = None
        self.fast_timeout = False
        self.terminate_calls = 0
        self.kill_calls = 0

    def feed(self, data):
        self.stdout.lines.put(line(data) + "\n")

    def finish(self, code=0):
        with self.lock:
            if self.returncode is None:
                self.returncode = code
                self.exit_event.set()
                self.stdout.lines.put(None)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if timeout is not None and self.fast_timeout and not self.exit_event.is_set():
            raise subprocess.TimeoutExpired("fake child", timeout)
        if not self.exit_event.wait(timeout):
            raise subprocess.TimeoutExpired("fake child", timeout)
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        self.finish(-15)

    def kill(self):
        self.kill_calls += 1
        self.finish(-9)


class ParsingTests(unittest.TestCase):
    def test_parse_data_amid_logs(self):
        self.assertIsNone(monitor.parse_data_line("12:00:00 [INFO] diagnostic log"))
        self.assertIsNone(monitor.parse_data_line("[INFO] ordinary diagnostic log"))
        self.assertEqual(monitor.parse_data_line(line(imu())), imu())

    def test_reject_bad_data(self):
        cases = []
        for key, value in (("quaternion_wxyz", [1, 0, 0]), ("quaternion_wxyz", [0, 0, 0, 0]),
            ("gyro_rad_s", [1, 2, float("nan")]), ("accel_mps2", [0, True, 0]),
            ("host_elapsed_s", float("inf")), ("schema_version", True),
            ("valid", 1), ("tick24", 2**24), ("state", "invented"),
            ("state", []), ("event", {}), ("host_elapsed_s", 10**1000)):
            data = imu()
            data[key] = value
            cases.append(data)
        data = imu()
        data["angles_deg"]["yaw"] = "0"
        cases.append(data)
        data = imu()
        data["rate"]["sample_hz"] = -1
        cases.append(data)
        data = imu()
        data["extension"] = {"value": float("inf")}
        cases.append(data)
        for data in cases:
            with self.subTest(data=data), self.assertRaises(monitor.TelemetryError):
                monitor.parse_data_line(line(data))
        for payload in ("[]", "{}", "{oops", "null"):
            with self.subTest(payload=payload), self.assertRaises(monitor.TelemetryError):
                monitor.parse_data_line("timestamp [DATA] " + payload)

    def test_floating_point_overflow(self):
        payload = line(imu()).replace('"host_elapsed_s": 2.0', '"host_elapsed_s": 1e999')
        with self.assertRaises(monitor.TelemetryError):
            monitor.parse_data_line(payload)

    def test_actual_calibration_elapsed_validation(self):
        data = imu()
        data["calibration"]["elapsed_ms"] = 750.25
        self.assertEqual(monitor.parse_data_line(line(data))["calibration"]["elapsed_ms"], 750.25)
        for value in (-1, float("nan"), float("inf"), "750", True):
            with self.subTest(value=value), self.assertRaises(monitor.TelemetryError):
                data["calibration"]["elapsed_ms"] = value
                monitor.parse_data_line(line(data))

    def test_optional_calibration_quality_fields(self):
        data = imu()
        data["calibration"].update({"gyro_halves_mean_shift_dps": [.01, -.02, .03],
            "gyro_std_limit_dps": .75, "gyro_mean_shift_limit_dps": .05})
        self.assertEqual(monitor.parse_data_line(line(data)), data)
        for key, value in (("gyro_halves_mean_shift_dps", [1, 2]),
            ("gyro_halves_mean_shift_dps", [0, float("inf"), 0]),
            ("gyro_std_limit_dps", -1), ("gyro_mean_shift_limit_dps", True)):
            with self.subTest(key=key, value=value), self.assertRaises(monitor.TelemetryError):
                invalid = imu()
                invalid["calibration"][key] = value
                monitor.parse_data_line(line(invalid))

    def test_sensor_raw_integer_boundaries(self):
        for key, maximum in (("status_raw", 255), ("ir_raw", 65535)):
            for value in (0, maximum):
                with self.subTest(key=key, value=value):
                    data = imu()
                    data["sensor"][key] = value
                    self.assertEqual(monitor.parse_data_line(line(data))["sensor"][key], value)
            for value in (-1, maximum + 1, True, 1.0):
                with self.subTest(key=key, value=value), self.assertRaises(monitor.TelemetryError):
                    data = imu()
                    data["sensor"][key] = value
                    monitor.parse_data_line(line(data))

    def test_control_raw_byte_boundaries_not_boolean_flags(self):
        for key in ("vr_active", "status_mask"):
            for value in (0, 255, -1, 256, True, 1.0):
                data = {"event": "control_status", "schema_version": 1, "power_on": True,
                        "worn": False, "mode": "transition", "vr_active": 1, "status_mask": 0}
                data[key] = value
                with self.subTest(key=key, value=value):
                    if type(value) is int and 0 <= value <= 255:
                        self.assertEqual(monitor.parse_data_line(line(data))[key], value)
                    else:
                        with self.assertRaises(monitor.TelemetryError):
                            monitor.parse_data_line(line(data))

    def test_standard_deviations_nonnegative_but_means_and_shift_signed(self):
        for key in ("gyro_std_dps", "accel_std_mps2"):
            data = imu()
            data["calibration"][key] = [0, 0, 0]
            self.assertEqual(monitor.parse_data_line(line(data)), data)
            for axis in range(3):
                with self.subTest(key=key, axis=axis), self.assertRaises(monitor.TelemetryError):
                    data = imu()
                    data["calibration"][key][axis] = -.000001
                    monitor.parse_data_line(line(data))
        data = imu()
        data["calibration"].update({"gyro_mean_dps": [-.5, .1, -.2],
            "gyro_halves_mean_shift_dps": [-.01, .02, -.03], "gyro_std_dps": [1., 2., 3.],
            "gyro_std_limit_dps": .75})  # Actual STD can exceed limit: diagnostic, not invalid JSON.
        self.assertEqual(monitor.parse_data_line(line(data)), data)

    def test_calibration_time_and_limit_boundaries(self):
        for target in (500, 10000):
            for elapsed in (0, target, target + 20):
                data = imu()
                data["calibration"].update({"target_ms": target, "elapsed_ms": elapsed})
                self.assertEqual(monitor.parse_data_line(line(data)), data)
            data["calibration"]["elapsed_ms"] = target + 20.001
            with self.assertRaises(monitor.TelemetryError):
                monitor.parse_data_line(line(data))
        for target in (0, 499, 10001):
            data = imu()
            data["calibration"]["target_ms"] = target
            with self.assertRaises(monitor.TelemetryError):
                monitor.parse_data_line(line(data))
        for key, low, high in (("gyro_std_limit_dps", .01, 5), ("gyro_mean_shift_limit_dps", .001, 1)):
            for value in (low, high, low / 2, high + .001, 0, True, float("inf")):
                data = imu()
                data["calibration"][key] = value
                with self.subTest(key=key, value=value):
                    if type(value) in (int, float) and low <= value <= high:
                        self.assertEqual(monitor.parse_data_line(line(data)), data)
                    else:
                        with self.assertRaises(monitor.TelemetryError):
                            monitor.parse_data_line(line(data))

    def test_other_events(self):
        data = {"event": "imu_state", "schema_version": 1, "state": "warming", "valid": False}
        self.assertEqual(monitor.parse_data_line(line(data)), data)
        data = {"event": "control_status", "schema_version": 1, "power_on": False,
                "worn": True, "mode": "transition", "vr_active": 1, "status_mask": 4}
        self.assertEqual(monitor.parse_data_line(line(data)), data)

    def test_oversize_and_nested_data_rejected(self):
        data = imu()
        data["extension"] = "x" * 65536
        with self.assertRaises(monitor.TelemetryError):
            monitor.parse_data_line(line(data))
        data = imu()
        child = []
        data["extension"] = child
        for _ in range(40):
            nested = []
            child.append(nested)
            child = nested
        with self.assertRaises(monitor.TelemetryError):
            monitor.parse_data_line(line(data))


class StateTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.
        self.state = monitor.MonitorState(clock=lambda: self.now)
        self.state.running = True
        self.state.accept(imu())

    def test_freshness_and_snapshot_isolation(self):
        snapshot = self.state.snapshot()
        self.assertTrue(snapshot["pose_valid"])
        self.assertIsNone(snapshot["control_status"])
        snapshot["latest_data"]["angles_deg"]["yaw"] = 999
        self.assertEqual(self.state.snapshot()["latest_data"]["angles_deg"]["yaw"], -10)
        self.now += 2
        stale = self.state.snapshot()
        self.assertFalse(stale["fresh"])
        self.assertIsNone(stale["latest_pose"])
        self.assertEqual(stale["age_s"], 2.)

    def test_queue_delay_does_not_make_old_data_fresh(self):
        backend = monitor.Backend(state=self.state)
        backend._emit("line", line(imu()), 0)
        self.now += 3
        backend.pump()
        self.assertFalse(self.state.snapshot()["fresh"])
        self.assertEqual(self.state.snapshot()["age_s"], 3)

    def test_invalidate_each_nontracking_state(self):
        for phase in monitor.STATES - {"tracking"}:
            self.state.accept(imu())
            self.state.accept({"event": "imu_state", "state": phase, "valid": False})
            self.assertIsNone(self.state.snapshot()["latest_pose"])
        self.state.accept(imu())
        data = imu()
        data["valid"] = False
        self.state.accept(data)
        self.assertFalse(self.state.snapshot()["pose_valid"])

    def test_eof_error_and_malformed_line_invalidate(self):
        backend = monitor.Backend(state=self.state)
        backend.messages.put(("eof", 0, 0))
        backend.pump()
        self.assertFalse(self.state.running)
        self.assertIsNone(self.state.pose)
        self.assertEqual(self.state.counters["eof"], 1)
        self.state.running = True
        self.state.accept(imu())
        backend.messages.put(("error", "reader failed", 0))
        backend.pump()
        self.assertIsNone(self.state.pose)
        self.assertEqual(self.state.last_error, "reader failed")
        self.state.accept(imu())
        backend.messages.put(("line", "timestamp [DATA] []", 0))
        backend.pump()
        self.assertIsNone(self.state.pose)
        self.assertEqual(self.state.counters["parse_errors"], 1)

    def test_old_generation_ignored(self):
        backend = monitor.Backend(state=self.state)
        backend.generation = 1
        backend.messages.put(("eof", 1, 0))
        backend.pump()
        self.assertTrue(self.state.running)

    def test_stream_eof_invalidates_before_process_exit(self):
        backend = monitor.Backend(state=self.state)
        backend.messages.put(("stream_end", None, 0))
        backend.pump()
        self.assertTrue(self.state.running)  # USB still owned until actual process exit
        self.assertIsNone(self.state.snapshot()["latest_pose"])

    def test_precise_calibration_rejection_survives_eof_exit7(self):
        reason = "gyro first/second-half mean shift 0.648035 dps exceeds limit 0.05 dps"
        backend = monitor.Backend(state=self.state)
        self.state.pose_gate = "calibration"
        backend.messages.put(("line", line({"event": "imu_state", "schema_version": 1,
            "state": "calibration-failed", "valid": False, "reason": reason}), 0))
        backend.pump()
        self.assertEqual(self.state.snapshot()["last_error"], reason)
        backend.messages.put(("stream_end", None, 0))
        backend.messages.put(("eof", 7, 0))
        backend.pump()
        state = self.state.snapshot()
        self.assertEqual(state["last_error"], reason)
        self.assertEqual(state["last_calibration_error"], reason)
        self.assertEqual(state["phase"], "calibration-failed")
        self.assertFalse(state["running"])
        self.assertIsNone(state["latest_pose"])
        self.assertIsNone(state["pending_transition"])

    def test_eof_clears_each_pending_transition_and_profile(self):
        for pending in ("stop", "calibration"):
            with self.subTest(pending=pending):
                state = monitor.MonitorState()
                state.running = True
                state.pose_gate = pending
                state.measurement_profile = "stationary"
                backend = monitor.Backend(state=state)
                backend.messages.put(("eof", 0, 0))
                backend.pump()
                snapshot = state.snapshot()
                self.assertFalse(snapshot["running"])
                self.assertEqual(snapshot["phase"], "stopped")
                self.assertIsNone(snapshot["pending_transition"])
                self.assertIsNone(snapshot["measurement_profile"])
                self.assertIsNone(snapshot["latest_pose"])

    def test_pending_stop_or_calibration_cannot_revive_old_pose(self):
        self.state.pose_gate = "stop"
        self.state.accept(imu())
        self.assertFalse(self.state.snapshot()["pose_valid"])
        self.state.pose_gate = "calibration"
        self.state.accept(imu())
        self.assertFalse(self.state.snapshot()["pose_valid"])
        self.state.accept({"event": "imu_state", "state": "calibrating", "valid": False})
        self.state.accept(imu())
        self.assertTrue(self.state.snapshot()["pose_valid"])

    def test_usb_and_panel_are_independent(self):
        for interface, service in ((4, "HidUsb"), (5, "WinUSB")):
            data = {"event": "usb_device", "schema_version": 1,
                "instance_id": f"USB\\VID_054C&PID_09AF&MI_0{interface}\\abc",
                "name": "PSVR", "service": service, "problem_code": 0, "is_psvr": True}
            self.state.accept(monitor.parse_data_line(line(data)))
        nodes = self.state.snapshot()["usb_devices"]
        self.assertEqual([node["interface"] for node in nodes], [4, 5])
        self.assertTrue(all(node["binding_ok"] for node in nodes))
        self.assertIsNone(self.state.snapshot()["control_status"])
        panel = {"event": "control_status", "schema_version": 1, "power_on": False,
                 "worn": False, "mode": "vr", "vr_active": 1, "status_mask": 8}
        self.state.accept(panel)
        self.assertEqual(self.state.snapshot()["control_status"], panel)

    def test_bounded_logs_and_samples(self):
        for i in range(300):
            self.state.log(str(i))
            self.state.accept(imu())
        self.assertEqual(len(self.state.logs), 200)
        self.assertEqual(len(self.state.samples), 200)
        self.assertEqual(self.state.history("logs", 0), [])


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.discovery = Path(self.temp.name) / "monitor_api.json"
        self.state = monitor.MonitorState()
        self.api = monitor.MonitorAPI(self.state, self.discovery)
        self.url = self.api.start()

    def tearDown(self):
        self.api.close()
        self.temp.cleanup()

    def request(self, path, method="GET", headers=None):
        request = Request(self.url + path, method=method, headers=headers or {})
        try:
            response = urlopen(request, timeout=2)
        except HTTPError as exc:
            response = exc
        with response:
            body = response.read()
            return response.status, json.loads(body) if body else {}, dict(response.headers)

    def test_health_state_and_discovery(self):
        metadata = json.loads(self.discovery.read_text())
        self.assertEqual(metadata["base_url"], self.url)
        self.assertEqual(self.api.server.server_address[0], "127.0.0.1")
        self.assertEqual(self.request("/health")[0], 200)
        status, state, headers = self.request("/state")
        self.assertEqual(status, 200)
        self.assertFalse(state["running"])
        self.assertIsNone(state["latest_pose"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_history_limits_and_errors(self):
        for i in range(300):
            self.state.log(str(i))
            self.state.accept(imu())
        for endpoint in ("logs", "samples"):
            for limit, expected in ((9999, 200), (2, 2), (0, 0)):
                status, result, _ = self.request(f"/{endpoint}?limit={limit}")
                self.assertEqual(status, 200)
                self.assertEqual(len(result["items"]), expected)
            for value in ("-1", "bad", "", "1.5"):
                self.assertEqual(self.request(f"/{endpoint}?limit={value}")[0], 400)
        self.assertEqual(self.request("/missing")[0], 404)

    def test_write_and_browser_access_rejected(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "TRACE", "CONNECT"):
            status, _, headers = self.request("/state", method)
            self.assertEqual(status, 405)
            self.assertEqual(headers["Allow"], "GET")
        self.assertEqual(self.request("/state", headers={"Origin": "https://example.com"})[0], 403)
        self.assertEqual(self.request("/state", headers={"Host": "evil.example"})[0], 403)

    def test_discovery_cleanup_only_owner(self):
        self.discovery.write_text(json.dumps({"pid": -1, "base_url": self.url}))
        self.api.close()
        self.assertTrue(self.discovery.exists())

    def test_discovery_collision_excludes_second_instance_then_releases(self):
        original = self.discovery.read_text()
        second = monitor.MonitorAPI(monitor.MonitorState(), self.discovery)
        try:
            with self.assertRaisesRegex(RuntimeError, "Monitor già attivo"):
                second.start()
            second.close()
            self.assertEqual(self.discovery.read_text(), original)
        finally:
            second.close()
        self.api.close()
        # OS lock releases; stale metadata is safe to replace after a crash.
        self.discovery.write_text(original)
        replacement = monitor.MonitorAPI(monitor.MonitorState(), self.discovery)
        try:
            replacement.start()
            self.assertEqual(json.loads(self.discovery.read_text())["base_url"], replacement.base_url)
        finally:
            replacement.close()
        self.assertFalse(self.discovery.exists())

    def test_api_responsive_during_busy_and_concurrent_updates(self):
        self.state.busy = True
        errors = []
        def update():
            try:
                for _ in range(100):
                    self.state.accept(imu())
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=update)
        thread.start()
        for _ in range(10):
            self.assertEqual(self.request("/state")[0], 200)
        thread.join()
        self.assertFalse(errors)


class WorkerTests(unittest.TestCase):
    def settle(self, backend):
        for thread in backend.threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        backend.pump()

    def test_reader_only_queues_no_tk_calls(self):
        proc = Mock(pid=1234)
        proc.stdout = io.StringIO(line(imu()) + "\n")
        proc.poll.return_value = None
        proc.wait.return_value = 0
        popen = Mock(return_value=proc)
        backend = monitor.Backend(popen=popen)
        accept_threads = []
        original = backend.state.accept
        def accept(data, received_at=None):
            accept_threads.append(threading.get_ident())
            original(data, received_at)
        backend.state.accept = accept
        self.assertTrue(backend.start_imu())
        for thread in backend.threads:
            thread.join(timeout=2)
        self.assertEqual(accept_threads, [])
        backend.pump()
        self.assertEqual(accept_threads, [threading.get_ident()])
        self.assertFalse(backend.state.running)
        args = popen.call_args.args[0]
        self.assertIn("--json-stream", args)
        self.assertEqual(args[args.index("--duration-ms") + 1], "0")
        self.assertEqual(args[args.index("--output-interval-ms") + 1], "100")

    @patch.object(monitor, "stationary_minimum_ms", return_value=39200)
    def test_finite_stationary_command_and_profile_lifecycle(self, _minimum):
        proc = Mock(pid=1234)
        proc.stdout = io.StringIO(line(imu()) + "\n")
        proc.poll.return_value = None
        proc.wait.return_value = 0
        backend = monitor.Backend(popen=Mock(return_value=proc))
        self.assertTrue(backend.start_imu(duration_ms=42000, stationary_test=True))
        state = backend.state.snapshot()
        self.assertEqual(state["measurement_profile"], "stationary")
        self.assertEqual(state["imu_duration_ms"], 42000)
        self.assertIn("Test statico su tavolo", state["action"])
        command = backend.popen.call_args.args[0]
        self.assertEqual(command[command.index("--duration-ms") + 1], "42000")
        self.assertIn("--stationary-test", command)
        self.assertIn("--json-stream", command)
        self.settle(backend)
        self.assertFalse(backend.state.running)
        self.assertIsNone(backend.state.measurement_profile)
        self.assertEqual(backend.state.action, "IMU arrestata")

    @patch.object(monitor, "stationary_minimum_ms", return_value=39200)
    def test_invalid_duration_or_profile_never_spawns(self, _minimum):
        backend = monitor.Backend(popen=Mock())
        for duration, profile in ((-1, False), (180001, False), (True, False),
                                  (42000.0, True), (0, True), (38200, True), (42000, 1)):
            with self.subTest(duration=duration, profile=profile), self.assertRaises(ValueError):
                backend.start_imu(duration_ms=duration, stationary_test=profile)
        backend.popen.assert_not_called()
        self.assertFalse(backend.state.busy)
        for duration, profile in ((0, False), (180000, False), (39200, True), (180000, True)):
            monitor.validate_imu_options(duration, profile)

    def test_measurement_rejects_reference_and_bias_reset_but_allows_stop(self):
        proc = Mock(pid=222)
        proc.poll.return_value = None
        proc.wait.return_value = 0
        backend = monitor.Backend(run=Mock(return_value=Mock(returncode=0, stdout="")))
        attach_ready_capture(backend, proc)
        backend.state.running = True
        backend.state.measurement_profile = "stationary"
        self.assertFalse(backend.signal_worker("--recenter"))
        self.assertFalse(backend.signal_worker("--recalibrate"))
        backend.run.assert_not_called()
        self.assertTrue(backend.signal_worker("--stop-imu"))
        self.settle(backend)
        self.assertIn("--stop-imu", backend.run.call_args.args[0])

    def test_one_worker_rule(self):
        backend = monitor.Backend(popen=Mock())
        backend.state.running = True
        for flag in ("--enumerate-only", "--power-on", "--power-off", "--vr-mode", "--cinema-mode"):
            self.assertFalse(backend.one_shot(flag))
        self.assertFalse(backend.start_imu())
        backend.popen.assert_not_called()
        with self.assertRaises(ValueError):
            backend.one_shot("--unknown")

    def test_default_refresh_is_read_only(self):
        proc = Mock(returncode=0)
        proc.communicate.return_value = ("log\n", None)
        proc.poll.return_value = 0
        backend = monitor.Backend(popen=Mock(return_value=proc))
        self.assertTrue(backend.one_shot("--enumerate-only"))
        self.settle(backend)
        args = backend.popen.call_args.args[0]
        self.assertIn("--enumerate-only", args)
        self.assertNotIn("--power-on", args)
        self.assertFalse(backend.state.busy)

    def test_one_shot_timeout_does_not_terminate(self):
        proc = Mock(pid=444, returncode=0)
        proc.communicate.side_effect = [subprocess.TimeoutExpired("mock", 15), ("done", None)]
        proc.poll.return_value = 0
        backend = monitor.Backend(popen=Mock(return_value=proc))
        self.assertTrue(backend.one_shot("--power-on"))
        self.settle(backend)
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()
        self.assertIn("Timeout", backend.state.last_error)

    def test_exit6_is_unverified_not_transport_error(self):
        proc = Mock(pid=555, returncode=6)
        proc.communicate.return_value = ("[INFO] redundant power-on; no new F0", None)
        proc.poll.return_value = 6
        backend = monitor.Backend(popen=Mock(return_value=proc))
        self.assertTrue(backend.one_shot("--power-on"))
        self.settle(backend)
        state = backend.state.snapshot()
        self.assertIsNone(state["last_error"])
        self.assertEqual(state["counters"]["errors"], 0)
        self.assertFalse(state["last_control_result"]["state_verified"])
        self.assertEqual(state["last_control_result"]["exit_code"], 6)
        self.assertIn("Stato non verificato", state["action"])
        self.assertEqual(state["logs"][-1]["level"], "warning")
        self.assertIsNone(state["control_status"])

    def test_imu_exit_zero_is_stopped_not_profile_pass(self):
        backend = monitor.Backend()
        backend.state.running = True
        backend.state.accept(imu())
        backend.messages.put(("eof", 0, 0))
        backend.pump()
        state = backend.state.snapshot()
        self.assertEqual(state["phase"], "stopped")
        self.assertEqual(state["action"], "IMU arrestata")
        self.assertIsNone(state["latest_pose"])
        self.assertNotIn("pass", state["action"].lower())

    def test_stop_graceful_no_termination(self):
        proc = Mock(pid=222)
        proc.poll.return_value = None
        proc.wait.return_value = 0
        run = Mock(return_value=Mock(returncode=0, stdout="stop delivered"))
        backend = monitor.Backend(run=run)
        attach_ready_capture(backend, proc)
        backend.state.running = True
        backend.state.worker_pid = proc.pid
        self.assertTrue(backend.signal_worker("--stop-imu"))
        self.settle(backend)
        self.assertIn("--stop-imu", run.call_args.args[0])
        self.assertIn("222", run.call_args.args[0])
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()

    def test_recenter_and_recalibrate_use_own_pid(self):
        for flag in ("--recenter", "--recalibrate"):
            with self.subTest(flag=flag):
                proc = Mock(pid=222)
                proc.poll.return_value = None
                run = Mock(return_value=Mock(returncode=0, stdout="delivered"))
                backend = monitor.Backend(run=run, popen=Mock())
                attach_ready_capture(backend, proc)
                backend.state.running = True
                backend.state.accept(imu())
                self.assertTrue(backend.signal_worker(flag))
                self.settle(backend)
                self.assertIn(flag, run.call_args.args[0])
                self.assertIn("222", run.call_args.args[0])
                backend.popen.assert_not_called()
                if flag == "--recalibrate":
                    self.assertIsNone(backend.state.snapshot()["latest_pose"])

    def test_start_failure_clears_busy_without_fake_data(self):
        backend = monitor.Backend(popen=Mock(side_effect=FileNotFoundError("missing native exe")))
        self.assertFalse(backend.start_imu())
        self.assertFalse(backend.state.busy)
        self.assertFalse(backend.state.running)
        self.assertIsNone(backend.state.pose_gate)
        self.assertIsNone(backend.state.latest_data)
        self.assertIn("missing native exe", backend.state.last_error)

    def test_stop_timeout_uses_only_owned_shutdown_fallback(self):
        proc = FakeChild()
        proc.fast_timeout = True
        backend = monitor.Backend(popen=Mock(return_value=proc), run=Mock(return_value=Mock(returncode=0, stdout="")))
        backend.start_imu()
        backend.messages.put(("worker_line", line(imu()), backend.generation))
        backend.pump()
        self.assertTrue(backend.signal_worker("--stop-imu"))
        self.settle(backend)
        self.assertFalse(backend.state.running)
        self.assertIsNone(backend.state.snapshot()["latest_pose"])
        self.assertIsNone(backend.state.pose_gate)
        self.assertEqual(proc.terminate_calls, 1)
        self.assertEqual(proc.kill_calls, 0)
        self.assertIn("Stop non confermato", backend.state.last_error)
        backend.run.assert_called_once()

    def test_shutdown_bounded_fallback_only_owned_handle(self):
        proc = Mock(pid=333)
        proc.poll.return_value = None
        proc.wait.side_effect = [subprocess.TimeoutExpired("mock", 3), 0]
        backend = monitor.Backend(run=Mock(return_value=Mock(returncode=0, stdout="")))
        attach_ready_capture(backend, proc)
        backend.state.running = True
        backend.shutdown()
        self.assertIn("333", backend.run.call_args.args[0])
        proc.terminate.assert_called_once()
        proc.kill.assert_not_called()
        self.assertFalse(backend.state.running)
        self.assertIsNone(backend.state.pose_gate)

    def test_headless_does_not_access_hardware_or_tk(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(monitor, "Backend", wraps=monitor.Backend) as factory, \
                 patch.object(monitor, "MonitorWindow", side_effect=AssertionError("Tk called")), \
                 patch.object(monitor.subprocess, "Popen", side_effect=AssertionError("hardware called")):
                self.assertEqual(monitor.main(["--root", directory, "--headless", "--headless-seconds", ".05"]), 0)
            self.assertFalse((Path(directory) / "logs" / "monitor_api.json").exists())


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.owned = []
        self.thread_errors = []
        self.old_excepthook = threading.excepthook
        threading.excepthook = lambda args: self.thread_errors.append(args.exc_value)

    def tearDown(self):
        try:
            for backend, child in self.owned:
                child.finish()
            for backend, child in self.owned:
                for thread in backend.threads:
                    thread.join(timeout=2)
                    self.assertFalse(thread.is_alive())
                backend.pump()
            self.assertFalse(self.thread_errors, self.thread_errors)
        finally:
            threading.excepthook = self.old_excepthook

    def make_backend(self, timeout=1.):
        child = FakeChild()
        def native_signal(command, **_):
            if "--stop-imu" in command:
                child.finish()
            return subprocess.CompletedProcess(command, 0, stdout="")
        backend = monitor.Backend(popen=Mock(return_value=child), run=Mock(side_effect=native_signal))
        backend.native_ready_timeout_s = timeout  # Short deterministic fake deadlines only.
        self.owned.append((backend, child))
        self.assertTrue(backend.start_imu())
        return backend, child

    def spin(self, backend, predicate):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            backend.pump()
            if predicate():
                backend.pump()
                return
            time.sleep(.005)
        self.fail("fake worker condition did not complete")

    @staticmethod
    def started():
        return {"event": "imu_state", "schema_version": 1, "state": "warming",
                "valid": False, "reason": "capture-started"}

    def test_delayed_readiness_immediate_stop_is_one_pending_owned_request(self):
        backend, child = self.make_backend()
        self.assertFalse(backend.state.snapshot()["native_ready"])
        self.assertEqual(backend.state.snapshot()["startup_state"], "waiting-native")
        self.assertFalse(backend.signal_worker("--recenter"))
        self.assertFalse(backend.signal_worker("--recalibrate"))
        self.assertTrue(backend.signal_worker("--stop-imu"))
        self.assertFalse(backend.signal_worker("--stop-imu"))
        snapshot = backend.state.snapshot()
        self.assertTrue(snapshot["pending_stop"])
        self.assertEqual(snapshot["stop_status"], "waiting-ready")
        backend.run.assert_not_called()
        child.feed(self.started())
        self.spin(backend, lambda: backend.capture.stop_done.is_set() and not backend.state.running)
        backend.run.assert_called_once()
        command = backend.run.call_args.args[0]
        self.assertIn("--stop-imu", command)
        self.assertIn(str(child.pid), command)
        self.assertEqual(child.terminate_calls, 0)
        self.assertEqual(child.kill_calls, 0)
        snapshot = backend.state.snapshot()
        self.assertEqual(snapshot["startup_state"], "idle")
        self.assertFalse(snapshot["native_ready"])
        self.assertFalse(snapshot["pending_stop"])
        self.assertIsNone(snapshot["pending_transition"])
        self.assertIsNone(snapshot["last_error"])

    def test_exit_before_ready_cancels_pending_stop_without_any_ipc(self):
        backend, child = self.make_backend()
        self.assertTrue(backend.signal_worker("--stop-imu"))
        child.finish()
        self.spin(backend, lambda: backend.capture.stop_done.is_set() and not backend.state.running)
        backend.run.assert_not_called()
        self.assertEqual(child.terminate_calls, 0)
        self.assertFalse(backend.state.snapshot()["pending_stop"])
        self.assertIsNone(backend.state.pose_gate)

    def test_native_calibration_failure_before_ready_keeps_precise_reason(self):
        backend, child = self.make_backend()
        reason = "gyro mean changes between calibration halves"
        self.assertTrue(backend.signal_worker("--stop-imu"))
        child.feed({"event": "imu_state", "schema_version": 1, "state": "calibration-failed",
                    "valid": False, "reason": reason})
        child.finish(7)
        self.spin(backend, lambda: backend.capture.stop_done.is_set() and not backend.state.running)
        backend.run.assert_not_called()
        self.assertEqual(backend.state.last_error, reason)
        self.assertEqual(backend.state.phase, "calibration-failed")
        self.assertIsNone(backend.state.pose_gate)

    def test_never_ready_pending_stop_falls_back_only_on_owned_handle(self):
        backend, child = self.make_backend(timeout=.08)
        self.assertTrue(backend.signal_worker("--stop-imu"))
        self.spin(backend, lambda: backend.capture.stop_done.is_set() and not backend.state.running)
        backend.run.assert_not_called()
        self.assertEqual(child.terminate_calls, 1)
        self.assertEqual(child.kill_calls, 0)
        snapshot = backend.state.snapshot()
        self.assertIn("Readiness nativa non ricevuta", snapshot["last_error"])
        self.assertEqual(snapshot["last_error"], snapshot["stop_error"])
        self.assertFalse(snapshot["pending_stop"])
        self.assertIsNone(snapshot["pending_transition"])
        self.assertEqual(snapshot["startup_state"], "idle")

    def test_startup_watchdog_bounds_never_ready_child_even_without_user_stop(self):
        backend, child = self.make_backend(timeout=.08)
        self.spin(backend, lambda: backend.capture.stop_done.is_set() and not backend.state.running)
        backend.run.assert_not_called()
        self.assertEqual(child.terminate_calls, 1)
        self.assertIn("Readiness nativa non ricevuta", backend.state.last_error)

    def test_failed_ready_stop_does_not_leave_indefinite_child_or_pose_gate(self):
        backend, child = self.make_backend()
        child.feed(self.started())
        self.spin(backend, lambda: backend.state.native_ready)
        backend.run = Mock(return_value=subprocess.CompletedProcess([], 3, stdout="named event unavailable"))
        self.assertTrue(backend.signal_worker("--stop-imu"))
        self.spin(backend, lambda: backend.capture.stop_done.is_set() and not backend.state.running)
        backend.run.assert_called_once()
        self.assertEqual(child.terminate_calls, 1)
        self.assertIsNone(backend.state.pose_gate)
        self.assertFalse(backend.state.pending_stop)
        self.assertIn("Segnale Stop fallito: 3", backend.state.last_error)

    def test_never_ready_stubborn_owned_child_has_bounded_kill_fallback(self):
        backend, child = self.make_backend(timeout=.08)
        child.fast_timeout = True
        child.terminate = Mock()  # Owned fake ignores terminate; no real process exists.
        self.assertTrue(backend.signal_worker("--stop-imu"))
        self.spin(backend, lambda: backend.capture.stop_done.is_set() and not backend.state.running)
        backend.run.assert_not_called()
        child.terminate.assert_called_once()
        self.assertEqual(child.kill_calls, 1)
        self.assertIsNone(backend.state.pose_gate)
        self.assertIn("Readiness nativa non ricevuta", backend.state.last_error)

    def test_valid_nontracking_imu_implies_ready_calibrate_but_not_recenter(self):
        backend, child = self.make_backend()
        data = imu()
        data["state"], data["valid"] = "calibrating", False
        child.feed(data)
        self.spin(backend, lambda: backend.state.native_ready)
        self.assertEqual(backend.state.readiness_reason, "imu")
        self.assertFalse(backend.signal_worker("--recenter"))
        self.assertTrue(backend.signal_worker("--recalibrate"))
        self.spin(backend, lambda: not backend.state.busy)
        child.feed(data)  # Native calibration acknowledgement clears the bias-reset gate.
        child.feed(imu())
        self.spin(backend, lambda: backend.state.snapshot()["pose_valid"])
        self.assertTrue(backend.signal_worker("--recenter"))
        self.spin(backend, lambda: not backend.state.busy)
        self.assertTrue(backend.signal_worker("--stop-imu"))
        self.spin(backend, lambda: not backend.state.running)
        flags = [next(arg for arg in call.args[0] if arg in {"--stop-imu", "--recenter", "--recalibrate"})
                 for call in backend.run.call_args_list]
        self.assertEqual(flags, ["--recalibrate", "--recenter", "--stop-imu"])

    def test_unrelated_invalid_or_helper_data_cannot_confirm_readiness(self):
        backend, child = self.make_backend()
        child.feed({"event": "usb_device", "schema_version": 1, "instance_id": "USB\\VID_054C&PID_09AF",
                    "name": "PSVR", "service": "", "problem_code": 0, "is_psvr": True})
        invalid = self.started()
        invalid["schema_version"] = True
        child.feed(invalid)
        backend.messages.put(("line", line(self.started()), backend.generation))  # helper, not owned capture
        self.spin(backend, lambda: backend.state.counters["parse_errors"] == 1)
        self.assertFalse(backend.state.native_ready)
        self.assertFalse(backend.capture.ready.is_set())
        self.assertFalse(backend.signal_worker("--recalibrate"))
        backend.run.assert_not_called()
        child.finish()

    def test_old_generation_readiness_cannot_unlock_new_owned_worker(self):
        backend, first = self.make_backend()
        first_generation = backend.generation
        old_capture = backend.capture
        first.finish()
        self.spin(backend, lambda: not backend.state.running)
        second = FakeChild(pid=6002)
        self.owned.append((backend, second))
        backend.popen.return_value = second
        self.assertTrue(backend.start_imu())
        backend.messages.put(("worker_line", line(self.started()), first_generation))
        backend.pump()
        self.assertIsNot(backend.capture, old_capture)
        self.assertFalse(backend.capture.ready.is_set())
        self.assertFalse(backend.state.native_ready)
        self.assertFalse(backend.signal_worker("--recalibrate"))
        backend.run.assert_not_called()
        second.finish()

    def test_close_reuses_pending_stop_and_can_receive_readiness_while_closing(self):
        backend, child = self.make_backend()
        self.assertTrue(backend.signal_worker("--stop-imu"))
        errors = []
        def close():
            try:
                backend.shutdown()
            except Exception as exc:
                errors.append(exc)
        closer = threading.Thread(target=close)
        closer.start()
        self.spin(backend, lambda: backend.closing)
        child.feed(self.started())
        self.spin(backend, lambda: backend.capture.stop_done.is_set())
        closer.join(timeout=2)
        self.assertFalse(closer.is_alive())
        self.assertFalse(errors)
        backend.run.assert_called_once()
        self.assertIn(str(child.pid), backend.run.call_args.args[0])
        self.assertEqual(child.terminate_calls, 0)
        self.assertFalse(backend.state.pending_stop)
        self.assertIsNone(backend.state.pose_gate)

    def test_close_before_ready_uses_bounded_fallback_without_blind_ipc(self):
        backend, child = self.make_backend(timeout=.08)
        backend.shutdown()
        backend.pump()
        backend.run.assert_not_called()
        self.assertEqual(child.terminate_calls, 1)
        self.assertFalse(backend.state.running)
        self.assertFalse(backend.state.native_ready)
        self.assertFalse(backend.state.pending_stop)


class WindowLogicTests(unittest.TestCase):
    """Exercise UI formatting/close logic against widgets that never create Tk."""
    def window(self):
        window = monitor.MonitorWindow.__new__(monitor.MonitorWindow)
        window.backend = monitor.Backend(popen=Mock(side_effect=AssertionError("hardware called")))
        window.backend.state.native_ready = True  # Ready-session UI formatting fixtures.
        window.api = Mock()
        window.root = Mock()
        window.closing = False
        window.closed = threading.Event()
        window.buttons = {key: Mock() for key in ("start", "stop", "center", "cal")}
        window.labels = {i: Mock() for i in range(3)}
        window.instruction = Mock()
        window.draw_sketch = Mock()
        window.draw_history = Mock()
        window.console = Mock()
        window.last_logs = []
        return window

    def test_startup_read_only_default_or_one_explicit_capture_no_retry(self):
        window = self.window()
        window.backend.one_shot = Mock()
        window.backend.start_imu = Mock()
        window.startup()
        window.backend.one_shot.assert_called_once_with("--enumerate-only")
        window.backend.start_imu.assert_not_called()
        window.backend.one_shot.reset_mock()
        window.backend.start_imu.return_value = False
        window.startup(start_imu=True, duration_ms=42000, stationary_test=True)
        window.backend.start_imu.assert_called_once_with(duration_ms=42000, stationary_test=True)
        window.backend.one_shot.assert_not_called()
        window.closing = True
        window.startup(start_imu=True)
        self.assertEqual(window.backend.start_imu.call_count, 1)

    def test_profile_controls_and_table_instruction(self):
        window = self.window()
        window.backend.state.running = True
        window.backend.state.measurement_profile = "stationary"
        window.backend.state.accept(imu())
        window.update()
        for key in ("start", "cal", "center"):
            window.buttons[key].configure.assert_called_with(state="disabled")
        window.buttons["stop"].configure.assert_called_with(state="normal")
        instruction = window.instruction.set.call_args.args[0]
        self.assertIn("Non indossare né ricentrare durante la misura", instruction)
        self.assertIn("Stop interrompe, NON certifica un PASS", instruction)
        window.backend.messages.put(("eof", 0, 0))
        window.update()
        window.root.destroy.assert_not_called()  # Finite capture leaves GUI open.
        window.buttons["start"].configure.assert_called_with(state="normal")

    def test_startup_ready_and_pending_stop_are_visible_with_correct_controls(self):
        window = self.window()
        state = window.backend.state
        state.running = True
        state.native_ready = False
        window.update()
        window.buttons["stop"].configure.assert_called_with(state="normal")
        for key in ("cal", "center"):
            window.buttons[key].configure.assert_called_with(state="disabled")
        self.assertIn("Avvio nativo", window.instruction.set.call_args.args[0])
        self.assertIn("IPC: waiting-native", window.labels[1].configure.call_args.kwargs["text"])
        state.native_ready = True
        window.update()
        window.buttons["cal"].configure.assert_called_with(state="normal")
        window.buttons["center"].configure.assert_called_with(state="disabled")
        state.accept(imu())
        window.update()
        window.buttons["center"].configure.assert_called_with(state="normal")
        state.pending_stop, state.busy, state.stop_status = True, True, "waiting-ready"
        window.update()
        window.buttons["stop"].configure.assert_called_with(state="disabled")
        self.assertIn("Stop pendente", window.instruction.set.call_args.args[0])
        self.assertIn("Stop pendente: waiting-ready", window.labels[1].configure.call_args.kwargs["text"])

    def test_progress_and_frequencies_use_actual_values_readably(self):
        window = self.window()
        data = imu()
        data["calibration"]["elapsed_ms"] = 500.0
        data["calibration"]["samples"] = 99999  # Must not estimate from samples.
        data["rate"]["report_hz"] = 100.123456789
        data["rate"]["sample_hz"] = 200.987654321
        window.backend.state.running = True
        window.backend.state.accept(data)
        window.update()
        hardware = window.labels[0].configure.call_args.kwargs["text"]
        telemetry = window.labels[1].configure.call_args.kwargs["text"]
        orientation = window.labels[2].configure.call_args.kwargs["text"]
        self.assertIn("Indossato: sì · IR RAW: 2", hardware)
        self.assertIn("Pannello (ultimo controllo): sconosciuto", hardware)
        self.assertIn("500 / 2000 ms (25%, clock dispositivo)", telemetry)
        self.assertIn("100.1 Hz", telemetry)
        self.assertIn("201.0 Hz", telemetry)
        self.assertNotIn("100.123456789", telemetry)
        self.assertIn("Flag display RAW: True (non è potenza)", orientation)
        self.assertEqual(monitor.MonitorWindow.frequency(None), "— Hz")
        self.assertIn("400 campioni", monitor.MonitorWindow.calibration_progress(imu()["calibration"]))

    def test_transition_and_off_modes_are_displayed_verbatim(self):
        window = self.window()
        for mode in ("transition", "off"):
            window.backend.state.accept({"event": "control_status", "power_on": mode != "off",
                "worn": False, "mode": mode, "vr_active": 1, "status_mask": 0})
            window.update()
            self.assertIn("Modo (ultimo controllo): " + mode,
                          window.labels[0].configure.call_args.kwargs["text"])

    def test_bias_limits_and_reference_instructions_are_distinct(self):
        window = self.window()
        data = imu()
        data["calibration"].update({"target_ms": 8000, "elapsed_ms": 4000,
            "gyro_mean_dps": [-.398759, -.539445, .203057],
            "gyro_halves_mean_shift_dps": [.01, .02, .03],
            "gyro_std_limit_dps": .75, "gyro_mean_shift_limit_dps": .05})
        window.backend.state.running = True
        data["state"], data["valid"] = "calibrating", False
        window.backend.state.accept(data)
        window.update()
        instruction = window.instruction.set.call_args.args[0]
        self.assertIn("mani staccate durante calibrazione", instruction)
        self.assertIn("dopo tracking indossa e Ricentra", instruction)
        self.assertIn("Finestra di calibrazione richiesta: 8.0 s", instruction)
        self.assertIn("NON corregge drift né ricalcola bias", instruction)
        text = window.labels[1].configure.call_args.kwargs["text"]
        self.assertIn("4000 / 8000 ms (50%, clock dispositivo)", text)
        self.assertIn("Bias stimato gyro °/s (X Y Z): -0.399  -0.539  +0.203", text)
        self.assertIn("limite 0.750", text)
        self.assertIn("Δ medie metà °/s: +0.010  +0.020  +0.030 · limite 0.050", text)
        data["state"], data["valid"] = "tracking", True
        window.backend.state.accept(data)
        window.update()
        self.assertIn("senza ricalibrare il bias", window.instruction.set.call_args.args[0])

    def test_plot_legends_disambiguate_angles_from_body_axes(self):
        window = self.window()
        for name in ("sketch", "history_canvas"):
            canvas = Mock()
            canvas.winfo_width.return_value = 640
            canvas.winfo_height.return_value = 240
            setattr(window, name, canvas)
        monitor.MonitorWindow.draw_sketch(window, {"quaternion_wxyz": [1, 0, 0, 0]})
        monitor.MonitorWindow.draw_history(window, [])
        sketch_text = [call.kwargs.get("text") for call in window.sketch.create_text.call_args_list]
        plot_text = [call.kwargs.get("text") for call in window.history_canvas.create_text.call_args_list]
        self.assertIn("Assi corpo 3D: +X rosso · +Y verde · −Z blu (non colori degli angoli)", sketch_text)
        self.assertIn("Angoli · yaw(Y) ROSSO / pitch(X) VERDE / roll(Z) BLU · ±180°", plot_text)
        axis_colors = [call.kwargs.get("fill") for call in window.sketch.create_line.call_args_list[-3:]]
        self.assertEqual(axis_colors, ["#ff8787", "#80e5aa", "#8caaff"])

    def test_close_cleanup_then_destroy_only_on_main_thread(self):
        window = self.window()
        window.backend.shutdown = Mock()
        worker_threads = []
        real_thread = threading.Thread
        # Capture the real cleanup thread without instantiating any Tk widgets.
        def create_thread(**kwargs):
            thread = real_thread(**kwargs)
            worker_threads.append(thread)
            return thread
        destroy_threads = []
        window.root.destroy.side_effect = lambda: destroy_threads.append(threading.get_ident())
        with patch.object(monitor.threading, "Thread", side_effect=create_thread):
            window.close()
            window.close()  # repeated WM_DELETE_WINDOW is harmless
        for thread in worker_threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        window.backend.shutdown.assert_called_once()
        window.api.close.assert_called_once()
        self.assertTrue(window.closed.is_set())
        window.root.destroy.assert_not_called()
        window.update()
        self.assertEqual(destroy_threads, [threading.get_ident()])

    def test_cleanup_exception_still_releases_destroy_poll(self):
        window = self.window()
        window.backend.shutdown = Mock()
        window.api.close.side_effect = RuntimeError("cleanup failure")
        captured = []
        with patch.object(monitor.threading, "Thread", side_effect=lambda **kw: captured.append(kw["target"]) or Mock()):
            window.close()
        with self.assertRaisesRegex(RuntimeError, "cleanup failure"):
            captured[0]()
        self.assertTrue(window.closed.is_set())
        window.update()
        window.root.destroy.assert_called_once()


class CLITests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows PowerShell syntax check")
    def test_launcher_powershell_syntax_only_never_executes_script(self):
        source = (ROOT / "scripts" / "run_monitor.ps1").read_text(encoding="utf-8")
        parse_only = ("$tokens = $null; $errors = $null; "
            "[void][System.Management.Automation.Language.Parser]::ParseInput("
            "[Console]::In.ReadToEnd(), [ref]$tokens, [ref]$errors); "
            "if ($errors.Count -gt 0) { $errors | ForEach-Object { $_.Message }; exit 1 }")
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", parse_only],
            input=source, text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_launcher_python_preflight_reuses_config_validation_without_main(self):
        source = (ROOT / "scripts" / "run_monitor.ps1").read_text(encoding="utf-8")
        code = source.split("$validationCode = @'\n", 1)[1].split("\n'@", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "config.json").write_text(json.dumps({"tracking": {
                "calibration_ms": 10000, "startup_discard_ms": 2000}}), encoding="utf-8")
            output = io.StringIO()
            with patch.object(sys, "argv", ["-c", str(ROOT / "tools/psvr_monitor/monitor.py"), directory, "42000"]), \
                 redirect_stdout(output), self.assertRaises(SystemExit) as error:
                exec(compile(code, "launcher_preflight", "exec"), {})
            self.assertEqual(error.exception.code, 2)
            self.assertIn(">= 43000", output.getvalue())
            output = io.StringIO()
            with patch.object(sys, "argv", ["-c", str(ROOT / "tools/psvr_monitor/monitor.py"), directory, "43000"]), \
                 redirect_stdout(output):
                namespace = {}
                exec(compile(code, "launcher_preflight", "exec"), namespace)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(namespace["m"]["__name__"], "monitor_validation")

    def test_stationary_minimum_uses_config_windows_and_margin(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for tracking, expected in (({}, 39200),
                ({"calibration_ms": 10000, "startup_discard_ms": 2000}, 43000),
                ({"calibration_ms": 500, "startup_discard_ms": 0}, 31500),
                ({"calibration_ms": 8000, "startup_discard_ms": 200}, 39200)):
                with self.subTest(tracking=tracking):
                    path.write_text(json.dumps({"tracking": tracking}), encoding="utf-8")
                    self.assertEqual(monitor.stationary_minimum_ms(directory), expected)
                    monitor.validate_imu_options(expected, True, directory)
                    with self.assertRaisesRegex(ValueError, f">= {expected}"):
                        monitor.validate_imu_options(expected - 1, True, directory)
                    backend = monitor.Backend(root=directory, popen=Mock())
                    with self.assertRaises(ValueError):
                        backend.start_imu(duration_ms=expected - 1, stationary_test=True)
                    backend.popen.assert_not_called()

    def test_stationary_config_read_failures_before_hardware(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with self.assertRaisesRegex(ValueError, "config.json"):
                monitor.validate_imu_options(42000, True, directory)
            # Non-profile/default startup doesn't need or read a config for this preflight.
            monitor.validate_imu_options(0, False, directory)
            invalid = [[], {"tracking": None}, {"tracking": {"calibration_ms": True}},
                {"tracking": {"calibration_ms": 499}}, {"tracking": {"calibration_ms": 10001}},
                {"tracking": {"startup_discard_ms": -1}}, {"tracking": {"startup_discard_ms": 2001}},
                {"tracking": {"startup_discard_ms": .5}}]
            for config in invalid:
                with self.subTest(config=config):
                    path.write_text(json.dumps(config), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "Impossibile verificare"):
                        monitor.validate_imu_options(42000, True, directory)
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                monitor.validate_imu_options(42000, True, directory)

    def test_cli_profile_uses_selected_root_not_workspace_config(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "config.json").write_text(json.dumps({"tracking": {
                "calibration_ms": 10000, "startup_discard_ms": 2000}}), encoding="utf-8")
            with patch.object(monitor, "Backend") as backend, redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                monitor.main(["--root", directory, "--start-imu", "--stationary-test", "--imu-duration-ms", "42000"])
            self.assertEqual(error.exception.code, 2)
            backend.assert_not_called()

    def test_invalid_cli_rejected_before_backend_or_api(self):
        cases = [["--stationary-test"], ["--imu-duration-ms", "42000"],
                 ["--start-imu", "--stationary-test"],
                 ["--start-imu", "--stationary-test", "--imu-duration-ms", "38200"],
                 ["--start-imu", "--imu-duration-ms", "180001"],
                 ["--start-imu", "--imu-duration-ms", "-1"],
                 ["--start-imu", "--imu-duration-ms", "4.2"],
                 ["--start-imu", "--headless"]]
        with patch.object(monitor, "Backend") as backend, patch.object(monitor, "MonitorAPI") as api, \
             patch.object(monitor, "MonitorWindow") as window, \
             patch.object(monitor, "stationary_minimum_ms", return_value=39200):
            for arguments in cases:
                with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    monitor.main(arguments)
                self.assertEqual(error.exception.code, 2)
            backend.assert_not_called()
            api.assert_not_called()
            window.assert_not_called()

    def test_explicit_cli_passes_profile_to_gui_default_does_not_start(self):
        for arguments, expected in (([], {"start_imu": False, "imu_duration_ms": 0, "stationary_test": False}),
            (["--start-imu"], {"start_imu": True, "imu_duration_ms": 0, "stationary_test": False}),
            (["--start-imu", "--imu-duration-ms", "42000", "--stationary-test"],
             {"start_imu": True, "imu_duration_ms": 42000, "stationary_test": True})):
            with self.subTest(arguments=arguments), patch.object(monitor, "MonitorAPI"), \
                 patch.object(monitor, "MonitorWindow") as window, \
                 patch.object(monitor, "stationary_minimum_ms", return_value=39200), \
                 patch.object(monitor.subprocess, "Popen", side_effect=AssertionError("hardware called")):
                self.assertEqual(monitor.main(arguments), 0)
                self.assertEqual(window.call_args.kwargs, expected)
                window.return_value.run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
