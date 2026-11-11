# SPDX-License-Identifier: MIT
"""Regressione B11-06: start() deve avvenire sotto lo stesso lock dell'assegnazione.

Difetto: self._thread assegnato sotto lock ma thread.start() fuori -> stop
nel mezzo fotografa un thread mai avviato (join alza, ingoiato, ritorna True)
mentre il worker parte davvero dopo, orfano.

Offline only: nessun hardware/driver/rete/Tk. Solo mock di Thread e _run.
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.psvr_suite.tracking import TrackingWorker, default_prefs

_REAL_THREAD = threading.Thread


def _wait_for(predicate, timeout=5.0, step=0.01):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


class B1106StartUnderLockTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_start_called_while_lock_held(self):
        """start() deve avvenire con self._lock acquisito dal chiamante."""
        worker = TrackingWorker(self.root, lambda e: None)
        seen = {}

        def fake_run(self, cleaned):
            # Worker stub: termina subito senza hardware.
            return None

        class ProbeThread:
            def __init__(self, target, args, daemon, name):
                self._real = _REAL_THREAD(
                    target=target, args=args, daemon=daemon, name=name)

            def start(self):
                # Lock non rientrante: se il chiamante lo detiene, l'acquire
                # non bloccante fallisce. E' il segnale atteso post-fix.
                acquired = worker._lock.acquire(blocking=False)
                try:
                    seen["held"] = not acquired
                finally:
                    if acquired:
                        worker._lock.release()
                self._real.start()

            def is_alive(self):
                return self._real.is_alive()

            def join(self, timeout=None):
                return self._real.join(timeout)

        with patch.object(TrackingWorker, "_run", fake_run), \
             patch("tools.psvr_suite.tracking.threading.Thread", ProbeThread):
            worker.start(default_prefs())
            ok = _wait_for(lambda: "held" in seen, timeout=5.0)
            self.assertTrue(ok, "ProbeThread.start was never called")
            self.assertTrue(seen["held"],
                            "Thread.start() must run under self._lock (B11-06)")
            worker.stop(timeout=5.0)

    def test_stop_during_start_leaves_no_orphan(self):
        """Stop fra assegnazione e start: nessun worker vivo orfano."""
        worker = TrackingWorker(self.root, lambda e: None)
        start_entered = threading.Event()
        allow_start = threading.Event()
        created = {}

        def fake_run(self, cleaned):
            # Simula un worker che, una volta avviato davvero, resta vivo a
            # prescindere dallo stop precedente: pre-fix lo stop fotografa
            # un thread mai avviato (join alza, ingoiato, True illusorio) e
            # questo thread parte orfano dopo. Post-fix lo stop attende il
            # lock e joina il thread avviato (2 s < timeout 10 s).
            # Nessun hardware: solo attesa, nessun I/O.
            time.sleep(2.0)
            return None

        class DelayedThread:
            def __init__(self, target, args, daemon, name):
                self._real = _REAL_THREAD(
                    target=target, args=args, daemon=daemon, name=name)
                created["thread"] = self

            def start(self):
                start_entered.set()
                # Simula start ritardato/intercettato: resta dentro start()
                # finche' il test non lo sblocca.
                if not allow_start.wait(timeout=10.0):
                    raise TimeoutError("test did not release delayed start")
                self._real.start()

            def is_alive(self):
                try:
                    return self._real.is_alive()
                except RuntimeError:
                    # Thread mai avviato: pre-fix stop lo fotografa cosi'.
                    return False

            def join(self, timeout=None):
                return self._real.join(timeout)

        with patch.object(TrackingWorker, "_run", fake_run), \
             patch("tools.psvr_suite.tracking.threading.Thread", DelayedThread):
            starter_result = {}
            starter_exc = {}

            def call_start():
                try:
                    starter_result["ok"] = worker.start(default_prefs())
                except Exception as exc:  # pragma: no cover - diagnostica
                    starter_exc["exc"] = exc

            starter = _REAL_THREAD(target=call_start, daemon=True)
            starter.start()
            self.assertTrue(start_entered.wait(timeout=10.0),
                            "delayed start was never reached")
            # Lo stop avviene mentre start() e' ancora in corso: pre-fix
            # vedrebbe un thread mai avviato e ritornerebbe True illusorio,
            # lasciando poi un orfano vivo; post-fix attende il lock e joina.
            stop_result = {}
            stopper = _REAL_THREAD(
                target=lambda: stop_result.update(
                    {"ok": worker.stop(timeout=10.0)}),
                daemon=True)
            stopper.start()
            time.sleep(0.2)  # lascia lo stop arrivare sul lock di start
            allow_start.set()  # sblocca start(), poi entrambi completano
            starter.join(timeout=10.0)
            stopper.join(timeout=10.0)
            self.assertFalse(starter.is_alive(), "starter hung")
            self.assertFalse(stopper.is_alive(), "stopper hung")
            self.assertNotIn("exc", starter_exc)
            self.assertTrue(starter_result.get("ok") is True)
            self.assertTrue(stop_result.get("ok") is True,
                            "stop must report terminated worker")
            # Nessun orfano: dopo stop=True il thread fotografato deve essere
            # gia' terminato (stop lo ha joinato). Pre-fix lo stop ritorna
            # True illusorio sul thread mai avviato e l'orfano parte dopo:
            # assert immediato, senza attese che ne mascherino la vita breve.
            thread = created.get("thread")
            self.assertIsNotNone(thread, "worker thread was never created")
            self.assertFalse(thread.is_alive(),
                             "orphan worker thread still alive after stop "
                             "(B11-06: stop photographed a never-started "
                             "thread)")
            ok = _wait_for(lambda: worker._thread is None, timeout=10.0)
            self.assertIsNone(worker._thread,
                              "_thread must be None after stop")
            self.assertFalse(worker.status()["running"])


if __name__ == "__main__":
    unittest.main()
