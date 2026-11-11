"""Read-only inspection/diagnostic cache fixtures, no DLL/device/COM graph."""
import copy
import threading
import unittest
from unittest.mock import Mock, patch

from tools.psvr_camera import ps4_owned_dshow as owned
from tools.psvr_camera.camera_backend import CameraError
from tests import test_camera_ps4_owned_dshow as fixtures
from tests import test_camera_ps4_owned_readiness as readiness
from tests import test_camera_ps4_owned_completion as completion
from tools.psvr_camera import ps4_suite_source as suite

MONIKER = r"@device:pnp:\\?\usb#vid_05a9&pid_058a#fixture-unit-a"


class InspectionNative(fixtures.FakeNative):
    def __init__(self):
        super().__init__()
        self.fail_diagnostic_copy = False
        self.metadata = [{"event": "diagnostic_end", "terminal_stage": "none", "terminal_hr": 0}]

    def inspect_index(self, index, expected_moniker):
        self.record("inspect", index, expected_moniker)
        self.open_entered.set()
        if self.open_gate is not None:
            self.open_gate.wait()
        if self.fail_open:
            self.metadata = [{"event": "diagnostic_end", "terminal_stage": "stream.GetStreamCaps",
                              "terminal_hr": -2147220992}]
            raise CameraError("fixture inspection HRESULT 0x80040200")

    def diagnostics(self):
        self.record("diagnostics")
        if self.fail_diagnostic_copy:
            raise RuntimeError("fixture metadata copy failed")
        return copy.deepcopy(self.metadata)


