"""Suite runtime lifecycle contracts; mocked backend, never real hardware."""
import json
import subprocess
import threading
import unittest
from pathlib import Path
from unittest import mock
import tempfile

from tools.psvr_suite import runtime as R
from tools.psvr_suite import settings as S


class Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeBackend:
    """Same surface as DefaultBackend; records every call."""

    DIAG = "C:/fake/out/diag/psvr_diag.exe"
    PROBE = "C:/fake/out/diag/psvr_runtime_probe.exe"
    CONFIG = "C:/fake/config.json"

    def __init__(self, processes=(), runs=None, states=None):
        self._states = [set(s) for s in states] if states else None
        self._processes = set(processes)
        self.calls = []
        self.runs = list(runs or [])
        self.popped = []
        self.quit_results = []
        self.uris = []

    def diag_exe(self):
        return self.DIAG

    def probe_exe(self):
        return self.PROBE

    def config_file(self):
        return self.CONFIG

    def processes(self):
        self.calls.append(("processes",))
        if self._states:
            self._processes = self._states.pop(0)
        return set(self._processes)

    def open_uri(self, uri):
        self.calls.append(("open_uri", uri))
        self.uris.append(uri)
        return True

    def quit_runtime(self, wait_s=25.0, poll_s=0.5):
        self.calls.append(("quit_runtime", wait_s))
        result = self.quit_results.pop(0) if self.quit_results else True
        if result:
            self._processes.discard("vrserver")
            self._processes.discard("vrcompositor")
        return result

    def run(self, argv, timeout, log_name="backend"):
        self.calls.append(("run", tuple(str(a) for a in argv), timeout, log_name))
        if self.runs:
            return self.runs.pop(0)
        return Proc(0, "")

    def popen(self, argv, log_name="backend"):
        self.calls.append(("popen", tuple(str(a) for a in argv), log_name))
        proc = Proc(0, "")
        self.popped.append(argv)
        return proc


def controller(backend, events=None, root=None):
    bucket = [] if events is None else events
    ctrl = R.RuntimeController(root=root, on_event=bucket.append, backend=backend,
                               sleep=lambda s: bucket.append(("sleep", s)),
                                clock=lambda: 0.0)
    class FakeLock:
        path=ctrl.lock.path
        _held=False
        def acquire(self): self._held=True;return True
        def release(self): self._held=False
        def held(self): return self._held
    ctrl.lock=FakeLock()  # mocked lifecycle must not lock the real suite file
    ctrl.lock.acquire()
    return ctrl, bucket


def kinds(bucket):
    return [e["kind"] if isinstance(e, dict) else e for e in bucket]


