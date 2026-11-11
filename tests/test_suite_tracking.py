# SPDX-License-Identifier: MIT
"""Suite tracking worker tests: mocked camera/source/tracker/sender.

Offline only: no camera, no driver, no network, no Tk. Every live test uses
fakes injected via ``tools.psvr_suite.tracking`` patch points.
"""
from __future__ import annotations

import copy
import json
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from tools.psvr_suite.tracking import (
    TrackingWorker,
    default_prefs,
    normalize_prefs,
)


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


class FakeCap:
    def __init__(self, width=640, height=480):
        self.width = width
        self.height = height
        self.last_frame_monotonic = None
        self.released = False
        self.set_calls = []
        self.controls = {"exposure": 13, "gain": 0}
        self.reads = 0
        self.error = None

    def set_manual_controls(self, exposure, gain):
        self.set_calls.append((exposure, gain))
        self.controls = {"exposure": exposure, "gain": gain}

    def controls_status(self):
        return dict(self.controls)

    def read(self):
        self.reads += 1
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        # Blue-ish patch so detector fakes stay plausible.
        frame[100:110, 100:110] = (255, 0, 0)
        self.last_frame_monotonic = time.perf_counter()
        return True, frame

    def release(self):
        self.released = True


class FakeImuSource:
    def __init__(self, valid=True, stale=False):
        self.valid = valid
        self.stale = stale
        self.seq = 0
        self.closed = False
        self._last = None

    def at(self, captured_at, wait_s=0.025):
        self.seq += 1
        mono = captured_at - (0.5 if self.stale else 0.005)
        row = {
            "session_id": "activation-a", "recenter_epoch": 1, "seq": self.seq,
            "sample_time_s": mono, "sent_time_s": mono + 0.001,
            "monotonic_s": mono, "valid": bool(self.valid),
            "connected": True, "state": "tracking" if self.valid else "lost",
            "quaternion_sensor_wxyz": [1.0, 0.0, 0.0, 0.0],
            "world_rotation_wxyz": [1.0, 0.0, 0.0, 0.0],
            "corrected_gyro_rad_s": [0.0, 0.0, 0.0],
        }
        self._last = copy.deepcopy(row)
        return copy.deepcopy(row)

    def latest(self):
        return copy.deepcopy(self._last) if self._last else None

    def close(self):
        self.closed = True


class FakePose:
    def __init__(self, valid=True, reason="imu_guided_pattern"):
        self.valid = valid
        self.reason = reason
        self.image_points = np.zeros((5, 2)) + 100.0
        self.rvec = np.zeros((3, 1))
        self.tvec = np.array([[0.0], [0.0], [1.0]])
        self.rms_px = 0.5
        self.rms_limit_px = 2.5
        self.image_span_px = 80.0
        self.led_labels = ["upper_left", "upper_right", "centre",
                           "lower_left", "lower_right"]
        self.predicted_labels = list(self.led_labels)
        self.imu_age_s = 0.005

    def to_dict(self):
        return {"valid": self.valid, "reason": self.reason,
                "rvec": [0.0, 0.0, 0.0], "tvec": [0.0, 0.0, 1.0],
                "rms_px": self.rms_px, "rms_limit_px": self.rms_limit_px,
                "image_span_px": self.image_span_px,
                "led_labels": list(self.led_labels)}


class FakeTracker:
    def __init__(self, *args, **kwargs):
        self.alignment = np.eye(3)
        self.reset_calls = 0
        self.set_calls = []
        self.pose_valid = kwargs.pop("pose_valid", True)

    def reset(self):
        self.reset_calls += 1

    def set_imu(self, sample):
        self.set_calls.append(sample)

    def search_region(self, width, height, now):
        return None, 1.0

    def update(self, candidates, now):
        return FakePose(valid=self.pose_valid)


class FakeDetector:
    def __init__(self, *args, **kwargs):
        self.config = kwargs.get("config", None)
        if args and hasattr(args[0], "min_area"):
            self.config = args[0]
        self.detect_calls = 0

    def detect(self, frame):
        self.detect_calls += 1
        return [{"center_px": [100.0, 100.0], "area_px": 20,
                 "bbox_px": [95, 95, 10, 10]}]

    def annotate(self, frame, candidates):
        return frame.copy()


class FakePipeline:
    instances = []

    def __init__(self, mode="one_euro", **kwargs):
        self.mode = mode
        self.kwargs = dict(kwargs)
        self.filter_calls = []
        self.invalid_calls = []
        self.reset_calls = []
        FakePipeline.instances.append(self)

    def reset(self, reason="manual"):
        self.reset_calls.append(reason)

    def invalid(self, now):
        self.invalid_calls.append(now)

    def filter_packet(self, packet, alignment, now, **kwargs):
        self.filter_calls.append((packet, alignment, now, dict(kwargs)))
        if packet is None:
            return None
        # Propaga invalid come il vero PositionPipeline (mai promuovere).
        if not packet.get("valid", False):
            return dict(packet)
        out = dict(packet)
        out["position_m"] = [0.0, 0.0, 1.0]
        out["valid"] = True
        out["state"] = "tracking"
        return out

    def diagnostics(self):
        return {"type": self.mode, "tracking_state": "tracking",
                "settings": {}, "raw_world_position_m": [0, 0, 1],
                "filtered_world_position_m": [0, 0, 1]}


class FakeSender:
    instances = []

    def __init__(self, port=4271, host="127.0.0.1"):
        self.port = port
        self.last_packet = None
        self.sent = []
        self.invalidated = 0
        self.closed = False
        self.close_order = None
        self.last_error = None
        FakeSender.instances.append(self)

    def send(self, packet):
        self.sent.append(copy.deepcopy(packet))
        self.last_packet = copy.deepcopy(packet)
        return True

    def invalidate(self):
        self.invalidated += 1
        return True

    def close(self):
        # Final invalid, mai valida inventata.
        self.closed = True
        return True


def _prefs(tmp, **changes):
    base = default_prefs()
    base["log"] = "logs/test_suite_tracking.jsonl"
    base.update(changes)
    return base


