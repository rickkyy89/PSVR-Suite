"""Tk-suite runtime lifecycle: VR/cinema/recenter/ownership, no force kills.

Blocking backend calls are designed for UI worker threads (see ``submit``);
the Tk thread only consumes ``on_event`` dicts (canonical ``kind`` field;
a bare dict is accepted too) and ``status()`` snapshots. USB belongs to
psvr_diag.exe; poses/HMD identity to psvr_runtime_probe.exe. This module
never kills arbitrary processes: only handles it started itself are
terminated, vrserver/vrcompositor stop exclusively via the graceful
``vrmonitor://quit`` URI with bounded waits. Probe/SDK responses are queued
observations, never accepted calibration state; the monitor UI keeps polling.
"""
from __future__ import annotations

from collections import deque
import json
import os
import subprocess
import threading
import time
import weakref
from pathlib import Path

from . import settings as suite_settings

ROOT = Path(__file__).resolve().parents[2]
QUIT_URI = "vrmonitor://quit"
QUIT_WAIT_S = 25.0
VRSTARTUP_WAIT_S = 30.0
GUIDED_CALIBRATION_S = 8.0
CALIBRATION_MARGIN_S = 1.0
VRSTARTUP_EXE = Path("C:/Program Files (x86)/Steam/steamapps/common/SteamVR/bin/win64/vrstartup.exe")
TARGET_SERIAL = suite_settings.TARGET_HMD_SERIAL
MAX_EVENTS = 200


def _utc():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


