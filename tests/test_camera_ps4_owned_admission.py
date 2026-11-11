"""Set receipt and queue-admission regressions: fake native, no DLL/hardware."""
import copy
import queue
import threading
import time
import unittest
from unittest.mock import patch

from tools.psvr_camera.camera_backend import CameraError
from tools.psvr_camera import ps4_owned_dshow as owned
from tests import test_camera_ps4_owned_dshow as fixtures
from tests import test_camera_ps4_owned_readiness as readiness
from tests import test_camera_ps4_owned_completion as completion


class AdmissionTests(unittest.TestCase):
    setUp = readiness.ReadinessTests.setUp
    cleanup_sessions = readiness.ReadinessTests.cleanup_sessions
    cleanup_fakes = fixtures.OwnerTests.cleanup_fakes
    start = fixtures.OwnerTests.start
    decoder = readiness.ReadinessTests.decoder
    call_async = completion.CompletionTests.call_async

    def test_set_manual_returns_verified_receipt_before_delayed_owner_notification(self):
        fake = fixtures.FakeNative()
        fake.finish_after_commit_gate = threading.Event()
        source, _ = self.start(fake, restore_controls=False)
        result, done, caller = self.call_async(lambda: source.set_manual("exposure", -7, timeout_s=0.1))
        self.assertTrue(fake.finish_committed.wait(1))
        self.assertTrue(done.wait(0.3))
        self.assertEqual(result[0][0], "success")
        self.assertEqual((result[0][1]["value"], result[0][1]["flags"]), (-7, 2))
        self.assertFalse(fake.finish_after_commit_gate.is_set())
        self.assertEqual(source._commands.qsize(), 0)  # no post-commit Get queued behind blocked owner
        self.assertIsNone(source.error)
        self.assertFalse(source._stop.is_set())
        fake.finish_after_commit_gate.set()
        caller.join(1)
        source.release(1)
        self.assertEqual(fake.values["exposure"], -7)

    def test_set_manual_never_issues_a_fallible_postcommit_get(self):
        source, _fake = self.start(restore_controls=False)
        with patch.object(source, "get_control", side_effect=CameraError("post-commit RPC must not run")) as getter:
            receipt = source.set_manual("gain", 8)
        getter.assert_not_called()
        self.assertEqual((receipt["value"], receipt["flags"]), (8, 2))
        receipt["value"] = 999  # caller owns the preallocated return snapshot
        self.assertEqual(source.controls_status()["native_gain"]["readback"]["value"], 8)

    def test_duplicate_readiness_admission_preserves_the_first_pending_decision(self):
        fake = fixtures.FakeNative()
        fake.accept_gate = threading.Event()
        source, _ = self.start(fake, timeout_s=3, initial_controls=readiness.TARGETS, restore_controls=False)
        pair = source.next_pair(module=self.decoder())
        accept = lambda: source.accept_readiness(pair["sequence"], pair["controls_revision"], timeout_s=2)
        result, done, caller = self.call_async(accept)
        self.assertTrue(fake.accept_entered.wait(1))
        before = self.metadata(source)
        with self.assertRaisesRegex(CameraError, "already pending"):
            accept()
        self.assertEqual(self.metadata(source), before)
        self.assertFalse(source._stop.is_set())
        fake.accept_gate.set()
        self.assertTrue(done.wait(1))
        self.assertEqual(result, [("success", True)])
        caller.join(1)

    def prepare_admission(self, startup):
        fake = fixtures.FakeNative()
        kwargs = {"initial_controls": readiness.TARGETS} if startup else {}
        source, _ = self.start(fake, timeout_s=3, restore_controls=False, **kwargs)
        pair = source.next_pair(module=self.decoder()) if startup else None
        fake.read_gate = threading.Event()
        self.assertTrue(fake.read_entered.wait(1))
        def operation():
            if startup:
                return source.accept_readiness(pair["sequence"], pair["controls_revision"], timeout_s=1)
            return source.request_controls({"exposure": -8, "gain": 7})
        return source, fake, operation

    def metadata(self, source):
        with source._condition:
            return (source._revision, id(source._active_decision), id(source._sample),
                    source._transaction_deadline, copy.deepcopy(source._controls["transaction"]),
                    tuple(id(item) for item in source._stop._decisions),
                    source._last_native_sequence, source._last_completed, source._consumed)

    def clear_fillers(self, source):
        with source._condition:
            while True:
                try:
                    source._commands.get_nowait()
                except queue.Empty:
                    break

    def full_queue_case(self, startup):
        source, fake, operation = self.prepare_admission(startup)
        with source._condition:
            for _ in range(source._commands.maxsize):
                source._commands.put_nowait(owned._Command("get", "gain", None, time.perf_counter() + 20))
        before = self.metadata(source)
        with self.assertRaisesRegex(CameraError, "queue full"):
            operation()
        self.assertEqual(self.metadata(source), before)
        self.assertEqual(source._commands.qsize(), source._commands.maxsize)
        self.assertIsNone(source.error)
        self.assertFalse(source._stop.is_set())
        self.clear_fillers(source)
        # A rejected edit consumed NO revision and did not poison readiness.
        result, done, caller = self.call_async(operation)
        fake.read_gate.set()
        self.assertTrue(done.wait(1))
        self.assertEqual(result[0][0], "success")
        self.assertEqual(result[0][1], True if startup else 1)
        caller.join(1)
        if not startup:
            # Await the asynchronous accepted transaction without creating a
            # second request. No hardware or foreign-thread controls involved.
            with source._condition:
                command = source._active_decision
            if command is not None:
                source._await_decided(command, "fixture accepted edit")
            self.assertEqual(source._revision, 1)

    def test_saturated_transaction_queue_preserves_all_admission_metadata(self):
        self.full_queue_case(startup=False)

    def test_saturated_readiness_queue_preserves_all_admission_metadata(self):
        self.full_queue_case(startup=True)

    def failing_put_case(self, startup):
        source, fake, operation = self.prepare_admission(startup)
        old = fixtures.FakeDecision()
        with source._condition:
            source._stop.register(old)
        before = self.metadata(source)
        created = fixtures.FakeDecision()
        with patch.object(source._native, "make_decision", return_value=created), \
             patch.object(source._commands, "put_nowait", side_effect=queue.Full):
            with self.assertRaisesRegex(CameraError, "queue full"):
                operation()
        self.assertEqual(created.status()[0], 2)
        self.assertEqual(old.status()[0], 0)
        self.assertEqual(self.metadata(source), before)
        self.assertEqual(source._commands.qsize(), 0)
        self.assertFalse(source._stop.is_set())
        fake.read_gate.set()

    def test_transaction_put_full_after_registration_rolls_back_registration(self):
        self.failing_put_case(startup=False)

    def test_readiness_put_full_after_registration_rolls_back_registration(self):
        self.failing_put_case(startup=True)

    def stop_race_case(self, startup):
        source, fake, operation = self.prepare_admission(startup)
        before = self.metadata(source)
        registered, resume = threading.Event(), threading.Event()
        self.decoder_gates.append(resume)
        original = source._stop.register
        def paused_registration(decision):
            original(decision)
            registered.set()
            if not resume.wait(2):
                raise RuntimeError("admission fixture barrier not released")
        with patch.object(source._stop, "register", side_effect=paused_registration):
            result, done, caller = self.call_async(operation)
            self.assertTrue(registered.wait(1))
            began = time.monotonic()
            source._stop.set()  # no owner/COM lock, even during admission
            self.assertLess(time.monotonic() - began, 0.4)
            resume.set()
            self.assertTrue(done.wait(1))
            self.assertEqual(result[0][0], "error")
            self.assertIsInstance(result[0][1], CameraError)
        self.assertEqual(self.metadata(source), before)
        self.assertEqual(source._commands.qsize(), 0)
        self.assertFalse(fake.close_entered.is_set())
        fake.read_gate.set()
        caller.join(1)
        source.release(1)
        self.assertEqual(fake.values, readiness.ORIGINALS)

    def test_stop_racing_transaction_admission_leaves_no_phantom_revision(self):
        self.stop_race_case(startup=False)

    def test_stop_racing_readiness_admission_leaves_no_phantom_decision(self):
        self.stop_race_case(startup=True)


if __name__ == "__main__":
    unittest.main()
