"""Offline owner/timeout/conversion fixtures. Never loads a DLL or opens COM.

Native Windows COM fixture groups live in tools/psvr_ps4_dshow/offline_tests.cpp.
These tests intentionally inject a Python fake; they are not hardware evidence.
"""
import queue
import threading
import time
import unittest
from unittest.mock import Mock, patch

from tools.psvr_camera import ps4_owned_dshow as owned
from tools.psvr_camera.camera_backend import CameraError

MONIKER = "@device:pnp:usb#vid_05a9&pid_058b#fixture-only"


class FakeDecision:
    """Fake ABI atomic result, no external work inside the tiny phase lock.

    Production CAS implementation is independently exercised by native fixtures.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self.phase = 0

    def status(self):
        with self._lock:
            return self.phase, 0

    def cancel(self):
        with self._lock:
            if self.phase == 0:
                self.phase = 2
            return self.phase

    def try_commit(self):
        with self._lock:
            if self.phase != 0:
                return False
            self.phase = 1
            return True


class FakeNative:
    def __init__(self):
        self.calls = []
        self.frames = queue.Queue()
        self.raw = b"\x25" * owned.TRANSPORT_BYTES
        self.frames.put((self.raw, 1))
        self.open_entered = threading.Event()
        self.set_entered = threading.Event()
        self.close_entered = threading.Event()
        self.open_gate = self.set_gate = self.close_gate = None
        self.fail_open = self.fail_close = False
        self.values = {"exposure": -5, "gain": 3, "brightness": 2}
        self.sequence = 0
        self.active = False
        self.before = {}
        self.range_overrides = {}
        self.fail_set_name = None
        self.rollback_error = None
        self.auto_frames = True
        self.continuous = False
        self.duplicate_reads = 0
        self.settle_unique = 0
        self.delayed_until = 0
        self.finished = threading.Event()
        self.post_finish_mode = "normal"
        self.pending_before = None
        self.accepted_sequence = None
        self.restores = []
        self.accept_entered = threading.Event()
        self.accept_committed = threading.Event()
        self.accept_gate = self.accept_after_commit_gate = None
        self.finish_after_commit_gate = self.commit_gate = None
        self.finish_gate = None
        self.finish_entered = threading.Event()
        self.read_gate = None
        self.read_entered = threading.Event()
        self.finish_committed = threading.Event()
        self.commit_entered = threading.Event()

    def make_decision(self):
        return FakeDecision()

    def record(self, operation, *args):
        self.calls.append((operation, threading.get_ident(), args))

    def open(self, display_name):
        self.record("open", display_name)
        self.open_entered.set()
        if self.open_gate is not None:
            self.open_gate.wait()  # only test-controlled fake event; never COM
        if self.fail_open:
            raise CameraError("fixture open failed with a partial graph")

    def open_index(self, index):
        self.open(index)

    def read(self, timeout_ms):
        self.record("read", timeout_ms)
        if self.read_gate is not None:
            self.read_entered.set()
            self.read_gate.wait()  # deterministic fake owner suspension, no caller lock
        try:
            raw, sequence = self.frames.get_nowait()
        except queue.Empty:
            if (self.active and self.auto_frames) or self.continuous:
                if not self.active:
                    threading.Event().wait(0.005)
                sequence = self.sequence
                if self.duplicate_reads:
                    self.duplicate_reads -= 1
                else:
                    sequence += 1
                    if self.active:
                        self.settle_unique += 1
                raw = self.raw
            else:
                try:
                    raw, sequence = self.frames.get(timeout=timeout_ms / 1000)
                except queue.Empty:
                    return None
        self.sequence = max(self.sequence, sequence)
        return raw, sequence, time.perf_counter()

    def get_control(self, name):
        self.record("get", name)
        value = (self.before.get(name, self.values[name]) if self.active and self.settle_unique < self.delayed_until
                 else self.values[name])
        result = {"minimum": -13 if name == "exposure" else 0,
                "maximum": -1 if name == "exposure" else 8,
                "step": 1, "capabilities": 3, "default": 0,
                "value": value, "flags": 2}
        result.update(self.range_overrides.get(name, {}))
        return result

    def set_manual(self, name, value):
        self.record("set", name, value)
        self.set_entered.set()
        if self.set_gate is not None:
            self.set_gate.wait()
        if name == self.fail_set_name:
            raise CameraError("fixture simultaneous setter failure")
        self.values[name] = value
        return self.get_control(name)

    def begin_controls(self, targets):
        # Fake native observations, NOT a duplicate transaction algorithm.
        # Validation/settling are executed by production Python; the actual C++
        # begin/rollback bodies have their own native COM fixtures.
        self.record("begin", dict(targets))
        self.before = dict(self.values)
        self.active = True
        self.settle_unique = 0
        for name, value in targets.items():
            self.set_manual(name, value)
        return self.sequence

    def commit_controls(self):
        self.record("commit")
        self.commit_entered.set()
        if self.commit_gate is not None:
            self.commit_gate.wait()  # simulated blocking COM verification BEFORE commit
        return self.sequence

    def finish_controls(self, persist=False, decision=None):
        if decision is not None:
            self.finish_entered.set()
            if self.finish_gate is not None:
                self.finish_gate.wait()  # before CAS, after Python preparation lock was released
        if decision is not None and not decision.try_commit():
            raise CameraError("fixture finish decision cancelled")
        self.record("finish", persist)
        if not persist and self.pending_before is None:
            self.pending_before = dict(self.before)
        self.active = False
        if self.post_finish_mode != "missing":
            self.sequence += 1
            self.frames.put((b"short" if self.post_finish_mode == "bad" else self.raw, self.sequence))
        self.finished.set()
        if decision is not None:
            self.finish_committed.set()
            if self.finish_after_commit_gate is not None:
                self.finish_after_commit_gate.wait()

    def accept_persistence(self, sequence, decision):
        self.record("accept", sequence)
        self.accept_entered.set()
        if self.accept_gate is not None:
            self.accept_gate.wait()  # before CAS; no Python owner/state lock
        if self.pending_before is None:
            raise CameraError("fixture acceptance has no retained originals")
        if not decision.try_commit():
            raise CameraError("fixture acceptance decision cancelled")
        self.accepted_sequence = sequence
        self.pending_before = None
        self.accept_committed.set()
        if self.accept_after_commit_gate is not None:
            self.accept_after_commit_gate.wait()  # after commit, before caller notification

    def accept_readiness_nonpersistent(self, sequence, decision):
        self.record("accept_nonpersistent", sequence)
        if not decision.try_commit():
            raise CameraError("fixture readiness decision cancelled")

    def rollback_controls(self):
        self.record("rollback")
        self.values.update(self.before)
        self.active = False
        return {"confirmed": list(owned.PROPERTIES) if not self.rollback_error else [],
                "error": self.rollback_error, "active": True}

    def close(self):
        self.record("close")
        self.close_entered.set()
        if self.close_gate is not None:
            self.close_gate.wait()
        if self.fail_close:
            raise CameraError("fixture restore failed")
        if self.pending_before is not None:
            self.restores.append(dict(self.pending_before))
            self.values.update(self.pending_before)
            self.pending_before = None


class OwnerTests(unittest.TestCase):
    def setUp(self):
        # Safe ONLY because this module's tests use no native handle/DLL. No
        # production API can reset a retained cleanup-failure reservation.
        patcher = patch.object(owned, "_active_owner", None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sources = []
        self.fakes = []
        self.addCleanup(self.cleanup_fakes)

    def cleanup_fakes(self):
        for fake in self.fakes:
            fake.fail_close = False  # explicit fake-only retry before joining
            for gate in (fake.open_gate, fake.set_gate, fake.close_gate, fake.accept_gate,
                         fake.accept_after_commit_gate, fake.finish_after_commit_gate, fake.commit_gate,
                         fake.finish_gate, fake.read_gate):
                if gate is not None:
                    gate.set()
        for source in self.sources:
            try:
                source.release(1)
            except CameraError:
                pass
            self.assertTrue(source._done.wait(1), "offline fixture owner leaked")

    def start(self, fake=None, timeout_s=1, **kwargs):
        fake = fake or FakeNative()
        self.fakes.append(fake)
        try:
            source = owned.OwnedDshowCapture.open(MONIKER,
                owner_release_confirmed=True, timeout_s=timeout_s,
                _native_factory=lambda _: fake, **kwargs)
        except owned.OwnedDshowOpenError as exc:
            self.sources.append(exc.source)
            raise
        self.sources.append(source)
        return source, fake

    def test_import_opt_in_and_explicit_release_confirmation(self):
        factory = Mock()
        with self.assertRaisesRegex(CameraError, "owner release"):
            owned.OwnedDshowCapture.open(MONIKER, _native_factory=factory)
        factory.assert_not_called()
        with self.assertRaisesRegex(CameraError, "moniker"):
            owned.OwnedDshowCapture.open(0, owner_release_confirmed=True,
                _native_factory=factory)
        factory.assert_not_called()

    def test_open_read_controls_cleanup_stay_on_one_owner_no_default_writes(self):
        source, fake = self.start()
        raw, sequence, completed = source.read_raw(0.5)
        self.assertEqual(len(raw), 1748 * 408 * 2)
        self.assertEqual(sequence, 1)
        self.assertLessEqual(completed, time.perf_counter())
        self.assertNotIn("set", [call[0] for call in fake.calls])
        self.assertEqual(source.get_control("exposure")["value"], -5)
        self.assertEqual(source.set_manual("exposure", -7)["value"], -7)
        self.assertEqual(source.set_manual("gain", 8)["flags"], 2)
        self.assertEqual(source.set_manual("brightness", 1)["value"], 1)
        source.release(1)
        owners = {call[1] for call in fake.calls}
        self.assertEqual(owners, {source._thread.ident})
        self.assertNotIn(threading.get_ident(), owners)
        self.assertEqual(fake.calls[-1][0], "close")
        self.assertFalse(source.cleanup_pending)
        self.assertIsNone(owned._active_owner)

    def test_live_owner_blocks_reopen_before_native_factory(self):
        self.start()
        factory = Mock()
        with self.assertRaisesRegex(CameraError, "no reopen"):
            owned.OwnedDshowCapture.open(MONIKER, owner_release_confirmed=True,
                _native_factory=factory)
        factory.assert_not_called()

    def test_open_timeout_retains_owner_no_foreign_cleanup_or_reopen(self):
        fake = FakeNative()
        fake.open_gate = threading.Event()
        with self.assertRaises(owned.OwnedDshowOpenError) as caught:
            self.start(fake, timeout_s=0.05)
        source = caught.exception.source
        self.assertTrue(fake.open_entered.is_set())
        self.assertTrue(source.cleanup_pending)
        self.assertFalse(fake.close_entered.is_set())
        with self.assertRaisesRegex(CameraError, "no reopen"):
            self.start()
        fake.open_gate.set()
        source.release(1)
        self.assertFalse(source.cleanup_pending)

    def test_failed_partial_open_is_closed_by_same_owner(self):
        fake = FakeNative()
        fake.fail_open = True
        with self.assertRaises(owned.OwnedDshowOpenError) as caught:
            self.start(fake)
        source = caught.exception.source
        source.release(1)
        self.assertEqual([call[0] for call in fake.calls], ["open", "close"])
        self.assertEqual({call[1] for call in fake.calls}, {source._thread.ident})

    def test_control_timeout_never_closes_inflight_com(self):
        fake = FakeNative()
        fake.set_gate = threading.Event()
        source, _ = self.start(fake)
        with self.assertRaisesRegex(CameraError, "quarantined"):
            source.set_manual("gain", 8, timeout_s=0.1)
        self.assertTrue(fake.set_entered.is_set())
        self.assertFalse(fake.close_entered.is_set())
        self.assertTrue(source.cleanup_pending)
        with self.assertRaises(CameraError):
            source.read_raw(0.1)
        with self.assertRaisesRegex(CameraError, "no reopen"):
            self.start()
        fake.set_gate.set()
        source.release(1)
        self.assertTrue(fake.close_entered.is_set())

    def test_stop_timeout_quarantines_until_owner_returns(self):
        fake = FakeNative()
        fake.close_gate = threading.Event()
        source, _ = self.start(fake)
        with self.assertRaisesRegex(CameraError, "cleanup deadline"):
            source.release(0.1)
        self.assertTrue(fake.close_entered.is_set())
        self.assertTrue(source.cleanup_pending)
        with self.assertRaisesRegex(CameraError, "no reopen"):
            self.start()
        fake.close_gate.set()
        source.release(1)
        self.assertIsNone(owned._active_owner)

    def test_restore_failure_is_visible_and_reservation_remains(self):
        source, fake = self.start()
        fake.fail_close = True
        with self.assertRaisesRegex(CameraError, "restore failed"):
            source.release(1)
        self.assertTrue(source.cleanup_pending)
        self.assertIs(owned._active_owner, source)
        self.assertFalse(source.report()["owner_done"])
        self.assertIn("restore failed", source.report()["cleanup_error"])
        with self.assertRaisesRegex(CameraError, "no reopen"):
            self.start()
        fake.fail_close = False
        source.release(1)  # explicit retry occurs on the original owner
        self.assertFalse(source.cleanup_pending)
        self.assertIsNone(owned._active_owner)

    def test_frame_deadline_is_terminal_not_automatic_reopen(self):
        source, fake = self.start()
        source.read_raw()
        with self.assertRaisesRegex(CameraError, "frame deadline"):
            source.read_raw(0.05)
        source.release(1)
        self.assertEqual(sum(call[0] == "open" for call in fake.calls), 1)

    def test_invalid_targets_rejected_before_owner_write(self):
        source, fake = self.start()
        for value in (True, 1.5, "8", 2**31, -(2**31) - 1, float("nan")):
            with self.subTest(value=value), self.assertRaises(CameraError):
                source.set_manual("gain", value)
        with self.assertRaises(CameraError):
            source.get_control("ps3_exposure")
        self.assertNotIn("set", [call[0] for call in fake.calls])

    def test_repeat_sequence_is_terminal_no_reopen(self):
        source, fake = self.start()
        source.read_raw()
        fake.frames.put((b"\0" * owned.TRANSPORT_BYTES, 1))
        with self.assertRaisesRegex(CameraError, "invalid.*transport"):
            source.read_raw(0.5)
        source.release(1)


class DecodeTests(unittest.TestCase):
    def test_exact_yuy2_conversion_and_row_offset48_eye_crops(self):
        import numpy as np
        from tests.test_camera_ps4_pixel_parity import scalar_bt601_bgr
        # Independent scalar expectations, NOT cv2 conversion compared to itself.
        try:
            import cv2
        except ImportError:
            self.skipTest("local camera environment has no cv2; no installation attempted")
        pair = (37, 83, 193, 171)  # distinct Ys, unequal U/V
        expected = np.empty((400, 640, 3), dtype=np.uint8)
        expected[:, 0::2] = scalar_bt601_bgr(pair[0], pair[1], pair[3])
        expected[:, 1::2] = scalar_bt601_bgr(pair[2], pair[1], pair[3])
        actual = owned.decode_pair(bytes(pair) * (owned.TRANSPORT_BYTES // 4))
        for image in actual:
            self.assertEqual(image.shape, (400, 640, 3))
            self.assertLessEqual(int(np.abs(image.astype(np.int16) - expected.astype(np.int16)).max()), 1)
        self.assertFalse(np.shares_memory(actual[0], actual[1]))

    def test_crop_offsets_without_cv2_conversion_dependency(self):
        import numpy as np
        frame = np.zeros((408, 1748, 3), dtype=np.uint8)
        frame[:400, 48:688] = [11, 23, 47]
        frame[:400, 688:1328] = [83, 97, 137]
        cv = Mock(COLOR_YUV2BGR_YUY2=116)
        cv.cvtColor.return_value = frame
        left, right = owned.decode_pair(b"\0" * owned.TRANSPORT_BYTES, module=cv)
        yuy2, code = cv.cvtColor.call_args.args
        self.assertEqual(yuy2.shape, (408, 1748, 2))
        self.assertEqual(code, 116)
        self.assertTrue(np.all(left == [11, 23, 47]))
        self.assertTrue(np.all(right == [83, 97, 137]))

    def test_invalid_raw_never_reaches_conversion(self):
        cv = Mock()
        for raw in (b"", b"\0" * (owned.TRANSPORT_BYTES - 2), bytearray(owned.TRANSPORT_BYTES)):
            with self.assertRaises(CameraError):
                owned.decode_pair(raw, module=cv)
        cv.cvtColor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
