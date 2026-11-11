"""Regressione B12-02: on_close non deve toccare Tk dal thread background.

Difetto: _shutdown_bg su thread chiamava root.after(0, destroy) — Tk non e'
thread-safe (corruzione/crash intermittente in chiusura). Fix: il bg fa solo
serial.shutdown() e setta un Event; poll_finish (main, via root.after) chiama
destroy quando l'evento e' settato. Nessun join sul main; semantica invariata
(stop tracking posseduto + runtime.close(), mai fermare SteamVR esterno).

Stile headless test_suite_run_serial_nonblocking.py: SuiteApp senza Tk
(__new__), FakeRoot che registra il thread chiamante di after/destroy.
Niente Tk/display reale/HW.
"""

import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.psvr_suite import app as suite_app


class FakeVar:
    def __init__(self, value=""):
        self._v = value

    def get(self):
        return self._v

    def set(self, value):
        self._v = value


class FakeRoot:
    """Tk finto: registra il thread chiamante di after/destroy."""

    def __init__(self):
        self._lock = threading.Lock()
        self.after_calls = []  # [(caller_ident, delay_ms, func)]
        self.destroy_callers = []
        self.destroyed = threading.Event()

    def after(self, delay, func):
        with self._lock:
            self.after_calls.append((threading.current_thread().ident, delay, func))
        return "after-id-%d" % len(self.after_calls)

    def destroy(self):
        with self._lock:
            self.destroy_callers.append(threading.current_thread().ident)
        self.destroyed.set()


class FakeSerial:
    """SerialWorker finto: task sincrono, shutdown con thread reale da joinare."""

    def __init__(self, release=None):
        self.submitted = []
        self.shutdown_caller = None
        self.shutdown_called = threading.Event()
        self.shutdown_done = threading.Event()
        self._release = release  # Event opzionale: se settato, shutdown si sblocca

    def submit(self, label, func, drop_if_pending=False):
        self.submitted.append(label)
        func()  # sincrono: come il worker che esegue final_task
        return True

    def shutdown(self, timeout=5.0):
        self.shutdown_caller = threading.current_thread().ident
        self.shutdown_called.set()
        try:
            if self._release is not None:
                self._release.wait(timeout=timeout)
        finally:
            self.shutdown_done.set()


class FakeTracking:
    def __init__(self):
        self.stops = 0

    def stop(self):
        self.stops += 1
        return True


class FakeRuntime:
    def __init__(self):
        self.closes = 0
        self.stop_vr_calls = 0

    def close(self):
        self.closes += 1

    def stop_vr(self):  # mai chiamato in chiusura UI: SteamVR esterno resta su
        self.stop_vr_calls += 1
        raise AssertionError("on_close non deve fermare SteamVR esterno")


def make_shell(serial):
    app = suite_app.SuiteApp.__new__(suite_app.SuiteApp)
    app.lang = "it"
    app._L = lambda key, **kw: key  # noqa: E731
    app.closing = False
    app.var_msg = FakeVar("")
    app._set_buttons = lambda enabled: None  # noqa: E731
    app._poll_stop = threading.Event()
    app.serial = serial
    app.tracking = FakeTracking()
    app.runtime = FakeRuntime()
    app.root = FakeRoot()
    return app


def drive_main_loop(root, timeout=5.0):
    """Esegue i callback accodati via after sul thread corrente (main)."""
    deadline = time.time() + timeout
    ran = 0
    while time.time() < deadline:
        with root._lock:
            pending = list(root.after_calls[ran:])
        for _, _, func in pending:
            func()  # sul thread chiamante = main simulato
            ran += 1
            if root.destroyed.is_set():
                return True
        if root.destroyed.is_set():
            return True
        time.sleep(0.02)
    return root.destroyed.is_set()


class OnCloseThreadTests(unittest.TestCase):
    def test_destroy_solo_main_e_bg_senza_tk(self):
        main_ident = threading.current_thread().ident
        serial = FakeSerial()
        app = make_shell(serial)
        app.on_close()
        self.assertTrue(drive_main_loop(app.root, timeout=5.0),
                        "destroy mai chiamato dal main")
        # destroy: una sola volta, solo dal main.
        self.assertEqual(len(app.root.destroy_callers), 1)
        self.assertEqual(app.root.destroy_callers[0], main_ident)
        # after: mai chiamato dal bg (solo main). Il bg non tocca Tk.
        bg_after = [c for c in app.root.after_calls if c[0] != main_ident]
        self.assertEqual(bg_after, [])
        # shutdown: eseguito dal bg (join fuori dal main), mai sul main.
        self.assertTrue(serial.shutdown_called.is_set())
        self.assertIsNotNone(serial.shutdown_caller)
        self.assertNotEqual(serial.shutdown_caller, main_ident)
        # Semantica invariata: stop posseduto + runtime.close(), mai stop_vr.
        self.assertEqual(app.tracking.stops, 1)
        self.assertEqual(app.runtime.closes, 1)
        self.assertEqual(app.runtime.stop_vr_calls, 0)
        self.assertIn("stop-posseduto", serial.submitted)

    def test_nessun_join_sul_main_shutdown_lento(self):
        # shutdown bloccato: on_close + primo poll devono tornare subito,
        # destroy arriva solo dopo lo sblocco, comunque dal main.
        main_ident = threading.current_thread().ident
        release = threading.Event()
        serial = FakeSerial(release=release)
        app = make_shell(serial)
        t0 = time.time()
        app.on_close()
        dt_close = time.time() - t0
        self.assertLess(dt_close, 2.0, "on_close bloccato (join sul main?)")
        # Primo poll sul main: avvia il bg che resta bloccato in shutdown.
        with app.root._lock:
            first = list(app.root.after_calls)
        self.assertEqual(len(first), 1)
        first[0][2]()  # poll_finish sul main
        self.assertTrue(serial.shutdown_called.wait(timeout=5.0))
        self.assertNotEqual(serial.shutdown_caller, main_ident)
        time.sleep(0.2)
        # Shutdown ancora bloccato -> nessun destroy, nessun after dal bg.
        self.assertFalse(app.root.destroyed.is_set())
        bg_after = [c for c in app.root.after_calls if c[0] != main_ident]
        self.assertEqual(bg_after, [])
        # Sblocco: il main osserva l'evento e distrugge.
        release.set()
        self.assertTrue(drive_main_loop(app.root, timeout=5.0))
        self.assertEqual(app.root.destroy_callers, [main_ident])
        self.assertEqual(app.tracking.stops, 1)
        self.assertEqual(app.runtime.closes, 1)
        self.assertEqual(app.runtime.stop_vr_calls, 0)


if __name__ == "__main__":
    unittest.main()