class DiagnosticsTests(unittest.TestCase):
    setUp = readiness.ReadinessTests.setUp
    cleanup_sessions = readiness.ReadinessTests.cleanup_sessions
    cleanup_fakes = fixtures.OwnerTests.cleanup_fakes
    call_async = completion.CompletionTests.call_async

    def test_readonly_probe_never_starts_capture_or_requests_any_controls(self):
        fake = InspectionNative()
        self.fakes.append(fake)
        source = owned.OwnedDshowCapture.inspect_index(0, expected_moniker=MONIKER, operator_owner_release_attested=True,
            timeout_s=1, _native_factory=lambda _: fake)
        self.sources.append(source)
        report = source.report()
        self.assertTrue(report["read_only_inspection"])
        self.assertTrue(report["owner_done"])
        self.assertFalse(report["cleanup_pending"])
        self.assertIn("no_external_exclusivity", report["ownership_scope"])
        self.assertEqual(report["probe_identity_kind"], "IMoniker::GetDisplayName")
        self.assertEqual(fake.calls[0][2], (0, MONIKER))
        self.assertEqual(report["controls"]["transaction"]["state"], "idle")
        self.assertEqual([call[0] for call in fake.calls], ["inspect", "diagnostics", "close"])
        self.assertEqual(len({call[1] for call in fake.calls}), 1)
        self.assertEqual(fake.values, readiness.ORIGINALS)
        report["negotiation_diagnostics"][0]["terminal_stage"] = "external mutation"
        self.assertEqual(source.report()["negotiation_diagnostics"][0]["terminal_stage"], "none")

    def test_failed_inspection_retains_precise_metadata_and_original_error(self):
        fake = InspectionNative()
        fake.fail_open = True
        self.fakes.append(fake)
        with self.assertRaises(owned.OwnedDshowOpenError) as caught:
            owned.OwnedDshowCapture.inspect_index(0, expected_moniker=MONIKER, operator_owner_release_attested=True,
                timeout_s=1, _native_factory=lambda _: fake)
        source = caught.exception.source
        self.sources.append(source)
        source.release(1)
        self.assertIn("0x80040200", source.error)
        self.assertEqual(source.report()["negotiation_diagnostics"][0]["terminal_stage"], "stream.GetStreamCaps")
        self.assertFalse(any(call[0] in ("open", "set", "get", "read", "begin", "finish") for call in fake.calls))

    def test_failed_diagnostic_copy_does_not_replace_capture_failure(self):
        fake = InspectionNative()
        fake.fail_open = fake.fail_diagnostic_copy = True
        self.fakes.append(fake)
        with self.assertRaises(owned.OwnedDshowOpenError) as caught:
            owned.OwnedDshowCapture.inspect_index(0, expected_moniker=MONIKER, operator_owner_release_attested=True,
                timeout_s=1, _native_factory=lambda _: fake)
        source = caught.exception.source
        self.sources.append(source)
        source.release(1)
        self.assertIn("inspection HRESULT 0x80040200", source.error)
        self.assertEqual(source.report()["negotiation_diagnostics"],
            [{"event": "diagnostic_copy_failed", "error_type": "RuntimeError"}])

    def test_cancelled_readonly_probe_keeps_owner_until_real_inspection_returns(self):
        fake = InspectionNative()
        fake.open_gate = threading.Event()
        token = owned.CancellationToken()
        self.fakes.append(fake)
        result, done, caller = self.call_async(lambda: owned.OwnedDshowCapture.inspect_index(0,
            expected_moniker=MONIKER, operator_owner_release_attested=True,
            timeout_s=1, _native_factory=lambda _: fake, cancel_token=token))
        self.assertTrue(fake.open_entered.wait(1))
        token.set()
        self.assertTrue(done.wait(0.3))
        self.assertEqual(result[0][0], "error")
        self.assertIsInstance(result[0][1], owned.OwnedDshowOpenError)
        source = result[0][1].source
        self.sources.append(source)
        self.assertTrue(source.cleanup_pending)
        self.assertFalse(fake.close_entered.is_set())
        fake.open_gate.set()
        caller.join(1)
        source.release(1)
        self.assertFalse(source.cleanup_pending)
        self.assertEqual(len({call[1] for call in fake.calls}), 1)
        self.assertFalse(any(call[0] in ("read", "get", "set", "begin", "finish") for call in fake.calls))

    def test_missing_or_invalid_expected_identity_never_creates_native(self):
        factory = Mock()
        for identity in (None, "", "  ", "bad\0moniker", 0):
            with self.subTest(identity=identity):
                with self.assertRaisesRegex(CameraError, "GetDisplayName"):
                    owned.OwnedDshowCapture.inspect_index(0, expected_moniker=identity,
                        operator_owner_release_attested=True, _native_factory=factory)
        factory.assert_not_called()

    def test_operator_attestation_is_required_not_an_external_owner_check(self):
        factory = Mock()
        with self.assertRaisesRegex(CameraError, "no cross-process exclusivity"):
            owned.OwnedDshowCapture.inspect_index(0, expected_moniker=MONIKER, _native_factory=factory)
        factory.assert_not_called()

    def test_detected_suite_startup_or_cleanup_owner_blocks_probe_before_native(self):
        factory = Mock()
        for done, pending in ((False, False), (True, True)):
            session = Mock()
            session.done.is_set.return_value = done
            session.report.return_value = {"cleanup_pending": pending}
            with patch.object(suite, "_active_session", session):
                with self.assertRaisesRegex(CameraError, "internal PS4 Suite owner"):
                    owned.OwnedDshowCapture.inspect_index(0, expected_moniker=MONIKER,
                        operator_owner_release_attested=True, _native_factory=factory)
        factory.assert_not_called()

    def test_timeout_retains_adapter_latch_through_late_inspect_and_blocked_close(self):
        fake = InspectionNative()
        fake.open_gate = threading.Event()
        fake.close_gate = threading.Event()
        self.fakes.append(fake)
        result, done, caller = self.call_async(lambda: owned.OwnedDshowCapture.inspect_index(0,
            expected_moniker=MONIKER, operator_owner_release_attested=True,
            timeout_s=0.1, _native_factory=lambda _: fake))
        self.assertTrue(fake.open_entered.wait(1))
        self.assertTrue(done.wait(1))
        self.assertEqual(result[0][0], "error")
        self.assertIsInstance(result[0][1], owned.OwnedDshowOpenError)
        source = result[0][1].source
        self.sources.append(source)
        self.assertTrue(source.cleanup_pending)
        self.assertIs(owned._active_owner, source)
        fake.open_gate.set()
        self.assertTrue(fake.close_entered.wait(1))
        factory = Mock()
        with self.assertRaisesRegex(CameraError, "no reopen"):
            owned.OwnedDshowCapture.inspect_index(1, expected_moniker=MONIKER,
                operator_owner_release_attested=True, _native_factory=factory)
        with self.assertRaisesRegex(CameraError, "no reopen"):
            owned.OwnedDshowCapture.open("fixture", owner_release_confirmed=True, _native_factory=factory)
        factory.assert_not_called()
        self.assertTrue(source.cleanup_pending)
        self.assertIs(owned._active_owner, source)
        fake.close_gate.set()
        caller.join(1)
        source.release(1)
        self.assertFalse(source.cleanup_pending)
        self.assertIsNone(owned._active_owner)
        self.assertEqual(len({call[1] for call in fake.calls}), 1)
        self.assertEqual([call[0] for call in fake.calls], ["inspect", "diagnostics", "close"])

    def test_failed_probe_close_retains_owner_until_explicit_owner_retry(self):
        fake = InspectionNative()
        fake.fail_close = True
        self.fakes.append(fake)
        with self.assertRaises(owned.OwnedDshowOpenError) as caught:
            owned.OwnedDshowCapture.inspect_index(0, expected_moniker=MONIKER,
                operator_owner_release_attested=True, timeout_s=1, _native_factory=lambda _: fake)
        source = caught.exception.source
        self.sources.append(source)
        self.assertTrue(source.cleanup_pending)
        self.assertIs(owned._active_owner, source)
        factory = Mock()
        with self.assertRaisesRegex(CameraError, "no reopen"):
            owned.OwnedDshowCapture.inspect_index(0, expected_moniker=MONIKER,
                operator_owner_release_attested=True, _native_factory=factory)
        factory.assert_not_called()
        fake.fail_close = False
        source.release(1)
        self.assertFalse(source.cleanup_pending)
        self.assertIsNone(owned._active_owner)
        self.assertEqual(len({call[1] for call in fake.calls}), 1)


if __name__ == "__main__":
    unittest.main()
