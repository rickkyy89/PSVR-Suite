"""Paired guided collection reuses PS Eye timing without hardware or a display."""
import heapq
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.psvr_camera import ps4_guided_calibration as guided
from tools.psvr_camera.calibration import ChessboardSpec, CalibrationError
from tools.psvr_camera.guided_calibration import CaptureGuide


class StereoGuideTests(unittest.TestCase):
    def setUp(self):
        self.spec = ChessboardSpec(9, 6, .02)
        self.left = self.spec.object_points()[:, :2] * 1000 + 30
        self.right = self.left + (10, 2)
        self.collector = guided.StereoCaptureGuide(self.spec, hold_s=.2, move_s=.3)

    def step(self, now, left=None, right=None):
        return self.collector.step(self.left if left is None else left,
                                   self.right if right is None else right, now)

    def test_reuses_original_countdown_and_saves_both_eyes_atomically(self):
        self.assertIsInstance(self.collector.guide, CaptureGuide)
        self.assertFalse(self.step(0)[1])
        self.assertFalse(self.step(.1)[1])
        self.assertTrue(self.step(.31)[1])
        pairs = self.collector.views
        self.assertEqual(len(pairs), 1)
        np.testing.assert_array_equal(pairs[0][0], self.left)
        np.testing.assert_array_equal(pairs[0][1], self.right)
        pairs[0][0][:] = 0
        self.assertNotEqual(self.collector.views[0][0].max(), 0)

    def test_one_eye_missing_never_saves_and_resets_hold(self):
        self.step(0)
        self.step(.1)
        message, saved = self.collector.step(self.left, None, .25)
        self.assertIn("OCCHIO R", message)
        self.assertFalse(saved)
        self.assertFalse(self.step(.3)[1])
        self.assertFalse(self.step(.4)[1])
        self.assertFalse(self.step(.5)[1])
        self.assertTrue(self.step(.61)[1])

    def test_motion_in_one_eye_is_not_hidden_by_combined_rms(self):
        self.step(0)
        self.step(.1)
        self.assertFalse(self.step(.29, left=self.left + (2, 0))[1])
        self.assertFalse(self.step(.35, left=self.left + (2, 0))[1])
        self.assertTrue(self.step(.51, left=self.left + (2, 0))[1])

    def test_single_eye_drift_from_anchor_restarts_countdown(self):
        self.collector = guided.StereoCaptureGuide(self.spec, hold_s=.4)
        self.step(0)
        self.step(.1)
        self.step(.2, left=self.left + (.6, 0))
        self.step(.3, left=self.left + (1.2, 0))
        self.assertFalse(self.step(.5, left=self.left + (1.8, 0))[1])
        self.assertFalse(self.step(.6, left=self.left + (1.8, 0))[1])

    def test_gap_or_nonmonotonic_time_cannot_count_as_stability(self):
        self.step(0)
        self.step(.1)
        self.assertFalse(self.step(1)[1])
        self.assertFalse(self.step(1.1)[1])
        self.assertFalse(self.step(1.05)[1])
        self.assertFalse(self.step(1.2)[1])
        self.assertFalse(self.step(1.3)[1])

    def test_duplicate_pose_requires_movement_after_first_save(self):
        for now in (0, .1, .31):
            self.step(now)
        for now in (.5, .7, 1, 1.2):
            message, saved = self.step(now)
            self.assertFalse(saved)
        self.assertIn("CAMBIA POSA", message)
        self.assertEqual(len(self.collector.views), 1)

    def test_invalid_corner_geometry_is_rejected(self):
        for bad in (np.zeros((53, 2)), np.full((54, 2), np.nan)):
            message, saved = self.collector.step(bad, self.right, .1)
            self.assertFalse(saved)
            self.assertEqual(self.collector.views, [])