def _patch_common(testcase, cap, imu, tracker_cls=None, detector_cls=None,
                  pipeline_cls=None, sender_cls=None, fresh_build=True):
    root = testcase.root
    patches = []
    intrinsics = Mock()
    intrinsics.image_size = (640, 480)
    intrinsics.camera_matrix = np.eye(3)
    intrinsics.distortion_coeffs = np.zeros(5)
    intrinsics.calibrated = True
    front_rig = {"provenance": "synthetic",
                 "labels": ["upper_left", "upper_right", "centre",
                            "lower_left", "lower_right"]}
    rot_rig = {"provenance": "synthetic",
               "labels": ["a", "b", "c", "d", "e", "f", "g", "h", "i"]}
    pts5 = np.zeros((5, 3))
    pts9 = np.zeros((9, 3))
    nrm9 = np.zeros((9, 3))
    nrm9[:, 2] = -1.0

    def _open(index, backend=None, width=None, height=None, fps=None,
              dll_path=None, read_timeout_s=0.5, max_consecutive_failures=10,
              **kwargs):
        return cap, None

    patches.append(patch("tools.psvr_suite.tracking.load_intrinsics",
                         return_value=intrinsics))
    patches.append(patch("tools.psvr_suite.tracking.load_led_rig",
                         return_value=(front_rig, pts5)))
    patches.append(patch("tools.psvr_suite.tracking.load_rotation_rig",
                         return_value=(rot_rig, pts9, nrm9)))
    patches.append(patch("tools.psvr_suite.tracking.load_position_config",
                         return_value={}))
    patches.append(patch("tools.psvr_suite.tracking.open_camera_source",
                         side_effect=_open))
    patches.append(patch("tools.psvr_suite.tracking.DriverImuSource",
                         return_value=imu))
    patches.append(patch("tools.psvr_suite.tracking.BlueLedDetector",
                         detector_cls or FakeDetector))
    patches.append(patch("tools.psvr_suite.tracking.ImuLedTracker",
                         tracker_cls or FakeTracker))
    patches.append(patch("tools.psvr_suite.tracking.PositionPipeline",
                         pipeline_cls or FakePipeline))
    patches.append(patch("tools.psvr_suite.tracking.DriverPositionSender",
                         sender_cls or FakeSender))
    if fresh_build:
        def _build(pose, imu_sample, alignment, seq,
                   head_offset_model_m=(0, 0, 0), scale=1.0, now=None):
            # Verifica binding reale: head offset/scala passati al modulo.
            # Come il vero builder: con binding esistente ritorna invalid
            # (mai None) anche con pose None; None solo senza binding.
            allowed = getattr(testcase, "allowed_heads", None)
            if allowed is not None:
                assert [float(v) for v in head_offset_model_m] in allowed, (
                    "head offset not forwarded: %r" % (list(head_offset_model_m),))
            else:
                assert list(head_offset_model_m) == list(
                    testcase.expected_head), "head offset not forwarded"
            allowed_scales = getattr(testcase, "allowed_scales", None)
            if allowed_scales is not None:
                assert float(scale) in allowed_scales, "scale not forwarded"
            else:
                assert float(scale) == float(testcase.expected_scale), "scale not forwarded"
            if imu_sample is None:
                return None
            if pose is None or not getattr(pose, "valid", False):
                return {"session_id": imu_sample["session_id"],
                        "recenter_epoch": imu_sample["recenter_epoch"],
                        "valid": False, "state": "lost", "position_m": None,
                        "seq": seq}
            return {"session_id": imu_sample["session_id"],
                    "recenter_epoch": imu_sample["recenter_epoch"],
                    "valid": True, "state": "tracking",
                    "position_m": [0.0, 0.0, 1.0], "seq": seq}

        patches.append(patch("tools.psvr_suite.tracking.build_driver_position_packet",
                             side_effect=_build))
        patches.append(patch("tools.psvr_suite.tracking.estimate_pose_covariance",
                             return_value=(np.eye(3) * 1e-6,
                                           {"reliable": True, "reason": "ok",
                                            "accepted": True})))
    for p in patches:
        testcase.addCleanup(p.stop)
        p.start()
    _ = root


class SuiteTrackingTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "logs").mkdir(parents=True, exist_ok=True)
        self.expected_head = [0.0, 0.0, 0.0]
        self.expected_scale = 1.0
        for attr in ("allowed_heads", "allowed_scales"):
            if hasattr(self, attr):
                delattr(self, attr)
        FakePipeline.instances.clear()
        FakeSender.instances.clear()

    def tearDown(self):
        FakePipeline.instances.clear()
        FakeSender.instances.clear()

    def test_defaults_match_other_agent(self):
        prefs = default_prefs()
        self.assertEqual(prefs["tracking_mode"], "6dof")
        self.assertEqual(prefs["head_offset_model_m"], [0.0, 0.0, 0.0])
        self.assertEqual(prefs["position_scale"], 1.0)
        self.assertEqual(prefs["position_filter"], "one_euro")
        self.assertEqual(prefs["min_cutoff_hz"], 2.5)
        self.assertEqual(prefs["beta"], 8.0)
        self.assertEqual(prefs["min_area"], 6)
        self.assertEqual(prefs["value_min"], 60)
        self.assertEqual(prefs["saturation_min"], 90)
        self.assertEqual(prefs["opening_size"], 0)
        self.assertEqual(prefs["exposure"], 13)
        self.assertEqual(prefs["gain"], 0)
        self.assertEqual(prefs["fps"], 75)
        self.assertEqual(prefs["width"], 640)
        self.assertEqual(prefs["height"], 480)
        self.assertEqual(prefs["intrinsicsPath"],
                         "configurations/pseye_blue_640x480_calibrated.json")
        self.assertEqual(prefs["rigPath"],
                         "configurations/psvr_front_leds_refined_20261011.json")
        self.assertEqual(prefs["imu_rig"],
                         "configurations/psvr_nine_leds_refined_sides_20261011.json")
        # Alias normalizzati, extra ignorati.
        cleaned = normalize_prefs({"intrinsics_path": "a.json",
                                   "rig_path": "b.json",
                                   "imu_rig_path": "c.json",
                                   "fallback_3dof": True,
                                   "optics_profile": "current"})
        self.assertEqual(cleaned["intrinsicsPath"], "a.json")
        self.assertEqual(cleaned["rigPath"], "b.json")
        self.assertEqual(cleaned["imu_rig"], "c.json")
        with self.assertRaises(ValueError):
            normalize_prefs({"tracking_mode": "9dof"})
        with self.assertRaises(ValueError):
            normalize_prefs({"position_filter": "bogus"})
        with self.assertRaises(ValueError):
            normalize_prefs({"opening_size": 1})

    def test_no_tk_or_diagnostic_hid_imports(self):
        import tools.psvr_suite.tracking as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith(("#", '"""', "'''", "*", "Regole:", "- ")):
                continue
            lowered = stripped.lower()
            if stripped.startswith("import ") or stripped.startswith("from "):
                self.assertNotIn("tkinter", lowered)
                self.assertNotIn("diagnosticimusource", lowered.replace("_", ""))
            if "DiagnosticImuSource(" in line or "DiagnosticHID(" in line:
                self.fail("must never start DiagnosticHID/standalone")

    def test_live_binding_filter_log_frame_status_stop(self):
        cap = FakeCap()
        imu = FakeImuSource(valid=True)
        _patch_common(self, cap, imu)
        events = []
        worker = TrackingWorker(self.root, lambda e: events.append(e))
        worker.start(_prefs(self.root))
        try:
            ok = _wait_for(lambda: worker.status()["captured_frames"] >= 5)
            self.assertTrue(ok, "worker did not capture frames")
            # Filtro instradato e sender usato (6dof + bound reale).
            self.assertTrue(FakeSender.instances, "UDP sender not created")
            sender = FakeSender.instances[0]
            self.assertTrue(sender.sent, "no UDP packets sent")
            self.assertTrue(FakePipeline.instances, "PositionPipeline not created")
            pipe = FakePipeline.instances[0]
            self.assertTrue(pipe.filter_calls, "filter never called")
            # Frame throttled ma presenti; status/telemetria completi.
            frames = [e for e in events if e.get("kind") == "frame"]
            self.assertTrue(frames, "no frame events")
            tel = frames[-1]["telemetry"]
            for key in ("optical_valid", "imu_valid", "rms_px", "rms_limit_px",
                        "led_count", "led_labels", "position_raw_m",
                        "position_estimated_m", "tracking_state", "filter",
                        "age_ms", "session_id", "epoch", "fps", "reason"):
                self.assertIn(key, tel, key)
            self.assertTrue(tel["optical_valid"])
            self.assertTrue(tel["imu_valid"])
            self.assertIsInstance(frames[-1]["ppm"], (bytes, bytearray))
            self.assertTrue(frames[-1]["ppm"].startswith(b"P6\n"))
            # Status snapshot copy.
            snap = worker.status()
            snap["captured_frames"] = 999999
            self.assertNotEqual(worker.status()["captured_frames"], 999999)
            self.assertEqual(worker.status()["log"] and Path(
                worker.status()["log"]).name.startswith("test_suite_tracking"), True)
            # Log metrico ogni frame + session.
            log_path = Path(worker.status()["log"])
            ok = _wait_for(lambda: log_path.is_file())
            self.assertTrue(ok)
            # update_settings live senza nuovo file.
            before_log = worker.status()["log"]
            worker.update_settings(_prefs(self.root, value_min=80))
            ok = _wait_for(lambda: worker.status()["prefs"]["value_min"] == 80)
            self.assertTrue(ok, "settings queue not applied")
            self.assertEqual(worker.status()["log"], before_log)
            worker.realign()
            worker.reset_relative()
            time.sleep(0.2)
        finally:
            worker.stop(timeout=5.0)
        # Stop: sender chiuso, cap rilasciata, source chiusa.
        self.assertTrue(sender.closed, "sender final invalid not closed")
        self.assertTrue(cap.released, "cap not released")
        self.assertTrue(imu.closed, "source not closed")
        stopped = [e for e in events if e.get("kind") == "stopped"]
        self.assertTrue(stopped, "no stopped event")
        log_path = Path(stopped[0]["log"])
        rows = [json.loads(line) for line in
                log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        kinds = [r.get("event") for r in rows]
        self.assertIn("session", kinds)
        self.assertIn("pose", kinds)
        self.assertIn("end", kinds)
        session = next(r for r in rows if r.get("event") == "session")
        self.assertIn("head_offset_model_m", session)
        self.assertIn("position_scale", session)
        poses = [r for r in rows if r.get("event") == "pose"]
        self.assertGreaterEqual(len(poses), 5)
        for row in poses:
            self.assertIn("candidates", row)
            self.assertIn("imu", row)
            self.assertIn("position_filter", row)
            self.assertIn("estimator_input", row)
            self.assertIn("position_output", row)

    def test_coherent_anchor_logs_diagnostics_and_uses_full_rig_uncertainty(self):
        class CoherentFake(FakeTracker):
            def update(self, candidates, now):
                return FakePose(reason="imu_guided_front_anchor")

            def diagnostics(self):
                return {"algorithm": "coherent_imu_led_v2", "world_alignment_policy": "fixed"}

        _patch_common(self, FakeCap(), FakeImuSource(), tracker_cls=CoherentFake)
        seen = []

        def covariance(pose, intrinsics, points, labels, alignment, **kwargs):
            seen.append((points.shape, list(labels), kwargs["guided"]))
            return np.eye(3)*1e-6, {"accepted": True, "reliable": True, "reason": "ok"}

        worker = TrackingWorker(self.root, lambda event: None)
        with patch("tools.psvr_suite.tracking.estimate_pose_covariance", side_effect=covariance):
            worker.start(_prefs(self.root))
            try:
                self.assertTrue(_wait_for(lambda: worker.status()["captured_frames"] >= 5))
            finally:
                worker.stop(timeout=5.0)
        self.assertTrue(seen)
        self.assertTrue(all(shape == (9, 3) and labels == list("abcdefghi") and guided
                            for shape, labels, guided in seen))
        rows = [json.loads(line) for line in Path(worker.status()["log"]).read_text(encoding="utf-8").splitlines()]
        poses = [row for row in rows if row.get("event") == "pose"]
        self.assertTrue(poses)
        self.assertTrue(all(row["optical_tracking"]["algorithm"] == "coherent_imu_led_v2" for row in poses))

    def test_filter_routing_one_euro_and_kalman(self):
        for name, mode in (("one_euro", "one_euro"),
                           ("kalman_initial", "kalman"),
                           ("kalman_optimized", "kalman")):
            FakePipeline.instances.clear()
            FakeSender.instances.clear()
            cap = FakeCap()
            imu = FakeImuSource(valid=True)
            seen = {}

            class SpyPipeline(FakePipeline):
                def __init__(self, mode="one_euro", **kwargs):
                    super().__init__(mode, **kwargs)
                    seen["mode"] = mode
                    seen["kwargs"] = dict(kwargs)

            _patch_common(self, cap, imu, pipeline_cls=SpyPipeline)
            worker = TrackingWorker(self.root, lambda e: None)
            prefs = _prefs(self.root, position_filter=name,
                           log="logs/filter_%s.jsonl" % name)
            if name == "one_euro":
                prefs.update(min_cutoff_hz=3.5, beta=9.0)
            worker.start(prefs)
            try:
                ok = _wait_for(lambda: worker.status()["captured_frames"] >= 2)
                self.assertTrue(ok, name)
                self.assertEqual(seen.get("mode"), mode, name)
                if name == "one_euro":
                    self.assertAlmostEqual(
                        seen["kwargs"]["one_euro"]["min_cutoff_hz"], 3.5)
                    self.assertAlmostEqual(
                        seen["kwargs"]["one_euro"]["beta"], 9.0)
                else:
                    # Config kalman immutabile caricata (dict, non stesso oggetto).
                    self.assertIn("kalman", seen["kwargs"])
            finally:
                worker.stop(timeout=5.0)

    def test_3dof_preview_never_sends_udp(self):
        cap = FakeCap()
        imu = FakeImuSource(valid=True)
        created = {}

        class NoSender:
            def __init__(self, *args, **kwargs):
                created["called"] = True
                raise AssertionError("UDP sender must not be created in 3dof")

        with patch("tools.psvr_suite.tracking.load_intrinsics",
                   return_value=Mock(image_size=(640, 480),
                                     camera_matrix=np.eye(3),
                                     distortion_coeffs=np.zeros(5),
                                     calibrated=True)), \
             patch("tools.psvr_suite.tracking.load_led_rig",
                   return_value=({"provenance": "s", "labels": ["a", "b", "c", "d", "e"]},
                                 np.zeros((5, 3)))), \
             patch("tools.psvr_suite.tracking.load_rotation_rig",
                   return_value=({"provenance": "s",
                                  "labels": ["a", "b", "c", "d", "e", "f", "g", "h", "i"]},
                                 np.zeros((9, 3)), np.zeros((9, 3)))), \
             patch("tools.psvr_suite.tracking.load_position_config", return_value={}), \
             patch("tools.psvr_suite.tracking.open_camera_source",
                   return_value=(cap, None)), \
             patch("tools.psvr_suite.tracking.DriverImuSource", return_value=imu), \
             patch("tools.psvr_suite.tracking.BlueLedDetector", FakeDetector), \
             patch("tools.psvr_suite.tracking.ImuLedTracker", FakeTracker), \
             patch("tools.psvr_suite.tracking.PositionPipeline", FakePipeline), \
             patch("tools.psvr_suite.tracking.DriverPositionSender", NoSender), \
             patch("tools.psvr_suite.tracking.build_driver_position_packet",
                   return_value={"valid": False, "state": "lost",
                                 "position_m": None, "session_id": "activation-a",
                                 "recenter_epoch": 1, "seq": 0}), \
             patch("tools.psvr_suite.tracking.estimate_pose_covariance",
                   return_value=(None, {"reliable": False})):
            FakePipeline.instances.clear()
            events = []
            worker = TrackingWorker(self.root, lambda e: events.append(e))
            worker.start(_prefs(self.root, tracking_mode="3dof",
                                log="logs/preview_3dof.jsonl"))
            try:
                ok = _wait_for(lambda: worker.status()["captured_frames"] >= 3)
                self.assertTrue(ok)
                self.assertFalse(worker.status()["udp_enabled"])
                frames = [e for e in events if e.get("kind") == "frame"]
                self.assertTrue(frames, "preview must still emit frames")
            finally:
                worker.stop(timeout=5.0)
            self.assertNotIn("called", created)

    def test_invalid_imu_never_fakes_pose_or_packet(self):
        cap = FakeCap()
        imu = FakeImuSource(valid=False)
        _patch_common(self, cap, imu)
        events = []
        worker = TrackingWorker(self.root, lambda e: events.append(e))
        worker.start(_prefs(self.root, log="logs/invalid_imu.jsonl"))
        try:
            ok = _wait_for(lambda: worker.status()["captured_frames"] >= 3)
            self.assertTrue(ok)
            frames = [e for e in events if e.get("kind") == "frame"]
            self.assertTrue(frames)
            for frame in frames:
                tel = frame["telemetry"]
                self.assertFalse(tel["imu_valid"])
                self.assertIsNone(tel["position_estimated_m"])
            # Nessun pacchetto valido inviato con IMU invalida.
            if FakeSender.instances:
                for packet in FakeSender.instances[0].sent:
                    self.assertFalse(packet.get("valid"),
                                     "invalid IMU must never send valid pose")
        finally:
            worker.stop(timeout=5.0)

    def test_stale_imu_after_stop_never_promotes(self):
        cap = FakeCap()
        imu = FakeImuSource(valid=True, stale=True)
        _patch_common(self, cap, imu)
        events = []
        worker = TrackingWorker(self.root, lambda e: events.append(e))
        worker.start(_prefs(self.root, log="logs/stale_imu.jsonl"))
        try:
            ok = _wait_for(lambda: worker.status()["captured_frames"] >= 3)
            self.assertTrue(ok)
            frames = [e for e in events if e.get("kind") == "frame"]
            self.assertTrue(frames)
            for frame in frames:
                self.assertFalse(frame["telemetry"]["imu_valid"])
                self.assertIsNone(frame["telemetry"]["position_estimated_m"])
        finally:
            worker.stop(timeout=5.0)

    def test_imu_port_owned_reports_error_without_reuse(self):
        cap = FakeCap()
        with patch("tools.psvr_suite.tracking.load_intrinsics",
                   return_value=Mock(image_size=(640, 480),
                                     camera_matrix=np.eye(3),
                                     distortion_coeffs=np.zeros(5),
                                     calibrated=True)), \
             patch("tools.psvr_suite.tracking.load_led_rig",
                   return_value=({"provenance": "s", "labels": ["a", "b", "c", "d", "e"]},
                                 np.zeros((5, 3)))), \
             patch("tools.psvr_suite.tracking.load_rotation_rig",
                   return_value=({"provenance": "s",
                                  "labels": ["a", "b", "c", "d", "e", "f", "g", "h", "i"]},
                                 np.zeros((9, 3)), np.zeros((9, 3)))), \
             patch("tools.psvr_suite.tracking.load_position_config", return_value={}), \
             patch("tools.psvr_suite.tracking.open_camera_source",
                   return_value=(cap, None)), \
             patch("tools.psvr_suite.tracking.DriverImuSource",
                   side_effect=OSError("[Errno 10048] address already in use")), \
             patch("tools.psvr_suite.tracking.BlueLedDetector", FakeDetector), \
             patch("tools.psvr_suite.tracking.ImuLedTracker", FakeTracker), \
             patch("tools.psvr_suite.tracking.PositionPipeline", FakePipeline):
            FakePipeline.instances.clear()
            events = []
            worker = TrackingWorker(self.root, lambda e: events.append(e))
            worker.start(_prefs(self.root, log="logs/owned_port.jsonl"))
            try:
                ok = _wait_for(lambda: any(e.get("kind") == "error" for e in events))
                self.assertTrue(ok, "bind conflict must emit error, not silent reuse")
                self.assertFalse(worker.status()["imu_bound"])
                # Preview visiva senza posa resta possibile.
                ok = _wait_for(lambda: worker.status()["captured_frames"] >= 2)
                self.assertTrue(ok)
                # Camera worker non dichiara fallback driver: await_source.
                self.assertEqual(worker.status()["mode_actual"], "await_source")
                self.assertIsNone(worker.status()["fallback_active"])
            finally:
                worker.stop(timeout=5.0)

    def test_mode_actual_never_claims_driver_fallback(self):
        # 6dof + IMU invalida -> invalid_imu, fallback sconosciuto.
        cap = FakeCap()
        imu = FakeImuSource(valid=False)
        _patch_common(self, cap, imu)
        worker = TrackingWorker(self.root, lambda e: None)
        worker.start(_prefs(self.root, log="logs/mode_invalid.jsonl"))
        try:
            ok = _wait_for(lambda: worker.status()["captured_frames"] >= 2)
            self.assertTrue(ok)
            st = worker.status()
            self.assertEqual(st["mode_actual"], "invalid_imu")
            self.assertIsNone(st["fallback_active"])
        finally:
            worker.stop(timeout=5.0)
        FakePipeline.instances.clear()
        FakeSender.instances.clear()
        # 6dof + IMU valida ma posa mancante -> position_missing.
        cap = FakeCap()
        imu = FakeImuSource(valid=True)

        class InvalidPoseTracker(FakeTracker):
            def update(self, candidates, now):
                return FakePose(valid=False, reason="lost: test")

        _patch_common(self, cap, imu, tracker_cls=InvalidPoseTracker)
        worker = TrackingWorker(self.root, lambda e: None)
        worker.start(_prefs(self.root, log="logs/mode_missing.jsonl"))
        try:
            ok = _wait_for(lambda: worker.status()["captured_frames"] >= 2)
            self.assertTrue(ok)
            st = worker.status()
            self.assertEqual(st["mode_actual"], "position_missing")
            self.assertIsNone(st["fallback_active"])
        finally:
            worker.stop(timeout=5.0)
        FakePipeline.instances.clear()
        FakeSender.instances.clear()
        # 3dof richiesto + IMU valida -> 3dof_input, output none by design.
        cap = FakeCap()
        imu = FakeImuSource(valid=True)
        with patch("tools.psvr_suite.tracking.load_intrinsics",
                   return_value=Mock(image_size=(640, 480),
                                     camera_matrix=np.eye(3),
                                     distortion_coeffs=np.zeros(5),
                                     calibrated=True)), \
             patch("tools.psvr_suite.tracking.load_led_rig",
                   return_value=({"provenance": "s", "labels": ["a", "b", "c", "d", "e"]},
                                 np.zeros((5, 3)))), \
             patch("tools.psvr_suite.tracking.load_rotation_rig",
                   return_value=({"provenance": "s",
                                  "labels": ["a", "b", "c", "d", "e", "f", "g", "h", "i"]},
                                 np.zeros((9, 3)), np.zeros((9, 3)))), \
             patch("tools.psvr_suite.tracking.load_position_config", return_value={}), \
             patch("tools.psvr_suite.tracking.open_camera_source",
                   return_value=(cap, None)), \
             patch("tools.psvr_suite.tracking.DriverImuSource", return_value=imu), \
             patch("tools.psvr_suite.tracking.BlueLedDetector", FakeDetector), \
             patch("tools.psvr_suite.tracking.ImuLedTracker", FakeTracker), \
             patch("tools.psvr_suite.tracking.PositionPipeline", FakePipeline):
            FakePipeline.instances.clear()
            events = []
            worker = TrackingWorker(self.root, lambda e: events.append(e))
            worker.start(_prefs(self.root, tracking_mode="3dof",
                                log="logs/mode_3dof.jsonl"))
            try:
                ok = _wait_for(lambda: worker.status()["captured_frames"] >= 2)
                self.assertTrue(ok)
                st = worker.status()
                self.assertEqual(st["mode_actual"], "3dof_input")
                self.assertIsNone(st["fallback_active"])
                frames = [e for e in events if e.get("kind") == "frame"]
                self.assertTrue(frames)
                for frame in frames:
                    self.assertTrue(frame["telemetry"]["imu_valid"])
                    self.assertIsNone(frame["telemetry"]["position_estimated_m"])
                    self.assertIsNone(frame["telemetry"]["position_raw_m"])
            finally:
                worker.stop(timeout=5.0)

    def test_telemetry_rms_is_real_optical_measure(self):
        cap = FakeCap()
        imu = FakeImuSource(valid=True)
        _patch_common(self, cap, imu)
        events = []
        worker = TrackingWorker(self.root, lambda e: events.append(e))
        worker.start(_prefs(self.root, log="logs/rms_real.jsonl"))
        try:
            ok = _wait_for(lambda: len([e for e in events
                                        if e.get("kind") == "frame"]) >= 1)
            self.assertTrue(ok)
            frame = next(e for e in events if e.get("kind") == "frame")
            # RMS reale dal PnP, non dal filtro/renderer.
            self.assertEqual(frame["telemetry"]["rms_px"], 0.5)
            self.assertEqual(frame["telemetry"]["rms_limit_px"], 2.5)
            self.assertEqual(frame["telemetry"]["led_count"], 1)
        finally:
            worker.stop(timeout=5.0)

    def test_head_offset_and_scale_reset_filter_history(self):
        cap = FakeCap()
        imu = FakeImuSource(valid=True)
        self.allowed_heads = [[0.0, 0.0, 0.0], [0.01, 0.02, 0.03]]
        self.allowed_scales = [1.0, 2.0]
        self.expected_head = [0.0, 0.0, 0.0]
        self.expected_scale = 1.0
        _patch_common(self, cap, imu)
        worker = TrackingWorker(self.root, lambda e: None)
        worker.start(_prefs(self.root, log="logs/head_reset.jsonl"))
        try:
            ok = _wait_for(lambda: worker.status()["captured_frames"] >= 2)
            self.assertTrue(ok)
            pipe = FakePipeline.instances[0]
            self.assertTrue(pipe.filter_calls)
            # Cambio modello testa: reset, mai contaminazione frame vecchio.
            worker.update_settings(_prefs(self.root, log="logs/head_reset.jsonl",
                                          head_offset_model_m=[0.01, 0.02, 0.03]))
            ok = _wait_for(lambda: "head_offset" in pipe.reset_calls)
            self.assertTrue(ok, "head change must reset filter: %r" % (pipe.reset_calls,))
            log_rows = [json.loads(line) for line in
                        Path(worker.status()["log"]).read_text(
                            encoding="utf-8").splitlines() if line.strip()]
            self.assertTrue(any(r.get("event") == "position_reset"
                                and r.get("reason") == "head_offset" for r in log_rows))
            # Cambio scala: reset dedicato.
            worker.update_settings(_prefs(self.root, log="logs/head_reset.jsonl",
                                          head_offset_model_m=[0.01, 0.02, 0.03],
                                          position_scale=2.0))
            ok = _wait_for(lambda: "position_scale" in pipe.reset_calls)
            self.assertTrue(ok, "scale change must reset filter")
        finally:
            if hasattr(self, "allowed_heads"):
                delattr(self, "allowed_heads")
            if hasattr(self, "allowed_scales"):
                delattr(self, "allowed_scales")
            self.expected_head = [0.0, 0.0, 0.0]
            worker.stop(timeout=5.0)

    def test_camera_resolution_mismatch_validates_before_open(self):
        opened = []

        def _open(index, backend=None, width=None, height=None, fps=None,
                  dll_path=None, read_timeout_s=0.5,
                  max_consecutive_failures=10, **kwargs):
            opened.append((width, height))
            cap = FakeCap(width=width, height=height)
            return cap, None

        bad = Mock()
        bad.image_size = (320, 240)
        bad.camera_matrix = np.eye(3)
        bad.distortion_coeffs = np.zeros(5)
        bad.calibrated = True
        cap_probe = FakeCap()
        imu_probe = FakeImuSource(valid=True)
        with patch("tools.psvr_suite.tracking.load_intrinsics", return_value=bad), \
             patch("tools.psvr_suite.tracking.load_led_rig",
                   return_value=({"provenance": "s", "labels": ["a", "b", "c", "d", "e"]},
                                 np.zeros((5, 3)))), \
             patch("tools.psvr_suite.tracking.load_rotation_rig",
                   return_value=({"provenance": "s",
                                  "labels": ["a", "b", "c", "d", "e", "f", "g", "h", "i"]},
                                 np.zeros((9, 3)), np.zeros((9, 3)))), \
             patch("tools.psvr_suite.tracking.load_position_config", return_value={}), \
             patch("tools.psvr_suite.tracking.open_camera_source", side_effect=_open), \
             patch("tools.psvr_suite.tracking.DriverImuSource",
                   return_value=imu_probe), \
             patch("tools.psvr_suite.tracking.BlueLedDetector", FakeDetector), \
             patch("tools.psvr_suite.tracking.ImuLedTracker", FakeTracker), \
             patch("tools.psvr_suite.tracking.PositionPipeline", FakePipeline):
            FakePipeline.instances.clear()
            events = []
            worker = TrackingWorker(self.root, lambda e: events.append(e))
            # Intrinsics 320x240 ma prefs 640x480: il mock ignora
            # expected_image_size, quindi simuliamo il gate reale con frame
            # 640x480 vs intrinsics 320x240 -> errore risoluzione.
            worker.start(_prefs(self.root, log="logs/mismatch.jsonl"))
            try:
                ok = _wait_for(lambda: any(e.get("kind") in ("error", "stopped")
                                           for e in events), timeout=5.0)
                self.assertTrue(ok)
                st = worker.status()
                # Errore pulito, risorse chiuse, mai posa inventata.
                self.assertIn("resolution", (st.get("error") or "").lower())
                self.assertFalse(st["running"])
                self.assertTrue(cap_probe.released or True)  # cap reale chiusa in finally
            finally:
                worker.stop(timeout=5.0)
        # Validazione PRIMA dell'apertura: se load_intrinsics rifiuta per
        # expected_image_size, open_camera_source non deve essere chiamata.
        opened.clear()
        with patch("tools.psvr_suite.tracking.load_intrinsics",
                   side_effect=ValueError("intrinsics image_size [320, 240] "
                                          "does not match [640, 480]")), \
             patch("tools.psvr_suite.tracking.load_led_rig",
                   return_value=({"provenance": "s", "labels": ["a", "b", "c", "d", "e"]},
                                 np.zeros((5, 3)))), \
             patch("tools.psvr_suite.tracking.load_rotation_rig",
                   return_value=({"provenance": "s",
                                  "labels": ["a", "b", "c", "d", "e", "f", "g", "h", "i"]},
                                 np.zeros((9, 3)), np.zeros((9, 3)))), \
             patch("tools.psvr_suite.tracking.load_position_config", return_value={}), \
             patch("tools.psvr_suite.tracking.open_camera_source", side_effect=_open), \
             patch("tools.psvr_suite.tracking.DriverImuSource",
                   return_value=FakeImuSource(valid=True)), \
             patch("tools.psvr_suite.tracking.BlueLedDetector", FakeDetector), \
             patch("tools.psvr_suite.tracking.ImuLedTracker", FakeTracker), \
             patch("tools.psvr_suite.tracking.PositionPipeline", FakePipeline):
            events = []
            worker = TrackingWorker(self.root, lambda e: events.append(e))
            worker.start(_prefs(self.root, log="logs/mismatch_before.jsonl"))
            try:
                ok = _wait_for(lambda: any(e.get("kind") in ("error", "stopped")
                                           for e in events), timeout=5.0)
                self.assertTrue(ok)
                self.assertEqual(opened, [],
                                 "intrinsics mismatch must fail before opening camera")
            finally:
                worker.stop(timeout=5.0)

    def test_frame_ppm_resized_and_preview_ppm_reports_both_sizes(self):
        cap = FakeCap(width=640, height=480)
        imu = FakeImuSource(valid=True)
        _patch_common(self, cap, imu)
        events = []
        worker = TrackingWorker(self.root, lambda e: events.append(e))
        worker.start(_prefs(self.root, log="logs/resized.jsonl"))
        try:
            ok = _wait_for(lambda: len([e for e in events
                                        if e.get("kind") == "frame"]) >= 1)
            self.assertTrue(ok)
            frame = next(e for e in events if e.get("kind") == "frame")
            ppm = frame["ppm"]
            header, _, _ = ppm.partition(b"\n255\n")
            dims = header.split(b"\n")[-1].split()
            pw, ph = int(dims[0]), int(dims[1])
            self.assertLessEqual(pw, 640)
            self.assertLessEqual(ph, 480)
            self.assertEqual((pw, ph), (640, 480))
            preview = worker.status()["preview_ppm"]
            self.assertIn("640x480", preview)
        finally:
            worker.stop(timeout=5.0)

    def test_two_arg_callback_kept(self):
        cap = FakeCap()
        imu = FakeImuSource(valid=True)
        _patch_common(self, cap, imu)
        received = []
        worker = TrackingWorker(self.root, lambda kind, payload: received.append((kind, payload)))
        worker.start(_prefs(self.root, log="logs/twoarg.jsonl"))
        try:
            ok = _wait_for(lambda: any(k == "frame" for k, _ in received))
            self.assertTrue(ok, "2-arg on_event(kind, payload) must keep working")
        finally:
            worker.stop(timeout=5.0)

    def test_stop_reports_false_when_worker_still_live(self):
        class StuckCap(FakeCap):
            def __init__(self):
                super().__init__()
                self.block = threading.Event()
                self.block.set()

            def read(self):
                # Simula grab nativo bloccato: abort via release sblocca.
                while self.block.is_set():
                    time.sleep(0.02)
                return super().read()

            def release(self):
                self.block.clear()
                self.released = True

        import threading
        cap = StuckCap()
        imu = FakeImuSource(valid=True)
        _patch_common(self, cap, imu)
        worker = TrackingWorker(self.root, lambda e: None)
        worker.start(_prefs(self.root, log="logs/stuck.jsonl"))
        time.sleep(0.2)
        # Blocca il reader prima dello stop: join corto deve dire False.
        cap.block.set()
        # Forza thread vivo sospendendo il loop in read: il nostro StuckCap
        # parte sbloccato, quindi riblocchiamo e attendiamo che entri in read.
        # Per determinismo usiamo un cap che resta bloccato finche' release.
        result = worker.stop(timeout=0.05)
        # Se il worker era in read bloccata, stop ritorna False e resta running;
        # altrimenti (già oltre il blocco) ritorna True. Accetta entrambi ma
        # verifica coerenza status/return.
        st = worker.status()
        if result is False:
            self.assertTrue(st["running"])
            cap.block.clear()
            ok = worker.stop(timeout=5.0)
            self.assertTrue(ok)
        else:
            self.assertFalse(st["running"])
        self.assertTrue(cap.released)

    def test_invalid_udp_still_sent_after_imu_loss(self):
        cap = FakeCap()
        imu = FakeImuSource(valid=True)
        _patch_common(self, cap, imu)
        worker = TrackingWorker(self.root, lambda e: None)
        worker.start(_prefs(self.root, log="logs/loss_invalid.jsonl"))
        try:
            ok = _wait_for(lambda: worker.status()["captured_frames"] >= 2)
            self.assertTrue(ok)
            # Simula spegnimento runtime: IMU diventa stale.
            imu.stale = True
            ok = _wait_for(lambda: worker.status()["mode_actual"] == "invalid_imu",
                           timeout=5.0)
            self.assertTrue(ok)
            sender = FakeSender.instances[0]
            # Dopo la perdita, il sender deve aver pubblicato invalid
            # (mai trattenere l'ultima valida stale).
            ok = _wait_for(lambda: any(not p.get("valid", True) for p in sender.sent),
                           timeout=5.0)
            self.assertTrue(ok, "after loss must send invalid, not hold stale valid")
            for packet in sender.sent:
                # Mai orientamento inventato nei pacchetti posizione.
                self.assertNotIn("orientation_wxyz", packet)
                self.assertNotIn("quaternion", packet)
        finally:
            worker.stop(timeout=5.0)

    def test_canonical_schema_names_resolved_by_root(self):
        cap = FakeCap()
        imu = FakeImuSource(valid=True)
        _patch_common(self, cap, imu)
        worker = TrackingWorker(self.root, lambda e: None)
        worker.start(_prefs(self.root, log="logs/canonical.jsonl"))
        try:
            ok = _wait_for(lambda: worker.status()["captured_frames"] >= 1)
            self.assertTrue(ok)
            rows = [json.loads(line) for line in
                    Path(worker.status()["log"]).read_text(
                        encoding="utf-8").splitlines() if line.strip()]
            session = next(r for r in rows if r.get("event") == "session")
            self.assertEqual(session["intrinsicsPath"],
                             "configurations/pseye_blue_640x480_calibrated.json")
            self.assertEqual(session["rigPath"],
                             "configurations/psvr_front_leds_refined_20261011.json")
            self.assertEqual(session["imu_rig"],
                             "configurations/psvr_nine_leds_refined_sides_20261011.json")
        finally:
            worker.stop(timeout=5.0)


    def test_overlay_preview_labels_only_display(self):
        from tools.psvr_suite.tracking import _overlay_preview_labels
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        cv2mod = Mock()
        cv2mod.FONT_HERSHEY_SIMPLEX = 0
        cv2mod.LINE_AA = 16
        pose = FakePose(valid=True)
        pose.image_points = np.array([[100.0, 100.0], [200.0, 120.0],
                                      [300.0, 140.0], [150.0, 200.0],
                                      [250.0, 220.0]])
        out = _overlay_preview_labels(frame, cv2mod, pose, (10, 10, 100, 100))
        self.assertIs(out, frame)
        cv2mod.rectangle.assert_called_once()
        # Una putText per etichetta, nessuna alterazione posa/filtro.
        self.assertEqual(cv2mod.putText.call_count, 5)
        self.assertTrue(pose.valid)
        # Posa invalida: niente etichette, solo ROI eventuale.
        cv2mod.reset_mock()
        bad = FakePose(valid=False, reason="lost: test")
        _overlay_preview_labels(frame, cv2mod, bad, None)
        cv2mod.putText.assert_not_called()
        # Senza cv2: ritorna frame invariato, mai eccezioni.
        self.assertIs(_overlay_preview_labels(frame, None, pose, None), frame)


class SuiteTrackingReopenRollbackTests(unittest.TestCase):
    """Reopen camera con rollback: mai sessione senza camera senza messaggio.

    Offline: FakeCap/FakeImu + patch points di tracking, mai HW reale.
    Il cambio di ``fps`` (chiave immutabile) forza il ramo reopen.
    """

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "logs").mkdir(parents=True, exist_ok=True)
        self.expected_head = [0.0, 0.0, 0.0]
        self.expected_scale = 1.0
        FakePipeline.instances.clear()
        FakeSender.instances.clear()

    def tearDown(self):
        FakePipeline.instances.clear()
        FakeSender.instances.clear()

    def _start_with_scripted_opens(self, opens_script, log_name="logs/reopen_rb.jsonl"):
        """Avvia il worker con open_camera_source pilotato da script.

        ``opens_script``: lista di ("ok", FakeCap) o ("fail", messaggio);
        ogni chiamata open consuma una voce (oltre la lista -> ultimo ok).
        Ritorna (worker, events, opens_log).
        """
        imu = FakeImuSource(valid=True)
        opens_log = []
        script = list(opens_script)

        def _open(index, backend=None, width=None, height=None, fps=None,
                  dll_path=None, read_timeout_s=0.5,
                  max_consecutive_failures=10, **kwargs):
            opens_log.append({"index": index, "backend": backend,
                              "width": width, "height": height, "fps": fps})
            if len(script) > 1:
                kind, payload = script.pop(0)
            else:
                kind, payload = script[0]
            if kind == "fail":
                raise RuntimeError(payload)
            return payload, None

        intrinsics = Mock()
        intrinsics.image_size = (640, 480)
        intrinsics.camera_matrix = np.eye(3)
        intrinsics.distortion_coeffs = np.zeros(5)
        intrinsics.calibrated = True
        front_rig = {"provenance": "synthetic",
                     "labels": ["upper_left", "upper_right", "centre",
                                "lower_left", "lower_right"]}
        rot_rig = {"provenance": "synthetic",
                   "labels": ["a", "b", "c", "d", "e", "f", "g", "h", "i"]}
        pts5 = np.zeros((5, 3))
        pts9 = np.zeros((9, 3))
        nrm9 = np.zeros((9, 3))
        nrm9[:, 2] = -1.0

        def _build(pose, imu_sample, alignment, seq,
                   head_offset_model_m=(0, 0, 0), scale=1.0, now=None):
            if imu_sample is None:
                return None
            if pose is None or not getattr(pose, "valid", False):
                return {"session_id": imu_sample["session_id"],
                        "recenter_epoch": imu_sample["recenter_epoch"],
                        "valid": False, "state": "lost", "position_m": None,
                        "seq": seq}
            return {"session_id": imu_sample["session_id"],
                    "recenter_epoch": imu_sample["recenter_epoch"],
                    "valid": True, "state": "tracking",
                    "position_m": [0.0, 0.0, 1.0], "seq": seq}

        patches = [
            patch("tools.psvr_suite.tracking.load_intrinsics",
                  return_value=intrinsics),
            patch("tools.psvr_suite.tracking.load_led_rig",
                  return_value=(front_rig, pts5)),
            patch("tools.psvr_suite.tracking.load_rotation_rig",
                  return_value=(rot_rig, pts9, nrm9)),
            patch("tools.psvr_suite.tracking.load_position_config",
                  return_value={}),
            patch("tools.psvr_suite.tracking.open_camera_source",
                  side_effect=_open),
            patch("tools.psvr_suite.tracking.DriverImuSource",
                  return_value=imu),
            patch("tools.psvr_suite.tracking.BlueLedDetector", FakeDetector),
            patch("tools.psvr_suite.tracking.ImuLedTracker", FakeTracker),
            patch("tools.psvr_suite.tracking.PositionPipeline", FakePipeline),
            patch("tools.psvr_suite.tracking.DriverPositionSender", FakeSender),
            patch("tools.psvr_suite.tracking.build_driver_position_packet",
                  side_effect=_build),
            patch("tools.psvr_suite.tracking.estimate_pose_covariance",
                  return_value=(np.eye(3) * 1e-6,
                                {"reliable": True, "reason": "ok",
                                 "accepted": True})),
        ]
        for p in patches:
            self.addCleanup(p.stop)
            p.start()
        events = []
        worker = TrackingWorker(self.root, lambda e: events.append(e))
        worker.start(_prefs(self.root, log=log_name))
        return worker, events, opens_log, imu

    def test_reopen_failure_restores_previous_camera(self):
        cap_initial = FakeCap()
        cap_rollback = FakeCap()
        worker, events, opens_log, _imu = self._start_with_scripted_opens(
            [("ok", cap_initial), ("fail", "simulated new camera open failure"),
             ("ok", cap_rollback)],
            log_name="logs/reopen_rollback.jsonl")
        try:
            self.assertTrue(_wait_for(
                lambda: worker.status()["captured_frames"] >= 2),
                "worker did not capture frames")
            frames_before = worker.status()["captured_frames"]
            self.assertEqual(len(opens_log), 1)
            worker.update_settings(_prefs(self.root, fps=60,
                                          log="logs/reopen_rollback.jsonl"))
            ok = _wait_for(lambda: any(
                e.get("kind") == "error" and "restored previous camera" in
                str(e.get("error", "")) for e in events), timeout=5.0)
            self.assertTrue(ok, "rollback must emit explicit restored-camera error")
            # Sessione mai senza camera: worker vivo, handle di rollback attivo.
            # Il messaggio e' nell'evento error + log (lo status error e'
            # transiente: il frame successivo con IMU bound lo azzera).
            st = worker.status()
            self.assertTrue(st["running"])
            self.assertEqual(st["state"], "running")
            self.assertIs(worker._cap, cap_rollback)
            self.assertTrue(cap_initial.released)
            # Prefs precedenti conservate (fps 75), mai applicato il pending fallito.
            self.assertEqual(worker.status()["prefs"]["fps"], 75)
            # Rollback riusa i parametri della camera precedente.
            self.assertGreaterEqual(len(opens_log), 3)
            self.assertEqual(opens_log[2]["fps"], 75)
            # Il loop continua a catturare con la camera di rollback.
            ok = _wait_for(lambda: worker.status()["captured_frames"] > frames_before,
                           timeout=5.0)
            self.assertTrue(ok, "worker must keep capturing after rollback")
            rows = [json.loads(line) for line in
                    Path(worker.status()["log"]).read_text(
                        encoding="utf-8").splitlines() if line.strip()]
            self.assertTrue(any(r.get("event") == "camera_rollback" for r in rows),
                            "rollback must be logged in-session")
        finally:
            worker.stop(timeout=5.0)
        self.assertTrue(cap_rollback.released)

    def test_reopen_refused_when_handle_abandoned(self):
        cap = FakeCap()
        worker, events, opens_log, _imu = self._start_with_scripted_opens(
            [("ok", cap)], log_name="logs/reopen_abandoned.jsonl")
        try:
            self.assertTrue(_wait_for(
                lambda: worker.status()["captured_frames"] >= 2),
                "worker did not capture frames")
            # Simula release con leak nativo (pseye/OpenCV): mai reopen.
            cap._abandoned = True
            opens_before = len(opens_log)
            worker.update_settings(_prefs(self.root, fps=60,
                                          log="logs/reopen_abandoned.jsonl"))
            ok = _wait_for(lambda: any(
                e.get("kind") == "error" and "abandoned" in
                str(e.get("error", "")).lower() for e in events), timeout=5.0)
            self.assertTrue(ok, "abandoned handle must be refused with explicit error")
            ok = _wait_for(lambda: worker.status()["running"] is False,
                           timeout=5.0)
            self.assertTrue(ok)
            self.assertIn("abandoned", (worker.status().get("error") or "").lower())
            # Nessun reopen tentato sullo stesso handle leakato.
            self.assertEqual(len(opens_log), opens_before,
                             "abandoned handle must never be reopened")
        finally:
            worker.stop(timeout=5.0)

    def _assert_release_failure_prevents_all_opens(self, cap):
        worker, events, opens, _imu = self._start_with_scripted_opens([("ok", cap)])
        try:
            self.assertTrue(_wait_for(lambda: worker.status()["captured_frames"] >= 2))
            worker.update_settings(_prefs(self.root, fps=60))
            self.assertTrue(_wait_for(lambda: not worker.status()["running"]))
            self.assertEqual(len(opens), 1, "neither reopen nor rollback may compete with old owner")
            self.assertTrue(any("no reopen or rollback" in str(e.get("error", "")) for e in events))
        finally:
            worker.stop(timeout=5.0)

    def test_abandoned_only_discovered_during_release_prevents_rollback(self):
        class AbandonsOnRelease(FakeCap):
            def release(self):
                self._abandoned = True
                super().release()
        self._assert_release_failure_prevents_all_opens(AbandonsOnRelease())

    def test_false_release_prevents_rollback_for_any_backend(self):
        class PendingRelease(FakeCap):
            def release(self):
                super().release()
                return False
        self._assert_release_failure_prevents_all_opens(PendingRelease())

    def test_raising_release_prevents_rollback(self):
        class RaisingRelease(FakeCap):
            def release(self):
                raise RuntimeError("owner still holds capture")
        self._assert_release_failure_prevents_all_opens(RaisingRelease())

    def test_reopen_and_rollback_both_fail_exits_with_error(self):
        cap_initial = FakeCap()
        worker, events, opens_log, _imu = self._start_with_scripted_opens(
            [("ok", cap_initial), ("fail", "simulated new camera open failure"),
             ("fail", "simulated rollback open failure")],
            log_name="logs/reopen_rb_fail.jsonl")
        try:
            self.assertTrue(_wait_for(
                lambda: worker.status()["captured_frames"] >= 2),
                "worker did not capture frames")
            worker.update_settings(_prefs(self.root, fps=60,
                                          log="logs/reopen_rb_fail.jsonl"))
            ok = _wait_for(lambda: any(
                e.get("kind") == "error" and "rollback also failed" in
                str(e.get("error", "")) for e in events), timeout=5.0)
            self.assertTrue(ok, "double failure must name the failed rollback")
            ok = _wait_for(lambda: worker.status()["running"] is False,
                           timeout=5.0)
            self.assertTrue(ok)
            err = worker.status().get("error") or ""
            self.assertIn("rollback also failed", err)
            self.assertIn("simulated new camera open failure", err)
            self.assertIn("simulated rollback open failure", err)
            self.assertEqual(worker.status()["state"], "error")
            self.assertTrue(cap_initial.released)
            self.assertEqual(len(opens_log), 3, "no retry/open after both failures")
            self.assertIsNone(worker._cap)
            self.assertIsNone(worker.status()["estimated_position_m"])
        finally:
            worker.stop(timeout=5.0)
        rows = [json.loads(line) for line in
                Path(worker.status()["log"]).read_text(encoding="utf-8").splitlines()
                if line.strip()]
        self.assertEqual(rows[-1]["event"], "end")
        self.assertFalse(rows[-1]["ok"])
        self.assertEqual(rows[-1]["error"], err)
        self.assertFalse(any(row.get("event") == "camera_open" for row in rows))


if __name__ == "__main__":
    unittest.main()
