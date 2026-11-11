"""Deterministic regressions for startup readiness/stop/strict source controls.

No device is opened: the only synchronous pump exercise injects its entire
bounded owner session. The implementation agent does not execute these tests.
"""
import threading
import unittest
from unittest.mock import Mock, patch

from tools.psvr_camera.camera_backend import CameraError
from tools.psvr_camera import ps4_stereo_session as stereo
from tools.psvr_camera import ps4_suite_source as source
from tools.psvr_suite import tracking
from tests import test_suite_tracking_source_profiles as transition_fixtures
from tests import test_camera_source_ownership as owner_fixtures


class ReadinessTests(unittest.TestCase):
    def test_ready_retains_first_queued_pair_and_terminal_error_overrides_ready(self):
        session = stereo.StereoSession(0, module=Mock(), restore_controls=False)
        session._thread = object()  # no thread starts
        session._ready = True
        sample = {"sequence": 1}
        session._slot = sample
        self.assertTrue(session.wait_ready(0))
        self.assertIs(session._slot, sample)
        self.assertIs(session.next_pair(0), sample)
        session._error = "late driver failure"
        with self.assertRaisesRegex(CameraError, "late driver failure"):
            session.wait_ready(0)

    def test_no_ready_frame_times_out_without_consuming_a_slot(self):
        session = stereo.StereoSession(0, module=Mock(), restore_controls=False)
        session._thread = object()
        with self.assertRaisesRegex(CameraError, "readiness timed out"):
            session.wait_ready(0)
        self.assertIsNone(session._slot)

    def test_old_queued_frame_does_not_acknowledge_pending_controls(self):
        session = stereo.StereoSession(0, module=Mock(), restore_controls=False)
        session._thread = object()
        session._ready = True
        session._slot = {"sequence": 1}
        session._controls.put("transaction", {"state": "pending", "revision": 1})
        with self.assertRaisesRegex(CameraError, "readiness timed out"):
            session.wait_ready(0)
        session._slot = {"sequence": 2}
        session._controls.put("transaction", {"state": "confirmed", "revision": 1})
        self.assertTrue(session.wait_ready(0))
        self.assertEqual(session.next_pair(0)["sequence"], 2)

    def test_async_open_and_strict_read_errors_are_visible_before_ready(self):
        for stage in ("open", "read"):
            with self.subTest(stage=stage):
                session = stereo.StereoSession(0, module=Mock(), restore_controls=False,
                                               strict_controls=True)
                bounded = Mock()
                bounded.release.return_value = True
                bounded.open.return_value.to_dict.return_value = {}
                getattr(bounded, stage).side_effect = CameraError(stage + " rejected")
                with patch.object(stereo, "BoundedCameraSession", return_value=bounded):
                    session._pump()  # injected owner only; no native/Tk/thread
                with self.assertRaisesRegex(CameraError, stage + " rejected"):
                    session.wait_ready(0)
                bounded.release.assert_called_once_with(timeout_s=0.75)
                self.assertFalse(session.report()["cleanup_pending"])

    def fake_session(self, pending):
        session = Mock()
        session.start.return_value = session
        session.done = threading.Event()
        session.report.return_value = {"error": "strict controls rejected",
                                       "cleanup_pending": pending}
        session.wait_ready.side_effect = CameraError("strict controls rejected")
        def stop(**kwargs):
            if not pending:
                session.done.set()
            return not pending
        session.stop.side_effect = stop
        return session

    def test_source_open_propagates_async_failure_after_completed_cleanup(self):
        session = self.fake_session(False)
        with patch.object(source, "_active_session", None), \
             patch.object(source, "StereoSession", return_value=session):
            with self.assertRaises(source.Ps4SourceOpenError) as failure:
                source.Ps4SuiteSource.open(0, controls={"exposure": -7, "gain": 8, "brightness": 1},
                                           ready_timeout_s=.1, strict_controls=True)
        self.assertFalse(failure.exception.cleanup_pending)
        self.assertTrue(failure.exception.source._released)
        session.wait_ready.assert_called_once_with(.1)
        session.stop.assert_called_once_with(timeout_s=.75)

    def test_source_timeout_with_pending_cleanup_prevents_another_owner(self):
        session = self.fake_session(True)
        session.wait_ready.side_effect = CameraError("readiness timed out")
        with patch.object(source, "_active_session", None), \
             patch.object(source, "StereoSession", return_value=session) as factory:
            with self.assertRaises(source.Ps4SourceOpenError) as failure:
                source.Ps4SuiteSource.open(0, controls={"exposure": -7, "gain": 8, "brightness": 1},
                                           ready_timeout_s=.1)
            self.assertTrue(failure.exception.cleanup_pending)
            with self.assertRaisesRegex(CameraError, "owner still active"):
                source.Ps4SuiteSource.open(0)
            factory.assert_called_once()