class StorageTests(unittest.TestCase):
    def test_offline_replay_preserves_original_pairs_hash_and_split_without_camera(self):
        from tools.psvr_camera import stereo_calibration
        spec = ChessboardSpec(9, 6, .02)
        points = spec.object_points()[:, :2] * 1000 + 30
        pairs = [(points + i * 10, points + i * 10 + 5) for i in range(15)]
        samples = [{"sequence": i + 1, "host_monotonic": i + .5} for i in range(15)]
        with tempfile.TemporaryDirectory() as folder:
            path, output = Path(folder) / "source.views.json", Path(folder) / "result.json"
            guided.save_views(path, pairs, samples, spec, (640, 400), "640x400", {})
            before = path.read_bytes()
            calibration = Mock()
            with patch.object(guided, "fit_pairs", return_value=(calibration, {"accepted": True})) as fit, \
                 patch.object(stereo_calibration, "save_stereo") as save:
                report = guided.calibrate_saved_views(path, output)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(report["views_sha256"], hashlib.sha256(before).hexdigest())
            self.assertFalse(report["camera_opened"])
            self.assertFalse(report["source_views_modified"])
            self.assertEqual(len(fit.call_args.args[0]), 15)
            for original, parsed in zip(pairs, fit.call_args.args[0]):
                np.testing.assert_array_equal(original[0], parsed[0])
                np.testing.assert_array_equal(original[1], parsed[1])
            save.assert_called_once_with(output, calibration)
            output.write_text("preserve me")
            with self.assertRaises(FileExistsError):
                guided.calibrate_saved_views(path, output)
            self.assertEqual(output.read_text(), "preserve me")

    def test_offline_cli_does_not_enter_live_ui(self):
        with patch.object(guided, "calibrate_saved_views", return_value={"accepted": True}) as replay, \
             patch.object(guided, "run_guided", side_effect=AssertionError("no live camera")), \
             contextlib.redirect_stdout(io.StringIO()) as stdout:
            result = guided.main(["--solve-views", "saved.views.json", "--out", "new.json"])
        self.assertEqual(result, 0)
        self.assertTrue(json.loads(stdout.getvalue())["accepted"])
        replay.assert_called_once_with(Path("saved.views.json"), Path("new.json"), 1.0)

    def test_every_fifth_pair_is_held_out_without_fitting(self):
        pairs = [(i, i) for i in range(15)]
        train, held = guided.training_split(pairs)
        self.assertEqual([p[0] for p in held], [4, 9, 14])
        self.assertEqual(len(train), 12)
        self.assertFalse(set(train) & set(held))
        with self.assertRaises(CalibrationError):
            guided.training_split(pairs[:9])

    def test_partial_views_are_preserved_without_overwrite_or_intrinsics(self):
        spec = ChessboardSpec(9, 6, .02)
        points = np.ones((54, 2))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "partial.views.json"
            guided.save_views(path, [(points, points + 2)],
                [{"sequence": 4, "host_monotonic": 1.5}], spec, (640, 400), "640x400", {})
            result = json.loads(path.read_text())
            self.assertEqual(result["schema"], "psvr.camera.stereo_views")
            self.assertEqual(len(result["views"]), 1)
            self.assertNotIn("camera_matrix", result)
            with self.assertRaises(FileExistsError):
                guided.save_views(path, [], [], spec, (640, 400), "640x400", {})
            with self.assertRaises(CalibrationError):
                guided.save_views(Path(folder) / "bad.json", [(points, points)], [],
                                  spec, (640, 400), "640x400", {})


class FakeRoot:
    def __init__(self):
        self.items = []
        self.number = 0
        self.closed = False

    def after(self, milliseconds, callback):
        self.number += 1
        heapq.heappush(self.items, (time.monotonic() + milliseconds / 1000,
                                    self.number, callback))

    def protocol(self, *_):
        pass

    def bell(self):
        pass

    def destroy(self):
        self.closed = True

    def mainloop(self):
        deadline = time.monotonic() + 3
        while not self.closed and time.monotonic() < deadline:
            if self.items and self.items[0][0] <= time.monotonic():
                heapq.heappop(self.items)[2]()
            time.sleep(.001)
        if not self.closed:
            raise AssertionError("guided UI did not close cooperatively")


