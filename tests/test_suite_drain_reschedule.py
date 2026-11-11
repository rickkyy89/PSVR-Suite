"""Regressione B12-04: _drain_display sopravvive se il ramo except solleva.

Difetto: `var_msg.set` non protetto nel ramo `except` — se solleva
(widget distrutto), `root.after(120)` mai rischedulato -> drain morto,
UI congelata. Fix: `set` in try/except + `after` in `finally`.

Stile headless test_suite_app_model.py: SuiteApp senza Tk (__new__),
FakeVar/FakeRoot, coda reale. Niente Tk/display reale/HW.
"""

import queue
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

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


class RaisingVar(FakeVar):
    """Simula widget distrutto: set solleva sempre (come Tk TclError)."""

    def set(self, value):
        raise RuntimeError("widget distrutto")


class FakeRoot:
    """Registra le rischedulazioni after senza Tk reale."""

    def __init__(self):
        self.calls = []

    def after(self, delay, func):
        self.calls.append((delay, func))
        return "after-id-%d" % len(self.calls)


def make_shell(var_msg=None):
    app = suite_app.SuiteApp.__new__(suite_app.SuiteApp)
    app.closing = False
    app.display = queue.Queue()
    app.var_msg = var_msg if var_msg is not None else FakeVar("")
    app._L = lambda key, **kw: key if not kw else "%s|%s" % (key, kw)
    app.root = FakeRoot()
    return app


class DrainRescheduleTests(unittest.TestCase):
    def test_except_sollevante_rischedula_comunque(self):
        # Evento che fa sollevare _handle_event + var_msg.set che
        # solleva a sua volta: senza fix after mai chiamato (drain morto).
        app = make_shell(var_msg=RaisingVar(""))
        app._handle_event = Mock(side_effect=RuntimeError("boom"))
        app.display.put_nowait(("task_done", {"ok": True}))
        app._drain_display()  # non deve propagare
        self.assertTrue(app.display.empty(), "evento deve essere consumato")
        self.assertEqual(len(app.root.calls), 1)
        delay, func = app.root.calls[0]
        self.assertEqual(delay, 120)
        self.assertEqual(func, app._drain_display)
        # Il loop sopravvive: seconda invocazione (callback Tk)
        # rischedula ancora.
        app._handle_event = Mock(return_value=None)
        app._drain_display()
        self.assertEqual(len(app.root.calls), 2)

    def test_caso_normale_invariato(self):
        # Nessuna eccezione: un evento consumato, una rischedulazione,
        # var_msg intatta (handler mock non la tocca).
        app = make_shell()
        app._handle_event = Mock(return_value=None)
        app.display.put_nowait(("task_done", {"ok": True}))
        app._drain_display()
        app._handle_event.assert_called_once_with("task_done", {"ok": True})
        self.assertTrue(app.display.empty())
        self.assertEqual(app.var_msg.get(), "")
        self.assertEqual(len(app.root.calls), 1)
        self.assertEqual(app.root.calls[0][0], 120)
        self.assertEqual(app.root.calls[0][1], app._drain_display)

    def test_coda_vuota_rischedula(self):
        # Nessun evento: loop comunque vivo.
        app = make_shell()
        app._handle_event = Mock()
        app._drain_display()
        app._handle_event.assert_not_called()
        self.assertEqual(len(app.root.calls), 1)

    def test_closing_non_rischedula(self):
        # Comportamento closing invariato: ritorno senza after.
        app = make_shell()
        app.closing = True
        app._handle_event = Mock()
        app.display.put_nowait(("task_done", {"ok": True}))
        app._drain_display()
        app._handle_event.assert_not_called()
        self.assertEqual(app.root.calls, [])
        self.assertFalse(app.display.empty())


if __name__ == "__main__":
    unittest.main()
