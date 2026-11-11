# SPDX-License-Identifier: MIT
"""Regressioni meccaniche B11-08 / B11-09 / B11-00 (P3 + nota documentale).

Solo mock/offline: nessun hardware, nessuno SteamVR, nessun Tk, nessuna
scrittura fuori da directory temporanee. Nessun push, nessuna logica di
produzione toccata qui.

* B11-08: update_settings serializza i produttori sotto self._lock
  (get_nowait+put_nowait coperti); stop(timeout) invalido -> ValueError
  prima del join; realign/reset_relative solo documentati.
* B11-09: stop_vr resetta anche i mode stale vr-*; --optics con resolve
  anticipata (alias + percorsi custom ammessi); smoke con prefs/out
  validati prima di qualsiasi hardware. API submit/show_grid invariate.
* B11-00: hardware_smoke dichiara rig reale + exit code (solo docs).
"""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.psvr_suite.tracking import TrackingWorker, default_prefs


def _lock_held(lock):
    acquired = lock.acquire(blocking=False)
    if acquired:
        lock.release()
        return False
    return True


class UpdateSettingsLockTests(unittest.TestCase):
    """B11-08: get_nowait+put_nowait sotto self._lock (produttori serializzati)."""

    def test_put_get_put_all_under_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = TrackingWorker(tmp, lambda e: None)
            release = threading.Event()

            def fake_run(self, cleaned):
                release.wait(timeout=15)

            observed = {"put": [], "get": []}
            orig_put = worker._settings.put_nowait
            orig_get = worker._settings.get_nowait

            def put(item):
                observed["put"].append(_lock_held(worker._lock))
                return orig_put(item)

            def get():
                observed["get"].append(_lock_held(worker._lock))
                return orig_get()

            with patch.object(TrackingWorker, "_run", fake_run):
                self.assertTrue(worker.start(default_prefs()))
                try:
                    worker._settings.put_nowait = put
                    worker._settings.get_nowait = get
                    first = dict(default_prefs())
                    first["exposure"] = 10
                    second = dict(default_prefs())
                    second["exposure"] = 11
                    self.assertTrue(worker.update_settings(first))
                    # Coda maxsize=1 piena: secondo update percorre
                    # put(Full)->get->put, tutto sotto lock. Il wrapper
                    # registra anche il put fallito (prima del Full).
                    self.assertTrue(worker.update_settings(second))
                    self.assertEqual(observed["put"], [True, True, True],
                                     "put_nowait deve avvenire sotto self._lock")
                    self.assertEqual(observed["get"], [True],
                                     "get_nowait deve avvenire sotto self._lock")
                finally:
                    release.set()
                    self.assertTrue(worker.stop(timeout=10.0))

    def test_concurrent_producers_no_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = TrackingWorker(tmp, lambda e: None)
            release = threading.Event()

            def fake_run(self, cleaned):
                release.wait(timeout=30)

            errors = []
            with patch.object(TrackingWorker, "_run", fake_run):
                self.assertTrue(worker.start(default_prefs()))
                try:
                    def produce(seed):
                        try:
                            for i in range(25):
                                prefs = dict(default_prefs())
                                prefs["exposure"] = (seed + i) % 256
                                self.assertTrue(worker.update_settings(prefs))
                        except Exception as exc:  # pragma: no cover - diagnostica
                            errors.append(exc)

                    threads = [threading.Thread(target=produce, args=(s,), daemon=True)
                               for s in range(8)]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join(timeout=30.0)
                        self.assertFalse(thread.is_alive(), "producer hung")
                    self.assertEqual(errors, [])
                finally:
                    release.set()
                    self.assertTrue(worker.stop(timeout=10.0))


class StopTimeoutValidationTests(unittest.TestCase):
    """B11-08: stop(timeout) invalido -> ValueError prima del join."""

    def test_invalid_timeout_raises_without_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = TrackingWorker(tmp, lambda e: None)
            for bad in (float("nan"), float("inf"), float("-inf"),
                        -1, -0.5, "x", None, object()):
                with self.subTest(timeout=repr(bad)):
                    with self.assertRaises(ValueError):
                        worker.stop(timeout=bad)

    def test_valid_timeout_ok_without_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = TrackingWorker(tmp, lambda e: None)
            for good in (0, 0.0, 5, 5.0):
                with self.subTest(timeout=repr(good)):
                    self.assertTrue(worker.stop(timeout=good))


class RealignDocsTests(unittest.TestCase):
    """B11-08(c): realign/reset_relative solo documentati, comportamento invariato."""

    def test_realign_reset_only_set_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = TrackingWorker(tmp, lambda e: None)
            self.assertTrue(worker.realign())
            self.assertTrue(worker._realign.is_set())
            self.assertTrue(worker.reset_relative())
            self.assertTrue(worker._recenter.is_set())
            # Nessun guard sullo stato: resta documentazione, non errore.
            self.assertIn("B11-08", (worker.realign.__doc__ or ""))
            self.assertIn("B11-08", (worker.reset_relative.__doc__ or ""))


