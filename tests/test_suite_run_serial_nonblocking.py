"""Regressione B12-01: il wrapper _run_serial non deve mai bloccare il worker.

Difetto: display.put bloccante nel wrapper; coda piena (512) + closing
(drain fermo) -> worker bloccato per sempre, final_task mai eseguita,
poll_finish infinito. Fix: put_nowait + drop su queue.Full.

Stile headless test_suite_app_model.py: SuiteApp senza Tk (__new__),
FakeVar, coda reale piccola. Niente Tk/display reale/HW.
"""

import queue
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.psvr_suite import settings as backend_settings
from tools.psvr_suite import app as suite_app
from tools.psvr_suite.app import AppModel


class FakeVar:
    def __init__(self, value=""):
        self._v = value

    def get(self):
        return self._v

    def set(self, value):
        self._v = value

    def configure(self, **kwargs):
        pass


class _CapturingSerial:
    """SerialWorker finto: cattura il wrapper senza thread/HW."""

    def __init__(self):
        self.funcs = []

    def submit(self, label, func, drop_if_pending=False):
        self.funcs.append(func)
        return True


def make_shell(maxsize=512):
    app = suite_app.SuiteApp.__new__(suite_app.SuiteApp)
    app.model = AppModel(backend_settings.default_preferences())
    app.lang = "it"
    app.closing = False
    app._pending = 0
    app._pending_lock = threading.Lock()
    app.display = queue.Queue(maxsize=maxsize)
    app.var_msg = FakeVar("")
    app._set_buttons = lambda enabled: None  # noqa: E731
    serial = _CapturingSerial()
    app.serial = serial
    return app, serial


def run_guarded(func, timeout=5.0):
    """Esegue func su thread daemon con join limitato; ritorna (thread, errori)."""
    errors = []

    def target():
        try:
            func()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    return thread, errors


class RunSerialNonblockingTests(unittest.TestCase):
    def test_success_coda_piena_closing_non_blocca(self):
        # Scenario deadlock: task sottomesso, poi coda riempita e closing
        # (drain fermo) -> il wrapper deve droppare l'evento senza bloccare.
        app, serial = make_shell(maxsize=1)
        self.assertTrue(app._run_serial("etichetta", lambda: True))
        wrapper = serial.funcs[0]
        app.display.put_nowait(("riempi", {}))
        app.closing = True
        thread, errors = run_guarded(wrapper)
        self.assertFalse(thread.is_alive(), "wrapper bloccato su coda piena")
        self.assertEqual(errors, [])
        # Evento droppato: in coda resta solo il riempitivo.
        kind, _ = app.display.get_nowait()
        self.assertEqual(kind, "riempi")
        self.assertTrue(app.display.empty())

    def test_errore_coda_piena_closing_non_blocca(self):
        # Stesso scenario sul ramo failure: nessuna eccezione, nessun blocco.
        app, serial = make_shell(maxsize=1)

        def boom():
            raise RuntimeError("boom")

        self.assertTrue(app._run_serial("etichetta", boom))
        wrapper = serial.funcs[0]
        app.display.put_nowait(("riempi", {}))
        app.closing = True
        thread, errors = run_guarded(wrapper)
        self.assertFalse(thread.is_alive(), "wrapper bloccato su coda piena")
        self.assertEqual(errors, [])
        kind, _ = app.display.get_nowait()
        self.assertEqual(kind, "riempi")
        self.assertTrue(app.display.empty())

    def test_success_caso_normale_invariato(self):
        app, serial = make_shell(maxsize=512)
        self.assertTrue(app._run_serial("etichetta", lambda: True))
        wrapper = serial.funcs[0]
        thread, errors = run_guarded(wrapper)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        kind, payload = app.display.get_nowait()
        self.assertEqual(kind, "task_done")
        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload.get("label"), "etichetta")

    def test_errore_caso_normale_invariato(self):
        app, serial = make_shell(maxsize=512)

        def boom():
            raise RuntimeError("boom")

        self.assertTrue(app._run_serial("etichetta", boom))
        wrapper = serial.funcs[0]
        thread, errors = run_guarded(wrapper)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        kind, payload = app.display.get_nowait()
        self.assertEqual(kind, "task_done")
        self.assertFalse(payload.get("ok"))
        self.assertIn("boom", payload.get("errore", ""))


if __name__ == "__main__":
    unittest.main()