class TransitionStartupTests(unittest.TestCase):
    def failure(self, pending):
        cap = Mock()
        cap._abandoned = cap.abandoned = False
        cap.cleanup_pending = pending
        cap.release.return_value = not pending
        return source.Ps4SourceOpenError("async strict startup failed", cap), cap

    def test_completed_failed_startup_rolls_back_without_committing_ps4(self):
        error, failed = self.failure(False)
        worker, _, opens, caps, rows = transition_fixtures.TransitionTests().exercise(
            rollback=True, startup_error=error)
        self.assertEqual([item["backend"] for item in opens], ["pseye", "ps4-stereo", "pseye"])
        self.assertTrue(rows[-1]["ok"], rows[-1])
        self.assertEqual(caps[-1].controls, [(21, 2)])
        self.assertFalse(any(row.get("event") == "camera_open" for row in rows))

    def test_pending_new_source_forbids_cross_backend_rollback_and_is_retired(self):
        error, failed = self.failure(True)
        worker, _, opens, caps, rows = transition_fixtures.TransitionTests().exercise(
            rollback=True, startup_error=error)
        self.assertEqual([item["backend"] for item in opens], ["pseye", "ps4-stereo"])
        self.assertTrue(caps[0].cleanup_pending is False)
        self.assertIs(worker._retired_cap, failed)
        self.assertFalse(rows[-1]["ok"], rows[-1])
        self.assertIn("no rollback", rows[-1]["error"])
        with self.assertRaisesRegex(RuntimeError, "cleanup pending"):
            worker.start({})


class IdentityAdapterTests(unittest.TestCase):
    def test_strict_unproven_adapter_never_queries_or_writes_a_different_device(self):
        adapter, state, controls, writes = owner_fixtures.ExplicitPs4ControlsTests().adapter()
        control = controls["exposure"]
        control.capture_identity_confirmed = False
        control.get = Mock(wraps=control.get)
        control.range = Mock(wraps=control.range)
        with self.assertRaisesRegex(CameraError, "identity not confirmed"):
            adapter._apply_transaction(1, {"exposure": -7})
        control.get.assert_not_called()
        control.range.assert_not_called()
        self.assertEqual(writes, [])
        self.assertTrue(control.closed)

    def test_legacy_unproven_adapter_exposes_no_foreign_readback_or_delayed_reads(self):
        adapter, state, controls, writes = owner_fixtures.ExplicitPs4ControlsTests().adapter()
        control = controls["exposure"]
        control.capture_identity_confirmed = False
        control.get = Mock(wraps=control.get)
        adapter._apply_native_control(-7, "exposure")
        for _ in range(4):
            adapter._check_native_readback()
        control.get.assert_not_called()
        self.assertEqual(writes, [])
        self.assertIsNone(state.snapshot()["readback"]["exposure"])
        self.assertIsNone(state.snapshot()["native_exposure"]["range"])


class StopAndPolicyTests(unittest.TestCase):
    def test_updates_during_stop_do_not_replace_queued_settings(self):
        for stopping, stopped in ((True, False), (False, True), (True, True)):
            with self.subTest(stopping=stopping, stopped=stopped):
                worker = tracking.TrackingWorker(".")
                worker._thread = Mock()
                worker._thread.is_alive.return_value = True
                worker._stopping = stopping
                if stopped:
                    worker._stop.set()
                queued = {"already": "queued"}
                worker._settings.put_nowait(queued)
                with self.assertRaises(RuntimeError):
                    worker.update_settings({})
                self.assertIs(worker._settings.get_nowait(), queued)

    def test_stop_between_normalization_and_enqueue_is_checked_under_lock(self):
        worker = tracking.TrackingWorker(".")
        worker._thread = Mock()
        worker._thread.is_alive.return_value = True
        normalize = tracking.normalize_prefs
        def stop_after_validation(prefs):
            result = normalize(prefs)
            with worker._lock:
                worker._stop.set()
            return result
        with patch.object(tracking, "normalize_prefs", side_effect=stop_after_validation):
            with self.assertRaises(RuntimeError):
                worker.update_settings({})
        self.assertTrue(worker._settings.empty())

    def test_restart_during_validation_does_not_receive_old_worker_settings(self):
        worker = tracking.TrackingWorker(".")
        worker._thread = Mock()
        worker._thread.is_alive.return_value = True
        normalize = tracking.normalize_prefs
        def restart_after_validation(prefs):
            result = normalize(prefs)
            with worker._lock:
                worker._thread = Mock()
                worker._thread.is_alive.return_value = True
            return result
        with patch.object(tracking, "normalize_prefs", side_effect=restart_after_validation):
            with self.assertRaises(RuntimeError):
                worker.update_settings({})
        self.assertTrue(worker._settings.empty())

    def test_strict_none_exposure_is_resolved_from_native_range_on_owner(self):
        adapter, state, controls, writes = owner_fixtures.ExplicitPs4ControlsTests().adapter()
        state.put("requested", {"exposure": None, "gain": 8, "brightness": 1})
        adapter._strict_controls = True
        adapter._exposure_policy = "led_frame"
        adapter._fps = 60
        adapter.read()
        self.assertEqual([row[:2] for row in writes], [("exposure", -6), ("gain", 8), ("brightness", 1)])
        report = state.snapshot()
        self.assertIsNone(report["transaction"]["requested"]["exposure"])
        self.assertEqual(report["transaction"]["resolved"]["exposure"], -6)
        self.assertEqual(report["native_exposure"]["policy"], "led_frame")
        self.assertTrue(report["readback_confirmed"]["exposure"])

    def test_strict_none_exposure_without_frame_bounded_manual_range_writes_nothing(self):
        adapter, state, controls, writes = owner_fixtures.ExplicitPs4ControlsTests().adapter()
        state.put("requested", {"exposure": None, "gain": 8, "brightness": 1})
        adapter._strict_controls = True
        adapter._exposure_policy = "led_frame"
        controls["exposure"].range = lambda: {"minimum": -5, "maximum": -1, "step": 1,
                                             "capabilities": 2}
        with self.assertRaisesRegex(CameraError, "frame-bounded"):
            adapter.read()
        self.assertEqual(writes, [])


if __name__ == "__main__":
    unittest.main()