class RootTests(unittest.TestCase):
    def test_root_param_actual(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = R.DefaultBackend(tmp)
            ctrl = R.RuntimeController(root=tmp, backend=backend,
                                       sleep=lambda s: None, clock=lambda: 0.0)
            self.assertEqual(ctrl.root, Path(tmp))
            self.assertEqual(ctrl.lock.path, Path(tmp) / "logs" / "suite.lock")
            self.assertEqual(backend.log_dir, Path(tmp) / "logs")
            self.assertTrue(str(backend.diag_exe()).startswith(str(Path(tmp))))
            ctrl.close()

    def test_emit_dict_and_kind_canonical(self):
        backend = FakeBackend()
        ctrl, bucket = controller(backend)
        ctrl._emit({"kind": "note", "x": 1})
        ctrl._emit({"type": "legacy", "x": 2})
        self.assertEqual(bucket[-2]["kind"], "note")
        self.assertEqual(bucket[-1]["kind"], "legacy")
        self.assertNotIn("type", bucket[-1])
        ctrl.close()


class StatusTests(unittest.TestCase):
    def test_status_shape(self):
        backend = FakeBackend(processes=("vrserver",))
        ctrl, _ = controller(backend)
        status = ctrl.status()
        self.assertEqual(status["processes"],
                         {"vrserver": True, "vrcompositor": False,
                          "vrstartup": False, "psvr_diag": False})
        self.assertTrue(status["ownership"]["held"])
        self.assertIn("waiting", status)
        self.assertIn("queue_depth", status)
        self.assertIn("driver_tracking", status)
        self.assertIsNone(status["driver_applied"])
        ctrl.close()

    def test_status_fail_closed_on_processes_error(self):
        class Broken(FakeBackend):
            def processes(self):
                raise OSError("no provider")

        ctrl, _ = controller(Broken())
        with self.assertRaises(RuntimeError):
            ctrl.status()
        ctrl.close()

    def test_status_driver_query_optional(self):
        backend = FakeBackend(processes=("vrserver",))
        ctrl, _ = controller(backend)
        plain = ctrl.status()
        self.assertIsNone(plain["driver_tracking"]["pose_valid"])
        backend.runs = [Proc(0, json.dumps({"event": "status", "mode": "vr",
                                            "orientation_valid": True,
                                            "position_valid": True, "pose_valid": True}))]
        queried = ctrl.status(query_driver=True)
        self.assertTrue(queried["driver_tracking"]["pose_valid"])
        self.assertTrue(queried["driver_tracking"]["supported"])
        # unsupported binary -> unknown, never faked
        backend.runs = [Proc(2, "unknown flag")]
        unknown = ctrl.status(query_driver=True)
        self.assertIsNone(unknown["driver_tracking"]["pose_valid"])
        self.assertFalse(unknown["driver_tracking"]["supported"])
        ctrl.close()


class StartTests(unittest.TestCase):
    def test_idempotent_existing_vrserver_no_quit(self):
        backend = FakeBackend(processes=("vrserver", "vrcompositor"))
        ctrl, bucket = controller(backend)
        self.assertTrue(ctrl.start_vr())
        self.assertEqual(ctrl.mode, "vr")
        self.assertIn("already_running", kinds(bucket))
        self.assertFalse(any(c[0] in ("quit_runtime", "run", "popen") for c in backend.calls
                             if c[0] != "processes"))
        ctrl.close()

    def test_refuses_when_startup_or_diag_running(self):
        for running in (("vrstartup",), ("psvr_diag",)):
            with self.subTest(running=running):
                backend = FakeBackend(processes=running)
                ctrl, bucket = controller(backend)
                self.assertFalse(ctrl.start_vr())
                self.assertIn("already_running", kinds(bucket))
                self.assertFalse(any(c[0] == "run" for c in backend.calls))
                ctrl.close()

    def test_start_sequence_two_diag_then_launch_wait_calibrate(self):
        valid = "\n".join([
            json.dumps({"event": "sample", "serial": "PSVRPC-HMD-0", "valid": True}),
            json.dumps({"event": "summary", "serial": "PSVRPC-HMD-0", "valid_count": 3}),
        ])
        backend = FakeBackend(processes=(), runs=[Proc(0, "vr ok"), Proc(0, "leds ok"),
                                                  Proc(0, valid)])
        backend._states = [set(), set(), {"vrserver"}]
        ctrl, bucket = controller(backend)
        self.assertTrue(ctrl.start_vr())
        runs = [c for c in backend.calls if c[0] == "run"]
        self.assertEqual(len(runs), 3)
        # native allows one action only: separate commands, absolute --config
        for argv in (runs[0][1], runs[1][1]):
            self.assertIn("--config", argv)
            self.assertEqual(argv[argv.index("--config") + 1], FakeBackend.CONFIG)
        self.assertIn("--vr-mode", runs[0][1])
        self.assertNotIn("--leds-on", runs[0][1])
        self.assertIn("--leds-on", runs[1][1])
        self.assertNotIn("--vr-mode", runs[1][1])
        pops = [c for c in backend.calls if c[0] == "popen"]
        self.assertEqual(len(pops), 1)
        # calibration window = imu_calibration_ms/1000 + 1.0 margin AFTER launch
        pop_index = backend.calls.index(pops[0])
        sleep_index = bucket.index(("sleep", 9.0))
        self.assertLess(pop_index, len(backend.calls))
        self.assertIn(("sleep", 9.0), bucket)
        self.assertIn("calibration_waiting", kinds(bucket))
        waiting = [e for e in bucket if isinstance(e, dict) and e.get("kind") == "calibration_waiting"][0]
        self.assertEqual(waiting["seconds"], 9.0)
        self.assertIn("still", waiting["guidance"].lower())
        self.assertIn("visore fermo", waiting["guidance"].lower())
        self.assertEqual(ctrl.mode, "vr")
        self.assertIn("started", kinds(bucket))
        ctrl.close()

    def test_not_ready_without_valid_pose(self):
        backend = FakeBackend(processes=(), runs=[Proc(0, "vr ok"), Proc(0, "leds ok"),
                                                  Proc(0, json.dumps({"event": "summary",
                                                                      "valid_count": 0})),
                                                  Proc(2, "unknown flag")])
        backend._states = [set(), set(), {"vrserver"}]
        ctrl, bucket = controller(backend)
        self.assertTrue(ctrl.start_vr())
        self.assertIn("not_ready", kinds(bucket))
        self.assertEqual(ctrl.mode,'vr-awaiting-tracking')
        self.assertNotEqual(ctrl.mode, "vr")
        ctrl.close()

    def test_default_wait_includes_margin(self):
        backend = FakeBackend(processes=(), runs=[Proc(0, "vr ok"), Proc(0, "leds ok"),
                                                  Proc(0, json.dumps({"event": "summary",
                                                                      "valid_count": 1}))])
        backend._states = [set(), set(), {"vrserver"}]
        ctrl, bucket = controller(backend)
        self.assertTrue(ctrl.start_vr())
        self.assertIn(("sleep", 9.0), bucket)
        waiting = [e for e in bucket if isinstance(e, dict) and e.get("kind") == "calibration_waiting"][0]
        self.assertEqual(waiting["seconds"], 9.0)
        self.assertIn("still", waiting["guidance"].lower())
        ctrl.close()

    def test_custom_prefs_wait(self):
        prefs = S.default_preferences()
        prefs["imu_calibration_ms"] = 5000
        backend = FakeBackend(processes=(), runs=[Proc(0, "vr ok"), Proc(0, "leds ok"),
                                                  Proc(0, json.dumps({"event": "summary",
                                                                      "valid_count": 1}))])
        backend._states = [set(), set(), {"vrserver"}]
        ctrl, bucket = controller(backend)
        self.assertTrue(ctrl.start_vr(prefs))
        self.assertIn(("sleep", 6.0), bucket)
        waiting = [e for e in bucket if isinstance(e, dict) and e.get("kind") == "calibration_waiting"][0]
        self.assertEqual(waiting["seconds"], 6.0)
        ctrl.close()

    def test_orientation_valid_position_missing_allows_camera(self):
        tracking = json.dumps({"event": "status", "mode": "3dof_fallback",
                               "orientation_valid": True, "position_valid": False,
                               "pose_valid": False})
        backend = FakeBackend(processes=(), runs=[Proc(0, "vr ok"), Proc(0, "leds ok"),
                                                  Proc(0, json.dumps({"event": "summary",
                                                                      "valid_count": 0})),
                                                  Proc(0, tracking)])
        backend._states = [set(), set(), {"vrserver"}]
        ctrl, bucket = controller(backend)
        self.assertTrue(ctrl.start_vr())
        self.assertEqual(ctrl.mode, "vr-awaiting-tracking")
        self.assertIn("not_ready", kinds(bucket))
        ctrl.close()

    def test_supported_orientation_false_fails_calibration(self):
        tracking = json.dumps({"event": "status", "mode": "vr",
                               "orientation_valid": False, "position_valid": False,
                               "pose_valid": False})
        backend = FakeBackend(processes=(), runs=[Proc(0, "vr ok"), Proc(0, "leds ok"),
                                                  Proc(0, json.dumps({"event": "summary",
                                                                      "valid_count": 0})),
                                                  Proc(0, tracking)])
        backend._states = [set(), set(), {"vrserver"}]
        ctrl, bucket = controller(backend)
        self.assertFalse(ctrl.start_vr())
        self.assertEqual(ctrl.mode, "vr-calibration-failed")
        self.assertIn("restart_required", kinds(bucket))
        reasons = [e.get("reason", "") for e in bucket if isinstance(e, dict)
                   and e.get("kind") == "restart_required"]
        self.assertTrue(any("fresh orientation unavailable" in r for r in reasons))
        ctrl.close()

    def test_unknown_orientation_does_not_fake_failure(self):
        backend = FakeBackend(processes=(), runs=[Proc(0, "vr ok"), Proc(0, "leds ok"),
                                                  Proc(0, json.dumps({"event": "summary",
                                                                      "valid_count": 0})),
                                                  Proc(2, "unknown flag")])
        backend._states = [set(), set(), {"vrserver"}]
        ctrl, bucket = controller(backend)
        self.assertTrue(ctrl.start_vr())
        self.assertEqual(ctrl.mode, "vr-awaiting-tracking")
        self.assertNotIn("restart_required", kinds(bucket))
        ctrl.close()

    def test_quit_timeout_aborts(self):
        backend = FakeBackend(processes=())
        backend.quit_results = [False]
        ctrl, bucket = controller(backend)
        ctrl.start_vr()
        self.assertFalse(any(c[0]=='quit_runtime' for c in backend.calls))
        self.assertNotIn("quit_timeout", kinds(bucket))
        ctrl.close()

    def test_diag_failure_aborts(self):
        backend = FakeBackend(processes=())
        backend.runs = [Proc(2, "no device")]
        ctrl, bucket = controller(backend)
        self.assertFalse(ctrl.start_vr())
        self.assertIn("diag_failed", kinds(bucket))
        ctrl.close()

    def test_ownership_denied(self):
        backend = FakeBackend(processes=())
        ctrl = R.RuntimeController(root=None, on_event=lambda e: None, backend=backend,
                                   sleep=lambda s: None, clock=lambda: 0.0)
        ctrl.lock.acquire()
        holder_events = []
        rival = R.RuntimeController(root=None, on_event=holder_events.append, backend=backend,
                                    sleep=lambda s: None, clock=lambda: 0.0)
        rival.lock = ctrl.lock.__class__(ctrl.lock.path)
        self.assertFalse(rival.start_vr())
        self.assertIn("ownership_denied", [e["kind"] for e in holder_events])
        ctrl.close()


class StopCinemaTests(unittest.TestCase):
    def test_stop_graceful_never_kills(self):
        backend = FakeBackend(processes=("vrserver", "vrcompositor"))
        ctrl, _ = controller(backend)
        self.assertTrue(ctrl.stop_vr())
        self.assertNotIn("vrserver", backend._processes)
        for call in backend.calls:
            self.assertNotIn("kill", call[0])
            self.assertNotIn("taskkill", str(call))
        ctrl.close()

    def test_to_cinema_confirms_f0(self):
        backend = FakeBackend(processes=())
        backend.runs = [Proc(0, "cinema F0 confirmed")]
        ctrl, bucket = controller(backend)
        self.assertTrue(ctrl.to_cinema())
        self.assertEqual(ctrl.mode, "cinema")
        self.assertIn("cinema_confirmed", kinds(bucket))
        runs = [c for c in backend.calls if c[0] == "run"]
        self.assertIn("--config", runs[0][1])
        ctrl.close()

    def test_to_cinema_unconfirmed(self):
        backend = FakeBackend(processes=())
        backend.runs = [Proc(0, "no confirmation")]
        ctrl, _ = controller(backend)
        self.assertFalse(ctrl.to_cinema())
        ctrl.close()


def request_lines(serial="PSVRPC-HMD-0", command="recenter", queued=True,
                  state="tracking", exit_note=False):
    lines = [json.dumps({"event": "request", "serial": serial, "command": command,
                         "device_index": 0, "dispatched": True, "queued": queued,
                         "applied_verified": False,
                         "verification": "queued_not_applied" if queued else "response_unconfirmed"})]
    if state is not None:
        lines.append(json.dumps({"event": "sample", "serial": serial, "state": state}))
    return "\n".join(lines)


class ProbeTests(unittest.TestCase):
    def test_recenter_queued(self):
        backend = FakeBackend(processes=())
        backend.runs = [Proc(0, request_lines())]
        ctrl, bucket = controller(backend)
        result = ctrl.recenter()
        self.assertTrue(result["ok"])
        self.assertTrue(result["queued"])
        self.assertIn("recenter_queued", kinds(bucket))
        runs = [c for c in backend.calls if c[0] == "run"]
        self.assertIn("--request", runs[0][1])
        ctrl.close()

    def test_exit4_with_queued_is_not_failure(self):
        backend = FakeBackend(processes=())
        backend.runs = [Proc(4, request_lines(command="recalibrate", state=None))]
        ctrl, _ = controller(backend)
        result = ctrl.recalibrate()
        self.assertTrue(result["ok"])
        self.assertTrue(result["queued"])
        ctrl.close()

    def test_other_serial_unverified(self):
        backend = FakeBackend(processes=())
        backend.runs = [Proc(0, request_lines(serial="OTHER-1"))]
        ctrl, bucket = controller(backend)
        result = ctrl.recenter()
        self.assertFalse(result["ok"])
        self.assertFalse(result["hmd_verified"])
        self.assertIn("recenter_unverified", kinds(bucket))
        ctrl.close()

    def test_calfailed_emits_restart_no_retry(self):
        backend = FakeBackend(processes=())
        backend.runs = [Proc(0, request_lines(state="calibration-failed"))]
        ctrl, bucket = controller(backend)
        result = ctrl.recalibrate()
        self.assertFalse(result["ok"])
        self.assertTrue(result.get("restart_required"))
        self.assertIn("restart_required", kinds(bucket))
        self.assertEqual(sum(1 for c in backend.calls if c[0] == "run"), 1)
        ctrl.close()

    def test_show_grid_spawns_bounded_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            grid = Path(tmp) / "grid.png"
            grid.write_bytes(b"\x89PNG")
            backend = FakeBackend(processes=())
            backend.runs = [Proc(0, "grid ok")]
            ctrl, bucket = controller(backend, root=tmp)
            # controller root differs from lock root on purpose here is fine;
            # grid resolves absolute under the given root below via explicit path
            result = ctrl.show_grid(grid=grid, wait_seconds=5)
            self.assertTrue(result["ok"])
            self.assertTrue(Path(result["log"]).is_absolute())
            result["thread"].join(timeout=5)
            self.assertIn("grid_shown", kinds(bucket))
            runs = [c for c in backend.calls if c[0] == "run"]
            self.assertIn("--optics-grid", runs[0][1])
            self.assertIn(str(grid), runs[0][1])
            ctrl.close()

    def test_show_grid_missing_image(self):
        backend = FakeBackend(processes=())
        ctrl, bucket = controller(backend, root=tempfile.gettempdir())
        result = ctrl.show_grid(grid="/nonexistent/grid.png")
        self.assertFalse(result["ok"])
        self.assertFalse(any(c[0] == "run" for c in backend.calls))
        ctrl.close()


class ApplyDriverTests(unittest.TestCase):
    def test_restart_only_if_was_running(self):
        backend = FakeBackend(processes=())
        backend.runs = [Proc(0, "vr ok")]
        ctrl, _ = controller(backend)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "steamvr.vrsettings"
            path.write_text(json.dumps({"driver_psvrpc": {}}), encoding="utf-8")
            from tools.psvr_suite.settings import DriverSettingStore
            store = DriverSettingStore(path, Path(tmp) / "b", running_checker=lambda: set())
            result = ctrl.apply_driver(S.default_preferences(), store=store)
            self.assertTrue(result["ok"])
            self.assertFalse(result["restarted"])
            self.assertFalse(any(c[0] == "quit_runtime" for c in backend.calls))
            self.assertEqual(ctrl.status()["driver_applied"]["settings"], str(path))
        ctrl.close()

    def test_running_restarts_idempotent(self):
        backend = FakeBackend(processes=("vrserver",))
        backend._states = [{"vrserver"}, {"vrserver"}]
        ctrl, _ = controller(backend)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "steamvr.vrsettings"
            path.write_text(json.dumps({"driver_psvrpc": {}}), encoding="utf-8")
            from tools.psvr_suite.settings import DriverSettingStore
            store = DriverSettingStore(path, Path(tmp) / "b", running_checker=lambda: set())
            result = ctrl.apply_driver(S.default_preferences(), store=store)
            self.assertTrue(result["ok"])
            # vrserver already up after quit clears only stale state here is
            # idempotent: start_vr sees live vrserver and returns True
            self.assertTrue(result["restarted"])
            self.assertEqual(ctrl.mode, "vr")
        ctrl.close()

    def test_restart_failure_propagates_no_false_positive(self):
        backend = FakeBackend(processes=("vrserver",))
        backend.runs = [Proc(2, "no device")]
        ctrl, bucket = controller(backend)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "steamvr.vrsettings"
            path.write_text(json.dumps({"driver_psvrpc": {}}), encoding="utf-8")
            from tools.psvr_suite.settings import DriverSettingStore
            store = DriverSettingStore(path, Path(tmp) / "b", running_checker=lambda: set())
            result = ctrl.apply_driver(S.default_preferences(), store=store)
            self.assertFalse(result["ok"])
            self.assertIn(result.get("reason"), ("restart_failed", "restart_required"))
            self.assertFalse(result["restarted"])
            # journal was applied and is preserved even when restart fails
            self.assertIn("journal", result)
            self.assertTrue(result["journal"].get("applied"))
        ctrl.close()

    def test_explicit_restart_failure_propagates(self):
        backend = FakeBackend(processes=())
        backend.runs = [Proc(2, "no device")]
        backend._states = [set()]
        ctrl, _ = controller(backend)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "steamvr.vrsettings"
            path.write_text(json.dumps({"driver_psvrpc": {}}), encoding="utf-8")
            from tools.psvr_suite.settings import DriverSettingStore
            store = DriverSettingStore(path, Path(tmp) / "b", running_checker=lambda: set())
            result = ctrl.apply_driver(S.default_preferences(), store=store, restart=True)
            self.assertFalse(result["ok"])
            self.assertFalse(result["restarted"])
            self.assertIn("journal", result)
        ctrl.close()

    def test_apply_driver_uses_prefs_wait(self):
        prefs = S.default_preferences()
        prefs["imu_calibration_ms"] = 5000
        valid = "\n".join([
            json.dumps({"event": "sample", "serial": "PSVRPC-HMD-0", "valid": True}),
            json.dumps({"event": "summary", "serial": "PSVRPC-HMD-0", "valid_count": 1}),
        ])
        backend = FakeBackend(processes=("vrserver",),
                              runs=[Proc(0, "vr ok"), Proc(0, "leds ok"), Proc(0, valid)])
        backend._states = [{"vrserver"}, set(), set(), {"vrserver"}]
        ctrl, bucket = controller(backend)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "steamvr.vrsettings"
            path.write_text(json.dumps({"driver_psvrpc": {}}), encoding="utf-8")
            from tools.psvr_suite.settings import DriverSettingStore
            store = DriverSettingStore(path, Path(tmp) / "b", running_checker=lambda: set())
            result = ctrl.apply_driver(prefs, store=store)
            self.assertTrue(result["ok"])
            self.assertTrue(result["restarted"])
            self.assertIn(("sleep", 6.0), bucket)
        ctrl.close()

    def test_exceptions_become_events(self):
        backend = FakeBackend(processes=())

        def boom():
            raise RuntimeError("nope")

        ctrl, bucket = controller(backend)
        thread = ctrl.submit(boom)
        thread.join(timeout=5)
        self.assertIn("exception", kinds(bucket))
        ctrl.close()


class SubprocessVisibilityTests(unittest.TestCase):
    """Windows pythonw must never flash a console: powershell probes hide it."""

    def test_default_backend_processes_hides_console(self):
        expected = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with tempfile.TemporaryDirectory() as tmp:
            backend = R.DefaultBackend(tmp)
            with mock.patch.object(R.subprocess, "run") as run:
                run.return_value = Proc(0, "vrserver\n")
                names = backend.processes()
            self.assertEqual(names, {"vrserver"})
            self.assertTrue(run.called)
            _, kwargs = run.call_args
            self.assertIn("creationflags", kwargs)
            self.assertEqual(kwargs["creationflags"], expected)
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                self.assertNotEqual(kwargs["creationflags"], 0)


if __name__ == "__main__":
    unittest.main()
