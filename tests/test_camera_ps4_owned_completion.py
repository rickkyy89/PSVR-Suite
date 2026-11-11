"""Atomic commit/result regressions. All barriers are fake-native responses.

Never loads a DLL/COM capture. Actual C CAS bodies have separate native fixtures.
"""
import threading
import time
import unittest

from tools.psvr_camera.camera_backend import CameraError
from tests import test_camera_ps4_owned_dshow as fixtures
from tests import test_camera_ps4_owned_readiness as readiness

FakeNative = fixtures.FakeNative
TARGETS, ORIGINALS = readiness.TARGETS, readiness.ORIGINALS


class WatchdogDecision(fixtures.FakeDecision):
    """Suspend a watchdog AFTER its pending read, outside the fake atomic lock."""
    def __init__(self):
        super().__init__()
        self.watchdog_ident = None
        self.armed = True
        self.pending_read = threading.Event()
        self.release_status = threading.Event()

    def status(self):
        snapshot = super().status()
        if (snapshot[0] == 0 and self.armed and
                threading.get_ident() == self.watchdog_ident):
            self.armed = False
            self.pending_read.set()
            if not self.release_status.wait(2):
                raise RuntimeError("watchdog fixture barrier not released")
        return snapshot  # intentionally stale pending observation


class CompletionTests(unittest.TestCase):
    setUp = readiness.ReadinessTests.setUp
    cleanup_sessions = readiness.ReadinessTests.cleanup_sessions
    cleanup_fakes = fixtures.OwnerTests.cleanup_fakes
    start = fixtures.OwnerTests.start
    decoder = readiness.ReadinessTests.decoder
    start_session = readiness.ReadinessTests.start_session

    def call_async(self, operation):
        result, done = [], threading.Event()
        def run():
            try:
                result.append(("success", operation()))
            except Exception as exc:
                result.append(("error", exc))
            finally:
                done.set()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return result, done, thread

    def test_committed_ack_succeeds_while_owner_paused_before_notification(self):
        fake = FakeNative()
        fake.accept_after_commit_gate = threading.Event()
        source, _ = self.start(fake, initial_controls=TARGETS, restore_controls=False)
        pair = source.next_pair(module=self.decoder())
        result, done, caller = self.call_async(lambda: source.accept_readiness(
            pair["sequence"], pair["controls_revision"], timeout_s=0.1))
        self.assertTrue(fake.accept_committed.wait(1))
        # Owner is STILL suspended inside the fake native ACK, before dispatcher
        # done.set. Completion must not depend on releasing that barrier.
        self.assertTrue(done.wait(0.3))
        self.assertEqual(result, [("success", True)])
        self.assertFalse(fake.accept_after_commit_gate.is_set())
        self.assertIsNone(source.error)
        self.assertEqual(source.controls_status()["transaction"]["state"], "confirmed")
        self.assertTrue(source.report()["readiness_accepted"])
        fake.accept_after_commit_gate.set()
        caller.join(1)
        source.release(1)
        self.assertEqual(fake.values, TARGETS)

    def test_pump_readiness_does_not_fail_when_commit_notification_is_delayed(self):
        fake = FakeNative()
        fake.accept_after_commit_gate = threading.Event()
        session = self.start_session(fake)
        self.assertTrue(fake.accept_committed.wait(1))
        self.assertTrue(session.wait_ready(0.5))
        self.assertFalse(fake.accept_after_commit_gate.is_set())
        self.assertIsNone(session.report()["error"])
        self.assertEqual(session.report()["frames_published"], 1)
        # A later stop is normal shutdown of ALREADY accepted acquisition, not
        # a retroactive failed-open timeout. It must remain bounded/retained.
        began = time.monotonic()
        self.assertFalse(session.stop(0.02))
        self.assertLess(time.monotonic() - began, 0.5)
        self.assertFalse(fake.close_entered.is_set())
        fake.accept_after_commit_gate.set()
        self.assertTrue(session.done.wait(1))
        self.assertTrue(session.stop(1))
        self.assertEqual(fake.values, TARGETS)

    def test_precommit_block_timeout_is_not_misreported_as_committed(self):
        fake = FakeNative()
        fake.accept_gate = threading.Event()
        source, _ = self.start(fake, initial_controls=TARGETS, restore_controls=False)
        pair = source.next_pair(module=self.decoder())
        began = time.monotonic()
        with self.assertRaises(CameraError):
            source.accept_readiness(pair["sequence"], pair["controls_revision"], timeout_s=0.05)
        self.assertLess(time.monotonic() - began, 0.4)
        self.assertTrue(fake.accept_entered.is_set())
        self.assertFalse(fake.accept_committed.is_set())
        self.assertFalse(fake.close_entered.is_set())
        self.assertTrue(source.cleanup_pending)
        self.assertEqual(fake.pending_before, ORIGINALS)
        fake.accept_gate.set()
        source.release(1)
        self.assertEqual(fake.values, ORIGINALS)
        self.assertFalse(fake.accept_committed.is_set())
        self.assertEqual(len({call[1] for call in fake.calls}), 1)

    def test_stop_wins_while_ack_is_blocked_before_atomic_commit(self):
        fake = FakeNative()
        fake.accept_gate = threading.Event()
        session = self.start_session(fake)
        self.assertTrue(fake.accept_entered.wait(1))
        began = time.monotonic()
        self.assertFalse(session.stop(0.02))
        self.assertLess(time.monotonic() - began, 0.5)
        self.assertFalse(fake.close_entered.is_set())
        fake.accept_gate.set()
        self.assertTrue(session.done.wait(1))
        self.assertTrue(session.stop(1))
        self.assertFalse(fake.accept_committed.is_set())
        self.assertEqual(fake.values, ORIGINALS)
        self.assertEqual(session.report()["frames_published"], 0)

    def test_runtime_transaction_has_same_atomic_result_completion(self):
        fake = FakeNative()
        fake.finish_after_commit_gate = threading.Event()
        source, _ = self.start(fake, restore_controls=False)
        result, done, caller = self.call_async(lambda: source.apply_controls(TARGETS, timeout_s=0.1))
        self.assertTrue(fake.finish_committed.wait(1))
        self.assertTrue(done.wait(0.3))
        self.assertEqual(result[0][0], "success")
        self.assertEqual(result[0][1]["state"], "confirmed")
        self.assertFalse(fake.finish_after_commit_gate.is_set())
        self.assertIsNone(source.error)
        fake.finish_after_commit_gate.set()
        caller.join(1)
        source.release(1)
        self.assertEqual(fake.values, TARGETS)

    def test_blocking_com_verification_before_commit_keeps_deadline_and_owner(self):
        fake = FakeNative()
        fake.commit_gate = threading.Event()
        source, _ = self.start(fake, restore_controls=False)
        began = time.monotonic()
        with self.assertRaises(CameraError):
            source.apply_controls(TARGETS, timeout_s=0.1)
        self.assertLess(time.monotonic() - began, 0.5)
        self.assertTrue(fake.commit_entered.is_set())
        self.assertFalse(fake.finish_committed.is_set())
        self.assertFalse(fake.close_entered.is_set())
        self.assertTrue(source.cleanup_pending)
        fake.commit_gate.set()
        source.release(1)
        self.assertEqual(fake.values, ORIGINALS)
        self.assertEqual(len({call[1] for call in fake.calls}), 1)

    def watchdog_race(self, *, startup, commit_wins):
        fake = FakeNative()
        if startup:
            fake.accept_gate = threading.Event()
            fake.accept_after_commit_gate = threading.Event()
            before, entered = fake.accept_gate, fake.accept_entered
            after, committed = fake.accept_after_commit_gate, fake.accept_committed
            source, _ = self.start(fake, timeout_s=3, initial_controls=TARGETS, restore_controls=False)
            pair = source.next_pair(module=self.decoder())
            operation = lambda: source.accept_readiness(pair["sequence"], pair["controls_revision"], timeout_s=2)
        else:
            fake.finish_gate = threading.Event()
            fake.finish_after_commit_gate = threading.Event()
            before, entered = fake.finish_gate, fake.finish_entered
            after, committed = fake.finish_after_commit_gate, fake.finish_committed
            source, _ = self.start(fake, timeout_s=3, restore_controls=False)
            operation = lambda: source.apply_controls(TARGETS, timeout_s=2)
        decision = WatchdogDecision()
        self.decoder_gates.append(decision.release_status)
        # Only the decision metadata is fake; the production owner, watchdog,
        # acceptance/transaction, timeout arbitration and cleanup all execute.
        source._native.make_decision = lambda: decision
        result, caller_done, caller = self.call_async(operation)
        self.assertTrue(entered.wait(1))
        with source._condition:
            source._transaction_deadline = time.perf_counter() - 1
        watchdog_result, watchdog_done = [], threading.Event()
        def watchdog():
            decision.watchdog_ident = threading.get_ident()
            try:
                source._check_deadline()
                watchdog_result.append("done")
            except Exception as exc:
                watchdog_result.append(exc)
            finally:
                watchdog_done.set()
        watcher = threading.Thread(target=watchdog, daemon=True)
        watcher.start()
        self.assertTrue(decision.pending_read.wait(1))
        if commit_wins:
            before.set()
            self.assertTrue(committed.wait(1))
            self.assertFalse(after.is_set())  # owner suspended before publication
        decision.release_status.set()
        self.assertTrue(watchdog_done.wait(1))
        self.assertEqual(watchdog_result, ["done"])
        self.assertTrue(caller_done.wait(1))
        if commit_wins:
            self.assertEqual(result[0][0], "success")
            self.assertTrue(result[0][1] if startup else result[0][1]["state"] == "confirmed")
            self.assertFalse(source._stop.is_set())
            self.assertIsNone(source.error)
            self.assertEqual(source.controls_status()["transaction"]["state"], "confirmed")
            self.assertEqual(decision.status()[0], 1)
        else:
            self.assertEqual(result[0][0], "error")
            self.assertEqual(decision.status()[0], 2)
            self.assertTrue(source._stop.is_set())
            self.assertFalse(committed.is_set())
            self.assertFalse(fake.close_entered.is_set())
            self.assertTrue(source.cleanup_pending)
        before.set()
        after.set()
        caller.join(1)
        watcher.join(1)
        source.release(1)
        self.assertEqual(fake.values, TARGETS if commit_wins else ORIGINALS)

    def test_watchdog_startup_pending_read_then_commit_wins_no_poison(self):
        self.watchdog_race(startup=True, commit_wins=True)

    def test_watchdog_runtime_pending_read_then_commit_wins_no_poison(self):
        self.watchdog_race(startup=False, commit_wins=True)

    def test_watchdog_startup_cancel_wins_restores_originals(self):
        self.watchdog_race(startup=True, commit_wins=False)

    def test_watchdog_runtime_cancel_wins_restores_originals(self):
        self.watchdog_race(startup=False, commit_wins=False)


if __name__ == "__main__":
    unittest.main()
