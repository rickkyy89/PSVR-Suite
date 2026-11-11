"""Offline regressions: production transaction code with fake native responses.

No DLL, camera enumeration, hardware, driver, SteamVR, install or deployment.
"""
import threading
import unittest
from unittest.mock import Mock, patch

from tools.psvr_camera import ps4_owned_dshow as owned
from tools.psvr_camera.camera_backend import CameraError
from tests import test_camera_ps4_owned_dshow as fixtures

FakeNative = fixtures.FakeNative


class TransactionTests(unittest.TestCase):
    setUp = fixtures.OwnerTests.setUp
    start = fixtures.OwnerTests.start
    cleanup_fakes = fixtures.OwnerTests.cleanup_fakes

    def test_all_native_ranges_step_manual_checked_before_any_write(self):
        for bad in ({"step": 2}, {"capabilities": 1}, {"maximum": 0}):
            fake = FakeNative()
            fake.range_overrides["brightness"] = bad
            source, _ = self.start(fake)
            with self.assertRaises(CameraError):
                source.apply_controls({"exposure": -7, "gain": 8, "brightness": 1})
            source.release(1)
            self.assertFalse(any(call[0] in ("set", "begin") for call in fake.calls))
            self.assertTrue(source.controls_status()["transaction"]["rollback_confirmed"])

    def test_three_property_targets_actuals_and_unique_4_16_settle(self):
        source, fake = self.start()
        source.read_raw()
        tx = source.apply_controls({"exposure": -7, "gain": 8, "brightness": 1})
        self.assertEqual(tx["state"], "confirmed")
        self.assertEqual([item["frames"] for item in tx["checkpoints"]], [4, 16])
        self.assertEqual(len(set(tx["settle_sequences"])), 16)
        self.assertGreater(min(tx["settle_sequences"]), tx["write_boundary_sequence"])
        raw, sequence, _ = source.read_raw()
        self.assertEqual(len(raw), owned.TRANSPORT_BYTES)
        self.assertGreater(sequence, tx["commit_boundary_sequence"])
        status = source.controls_status()
        self.assertEqual(status["requested"], {"exposure": -7, "gain": 8, "brightness": 1})
        self.assertTrue(all(status["readback_confirmed"].values()))
        self.assertEqual({call[1] for call in fake.calls}, {source._thread.ident})

    def test_duplicate_sequence_does_not_advance_settling(self):
        fake = FakeNative()
        fake.duplicate_reads = 12
        source, _ = self.start(fake)
        tx = source.apply_controls({"exposure": -7, "gain": 8})
        self.assertEqual(tx["settle_frames"], 16)
        self.assertEqual(len(set(tx["settle_sequences"])), 16)
        self.assertEqual(fake.duplicate_reads, 0)

    def test_delayed_readback_4_unconfirmed_16_confirmed(self):
        fake = FakeNative()
        fake.delayed_until = 16
        source, _ = self.start(fake)
        tx = source.apply_controls({"exposure": -7, "gain": 8, "brightness": 1})
        self.assertFalse(tx["checkpoints"][0]["confirmed"])
        self.assertTrue(tx["checkpoints"][1]["confirmed"])

    def test_unconfirmed_16_frames_rolls_back_and_never_republishes(self):
        fake = FakeNative()
        fake.delayed_until = 100
        source, _ = self.start(fake)
        with self.assertRaises(CameraError):
            source.apply_controls({"exposure": -7, "gain": 8})
        source.release(1)
        self.assertIn("rollback", [call[0] for call in fake.calls])
        self.assertNotIn("finish", [call[0] for call in fake.calls])
        self.assertEqual(source.controls_status()["transaction"]["state"], "failed")
        with self.assertRaises(CameraError):
            source.read_raw(0.1)

    def test_setter_and_rollback_and_cleanup_failures_retain_owner(self):
        fake = FakeNative()
        fake.fail_set_name = "gain"
        fake.rollback_error = "two properties failed restoration"
        fake.fail_close = True
        source, _ = self.start(fake)
        with self.assertRaises(CameraError):
            source.apply_controls({"exposure": -7, "gain": 8, "brightness": 1})
        with self.assertRaises(CameraError):
            source.release(1)
        self.assertTrue(source.cleanup_pending)
        self.assertIs(owned._active_owner, source)
        self.assertFalse(source.controls_status()["transaction"]["rollback_confirmed"])
        fake.fail_close = False
        source.release(1)
        self.assertFalse(source.cleanup_pending)

    def test_stalled_settling_is_bounded_no_reopen_or_stale_frame(self):
        fake = FakeNative()
        fake.auto_frames = False
        source, _ = self.start(fake)
        with self.assertRaises(CameraError):
            source.apply_controls({"exposure": -7}, timeout_s=0.1)
        source.release(1)
        self.assertEqual(sum(call[0] == "open" for call in fake.calls), 1)
        self.assertFalse(source.controls_status()["readback_confirmed"]["exposure"])

    def test_pending_edit_hides_old_slot_and_second_edit_rejected(self):
        fake = FakeNative()
        fake.set_gate = threading.Event()
        source, _ = self.start(fake)
        revision = source.request_controls({"gain": 8})
        self.assertEqual(revision, 1)
        self.assertTrue(fake.set_entered.wait(1))
        self.assertIsNone(source.read_raw(0.02, _terminal=False))
        with self.assertRaisesRegex(CameraError, "already pending"):
            source.request_controls({"brightness": 1})
        fake.set_gate.set()

    def test_edit_during_conversion_cannot_republish_old_pair(self):
        source, _ = self.start()
        entered, resume = threading.Event(), threading.Event()
        result = []
        def decode(_raw, module=None):
            entered.set()
            resume.wait(1)
            return object(), object()
        with patch.object(owned, "decode_pair", side_effect=decode):
            reader = threading.Thread(target=lambda: result.append(source.read_pair(0.5)))
            reader.start()
            try:
                self.assertTrue(entered.wait(1))
                source.request_controls({"exposure": -7})
            finally:
                resume.set()
                reader.join(1)
            self.assertFalse(reader.is_alive())
        self.assertEqual(result, [None])

    def test_switches_use_one_owner_and_explicit_suite_persistence(self):
        source, fake = self.start(restore_controls=False,
            initial_controls={"exposure": -7, "gain": 8, "brightness": 1})
        import numpy as np
        decoder = Mock(COLOR_YUV2BGR_YUY2=116)
        decoder.cvtColor.return_value = np.zeros((408, 1748, 3), dtype=np.uint8)
        candidate = source.next_pair(module=decoder)
        self.assertTrue(candidate["readiness_pending"])
        source.accept_readiness(candidate["sequence"], candidate["controls_revision"])
        for requested in ({"exposure": -8}, {"gain": 7}, {"brightness": 2},
                          {"exposure": -7, "gain": 8, "brightness": 1}):
            tx = source.apply_controls(requested)
            self.assertEqual(tx["state"], "confirmed")
        self.assertEqual(sum(call[0] == "open" for call in fake.calls), 1)
        finishes = [call[2] for call in fake.calls if call[0] == "finish"]
        self.assertEqual(finishes[0], (False,))
        self.assertTrue(all(item == (True,) for item in finishes[1:]))
        self.assertEqual(sum(call[0] == "accept" for call in fake.calls), 1)
        self.assertFalse(source.report()["controls_restore_on_close"])


if __name__ == "__main__":
    unittest.main()