class SuiteLock:
    """Exclusive suite ownership via an OS-held lock file (crash-safe)."""

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else ROOT / "logs" / "suite.lock"
        self._handle = None

    def acquire(self):
        if self._handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(str(self.path), "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self):
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                try:
                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

    def held(self):
        return self._handle is not None


class DefaultBackend:
    """Real subprocess backend rooted at a repo path (tests inject a fake)."""

    DIAG_REL = Path("out/diag/psvr_diag.exe")
    PROBE_CANDIDATES = (Path("out/runtime_probe/psvr_runtime_probe.exe"),
                        Path("out/diag/psvr_runtime_probe.exe"))
    CONFIG_REL = Path("config.json")

    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.log_dir = self.root / "logs"
        self.tracked = []

    def diag_exe(self):
        return self.root / self.DIAG_REL

    def probe_exe(self):
        for candidate in self.PROBE_CANDIDATES:
            if (self.root / candidate).is_file():
                return self.root / candidate
        return self.root / self.PROBE_CANDIDATES[0]

    def config_file(self):
        return (self.root / self.CONFIG_REL).resolve()

    def processes(self):
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process -Name vrserver,vrcompositor,vrstartup,psvr_diag -ErrorAction SilentlyContinue"
             " | ForEach-Object { $_.ProcessName }; exit 0"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {line.strip().lower() for line in result.stdout.splitlines() if line.strip()}

    def _transcript(self, name, text):
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with (self.log_dir / ("suite_%s.log" % name)).open("a", encoding="utf-8") as stream:
                stream.write(text)
        except OSError:
            pass

    def run(self, argv, timeout, log_name="backend"):
        proc = subprocess.run([str(a) for a in argv], capture_output=True, text=True,
                              timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self._transcript(log_name, "$ %s\n%s%s\n" % (" ".join(str(a) for a in argv),
                                                     proc.stdout, proc.stderr))
        return proc

    def popen(self, argv, log_name="backend"):
        proc = subprocess.Popen([str(a) for a in argv],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.tracked.append(proc)
        return proc

    def shutdown(self):
        """Reap terminated popen handles; never kills (B11-01).

        Non-blocking: ``poll()`` only; terminated handles are reaped with
        ``wait(timeout=0)`` plus best-effort stdout drain/close to release
        fds and pipe buffers. Live handles are kept for the graceful
        ``vrmonitor://quit`` path; this method never kills/terminates.
        Idempotent: safe to call repeatedly and on an empty list.
        Returns the number of still-live tracked handles.
        """
        alive = []
        for proc in list(self.tracked):
            try:
                exited = proc.poll()
            except Exception:
                # Uninspectable handle: keep it rather than dropping silently.
                alive.append(proc)
                continue
            if exited is None:
                alive.append(proc)
                continue
            try:
                try:
                    proc.wait(timeout=0)
                except Exception:
                    pass
                stream = getattr(proc, "stdout", None)
                if stream is not None and hasattr(stream, "close"):
                    try:
                        if not getattr(stream, "closed", False):
                            try:
                                # Best-effort drain; the writer is gone so EOF
                                # follows. Any stall/error must not block close.
                                if hasattr(proc, "communicate"):
                                    try:
                                        proc.communicate(timeout=0)
                                    except Exception:
                                        pass
                            finally:
                                try:
                                    stream.close()
                                except Exception:
                                    pass
                    except Exception:
                        pass
            except Exception:
                pass
        self.tracked = alive
        return len(alive)

    def close(self):
        """Backend close: reap only; never kills (B11-01). Idempotent."""
        try:
            self.shutdown()
        except Exception:
            pass

    def open_uri(self, uri):
        if hasattr(os, "startfile"):
            os.startfile(uri)
            return True
        raise RuntimeError("quit URI unsupported on this platform")

    def quit_runtime(self, wait_s=QUIT_WAIT_S, poll_s=0.5):
        """Graceful runtime quit only; never force-kills."""
        # The URI handler can START vrmonitor (and a server) when none exists.
        # Never dispatch quit to an already stopped runtime.
        if not (self.processes() & {'vrserver','vrcompositor','vrstartup'}):
            return True
        self.open_uri(QUIT_URI)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if not (self.processes() & {"vrserver", "vrcompositor"}):
                return True
            time.sleep(poll_s)
        return False


class RuntimeController:
    """Owns suite lifecycle sequencing; blocking calls run in UI workers."""

    def __init__(self, root=None, on_event=None, backend=None,
                 sleep=time.sleep, clock=time.monotonic):
        self.root = Path(root) if root is not None else ROOT
        self._on_event = on_event or (lambda event: None)
        self.backend = backend or DefaultBackend(self.root)
        self.sleep = sleep
        self.clock = clock
        self.lock = SuiteLock(self.root / "logs" / "suite.lock")
        self.control = threading.RLock()  # re-entrant: apply_driver may restart
        # B11-02: worker lifecycle registry. Dedicated lock on purpose:
        # shutdown() must snapshot/join without acquiring `control`, or a
        # long start_vr holding `control` would starve the join. WeakSet so
        # dead threads do not leak; daemon=True is kept until the UI calls
        # shutdown()/close() explicitly. No signal/atexit handlers here by
        # design (future UI decision, see shutdown docstring).
        self._workers_lock = threading.Lock()
        self._workers: weakref.WeakSet = weakref.WeakSet()
        self._grid_cancel = threading.Event()
        # B11-03: cooperative abort for a start_vr holding `control`.
        # Set by stop_vr WITHOUT acquiring `control` (Event.set is
        # thread-safe); consumed polled by start_vr at wait/sleep/probe
        # checkpoints. Sequencing under `control` is unchanged; the flag
        # only adds fail-closed abort exits. Cleared on start_vr entry
        # under `control` so a stale stop (no start running) never aborts
        # the next fresh start.
        self.abort_requested = threading.Event()
        self.events = deque(maxlen=MAX_EVENTS)
        self.last_event = None
        self.waiting = False
        self.mode = "unknown"
        self.last_driver_applied = None

    def _emit(self, kind_or_event, **fields):
        if isinstance(kind_or_event, dict):
            event = dict(kind_or_event)
            if "kind" not in event and "type" in event:
                event["kind"] = event.pop("type")
            event.setdefault("kind", "note")
        else:
            event = {"kind": kind_or_event}
            event.update(fields)
        event.setdefault("utc", _utc())
        event.setdefault("mode", self.mode)
        self.events.append(event)
        self.last_event = event
        try:
            self._on_event(dict(event))
        except Exception:
            pass
        return event

    def _register_worker(self, thread):
        # B11-02: registry under the dedicated lock, never under `control`.
        try:
            with self._workers_lock:
                self._workers.add(thread)
        except Exception:
            pass

    def submit(self, func, *args, **kwargs):
        """Run a blocking controller method in a daemon worker thread.

        The thread is registered in the B11-02 WeakSet and reaped by
        :meth:`shutdown` with a bounded join. daemon=True is kept so a
        forgotten worker never blocks interpreter exit; orderly UI exit
        calls shutdown()/close() instead.
        """
        def _guarded():
            try:
                func(*args, **kwargs)
            except Exception as exc:
                self._emit("exception", where=getattr(func, "__name__", "?"), error=str(exc))
        thread = threading.Thread(target=_guarded, daemon=True)
        self._register_worker(thread)
        thread.start()
        return thread

    def _processes(self):
        """Live process names; raises on backend failure (fail closed)."""
        try:
            result = self.backend.processes() or []
        except Exception as exc:
            raise RuntimeError("process ownership unverifiable: %s" % exc) from exc
        return {str(n).lower() for n in result}

    def _driver_tracking(self):
        """Optional DriverDebug query; unknown fields stay None, never faked."""
        run = self.backend.run(
            [str(self.backend.probe_exe()), "--openvr-dll",
             str(suite_settings.DEFAULT_OPENVR_DLL),
             "--request", "tracking-status", "--duration-ms", "100"],
            timeout=15, log_name="probe")
        tracking = {"mode": None, "orientation_valid": None,
                    "position_valid": None, "pose_valid": None,
                    "supported": False}
        for line in (run.stdout or "").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            payload = event.get("driver_tracking", event)
            if not isinstance(payload, dict):
                continue
            found = False
            for key in ("mode", "orientation_valid", "position_valid", "pose_valid"):
                value = payload.get(key)
                if key == "mode" and isinstance(value, str):
                    tracking[key] = value
                    found = True
                elif key != "mode" and isinstance(value, bool):
                    tracking[key] = value
                    found = True
            if found:
                tracking["supported"] = True
                for key in ('position_m','session_id','recenter_epoch','imu_sample_age_ms',
                            'recovery_offset_m','recovery_remaining_m','recovery_duration_ms','recovery_elapsed_ms','settings'):
                    if key in payload:
                        tracking[key]=payload[key]
        if tracking['supported']:
            tracking['fallback_active']=tracking['mode']=='3dof_fallback'
            tracking['recovery']=tracking['mode']=='6dof_reacquiring'
        return tracking

    def status(self, query_driver=False):
        """Snapshot; raises (fail closed) when ownership is unverifiable."""
        try:
            procs = self._processes()
        except RuntimeError as exc:
            self._emit("exception", where="status", error=str(exc))
            raise
        if self.mode=='unknown' and 'vrserver' in procs:
            self.mode='vr-attached'
        tracking = {"mode": None, "orientation_valid": None,
                    "position_valid": None, "pose_valid": None, "supported": False}
        if query_driver and procs & {"vrserver", "vrcompositor"}:
            try:
                tracking = self._driver_tracking()
            except Exception as exc:
                self._emit("exception", where="driver_tracking", error=str(exc))
        return {
            "mode": self.mode,
            "source": "driver_imu" if "vrserver" in procs else "idle",
            "readiness": ("tracking" if tracking.get('pose_valid') else "orientation_unavailable"
                          if tracking.get('orientation_valid') is False else "unknown"),
            "waiting": self.waiting,
            "processes": {
                "vrserver": "vrserver" in procs,
                "vrcompositor": "vrcompositor" in procs,
                "vrstartup": "vrstartup" in procs,
                "psvr_diag": "psvr_diag" in procs,
            },
            "ownership": {"held": self.lock.held()},
            "driver_tracking": tracking,
            "driver_applied": dict(self.last_driver_applied) if self.last_driver_applied else None,
            "queue_depth": len(self.events),
            "last_event": dict(self.last_event) if self.last_event else None,
        }

    def _require_ownership(self):
        if self.lock.held() or self.lock.acquire():
            return True
        self._emit("ownership_denied", reason="suite lock held by another app instance")
        return False

    def _diag(self, *flags, timeout=60, prefs=None):
        # Host diagnostic config only: the OpenVR driver reads its own
        # settings. Never fall back to config.json for an explicit path.
        if prefs is None:
            config = self.backend.config_file()
        else:
            config = Path(prefs["tracking_config_path"])
            if not config.is_absolute():
                config = self.root / config
            config = config.resolve()
        return self.backend.run(
            [str(self.backend.diag_exe()), "--config", str(config), *flags],
            timeout=timeout, log_name="diag")

    def _probe(self, *flags, timeout=30):
        return self.backend.run(
            [str(self.backend.probe_exe()), "--openvr-dll",
             str(suite_settings.DEFAULT_OPENVR_DLL), *flags],
            timeout=timeout, log_name="probe")

    def _wait_for(self, names, timeout, poll_s=0.5):
        # B11-03: abort-aware poll. Checked before/after each backend
        # poll so a stop_vr during the vrserver wait exits promptly
        # without releasing `control` (atomicity preserved).
        wanted = {n.lower() for n in names}
        for _ in range(int(timeout / poll_s) + 1):
            if self.abort_requested.is_set():
                return False
            try:
                if self._processes() & wanted:
                    return True
            except RuntimeError:
                return False
            if self.abort_requested.is_set():
                return False
            self.sleep(poll_s)
        return False

    @staticmethod
    def _observed_valid_pose(stdout):
        for line in (stdout or "").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("event") == "summary":
                if (event.get("valid_count") or 0) > 0:
                    return True
            if event.get("event") == "sample" and event.get("valid") is True:
                return True
        return False

    def start_vr(self, prefs=None):
        """Idempotent start: a running user VR session is never quit.

        Config is applied separately by the UI (see apply_driver). When
        stopped, the two diag commands run BEFORE launch; vrserver wait and
        the guided-calibration window run AFTER vrstartup. Readiness is
        observed from IMU/pose output, never inferred. The calibration
        window is ``imu_calibration_ms/1000 + 1.0`` s margin so the 8 s
        default never ends before the driver IMU calibration started
        ~200 ms after the server.

        B11-04: backend failures (diag/launch/probe) fail closed with an
        event + ``False`` and never raise on the caller; ``status()``
        keeps re-raising by design. ``waiting`` is reset via
        try/finally, never left stuck True.
        """
        if prefs is not None:
            cleaned_prefs = suite_settings.validate_preferences(prefs)
        else:
            cleaned_prefs = None
        if cleaned_prefs is not None:
            cal_ms = cleaned_prefs.get("imu_calibration_ms", 8000)
        else:
            try:
                cal_ms = suite_settings.default_preferences().get("imu_calibration_ms", 8000)
            except Exception:
                cal_ms = int(GUIDED_CALIBRATION_S * 1000)
        try:
            cal_wait = float(cal_ms) / 1000.0 + CALIBRATION_MARGIN_S
        except Exception:
            cal_wait = GUIDED_CALIBRATION_S + CALIBRATION_MARGIN_S
        with self.control:
            # B11-03: fresh sequence discards a stale stop (no start was
            # running when it was set). A stop setting the flag AFTER this
            # clear is observed at the checkpoints below.
            self.abort_requested.clear()
            if not self._require_ownership():
                return False
            try:
                procs = self._processes()
            except RuntimeError as exc:
                self._emit("processes_unknown", error=str(exc))
                return False
            if procs & {"vrserver", "vrcompositor"}:
                self.mode = "vr"
                self._emit("already_running", running=sorted(procs & {"vrserver", "vrcompositor"}))
                return True
            if procs & {"vrstartup", "psvr_diag"}:
                self._emit("already_running", running=sorted(procs & {"vrstartup", "psvr_diag"}))
                return False
            # B11-04: backend errors fail closed (event + False), never
            # raise on the UI thread. Validation errors from prefs above
            # still propagate (input contract, as in apply_driver).
            try:
                mode_run = self._diag("--vr-mode", timeout=60, prefs=cleaned_prefs)
            except Exception as exc:
                self._emit("exception", where="diag", flags=["--vr-mode"],
                           error=str(exc))
                return False
            # An idempotent mode request may be accepted without a NEW F0.
            # Preserve the native distinction: never claim fresh-state proof.
            accepted_unverified=(mode_run.returncode==6 and 'command response result_code=0' in (mode_run.stdout or ''))
            if mode_run.returncode != 0 and not accepted_unverified:
                self._emit("diag_failed", flags=["--vr-mode"],
                           returncode=mode_run.returncode, output=(mode_run.stdout or "")[-2000:])
                return False
            if accepted_unverified:
                self._emit('mode_unverified',reason='VR command accepted, no new F0; not an optical/display verification')
            try:
                leds_run = self._diag("--leds-on", timeout=60, prefs=cleaned_prefs)
            except Exception as exc:
                self._emit("exception", where="diag", flags=["--leds-on"],
                           error=str(exc))
                return False
            if leds_run.returncode != 0:
                self._emit("diag_failed", flags=["--leds-on"],
                           returncode=leds_run.returncode, output=(leds_run.stdout or "")[-2000:])
                return False
            try:
                self.backend.popen([str(VRSTARTUP_EXE)], log_name="vrstartup")
            except Exception as exc:
                self._emit("exception", where="vrstartup_launch", error=str(exc))
                return False
            if not self._wait_for(("vrserver",), VRSTARTUP_WAIT_S):
                # B11-03: abort (stop_vr during the wait) is fail-closed
                # with reason abort, distinct from a genuine timeout.
                if self.abort_requested.is_set():
                    self.waiting = False
                    self._emit("aborted", reason="abort")
                    return False
                self._emit("vrserver_timeout", wait_s=VRSTARTUP_WAIT_S)
                return False
            self.mode = "vr-starting"
            self.waiting = True
            try:
                self._emit("calibration_waiting", seconds=cal_wait,
                           guidance="keep headset still (visore fermo) for IMU calibration")
                # B11-03: abort checkpoint before the blocking calibration
                # sleep. `control` is still held (atomicity preserved).
                if self.abort_requested.is_set():
                    self.waiting = False
                    self._emit("aborted", reason="abort")
                    return False
                self.sleep(cal_wait)
                self.waiting = False
                # B11-03: abort checkpoint after the sleep, before touching
                # the probe. A stop during the sleep aborts here instead of
                # claiming started/not-ready.
                if self.abort_requested.is_set():
                    self._emit("aborted", reason="abort")
                    return False
                # B11-04: probe errors fail closed, never raise on the caller.
                try:
                    ready = self._probe("--duration-ms", "500", timeout=15)
                except Exception as exc:
                    self._emit("exception", where="probe", error=str(exc))
                    return False
                # B11-03: abort during the blocking probe is observed here,
                # before any readiness verdict (never fakes started).
                if self.abort_requested.is_set():
                    self._emit("aborted", reason="abort")
                    return False
                if self._observed_valid_pose(ready.stdout):
                    self.mode = "vr"
                    self._emit("started", mode="vr")
                    return True
                # No valid IMU pose after the window: consult driver tracking
                # (when supported) to distinguish a fresh-orientation failure
                # from a camera that is simply not started yet. Unknown or
                # unsupported tracking never fakes a calibration verdict.
                # B11-03: do not enter another blocking query after an abort.
                if self.abort_requested.is_set():
                    self._emit("aborted", reason="abort")
                    return False
                tracking = {"mode": None, "orientation_valid": None,
                            "position_valid": None, "pose_valid": None,
                            "supported": False}
                try:
                    tracking = self._driver_tracking()
                except Exception as exc:
                    self._emit("exception", where="driver_tracking", error=str(exc))
                    tracking = {"mode": None, "orientation_valid": None,
                                "position_valid": None, "pose_valid": None,
                                "supported": False}
                if tracking.get("supported") and tracking.get("orientation_valid") is False:
                    self.mode = "vr-calibration-failed"
                    self._emit("restart_required",
                               reason="fresh orientation unavailable after calibration window")
                    return False
                # Orientation valid but position missing (or tracking unknown /
                # unsupported legacy): start completed, camera may still be
                # started by the UI instead of deadlocking the sequence.
                self.mode = "vr-awaiting-tracking"
                self._emit("not_ready", reason="no valid IMU pose observed after calibration window")
                return True
            finally:
                # B11-04 backstop: an exception in sleep/probe-readiness
                # must never leave `waiting` stuck True (status() would
                # report a wait that is no longer running). Explicit
                # resets on the abort paths above are kept (B11-03
                # untouched); this only covers the exception path.
                self.waiting = False

    def stop_vr(self):
        """Graceful stop only; never force-kills the runtime.

        B11-04: no ownership gate by design ("lo stop funziona sempre").
        A stop must work even when this instance does not hold the suite
        lock (rival instance, stale holder): it never starts hardware or
        applies settings, it only flags abort and quits the runtime
        gracefully — the safe direction. Gating it on ownership could
        strand the headset in VR mode with LEDs on. Backend quit errors
        fail closed (event + False) instead of raising on the caller;
        ``status()`` keeps re-raising by design.

        B11-03: preemptive abort. The flag is set WITHOUT acquiring
        `control` (Event.set is thread-safe), so a stop issued while
        start_vr holds `control` for minutes is observed at the next
        start_vr checkpoint instead of starving behind the lock. The
        graceful quit is then attempted: under `control` when it is
        free (fast non-blocking acquire, same sequencing as before),
        otherwise concurrently without the lock (best-effort quit
        while start_vr is still aborting). Never kills; B11-04: quit
        errors fail closed (event + False), never raise.

        B11-09: a quit riuscita resetta anche i mode stale ``vr-*``
        (es. ``vr-attached`` impostato da ``status()``), non solo ``vr``.
        """
        try:
            self.abort_requested.set()
        except Exception:
            pass
        try:
            acquired = self.control.acquire(blocking=False)
        except Exception:
            acquired = False
        if acquired:
            try:
                # B11-04: quit errors fail closed, never raise on the caller.
                try:
                    ok = self.backend.quit_runtime(wait_s=QUIT_WAIT_S)
                except Exception as exc:
                    self._emit("exception", where="quit_runtime", error=str(exc))
                    return False
                if ok:
                    if isinstance(self.mode, str) and self.mode.startswith("vr"):  # B11-09: anche stale vr-* (es. vr-attached)
                        self.mode = "stopped"
                    self._emit("stopped")
                else:
                    self._emit("quit_timeout", wait_s=QUIT_WAIT_S)
                return ok
            finally:
                try:
                    self.control.release()
                except Exception:
                    pass
        # start_vr (or apply/to_cinema) holds `control`: quit without
        # blocking so the stop is preemptive; start_vr exits fail-closed
        # with reason abort at its next checkpoint.
        # B11-04: quit errors fail closed, never raise on the caller.
        try:
            ok = self.backend.quit_runtime(wait_s=QUIT_WAIT_S)
        except Exception as exc:
            self._emit("exception", where="quit_runtime", error=str(exc))
            return False
        if ok:
            if isinstance(self.mode, str) and self.mode.startswith("vr"):  # B11-09: anche stale vr-* (es. vr-attached)
                self.mode = "stopped"
            self._emit("stopped")
        else:
            self._emit("quit_timeout", wait_s=QUIT_WAIT_S)
        return ok

    def to_cinema(self, prefs=None):
        """Stop the runtime, then switch the headset to cinema mode (F0).

        B11-04: no ownership gate by design (same rationale as stop_vr,
        "lo stop funziona sempre"): the stop leg must work even for a
        non-owner, and the F0 switch is a confirmed idempotent mode
        request, never faked state. Adding ``_require_ownership`` here
        was considered and deliberately deferred: it would change the
        stop-then-switch contract for callers that never held the lock,
        with no safety gain (the destructive direction is start, which
        is gated). Backend errors fail closed (event + False) instead
        of raising on the caller; ``status()`` keeps re-raising.
        """
        cleaned = suite_settings.validate_preferences(prefs) if prefs is not None else None
        with self.control:
            try:
                quit_ok = self.backend.quit_runtime(wait_s=QUIT_WAIT_S)
            except Exception as exc:
                self._emit("exception", where="quit_runtime", error=str(exc))
                return False
            if not quit_ok:
                self._emit("quit_timeout", wait_s=QUIT_WAIT_S)
                return False
            try:
                run = self._diag("--cinema-mode", timeout=60, prefs=cleaned)
            except Exception as exc:
                self._emit("exception", where="diag", flags=["--cinema-mode"],
                           error=str(exc))
                return False
            confirmed = run.returncode == 0 and "F0" in (run.stdout or "")
            if confirmed:
                self.mode = "cinema"
                self._emit("cinema_confirmed")
            else:
                self._emit("cinema_unconfirmed", returncode=run.returncode)
            return confirmed

    def _verified_request(self, kind, flag):
        """Queue-only HMD request; never applies state, only queues it.

        B11-04: no ownership gate by design — the request is queued on
        the HMD verified by serial (``TARGET_SERIAL``) and never applied
        here, so there is no owned state to protect; gating it could
        strand a legitimate non-owner (same fail-safe rationale as
        stop_vr, "lo stop funziona sempre"). Probe errors fail closed
        with ``{ok: False}`` + event instead of raising on the caller.
        """
        with self.control:
            try:
                run = self._probe("--duration-ms", "1000", "--request", flag, timeout=30)
            except Exception as exc:
                self._emit("exception", where="probe", request=flag,
                           error=str(exc))
                return {"ok": False, "hmd_verified": False, "queued": False,
                        "error": str(exc)}
            queued = dispatched = False
            cal_failed = False
            for line in (run.stdout or "").splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if (event.get("event") == "request" and event.get("serial") == TARGET_SERIAL
                        and event.get("command", flag) == flag):
                    dispatched = bool(event.get("dispatched"))
                    queued = bool(event.get("queued"))
                if event.get("state") == "calibration-failed":
                    cal_failed = True
            if queued and dispatched:
                if cal_failed:
                    self._emit("restart_required", reason="%s queued but calibration failed" % kind)
                    return {"ok": False, "hmd_verified": True, "queued": True,
                            "restart_required": True}
                self._emit(kind + "_queued", hmd=TARGET_SERIAL, queued=True)
                return {"ok": True, "hmd_verified": True, "queued": True,
                        "returncode": run.returncode}
            if cal_failed:
                self._emit("restart_required", reason="%s reported calibration failure" % kind)
                return {"ok": False, "hmd_verified": True, "queued": queued,
                        "restart_required": True}
            self._emit(kind + "_unverified", returncode=run.returncode,
                       hmd_verified=False, queued=queued, dispatched=dispatched)
            return {"ok": False, "hmd_verified": False, "queued": queued,
                    "returncode": run.returncode}

    def recenter(self):
        """Queue a recenter on the verified HMD only (queued, never applied).

        No ownership gate by design, see :meth:`_verified_request`.
        """
        return self._verified_request("recenter", "recenter")

    def recalibrate(self):
        """Queue a recalibration; exit 4 without fresh poses is not failure
        once the request queued. No automatic retries.

        No ownership gate by design, see :meth:`_verified_request`.
        """
        return self._verified_request("recalibrate", "recalibrate")

    def show_grid(self, grid=None, wait_seconds=60, duration_ms=20000):
        """Spawn a bounded grid worker; never blocks control or the UI thread.

        Single bounded probe run with an absolute grid path and a unique
        absolute log; readiness is not gated on poses, so 3dof mode is safe.
        Returns immediately with the unique absolute log path.
        """
        default_grid = self.root / "logs" / "flat_optics_grid_20261009.png"
        target = Path(grid) if grid is not None else default_grid
        if not target.is_absolute():
            target = (self.root / target).resolve()
        if not target.is_file():
            self._emit("grid_failed", reason="grid image missing")
            return {"ok": False, "reason": "grid image missing"}
        log_dir = self.root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = "%d-%d" % (int(self.clock()), os.getpid())
        log_path = log_dir / ("suite_grid_%s.log" % stamp)
        counter = 0
        while log_path.exists():
            counter += 1
            log_path = log_dir / ("suite_grid_%s-%d.log" % (stamp, counter))
        log_path.touch()
        worker_argv = [str(self.backend.probe_exe()), "--openvr-dll",
                       str(suite_settings.DEFAULT_OPENVR_DLL),
                       "--duration-ms", str(int(duration_ms)),
                       "--optics-grid", str(target)]
        timeout = max(10, wait_seconds + duration_ms / 1000.0 + 30.0)

        def _worker():
            try:
                # B11-02: cooperative cancel checked before the blocking run.
                # A worker already inside backend.run() is not preempted;
                # shutdown() then only waits for it with a bounded join.
                if self._grid_cancel.is_set():
                    self._emit("grid_failed", reason="cancelled", log=str(log_path))
                    return
                run = self.backend.run(worker_argv, timeout=timeout, log_name=log_path.stem)
                self._emit("grid_shown" if run.returncode == 0 else "grid_failed",
                           returncode=run.returncode, log=str(log_path))
            except Exception as exc:
                self._emit("grid_failed", error=str(exc), log=str(log_path))

        thread = threading.Thread(target=_worker, daemon=True)
        self._register_worker(thread)
        thread.start()
        self._emit("grid_started", log=str(log_path))
        return {"ok": True, "log": str(log_path), "thread": thread}

    def needs_driver_apply(self,prefs):
        """Read-only comparison; optics remains separately staged."""
        section=suite_settings.DriverSettingStore().read().get('driver_psvrpc',{})
        expected=suite_settings.driver_keys_for(prefs)
        return any(section.get(k)!=v for k,v in expected.items()) or (
            not prefs['fallback_3dof'] and section.get('positionFallback3DofEnabled',False))

    def apply_driver(self, prefs, restart=None, apply_optics=False, store=None):
        """Stop runtime, apply owned driver keys, restart only if it ran."""
        from .settings import DriverSettingStore
        cleaned = suite_settings.validate_preferences(prefs)
        with self.control:
            if not self._require_ownership():
                return {"ok": False, "reason": "ownership_denied"}
            try:
                was_running = bool(self._processes() & {"vrserver", "vrcompositor"})
            except RuntimeError as exc:
                self._emit("processes_unknown", error=str(exc))
                return {"ok": False, "reason": "processes_unknown"}
            if was_running and not self.backend.quit_runtime(wait_s=QUIT_WAIT_S):
                self._emit("quit_timeout", wait_s=QUIT_WAIT_S)
                return {"ok": False, "reason": "quit_timeout"}
            journal = (store or DriverSettingStore(root=self.root)).apply(
                cleaned, apply_optics=apply_optics)
            self.last_driver_applied = {
                "utc": journal.get("utc"), "settings": journal.get("settings"),
                "changed_keys": list(journal.get("changed_keys", [])),
                "owned_keys": list(journal.get("owned_keys", [])),
            }
            self._emit("driver_applied", changed_keys=list(journal.get("changed_keys", [])))
            should_restart = restart if restart is not None else was_running
            restarted = False
            if should_restart:
                restarted = bool(self.start_vr(cleaned))
                if not restarted:
                    return {"ok": False, "reason": "restart_failed",
                            "journal": journal, "restarted": False}
            return {"ok": True, "journal": journal, "restarted": restarted}

    def shutdown(self, timeout=5.0):
        """Bounded worker join + backend reap (B11-02); idempotent.

        Signals the show_grid cancel Event, joins live registered workers
        within ``timeout`` seconds total (deadline via time.monotonic, so a
        fake injected clock cannot stretch the wait), then reaps terminated
        popen handles via ``backend.shutdown()`` (B11-01, never kills).
        Never acquires ``control``: a start_vr holding it for minutes must
        not starve shutdown. Workers already blocked in ``backend.run()``
        are waited at most until the deadline, then left orphaned as
        daemons. Returns the number of still-alive workers. Backend
        without ``shutdown()`` (older fakes) is tolerated. No
        signal/atexit handlers are installed here: process-exit policy is
        a future UI decision, the UI must call shutdown()/close().
        """
        try:
            self._grid_cancel.set()
        except Exception:
            pass
        try:
            timeout_s = float(timeout)
        except Exception:
            timeout_s = 5.0
        if not timeout_s >= 0:
            timeout_s = 5.0
        try:
            with self._workers_lock:
                threads = [t for t in list(self._workers)
                           if getattr(t, "is_alive", lambda: False)()]
        except Exception:
            threads = []
        if threads:
            deadline = time.monotonic() + timeout_s
            for thread in threads:
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    thread.join(timeout=remaining)
                except Exception:
                    continue
        # Reap terminated popen handles (B11-01 reuse; never kills).
        try:
            backend_shutdown = getattr(self.backend, "shutdown", None)
            if callable(backend_shutdown):
                backend_shutdown()
        except Exception:
            pass
        try:
            with self._workers_lock:
                alive = sum(1 for t in list(self._workers)
                            if getattr(t, "is_alive", lambda: False)())
        except Exception:
            alive = 0
        return alive

    def close(self):
        # B11-02: best-effort bounded shutdown (joins workers, reaps
        # terminated popen handles via B11-01, never kills), then release
        # the suite lock. Idempotent: safe to call repeatedly. Backend
        # errors never block the lock release.
        try:
            self.shutdown()
        except Exception:
            pass
        try:
            self.lock.release()
        except Exception:
            pass
