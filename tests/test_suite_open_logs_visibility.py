"""Regressione B12-03: on_open_logs ramo posix deve pinnare creationflags.

Difetto: unico Popen della suite senza CREATE_NO_WINDOW (tutti gli altri
pinnati e coperti in test_suite_subprocess_visibility.py), zero copertura
su on_open_logs. Fix: prima regression test che fallisce (mock + assert
flag), poi il fix con lo stesso pattern degli altri moduli
(getattr(subprocess, "CREATE_NO_WINDOW", 0)).

Stile headless esistente: SuiteApp senza Tk (__new__), FakeVar, mock
Popen/os.name/os.startfile. Niente Tk/display reale/HW/aperture reali.
"""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.psvr_suite import app as suite_app

EXPECTED = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def assert_hidden(test, kwargs):
    test.assertIn("creationflags", kwargs)
    test.assertEqual(kwargs["creationflags"], EXPECTED)
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        test.assertNotEqual(kwargs["creationflags"], 0)


class FakeVar:
    def __init__(self, value=""):
        self._v = value

    def get(self):
        return self._v

    def set(self, value):
        self._v = value


def make_shell(logs_dir):
    app = suite_app.SuiteApp.__new__(suite_app.SuiteApp)
    app.logs_dir = Path(logs_dir)
    app.lang = "it"
    app._L = lambda key, **kw: key  # noqa: E731
    app.var_msg = FakeVar("")
    return app


class OnOpenLogsVisibilityTests(unittest.TestCase):
    def test_posix_popen_hides_console(self):
        # Ramo posix: xdg-open via Popen con creationflags pinnato,
        # come runtime.py/settings.py/deploy.py.
        app = make_shell("/tmp/logs-test")
        with mock.patch.object(suite_app.os, "name", "posix"):
            with mock.patch.object(suite_app.subprocess, "Popen") as popen:
                app.on_open_logs()
        self.assertTrue(popen.called, "Popen non chiamato nel ramo posix")
        argv, kwargs = popen.call_args[0][0], popen.call_args[1]
        self.assertEqual(argv, ["xdg-open", str(app.logs_dir)])
        assert_hidden(self, kwargs)

    def test_nt_uses_startfile_without_popen(self):
        # Ramo Windows: os.startfile via mock (mai apertura reale),
        # nessun Popen.
        app = make_shell("C:\\logs-test")
        with mock.patch.object(suite_app.os, "name", "nt"):
            with mock.patch.object(
                suite_app.os, "startfile", create=True
            ) as startfile:
                with mock.patch.object(
                    suite_app.subprocess, "Popen"
                ) as popen:
                    app.on_open_logs()
        startfile.assert_called_once_with(str(app.logs_dir))
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
