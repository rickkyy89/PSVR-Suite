"""Offline Suite source contracts: synthetic UVC, centroids and driver IMU only."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import numpy as np

from tools.psvr_camera import ps4_suite_source as source
from tools.psvr_camera.camera_backend import CameraError
from tools.psvr_camera.camera_source import open_camera_source
from tools.psvr_camera.driver_position import build_driver_position_packet
from tools.psvr_camera.imu_led_pose import MODEL_TO_IMU
from tools.psvr_camera.ps4_suite_tracker import Ps4SuiteTracker
from tools.psvr_camera.stereo_calibration import load_stereo_calibration
from tools.psvr_camera.stereo_head_pose import load_front_led_rig
from tools.psvr_suite import tracking
from tools.psvr_suite.settings import default_preferences
from tests.test_camera_ps4_stereo_session import fake_cv

try:
    import cv2
except ImportError:
    cv2 = None

ROOT = Path(__file__).resolve().parents[1]


def imu(now, session="suite-test", epoch=1):
    return {"session_id": session, "recenter_epoch": epoch, "seq": 1,
            "sample_time_s": now, "monotonic_s": now, "connected": True,
            "valid": True, "quaternion_sensor_wxyz": [1., 0., 0., 0.]}


def synthetic(tracker, t=(.01, -.01, .7), rvec=(.12, -.12, .07)):
    rotation = cv2.Rodrigues(np.asarray(rvec, float))[0]
    points = (rotation @ tracker.points.T).T + np.asarray(t)
    cal = tracker.calibration
    def candidates(points, intrinsics):
        pixels = cv2.projectPoints(points, np.zeros(3), np.zeros(3),
                                   intrinsics.camera_matrix,
                                   intrinsics.distortion_coeffs)[0].reshape(-1, 2)
        return [{"center_px": p.tolist()} for p in pixels]
    return (candidates(points, cal.left),
            candidates((cal.R @ points.T).T + cal.T, cal.right), rotation)


def new_tracker():
    profile = source.load_suite_profile(ROOT)
    return Ps4SuiteTracker(load_stereo_calibration(ROOT / profile["calibration_path"]),
                            load_front_led_rig(ROOT / profile["rig_path"]))


class ProfileTests(unittest.TestCase):
    def test_exact_calibration_artifact_and_explicit_order(self):
        p = source.load_suite_profile(ROOT)
        original = ROOT / "logs/ps4_camera/calibration/ps4_stereo_20261023T231947385332Z.swapped.json"
        if original.exists():
            self.assertEqual((ROOT / p["calibration_path"]).read_bytes(), original.read_bytes())
        self.assertEqual(p["calibration_sha256"], "b7c937f920295cd8b2569059210be51b15adc2c1184fb7c80bb004797c072cee")
        self.assertEqual(p["eye_order"], ["right", "left"])
        self.assertEqual(p["controls"], {"brightness": 1, "exposure": -7, "gain": 8})

    def test_effective_profile_does_not_mutate_saved_ps3_fields(self):
        saved = tracking.normalize_prefs(default_preferences())
        before = copy.deepcopy(saved)
        ps4 = dict(saved, camera_source="ps4-stereo", backend="ps4-stereo")
        effective, p = tracking._source_preferences(ROOT, ps4)
        self.assertEqual((effective["width"], effective["height"], effective["fps"]), (640, 400, 60))
        self.assertEqual((effective["value_min"], effective["min_area"]), (160, 20))
        self.assertEqual(ps4["intrinsicsPath"], saved["intrinsicsPath"])
        returned, profile = tracking._source_preferences(ROOT, saved)
        self.assertEqual(returned, before)
        self.assertIsNone(profile)
        self.assertEqual(saved, before)
        self.assertEqual(len(default_preferences()), 29)
        metadata = tracking._camera_metadata(ps4, effective, p)
        self.assertEqual(metadata["requested_camera"]["height"], 480)
        self.assertEqual(metadata["effective_camera"]["height"], 400)
        for name in ("exposure", "gain", "brightness"):
            self.assertEqual(metadata["effective_camera"][name], p["controls"][name])
        self.assertIsNone(metadata["effective_camera"]["imu_rig"])

    def test_mismatch_source_backend_fails_closed(self):
        with self.assertRaises(ValueError):
            tracking.normalize_prefs({"camera_source": "ps4-stereo", "backend": "dshow"})
        with self.assertRaises(CameraError):
            open_camera_source(0, backend="unknown-suite-camera", module=Mock())

    def test_bad_profile_order_fails_without_opening(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "configurations").mkdir()
            data = json.loads((ROOT / source.PROFILE_PATH).read_bytes())
            data["eye_order"] = ["left", "right"]
            (root / source.PROFILE_PATH).write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError), patch.object(source, "StereoSession") as capture:
                source.load_suite_profile(root)
            capture.assert_not_called()


class AdapterTests(unittest.TestCase):
    def test_release_hides_historical_controls_even_while_cleanup_is_pending(self):
        for cleanup_complete in (False, True):
            with self.subTest(cleanup_complete=cleanup_complete):
                controls = {"requested": {"gain": 8}, "native_gain": {
                    "confirmed": True, "capture_identity_confirmed": True}}
                session = Mock()
                session.report.return_value = {"controls": controls, "error": None,
                    "cleanup_pending": not cleanup_complete}
                session.stop.return_value = cleanup_complete
                cap = source.Ps4SuiteSource(session)
                self.assertEqual(cap.controls_status(), controls)
                self.assertEqual(cap.release(), cleanup_complete)
                session.report.reset_mock()
                self.assertIsNone(cap.controls_status())
                self.assertIsNone(cap.settings_capabilities())
                self.assertEqual(cap.read(), (False, None))
                with self.assertRaisesRegex(CameraError, "source is released"):
                    cap.request_controls({"gain": 7})
                session.report.assert_not_called()  # no stale status re-publication
                session.request_controls.assert_not_called()
                self.assertEqual(cap.cleanup_pending, not cleanup_complete)
                self.assertEqual(session.report()["controls"], controls)  # diagnostics retained

    def test_consumed_pair_order_copy_sequence_and_timestamp_are_atomic(self):
        raw = {"left": np.full((400, 640, 3), 11, np.uint8),
               "right": np.full((400, 640, 3), 22, np.uint8),
               "sequence": 17, "host_monotonic": 123456.125}
        session = Mock()
        session.report.return_value = {"error": None, "cleanup_pending": False, "controls": {}}
        session.next_pair.side_effect = [raw, None]
        cap = source.Ps4SuiteSource(session)
        ok, image = cap.read()
        self.assertTrue(ok)
        pair = cap.last_pair
        self.assertIs(image, pair["left"])
        self.assertTrue(np.all(image == 22))
        self.assertTrue(np.all(pair["right"] == 11))
        self.assertEqual(pair["sequence"], 17)
        self.assertEqual(cap.last_frame_monotonic, 123456.125)
        raw["right"].fill(33)
        self.assertTrue(np.all(image == 22))
        self.assertFalse(hasattr(cap, "set_manual_controls"))
        self.assertEqual(cap.read(), (False, None))
        self.assertIsNone(cap.last_pair)

    def test_dispatch_uses_perf_counter_and_source_owned_ps4_controls(self):
        module, caps, work, _, _ = fake_cv()
        work.put(23)
        with patch.object(source, "_active_session", None):
            cap, descriptor = open_camera_source(0, backend="ps4-stereo", width=640,
                                                  height=400, fps=60, module=module)
            try:
                self.assertIs(cap._session._clock, time.perf_counter)
                before = time.perf_counter()
                ok, _ = cap.read()
                self.assertTrue(ok)
                self.assertLessEqual(cap.last_frame_monotonic, time.perf_counter())
                self.assertGreater(cap.last_frame_monotonic, before - 1.)
                writes = [event for event in caps[0].events if event[0] == "set"]
                self.assertIn(("set", 10, 1), writes)
                self.assertIn(("set", 14, 8), writes)
                # Numeric cv2 exposure is never used, even with a fake backend:
                # the missing native adapter is reported unconfirmed instead.
                self.assertFalse(any(e[1] == 15 for e in writes))
                self.assertEqual(descriptor["eye_order"], ["right", "left"])
            finally:
                self.assertTrue(cap.release())
            self.assertTrue(cap.release())

    def test_explicit_session_clock_without_monotonic_offset(self):
        module, _, work, _, _ = fake_cv()
        work.put(2)
        from tools.psvr_camera.ps4_stereo_session import StereoSession
        session = StereoSession(0, module=module, gain=None, brightness=None,
                                clock=lambda: 98765.25).start()
        try:
            self.assertEqual(session.next_pair(.5)["host_monotonic"], 98765.25)
        finally:
            self.assertTrue(session.stop())

    def test_format_validation_precedes_capture(self):
        for args in ({"width": 1748}, {"height": 480}, {"fps": 75}):
            with self.subTest(args=args), patch.object(source, "StereoSession") as factory:
                with self.assertRaises(CameraError):
                    source.Ps4SuiteSource.open(0, **args)
                factory.assert_not_called()

    def test_terminal_error_never_delivers_pending_stale_pair(self):
        session = Mock()
        session.report.return_value = {"error": "stuck read", "cleanup_pending": True}
        cap = source.Ps4SuiteSource(session)
        self.assertEqual(cap.read(), (False, None))
        session.next_pair.assert_not_called()
        self.assertEqual(cap.error, "stuck read")

    def test_cleanup_pending_prevents_new_owner(self):
        old = Mock()
        old.done.is_set.return_value = True
        old.report.return_value = {"cleanup_pending": True}
        with patch.object(source, "_active_session", old), patch.object(source, "StereoSession") as factory:
            with self.assertRaises(CameraError):
                source.Ps4SuiteSource.open(0)
            factory.assert_not_called()

    def test_invalid_geometry_and_cleanup_are_bounded(self):
        module, caps, work, _, released = fake_cv(invalid_geometry=True)
        work.put(1)
        with patch.object(source, "_active_session", None):
            # Startup now waits for a valid pair, rather than returning a source
            # which can fail only after the caller has committed the transition.
            with self.assertRaises(source.Ps4SourceOpenError) as failed:
                source.Ps4SuiteSource.open(0, module=module, read_timeout_s=.1)
            self.assertIn("requires BGR", str(failed.exception))
            self.assertFalse(failed.exception.cleanup_pending)
            self.assertTrue(released.is_set())


@unittest.skipIf(cv2 is None, "requires optional OpenCV, no numerical fallback")
class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.tracker = new_tracker()
        self.left, self.right, self.rotation = synthetic(self.tracker)

    def align(self, now=10.):
        self.tracker.set_imu(imu(now))
        pose = self.tracker.update_stereo(self.left, self.right, now)
        self.assertTrue(pose.valid, pose.reason)
        return pose

    def test_initial_pose_alignment_and_existing_v2_bridge(self):
        pose = self.align()
        np.testing.assert_allclose(self.tracker.alignment, self.rotation @ MODEL_TO_IMU.T, atol=1e-5)
        packet = build_driver_position_packet(pose, imu(10), self.tracker.alignment, 3,
                                               head_offset_model_m=(.01, .02, .03), scale=1.2, now=10)
        self.assertEqual(packet["version"], 2)
        self.assertTrue(packet["valid"])
        expected = self.tracker.alignment.T @ (1.2 * np.array([.01, -.01, .7])
                                                + self.rotation @ np.array([.01, .02, .03]))
        np.testing.assert_allclose(packet["position_m"], expected, atol=1e-5)
        self.assertEqual(packet["session_id"], "suite-test")

    def test_fast_path_never_repeats_free_stereo_pnp(self):
        self.align()
        left, right, _ = synthetic(self.tracker, t=(.012, -.01, .7))
        self.tracker.set_imu(imu(10.02))
        with patch("tools.psvr_camera.ps4_suite_tracker.estimate_stereo_head_pose_from_triangulation",
                   side_effect=AssertionError("free PnP in hot path")):
            pose = self.tracker.update_stereo(left[::-1], right, 10.02)
        self.assertTrue(pose.valid, pose.reason)
        self.assertEqual(pose.reason, "ps4_stereo_imu_guided")
        np.testing.assert_allclose(pose.tvec.reshape(3), [.012, -.01, .7], atol=1e-5)

    def test_same_session_recenter_and_reacquisition_preserve_alignment(self):
        self.align()
        alignment = self.tracker.alignment.copy()
        self.tracker.set_imu(imu(11, epoch=2))
        with patch("tools.psvr_camera.ps4_suite_tracker.estimate_stereo_head_pose_from_triangulation",
                   side_effect=AssertionError("realigned on recenter")):
            self.assertTrue(self.tracker.update_stereo(self.left, self.right, 11).valid)
            self.tracker.set_imu(imu(20, epoch=2))
            self.assertTrue(self.tracker.update_stereo(self.left, self.right, 20).valid)
        np.testing.assert_array_equal(self.tracker.alignment, alignment)

    def test_new_session_and_reset_clear_alignment(self):
        self.align()
        self.tracker.set_imu(imu(11, session="new-activation"))
        self.assertIsNone(self.tracker.alignment)
        self.tracker.reset()
        self.assertIsNone(self.tracker.alignment)

    def test_stale_imu_invalidates_existing_v2_without_poisoning_alignment(self):
        self.align()
        alignment = self.tracker.alignment.copy()
        self.tracker.set_imu(imu(10))
        pose = self.tracker.update_stereo(self.left, self.right, 11)
        self.assertFalse(pose.valid)
        packet = build_driver_position_packet(pose, imu(10), alignment, 4, now=11)
        self.assertFalse(packet["valid"])
        self.assertIsNone(packet["position_m"])
        np.testing.assert_array_equal(self.tracker.alignment, alignment)

    def test_partial_ambiguous_and_two_led_remain_invalid(self):
        self.align()
        self.tracker.set_imu(imu(10.02))
        self.assertFalse(self.tracker.update_stereo(self.left[:4], self.right[:4], 10.02).valid)
        self.assertEqual(self.tracker.diagnostics()["left_candidate_count"], 4)
        self.assertEqual(self.tracker.diagnostics()["right_candidate_count"], 4)
        with patch("tools.psvr_camera.ps4_suite_tracker.triangulate_candidates",
                   return_value={"state": "ambiguous", "matches": []}):
            self.assertFalse(self.tracker.update_stereo(self.left, self.right, 10.02).valid)
        self.tracker.allow_two_led = True
        self.assertIn("unsupported", self.tracker.update_stereo(self.left, self.right, 10.02).reason)

    def test_wrong_eye_order_no_silent_retry(self):
        self.tracker.set_imu(imu(10))
        self.assertFalse(self.tracker.update_stereo(self.right, self.left, 10).valid)
        self.assertIsNone(self.tracker.alignment)

    def test_initial_acquisition_is_rate_bounded(self):
        self.tracker.set_imu(imu(10))
        rejected = {"valid": False, "reason": "ambiguous"}
        with patch("tools.psvr_camera.ps4_suite_tracker.estimate_stereo_head_pose_from_triangulation",
                   return_value=rejected) as solve:
            self.assertFalse(self.tracker.update_stereo(self.left, self.right, 10).valid)
            self.tracker.set_imu(imu(10.02))
            self.assertIn("await_alignment", self.tracker.update_stereo(self.left, self.right, 10.02).reason)
            self.assertEqual(solve.call_count, 1)

    def test_teleport_rejection_keeps_last_good_translation(self):
        self.align()
        previous = self.tracker.translation.copy()
        left, right, _ = synthetic(self.tracker, t=(.11, -.01, .7))
        self.tracker.set_imu(imu(10.02))
        pose = self.tracker.update_stereo(left, right, 10.02)
        self.assertFalse(pose.valid)
        np.testing.assert_array_equal(self.tracker.translation, previous)

    def test_setup_uses_front5_and_stereo_loader_not_ps3_intrinsics(self):
        prefs, profile = tracking._source_preferences(ROOT, tracking.normalize_prefs({"camera_source": "ps4-stereo"}))
        with patch.object(tracking, "load_intrinsics", side_effect=AssertionError("mono loader")), \
             patch.object(tracking, "load_rotation_rig", side_effect=AssertionError("nine-point rig")):
            intrinsics, _, front, _, points, normals, labels, tracker = tracking._load_optical_setup(ROOT, prefs, profile)
        self.assertEqual(intrinsics.image_size, (640, 400))
        self.assertEqual(points.shape, (5, 3))
        self.assertIsNone(normals)
        self.assertEqual(len(labels), 5)


@unittest.skipIf(cv2 is None, "requires optional OpenCV")
class WorkerTests(unittest.TestCase):
    def test_existing_worker_ps4_bridge_ps3_roundtrip_and_metadata(self):
        template = new_tracker()
        left, right, _ = synthetic(template)
        def render(candidates, height=400):
            image = np.zeros((height, 640, 3), np.uint8)
            for candidate in candidates:
                cv2.circle(image, tuple(np.rint(candidate["center_px"]).astype(int)), 5, (255, 0, 0), -1)
            return image
        class Cap:
            error = None
            def __init__(self, stereo):
                self.stereo = stereo
                self.released = False
                self.seq = 0
            def read(self):
                time.sleep(.01)
                if self.released:
                    return False, None
                self.seq += 1
                self.last_frame_monotonic = time.perf_counter()
                if self.stereo:
                    self.last_pair = {"left": render(left), "right": render(right),
                                      "host_monotonic": self.last_frame_monotonic, "sequence": self.seq}
                    return True, self.last_pair["left"]
                return True, np.zeros((480, 640, 3), np.uint8)
            def release(self):
                self.released = True
                return True
        class Imu:
            def at(self, now):
                return imu(now)
            def latest(self):
                return imu(time.perf_counter())
            def close(self):
                pass
        class Sender:
            def __init__(self):
                self.packets = []
                self.last_packet = None
            def send(self, packet):
                self.packets.append(packet)
                self.last_packet = packet
                return True
            def invalidate(self):
                return True
            def close(self):
                pass
        sender = Sender()
        caps, opens, events = [], [], []
        def capture(index, **kwargs):
            opens.append((index, kwargs))
            cap = Cap(kwargs["backend"] == "ps4-stereo")
            caps.append(cap)
            return cap, None
        def wait(predicate):
            deadline = time.perf_counter() + 5
            while not predicate() and time.perf_counter() < deadline:
                time.sleep(.01)
            self.assertTrue(predicate())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "configurations").mkdir()
            for name in ("ps4_suite_source.json", "ps4_stereo_640x400_suite.json",
                         "psvr_front_leds_stereo_refined_20261028.json",
                         "pseye_blue_640x480_calibrated.json", "psvr_front_leds_refined_20261011.json",
                         "psvr_nine_leds_refined_sides_20261011.json"):
                shutil.copyfile(ROOT / "configurations" / name, root / "configurations" / name)
            with patch.object(tracking, "open_camera_source", side_effect=capture), \
                 patch.object(tracking, "DriverImuSource", return_value=Imu()), \
                 patch.object(tracking, "DriverPositionSender", return_value=sender):
                worker = tracking.TrackingWorker(root, events.append)
                prefs = default_preferences()
                prefs["camera_source"] = "ps4-stereo"
                worker.start(prefs)
                try:
                    wait(lambda: any(p["valid"] for p in sender.packets))
                    status = worker.status()
                    self.assertEqual(status["prefs"]["height"], 480)
                    self.assertEqual(status["effective_camera"]["height"], 400)
                    self.assertEqual(opens[0][1]["fps"], 60)
                    self.assertEqual(opens[0][1]["backend"], "ps4-stereo")
                    self.assertEqual(sender.packets[-1]["version"], 2)
                    for sid in ("pseye", "ps4-stereo"):
                        update = dict(prefs, camera_source=sid)
                        worker.update_settings(update)
                        wait(lambda: worker.status()["prefs"]["camera_source"] == sid)
                    self.assertEqual(worker.status()["prefs"]["intrinsicsPath"], prefs["intrinsicsPath"])
                    self.assertEqual(worker.status()["log"], status["log"])
                finally:
                    self.assertTrue(worker.stop())
            rows = [json.loads(line) for line in Path(status["log"]).read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[0]["source_profile"]["eye_order"], ["right", "left"])
            reopened = [row for row in rows if row.get("event") == "camera_open"]
            self.assertEqual(len(reopened), 2)
            self.assertEqual(reopened[-1]["source_profile"]["calibration_sha256"],
                             source.load_suite_profile(ROOT)["calibration_sha256"])
            self.assertTrue(all(cap.released for cap in caps))
            self.assertTrue(any(row.get("stereo_sequence") for row in rows))
            self.assertTrue(events)


class PublicationFreshnessTests(unittest.TestCase):
    """Exercise the actual worker/send/filter path, not just its age helper."""

    def exercise(self, stage=None, change=None):
        from tools.psvr_camera.led_pose import LedPose
        clock = [100.]
        original = imu(clock[0])
        if change == "original_age":
            original["monotonic_s"] -= tracking.MAX_AGE_S * .75
        latest = dict(imu(clock[0]), seq=999)

        def processing_hook(where):
            if stage != where:
                return
            if change == "delay":
                clock[0] += tracking.MAX_AGE_S + .01
            elif change == "original_age":
                clock[0] += tracking.MAX_AGE_S * .5
            elif change == "session":
                latest["session_id"] = "new-activation"
            elif change == "epoch":
                latest["recenter_epoch"] += 1
            elif change == "invalid":
                latest["valid"] = False
            elif change == "disconnected":
                latest["connected"] = False

        frame = np.zeros((400, 640, 3), np.uint8)
        cap = Mock()
        cap.error = None
        cap.last_frame_monotonic = clock[0]
        cap.last_pair = {"left": frame, "right": frame,
                         "host_monotonic": clock[0], "sequence": 1}
        cap.read.return_value = True, frame
        cap.release.return_value = True
        cap.controls_status.return_value = None
        # A generic Mock fabricates attributes; explicitly exclude PS3 controls.
        del cap.set_manual_controls
        imu_source = Mock()
        imu_source.at.return_value = original
        imu_source.latest.side_effect = lambda: latest.copy()
        sender = Mock()
        sender.last_packet = None
        packets = []
        sender.send.side_effect = lambda packet: packets.append(packet) or True

        pose = LedPose(True, "ps4_stereo_imu_guided", np.zeros((5, 2)),
                       np.zeros(3), np.array([0., 0., .7]), .1, 1.5, 100.)
        pose.led_labels = ["a", "b", "c", "d", "e"]
        tracker = Mock()
        tracker.alignment = np.eye(3)
        tracker.search_region.return_value = None, 1.
        tracker.diagnostics.return_value = {}
        def update(*args):
            processing_hook("solve")
            return pose
        tracker.update_stereo.side_effect = update
        intrinsics = Mock()
        intrinsics.image_size = (640, 400)
        rig = {"labels": pose.led_labels}
        setup = (intrinsics, rig, np.zeros((5, 3)), rig,
                 np.zeros((5, 3)), None, pose.led_labels, tracker)
        detector = Mock()
        detector.detect.return_value = []
        detector.annotate.side_effect = lambda image, candidates: image
        detector.cv2 = None
        pipeline = tracking.PositionPipeline("one_euro")
        pipeline.reset = Mock(wraps=pipeline.reset)
        filter_packet = pipeline.filter_packet
        def filter_then_check(*args, **kwargs):
            result = filter_packet(*args, **kwargs)
            processing_hook("filter")
            return result
        pipeline.filter_packet = filter_then_check
        def covariance(*args, **kwargs):
            processing_hook("covariance")
            return np.eye(3) * .0001, {"accepted": True, "reliable": True}
        profile = source.load_suite_profile(ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = tracking.TrackingWorker(root)
            # Stop only after the first frame has completed the entire pipeline.
            def event(kind, payload):
                if kind == "frame":
                    worker._stop.set()
            with patch.object(tracking.time, "perf_counter", side_effect=lambda: clock[0]), \
                 patch.object(source, "load_suite_profile", return_value=profile), \
                 patch.object(tracking, "_load_optical_setup", return_value=setup), \
                 patch.object(tracking, "open_camera_source", return_value=(cap, None)), \
                 patch.object(tracking, "DriverImuSource", return_value=imu_source), \
                 patch.object(tracking, "DriverPositionSender", return_value=sender), \
                 patch.object(tracking, "BlueLedDetector", return_value=detector), \
                 patch.object(tracking, "_make_pipeline", return_value=pipeline), \
                 patch.object(tracking, "estimate_pose_covariance", side_effect=covariance), \
                 patch.object(worker, "_emit", side_effect=event):
                worker._run(tracking.normalize_prefs({"camera_source": "ps4-stereo"}))
            rows = [json.loads(line) for line in Path(worker.status()["log"]).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows[-1]["event"], "end")
        self.assertTrue(rows[-1]["ok"], rows[-1])
        return packets, sender, pipeline, [r for r in rows if r["event"] == "pose"][0]

    def assert_veto(self, stage, change):
        packets, sender, pipeline, row = self.exercise(stage, change)
        self.assertFalse(any(packet["valid"] for packet in packets), packets)
        self.assertGreater(sender.invalidate.call_count, 0)
        self.assertTrue(any(str(call.args[0]).startswith("ps4_stereo_")
                            for call in pipeline.reset.call_args_list))
        self.assertFalse(row["valid"])
        self.assertFalse(row["estimator_input"]["context_valid"])
        self.assertFalse(row["estimator_input"]["prediction_allowed"])
        self.assertFalse(row["estimator_input"]["packet"]["valid"])
        return row

    def test_delayed_solve_covariance_and_filter_cannot_publish_valid_one_euro(self):
        for stage in ("solve", "covariance", "filter"):
            with self.subTest(stage=stage):
                self.assert_veto(stage, "delay")

    def test_inflight_session_and_epoch_changes_cannot_publish_valid_one_euro(self):
        for stage in ("solve", "covariance", "filter"):
            for change in ("session", "epoch"):
                with self.subTest(stage=stage, change=change):
                    self.assert_veto(stage, change)

    def test_original_sample_must_still_be_fresh_even_when_capture_is_fresh(self):
        row = self.assert_veto("covariance", "original_age")
        self.assertEqual(row["reason"], "ps4_stereo_original_imu_expired")

    def test_latest_invalid_or_disconnected_vetoes_before_send(self):
        for change in ("invalid", "disconnected"):
            with self.subTest(change=change):
                self.assert_veto("filter", change)

    def test_matching_binding_publishes_original_not_newer_sample(self):
        packets, sender, pipeline, row = self.exercise()
        self.assertEqual(len(packets), 1)
        self.assertTrue(packets[0]["valid"])
        self.assertEqual(packets[0]["imu_seq"], 1)  # latest() has seq=999
        self.assertEqual(packets[0]["imu_sample_time_s"], 100.)
        self.assertTrue(row["valid"])
        self.assertTrue(row["estimator_input"]["context_valid"])
        self.assertTrue(row["estimator_input"]["prediction_allowed"])
        self.assertEqual(row["estimator_input"]["quality"]["covariance_model"],
                         "reference_eye_front5_approximation")


if __name__ == "__main__":
    unittest.main()