class LifecycleTests(unittest.TestCase):
    def test_close_waits_for_late_owner_cleanup_instead_of_exiting_daemon(self):
        from tools.psvr_camera import ps4_stereo_session
        restored = threading.Event()

        class LateRoot(FakeRoot):
            def destroy(self):
                if not restored.is_set():
                    raise AssertionError("window closed before owner restored controls")
                super().destroy()

        root = LateRoot()
        ui = types.SimpleNamespace(root=root, status=Mock(), progress=Mock(),
                                   image_label=Mock(), start_button=Mock())
        session = Mock()
        session.next_pair.side_effect = lambda **kwargs: time.sleep(.005)
        deadline = [None]

        def begin_cleanup(**kwargs):
            deadline[0] = time.monotonic() + .3
            return False

        session.stop.side_effect = begin_cleanup

        def status():
            if deadline[0] is not None and time.monotonic() >= deadline[0]:
                restored.set()
            return {"error": None, "cleanup_pending": not restored.is_set(),
                    "controls": {"restoration_failed": False,
                                 "restoration_pending": not restored.is_set()}}

        session.report.side_effect = status
        with tempfile.TemporaryDirectory() as folder:
            args = guided.parser().parse_args([
                "--index", "0", "--out", str(Path(folder) / "cancelled.json")])
            with patch.object(guided, "build_guided_window", return_value=ui), \
                 patch.object(ps4_stereo_session, "StereoSession", return_value=session), \
                 patch.dict(sys.modules, {"tkinter": Mock()}):
                report = guided.run_guided(args, close_after_s=.1)
        self.assertTrue(restored.is_set())
        self.assertTrue(root.closed)
        self.assertFalse(report["capture"]["cleanup_pending"])
        self.assertFalse(report["accepted"])

    def test_finished_cleanup_error_is_not_reported_as_permanent_pending(self):
        from tools.psvr_camera.ps4_stereo_session import StereoSession
        session = StereoSession(0)
        closed = threading.Event()
        closed.set()
        session._session = types.SimpleNamespace(
            _closed=closed, _cleanup_error=RuntimeError("synthetic release error"))
        session._cleanup_pending = True
        session.done.set()
        report = session.report()
        self.assertFalse(report["cleanup_pending"])
        self.assertIn("synthetic release error", report["error"])

    def test_manual_start_fits_training_only_and_never_saves_failed_holdout(self):
        from tools.psvr_camera import ps4_stereo_session, stereo_calibration
        original_guide = guided.StereoCaptureGuide

        class FastGuide(original_guide):
            def __init__(self, spec, target):
                super().__init__(spec, target, hold_s=.002, move_s=.003)

        for accepted in (True, False):
            with self.subTest(heldout_accepted=accepted), tempfile.TemporaryDirectory() as folder:
                root = FakeRoot()
                ui = types.SimpleNamespace(root=root, status=Mock(), progress=Mock(),
                                           image_label=Mock(), start_button=Mock())
                calls = []

                class Session:
                    def __init__(self, *args, **kwargs):
                        self.sequence = 0

                    def start(self):
                        pass

                    def next_pair(self, timeout_s):
                        self.sequence += 1
                        pose = (self.sequence // 8) % 15
                        image = np.full((400, 640, 3), pose, np.uint8)
                        return {"left": image, "right": image + 30,
                                "sequence": self.sequence,
                                "host_monotonic": self.sequence * .01}

                    def stop(self, timeout_s):
                        calls.append("camera_stopped")
                        return True

                    def report(self):
                        return {"error": None, "cleanup_pending": False,
                                "controls": {"restoration_failed": False}}

                def detect(image, spec, cv2):
                    marker = int(image[0, 0, 0])
                    points = spec.object_points()[:, :2] * 1000 + (30 + marker * 10, 50)
                    return points

                calibration = types.SimpleNamespace(rms=.1, T=np.array([.06, 0, 0]))

                def solve(*args, **kwargs):
                    self.assertEqual(calls, ["camera_stopped"])
                    return calibration

                output = Path(folder) / "stereo.json"
                args = guided.parser().parse_args(["--index", "0", "--out", str(output)])
                tk = Mock()
                tk.BooleanVar.return_value.get.return_value = True
                # Simulate an operator pressing Start; production never auto-starts.
                root.after(1, lambda: ui.start_button.configure.call_args.kwargs["command"]())
                with patch.object(guided, "build_guided_window", return_value=ui), \
                     patch.object(guided, "StereoCaptureGuide", FastGuide), \
                     patch.object(guided, "detect_board", side_effect=detect), \
                     patch.object(ps4_stereo_session, "StereoSession", Session), \
                     patch.dict(sys.modules, {"tkinter": tk}), \
                     patch.object(stereo_calibration, "solve_stereo", side_effect=solve) as fit, \
                     patch.object(stereo_calibration, "validate_stereo",
                                  return_value={"accepted": accepted}) as validate, \
                     patch.object(stereo_calibration, "save_stereo") as save:
                    report = guided.run_guided(args, close_after_s=.5)
                self.assertEqual(report["views"], 15)
                self.assertEqual(report["accepted"], accepted)
                self.assertEqual(len(fit.call_args.args[0]), 12)
                self.assertEqual(len(validate.call_args.args[1]), 3)
                training_left = fit.call_args.args[0]
                for heldout in validate.call_args.args[1]:
                    self.assertFalse(any(np.array_equal(heldout, train) for train in training_left))
                if accepted:
                    save.assert_called_once_with(output, calibration)
                else:
                    save.assert_not_called()
                    self.assertTrue(report["errors"])

    def test_preview_never_collects_before_start_and_close_restores_before_destroy(self):
        import cv2
        from tools.psvr_camera import ps4_stereo_session
        root = FakeRoot()
        ui = types.SimpleNamespace(root=root, status=Mock(), progress=Mock(),
                                   image_label=Mock(), start_button=Mock())
        closed = []

        class Session:
            def __init__(self, *args, **kwargs):
                self.sent = False

            def start(self):
                pass

            def next_pair(self, timeout_s):
                if self.sent:
                    time.sleep(.005)
                    return None
                self.sent = True
                image = np.zeros((400, 640, 3), np.uint8)
                return {"left": image, "right": image.copy(), "sequence": 1,
                        "host_monotonic": time.monotonic()}

            def stop(self, timeout_s):
                self.assert_root_open = not root.closed
                closed.append(self.assert_root_open)
                return True

            def report(self):
                return {"error": None, "cleanup_pending": False,
                        "controls": {"restoration_failed": False}}

        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "never-started.json"
            args = guided.parser().parse_args(["--index", "0", "--out", str(output)])
            tk = Mock()
            with patch.object(guided, "build_guided_window", return_value=ui), \
                 patch.object(ps4_stereo_session, "StereoSession", Session), \
                 patch.dict(sys.modules, {"tkinter": tk}), \
                 patch.object(cv2, "VideoCapture", side_effect=AssertionError("no HW")):
                report = guided.run_guided(args, close_after_s=.15)
            self.assertEqual(closed, [True])
            self.assertTrue(root.closed)
            self.assertFalse(report["accepted"])
            self.assertEqual(report["views"], 0)
            self.assertEqual(report["preview_frames"], 1)
            tk.PhotoImage.assert_called_once()
            self.assertTrue(any("annullata" in error for error in report["errors"]))
            self.assertFalse(output.exists())
            data = json.loads(output.with_suffix(".views.json").read_text())
            self.assertEqual(data["views"], [])


if __name__ == "__main__":
    unittest.main()