class StopVrStaleModeTests(unittest.TestCase):
    """B11-09(a): stop_vr resetta anche i mode stale vr-* (es. vr-attached)."""

    def _controller(self, backend):
        from tools.psvr_suite import runtime as R
        ctrl = R.RuntimeController(root=None, on_event=lambda e: None,
                                   backend=backend, sleep=lambda s: None,
                                   clock=lambda: 0.0)

        class FakeLock:
            path = ctrl.lock.path
            _held = False

            def acquire(self):
                self._held = True
                return True

            def release(self):
                self._held = False

            def held(self):
                return self._held

        ctrl.lock = FakeLock()  # mai il lock reale della suite
        ctrl.lock.acquire()
        self.addCleanup(ctrl.close)
        return ctrl

    def test_stale_vr_modes_reset_on_quit(self):
        from tools.psvr_suite import runtime as R  # noqa: F401 (import offline-safe)

        class FakeBackend:
            def quit_runtime(self, wait_s=25.0, poll_s=0.5):
                return True

        for stale in ("vr", "vr-attached", "vr-awaiting-tracking"):
            with self.subTest(mode=stale):
                ctrl = self._controller(FakeBackend())
                ctrl.mode = stale
                self.assertTrue(ctrl.stop_vr())
                self.assertEqual(ctrl.mode, "stopped")

    def test_non_vr_mode_preserved(self):
        class FakeBackend:
            def quit_runtime(self, wait_s=25.0, poll_s=0.5):
                return True

        for mode in ("unknown", "cinema", "stopped"):
            with self.subTest(mode=mode):
                ctrl = self._controller(FakeBackend())
                ctrl.mode = mode
                self.assertTrue(ctrl.stop_vr())
                self.assertEqual(ctrl.mode, mode)


class SetupTrialOpticsTests(unittest.TestCase):
    """B11-09(b): --optics con resolve anticipata, percorsi custom ammessi."""

    def test_invalid_optics_rejected_before_writes(self):
        from tools.psvr_suite.setup_trial import main
        with tempfile.TemporaryDirectory() as tmp:
            backup = str(Path(tmp) / "new-backup-dir")
            with self.assertRaises(SystemExit) as ctx:
                main(["--backup-dir", backup, "--optics", "   "])
            self.assertEqual(ctx.exception.code, 2)
            self.assertFalse((Path(tmp) / "new-backup-dir").exists(),
                             "nessuna scrittura su --optics invalido")

    def test_custom_path_preserved_by_parser_and_resolver(self):
        from tools.psvr_suite.settings import resolve_optics_profile
        from tools.psvr_suite.setup_trial import argument_parser
        parsed = argument_parser().parse_args(
            ["--backup-dir", "unused-offline-fixture",
             "--optics", "configurations/custom_optics.json"])
        self.assertEqual(parsed.optics, "configurations/custom_optics.json")
        mode, _ = resolve_optics_profile(parsed.optics, root=Path("unused-root"))
        self.assertEqual(mode, "profile")

    def test_known_aliases_still_accepted(self):
        from tools.psvr_suite.settings import OPTICS_ALIASES, resolve_optics_profile
        for alias in OPTICS_ALIASES:
            with self.subTest(optics=alias):
                mode, _ = resolve_optics_profile(alias, root=Path("unused-root"))
                self.assertIn(mode, ("current", "identity", "profile"))


class HardwareSmokeGuardsTests(unittest.TestCase):
    """B11-09(c)(d): prefs richiesto + parent --out chiaro, prima dell'hardware."""

    def test_missing_prefs_fails_before_hardware(self):
        from tools.psvr_suite import hardware_smoke as smoke
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "result.json")
            with self.assertRaises(ValueError) as ctx:
                smoke.main(["--root", tmp, "--out", out])
            self.assertIn("preferences file not found", str(ctx.exception))
            self.assertFalse(Path(out).exists())

    def test_missing_out_parent_fails_before_hardware(self):
        from tools.psvr_suite import hardware_smoke as smoke
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefs_dir = root / "configurations"
            prefs_dir.mkdir()
            (prefs_dir / "psvr_suite_user.json").write_text("{}", encoding="utf-8")
            out = str(root / "no-such-dir" / "result.json")
            with self.assertRaises(ValueError) as ctx:
                smoke.main(["--root", tmp, "--out", out])
            self.assertIn("output parent must exist", str(ctx.exception))


class HardwareSmokeDocsTests(unittest.TestCase):
    """B11-00: docstring dichiara rig reale + exit code (nessuna logica cambiata)."""

    def test_docstring_labels_real_hardware(self):
        from tools.psvr_suite import hardware_smoke as smoke
        doc = (smoke.__doc__ or "").lower()
        self.assertIn("real", doc)
        self.assertIn("rig", doc)
        self.assertIn("exit", doc)


if __name__ == "__main__":
    unittest.main()
