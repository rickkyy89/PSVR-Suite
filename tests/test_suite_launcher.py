"""Test read-only launcher suite: foreground esistente, argv, errori. Niente HW/GUI."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.psvr_suite import psvr_suite_launcher as L

FAKE_REPO = Path("C:/fake/repo")


class FakeWin32:
    """Surrogato user32: registra restore/foreground senza WinAPI."""

    def __init__(self, hwnd=123, iconic=False, raise_find=False):
        self._hwnd = hwnd
        self._iconic = iconic
        self._raise_find = raise_find
        self.calls = []

    def FindWindowW(self, cls, title):
        self.calls.append(("find", cls, title))
        if self._raise_find:
            raise OSError("find failed")
        return self._hwnd

    def IsIconic(self, hwnd):
        self.calls.append(("iconic", hwnd))
        return self._iconic

    def ShowWindow(self, hwnd, cmd):
        self.calls.append(("show", hwnd, cmd))
        return True

    def SetForegroundWindow(self, hwnd):
        self.calls.append(("foreground", hwnd))
        return True


class BuildArgvTest(unittest.TestCase):
    def test_default_nessun_vr_implicito(self):
        argv = L.build_suite_argv(FAKE_REPO, [])
        self.assertEqual(argv[:2], ["--root", str(FAKE_REPO)])
        self.assertNotIn("--start-vr", argv)
        self.assertNotIn("--attach", argv)

    def test_passthrough_attach_tab(self):
        argv = L.build_suite_argv(FAKE_REPO, ["--attach", "--tab", "Camera"])
        self.assertEqual(argv, ["--root", str(FAKE_REPO), "--attach", "--tab", "Camera"])

    def test_root_esplicito_rispettato(self):
        argv = L.build_suite_argv(FAKE_REPO, ["--root", "C:/altro", "--tab", "Camera"])
        self.assertEqual(argv.count("--root"), 1)
        self.assertEqual(argv[1], "C:/altro")


class BringToFrontTest(unittest.TestCase):
    def test_trovata_minimizzata_restore_e_foreground(self):
        fake = FakeWin32(hwnd=77, iconic=True)
        self.assertTrue(L.bring_existing_to_front(win32=fake))
        self.assertIn(("show", 77, L.SW_RESTORE), fake.calls)
        self.assertIn(("foreground", 77), fake.calls)
        self.assertEqual(L.SW_RESTORE, 9)

    def test_trovata_non_minimizzata_solo_foreground(self):
        fake = FakeWin32(hwnd=77, iconic=False)
        self.assertTrue(L.bring_existing_to_front(win32=fake))
        self.assertFalse([c for c in fake.calls if c[0] == "show"])
        self.assertIn(("foreground", 77), fake.calls)

    def test_non_trovata_nessun_foreground(self):
        fake = FakeWin32(hwnd=0)
        self.assertFalse(L.bring_existing_to_front(win32=fake))
        self.assertFalse([c for c in fake.calls if c[0] == "foreground"])

    def test_titolo_esatto_suite(self):
        fake = FakeWin32(hwnd=5)
        L.bring_existing_to_front(win32=fake)
        self.assertIn(("find", None, "PSVR Suite"), fake.calls)

    def test_find_che_fallisce_ritorna_false(self):
        fake = FakeWin32(raise_find=True)
        self.assertFalse(L.bring_existing_to_front(win32=fake))


class MainTest(unittest.TestCase):
    def tearDown(self):
        while str(FAKE_REPO) in sys.path:
            sys.path.remove(str(FAKE_REPO))

    def _rec(self):
        logs, shown = [], []
        return logs, shown

    def test_esistente_ritorna_zero_senza_suite(self):
        chiamate, logs, shown = [], [], []
        code = L.main([], _bring=lambda: True,
                      _suite_main=lambda a: chiamate.append(a) or 0,
                      _repo=FAKE_REPO, _log=logs.append,
                      _show=lambda t, m: shown.append((t, m)))
        self.assertEqual(code, 0)
        self.assertEqual(chiamate, [])
        self.assertEqual(shown, [])

    def test_avvio_passthrough_argv(self):
        ricevuti = []
        code = L.main(["--attach", "--tab", "Camera"], _bring=lambda: False,
                      _suite_main=lambda a: ricevuti.append(a) or 0,
                      _repo=FAKE_REPO, _log=lambda t: None,
                      _show=lambda t, m: None)
        self.assertEqual(code, 0)
        self.assertEqual(ricevuti, [["--root", str(FAKE_REPO), "--attach", "--tab", "Camera"]])

    def test_lock_singleton_avviso_visibile(self):
        shown = []
        code = L.main([], _bring=lambda: False, _suite_main=lambda a: 3,
                      _repo=FAKE_REPO, _log=lambda t: None,
                      _show=lambda t, m: shown.append((t, m)))
        self.assertEqual(code, 3)
        self.assertEqual(len(shown), 1)

    def test_eccezione_log_e_messagebox_codice_1(self):
        def boom(argv):
            raise RuntimeError("nope")

        logs, shown = [], []
        code = L.main([], _bring=lambda: False, _suite_main=boom,
                      _repo=FAKE_REPO, _log=logs.append,
                      _show=lambda t, m: shown.append((t, m)))
        self.assertEqual(code, 1)
        self.assertEqual(len(logs), 1)
        self.assertEqual(len(shown), 1)

    def test_systemexit_argomenti_codice_2(self):
        def bad(argv):
            raise SystemExit(2)

        shown = []
        code = L.main(["--tab", "Bogus"], _bring=lambda: False, _suite_main=bad,
                      _repo=FAKE_REPO, _log=lambda t: None,
                      _show=lambda t, m: shown.append((t, m)))
        self.assertEqual(code, 2)
        self.assertEqual(len(shown), 1)


if __name__ == "__main__":
    unittest.main()
