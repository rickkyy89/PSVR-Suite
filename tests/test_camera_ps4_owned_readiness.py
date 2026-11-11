"""P1 regressions: real owner/pump code, deterministic fake native gates.

No DLL, COM graph, camera, SteamVR, install, deploy or hardware enablement.
"""
import threading
import time
import unittest
from unittest.mock import Mock

from tools.psvr_camera import ps4_owned_dshow as owned
from tools.psvr_camera import ps4_stereo_session as stereo
from tools.psvr_camera.camera_backend import CameraError
from tests import test_camera_ps4_owned_dshow as fixtures

FakeNative = fixtures.FakeNative

TARGETS = {"exposure": -7, "gain": 8, "brightness": 1}
ORIGINALS = {"exposure": -5, "gain": 3, "brightness": 2}


class ReadinessTests(unittest.TestCase):
    start = fixtures.OwnerTests.start
    cleanup_fakes = fixtures.OwnerTests.cleanup_fakes

    def setUp(self):
        fixtures.OwnerTests.setUp(self)
        self.sessions = []
        self.decoder_gates = []
        self.addCleanup(self.cleanup_sessions)

    def cleanup_sessions(self):
        for gate in self.decoder_gates:
            gate.set()
        for fake in self.fakes:
            fake.fail_close = False
            for gate in (fake.open_gate, fake.set_gate, fake.close_gate, fake.accept_gate,
                         fake.accept_after_commit_gate, fake.finish_after_commit_gate, fake.commit_gate,
                         fake.finish_gate, fake.read_gate):
                if gate is not None:
                    gate.set()
        for session in self.sessions:
            session.stop(1)
            cap = session._session
            if cap is not None and cap not in self.sources:
                self.sources.append(cap)

    def decoder(self):
        import numpy as np
        decoder = Mock(COLOR_YUV2BGR_YUY2=116)
        decoder.cvtColor.return_value = np.zeros((408, 1748, 3), dtype=np.uint8)
        return decoder

    def start_session(self, fake, decoder=None):
        self.fakes.append(fake)
        session = stereo.StereoSession(0, exposure=-7, gain=8, brightness=1,
            module=decoder if decoder is not None else self.decoder(),
            restore_controls=False, strict_controls=True,
            owned_capture_factory=owned.OwnedDshowCapture.open_index,
            owned_native_factory=lambda _: fake)
        self.sessions.append(session)
        session.start()
        return session

    def assert_no_persistent_accept(self, fake):
        self.assertFalse(any(call[0] == "finish" and call[2] == (True,) for call in fake.calls))
        self.assertFalse(any(call[0] == "accept" for call in fake.calls))

    def test_stop_during_blocked_open_is_shared_bounded_and_never_sets_profile(self):
        fake = FakeNative()
        fake.open_gate = threading.Event()
        session = self.start_session(fake)
        self.assertTrue(fake.open_entered.wait(1))
        began = time.monotonic()
        self.assertFalse(session.stop(0.02))
        self.assertLess(time.monotonic() - began, 0.5)
        self.assertTrue(session.report()["cleanup_pending"])
        self.assertFalse(fake.close_entered.is_set())
        self.assertFalse(any(call[0] == "set" for call in fake.calls))
        with self.assertRaisesRegex(CameraError, "no reopen"):
            owned.OwnedDshowCapture.open("fixture", owner_release_confirmed=True, _native_factory=Mock())
        fake.open_gate.set()
        self.assertTrue(session.done.wait(1))
        self.assertTrue(session.stop(1))
        self.assertEqual([call[0] for call in fake.calls], ["open", "close"])
        self.assertEqual(len({call[1] for call in fake.calls}), 1)
        self.assertEqual(fake.values, ORIGINALS)
        self.assertEqual(session.report()["frames_published"], 0)

    def test_stop_after_16_frames_but_before_first_postcommit_frame_restores(self):
        fake = FakeNative()
        fake.post_finish_mode = "missing"
        session = self.start_session(fake)
        self.assertTrue(fake.finished.wait(1))
        self.assertEqual([call[2] for call in fake.calls if call[0] == "finish"], [(False,)])
        session.stop(0.02)
        self.assertTrue(session.done.wait(1))
        self.assertTrue(session.stop(1))
        self.assertEqual(fake.values, ORIGINALS)
        self.assertTrue(fake.restores)
        self.assert_no_persistent_accept(fake)
        self.assertEqual(session.report()["frames_published"], 0)

    def test_stop_during_inflight_startup_set_never_finishes_persistently(self):
        fake = FakeNative()
        fake.set_gate = threading.Event()
        session = self.start_session(fake)
        self.assertTrue(fake.set_entered.wait(1))
        began = time.monotonic()
        self.assertFalse(session.stop(0.02))
        self.assertLess(time.monotonic() - began, 0.5)
        self.assertFalse(fake.close_entered.is_set())
        fake.set_gate.set()  # in-flight native work may return; never force free
        self.assertTrue(session.done.wait(1))
        self.assertTrue(session.stop(1))
        self.assertEqual(fake.values, ORIGINALS)
        self.assertIn("rollback", [call[0] for call in fake.calls])
        self.assert_no_persistent_accept(fake)
        self.assertEqual(session.report()["frames_published"], 0)

    def test_missing_first_postcommit_frame_times_out_with_retained_originals(self):
        fake = FakeNative()
        fake.post_finish_mode = "missing"
        with self.assertRaises(owned.OwnedDshowOpenError) as caught:
            self.start(fake, timeout_s=0.5, initial_controls=TARGETS, restore_controls=False)
        source = caught.exception.source
        source.release(1)
        tx = source.controls_status()["transaction"]
        self.assertEqual(tx["settle_frames"], 16)
        self.assertEqual(len(set(tx["settle_sequences"])), 16)
        self.assertEqual(tx["state"], "failed")
        self.assertTrue(tx["rollback_confirmed"])
        self.assertFalse(source.report()["readiness_accepted"])
        self.assertEqual(fake.values, ORIGINALS)
        self.assert_no_persistent_accept(fake)

    def test_bad_sample_size_after_setup_restores_without_acceptance(self):
        fake = FakeNative()
        fake.post_finish_mode = "bad"
        with self.assertRaises(owned.OwnedDshowOpenError) as caught:
            self.start(fake, initial_controls=TARGETS, restore_controls=False)
        source = caught.exception.source
        source.release(1)
        self.assertEqual(source.controls_status()["transaction"]["settle_frames"], 16)
        self.assertIn("invalid owned DirectShow transport", source.error)
        self.assertEqual(fake.values, ORIGINALS)
        self.assert_no_persistent_accept(fake)

    def test_raw_readiness_does_not_persist_until_decoded_pump_acceptance(self):
        source, fake = self.start(initial_controls=TARGETS, restore_controls=False)
        self.assertEqual(source.controls_status()["transaction"]["state"], "awaiting_acceptance")
        self.assertEqual(fake.pending_before, ORIGINALS)
        self.assertIsNone(source.read_raw(0.01, _terminal=False))
        self.assert_no_persistent_accept(fake)
        pair = source.next_pair(module=self.decoder())
        self.assertTrue(pair["readiness_pending"])
        self.assertTrue(source.accept_readiness(pair["sequence"], pair["controls_revision"]))
        self.assertTrue(source.report()["readiness_accepted"])
        self.assertEqual(source.controls_status()["transaction"]["state"], "confirmed")
        source.release(1)
        self.assertEqual(fake.values, TARGETS)
        self.assertFalse(fake.restores)
        self.assertEqual([call[2] for call in fake.calls if call[0] == "finish"], [(False,)])

    def test_undecoded_raw_candidate_cannot_be_accepted(self):
        source, fake = self.start(initial_controls=TARGETS, restore_controls=False)
        with self.assertRaisesRegex(CameraError, "matching decoded candidate"):
            source.accept_readiness(source._last_native_sequence, 0)
        source.release(1)
        self.assertEqual(fake.values, ORIGINALS)
        self.assert_no_persistent_accept(fake)

    def test_shared_cancellation_wins_before_persistent_acceptance(self):
        token = owned.CancellationToken()
        source, fake = self.start(initial_controls=TARGETS, restore_controls=False, cancel_token=token)
        pair = source.next_pair(module=self.decoder())
        token.set()
        with self.assertRaises(CameraError):
            source.accept_readiness(pair["sequence"], pair["controls_revision"])
        source.release(1)
        self.assertEqual(fake.values, ORIGINALS)
        self.assert_no_persistent_accept(fake)

    def test_decoder_failure_restores_before_any_readiness_acceptance(self):
        source, fake = self.start(initial_controls=TARGETS, restore_controls=False)
        decoder = self.decoder()
        decoder.cvtColor.side_effect = CameraError("fixture decode failure")
        with self.assertRaisesRegex(CameraError, "decode failure"):
            source.next_pair(module=decoder)
        source.release(1)
        self.assertEqual(fake.values, ORIGINALS)
        self.assert_no_persistent_accept(fake)

    def test_stop_during_pump_decoding_is_bounded_and_cannot_persist(self):
        fake = FakeNative()
        entered, resume = threading.Event(), threading.Event()
        self.decoder_gates.append(resume)
        decoder = self.decoder()
        image = decoder.cvtColor.return_value
        def blocked_decode(*_args):
            entered.set()
            resume.wait(2)
            return image
        decoder.cvtColor.side_effect = blocked_decode
        session = self.start_session(fake, decoder)
        self.assertTrue(entered.wait(1))
        began = time.monotonic()
        self.assertFalse(session.stop(0.02))
        self.assertLess(time.monotonic() - began, 0.5)
        self.assertTrue(session._session._done.wait(1))
        self.assertEqual(fake.values, ORIGINALS)
        resume.set()
        self.assertTrue(session.done.wait(1))
        self.assertTrue(session.stop(1))
        self.assert_no_persistent_accept(fake)
        self.assertEqual(session.report()["frames_published"], 0)

    def test_failed_startup_restoration_quarantines_then_explicit_owner_retry(self):
        fake = FakeNative()
        fake.post_finish_mode = "bad"
        fake.fail_close = True
        with self.assertRaises(owned.OwnedDshowOpenError) as caught:
            self.start(fake, initial_controls=TARGETS, restore_controls=False)
        source = caught.exception.source
        with self.assertRaises(CameraError):
            source.release(1)
        self.assertTrue(source.cleanup_pending)
        self.assertIs(owned._active_owner, source)
        self.assertIsNone(source.controls_status()["transaction"]["rollback_confirmed"])
        self.assertEqual(fake.pending_before, ORIGINALS)
        self.assert_no_persistent_accept(fake)
        fake.fail_close = False
        source.release(1)
        self.assertFalse(source.cleanup_pending)
        self.assertEqual(fake.values, ORIGINALS)
        self.assertEqual(len({call[1] for call in fake.calls}), 1)


if __name__ == "__main__":
    unittest.main()
