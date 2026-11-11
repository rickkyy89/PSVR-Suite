"""Synthetic PS4 panel tests: no Tk display, hardware, or subprocess."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.psvr_suite import i18n
from tools.psvr_suite.ps4_camera_panel import summarize_output


class PointsI18nTests(unittest.TestCase):
    def test_points_key_it_en(self):
        self.assertEqual(i18n.t("it", "ps4.points"), "Punti 3D (30 s)")
        self.assertEqual(i18n.t("en", "ps4.points"), "3D points (30 s)")

    def test_no_missing_translations(self):
        self.assertEqual(i18n.missing_translations(), {})


class StereoSummaryTests(unittest.TestCase):
    def make_report(self):
        stereo = {
            "pairs_analyzed": 4,
            "state_counts": {"points": 3, "no_matches": 1},
            "point_count_histogram": {"0": 1, "2": 3},
            "stream_pair_fps": 30.0,
            "point_observations": 6,
            "point_reprojection_rms_px": 0.5,
            "matched_patch_depth_range_m": [0.4, 1.2],
            # Blob pesante: non deve finire nel riepilogo.
            "last_result": {"state": "points",
                            "matches": [{"point_left_camera_m": [0, 0, 0.5]}] * 200},
        }
        return {"errors": [], "capture": {"error": None, "cleanup_pending": False},
                "stereo_probe": stereo, "report_file": "full.json",
                "snapshot": "last.jpg", "duration_s": 30.1}

    def test_stereo_summary_bounded_without_blobs(self):
        report = self.make_report()
        original = json.dumps(report)
        text = summarize_output(original)
        self.assertLess(len(text), 16000)
        summary = json.loads(text)
        self.assertEqual(summary["state_counts"], {"points": 3, "no_matches": 1})
        self.assertEqual(summary["pairs_analyzed"], 4)
        self.assertEqual(summary["point_observations"], 6)
        self.assertEqual(summary["point_count_histogram"], {"0": 1, "2": 3})
        self.assertEqual(summary["point_reprojection_rms_px"], 0.5)
        self.assertEqual(summary["matched_patch_depth_range_m"], [0.4, 1.2])
        self.assertEqual(summary["errors"], [])
        self.assertFalse(summary["cleanup_pending"])
        self.assertFalse(summary["tracking_6dof"])
        self.assertFalse(summary["camera_controls_modified"])
        self.assertEqual(summary["report_file"], "full.json")
        self.assertEqual(summary["snapshot"], "last.jpg")
        self.assertNotIn("last_result", text)
        self.assertNotIn("point_left_camera_m", text)

    def test_stereo_errors_and_cleanup(self):
        report = self.make_report()
        report["errors"] = ["brightness restore failed"]
        report["capture"] = {"error": "synthetic capture failure",
                             "cleanup_pending": True}
        summary = json.loads(summarize_output(json.dumps(report)))
        self.assertIn("brightness restore failed", summary["errors"])
        self.assertIn("synthetic capture failure", summary["errors"])
        self.assertTrue(summary["cleanup_pending"])

    def test_fallback_for_inventory_and_invalid(self):
        inventory = json.dumps({"state": "uvc_present"})
        self.assertEqual(summarize_output(inventory), inventory)
        self.assertEqual(summarize_output("[]"), "[]")
        self.assertEqual(len(summarize_output("x" * 20000)), 16000)


def make_tk_fakes():
    created = []

    class FakeVar:
        def __init__(self, value=""):
            self._v = value

        def get(self):
            return self._v

        def set(self, value):
            self._v = value

    class FakeWidget:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

        def grid(self, *args, **kwargs):
            return None

        def pack(self, *args, **kwargs):
            return None

        def configure(self, *args, **kwargs):
            return None

        def delete(self, *args, **kwargs):
            return None

        def insert(self, *args, **kwargs):
            return None

    class FakeText(FakeWidget):
        pass

    class FakeWindow(FakeWidget):
        def title(self, *args, **kwargs):
            return None

        def geometry(self, *args, **kwargs):
            return None

        def columnconfigure(self, *args, **kwargs):
            return None

        def rowconfigure(self, *args, **kwargs):
            return None

        def after(self, *args, **kwargs):
            return 1

        def after_cancel(self, *args, **kwargs):
            return None

        def bind(self, *args, **kwargs):
            return None

    def fake_button(parent, **kwargs):
        created.append(kwargs)
        return FakeWidget()

    def fake_frame(parent, **kwargs):
        return FakeWidget()

    def fake_label(parent, **kwargs):
        return FakeWidget()

    fake_tk = SimpleNamespace(Toplevel=lambda parent: FakeWindow(),
                              StringVar=FakeVar, Text=lambda *a, **k: FakeText())
    fake_ttk = SimpleNamespace(Label=fake_label, Frame=fake_frame,
                               Button=fake_button,
                               Combobox=lambda *a, **k: FakeWidget(),
                               Entry=lambda *a, **k: FakeWidget())
    fake_tk.ttk = fake_ttk
    return fake_tk, fake_ttk, created


class PointsCommandTests(unittest.TestCase):
    def open_with_fakes(self, run_thread=False):
        from tools.psvr_suite import ps4_camera_panel as panel
        fake_tk, fake_ttk, created = make_tk_fakes()
        threads = []

        class FakeThread:
            def __init__(self, target=None, daemon=None, name=None):
                self._target = target
                threads.append(self)

            def start(self):
                if run_thread and self._target is not None:
                    self._target()

        proc = Mock()
        proc.poll.return_value = None
        proc.communicate.return_value = ("{}", "")
        with patch.dict(sys.modules, {"tkinter": fake_tk, "tkinter.ttk": fake_ttk}), \
                patch.object(subprocess, "Popen", return_value=proc) as popen, \
                patch.object(panel.threading, "Thread", FakeThread), \
                patch.object(Path, "mkdir", lambda self, *a, **k: None):
            window = panel.open_panel(Mock(), language="it")
        return panel, window, created, popen, proc, threads

    def find_command(self, created, label_text):
        for entry in created:
            if entry.get("text") == label_text:
                return entry.get("command")
        self.fail(f"button {label_text!r} not found")

    def test_points_button_builds_stereo_probe_command(self):
        from tools.psvr_suite import ps4_camera_panel as panel
        fake_tk, fake_ttk, created = make_tk_fakes()

        class FakeThread:
            def __init__(self, target=None, daemon=None, name=None):
                self._target = target

            def start(self):
                return None

        proc = Mock()
        proc.poll.return_value = None
        proc.communicate.return_value = ("{}", "")
        with patch.dict(sys.modules, {"tkinter": fake_tk, "tkinter.ttk": fake_ttk}), \
                patch.object(subprocess, "Popen", return_value=proc) as popen, \
                patch.object(panel.threading, "Thread", FakeThread), \
                patch.object(Path, "mkdir", lambda self, *a, **k: None):
            panel.open_panel(Mock(), language="it")
            points_label = i18n.t("it", "ps4.points")
            self.assertTrue(any(e.get("text") == points_label for e in created))
            command = self.find_command(created, points_label)
            command()
            self.assertEqual(popen.call_count, 1)
            argv = popen.call_args.args[0]
        self.assertIn("tools.psvr_camera.ps4_stereo_probe", argv)
        self.assertIn("--index", argv)
        self.assertIn("--mode", argv)
        self.assertIn("--fps", argv)
        # Default UI: index 0, mode 640x400, fps 60.
        self.assertEqual(argv[argv.index("--index") + 1], "0")
        self.assertEqual(argv[argv.index("--mode") + 1], "640x400")
        self.assertEqual(argv[argv.index("--fps") + 1], "60")
        self.assertEqual(argv[argv.index("--seconds") + 1], "30")
        self.assertEqual(argv[argv.index("--brightness") + 1], "0")
        out_dir = argv[argv.index("--output-dir") + 1]
        self.assertTrue(out_dir.replace("\\", "/").endswith("logs/ps4_camera"))
        stop_file = argv[argv.index("--stop-file") + 1]
        self.assertTrue(stop_file.endswith(".stop"))
        self.assertNotIn("--backend", argv)
        self.assertNotIn("--preview", argv)

    def test_points_timeout_uses_cooperative_stop_file(self):
        from tools.psvr_suite import ps4_camera_panel as panel
        fake_tk, fake_ttk, created = make_tk_fakes()
        threads = []

        class FakeThread:
            def __init__(self, target=None, daemon=None, name=None):
                self._target = target
                threads.append(self)

            def start(self):
                if self._target is not None:
                    self._target()

        proc = Mock()
        proc.poll.return_value = None
        expired = subprocess.TimeoutExpired(cmd="ps4", timeout=60)
        proc.communicate.side_effect = [expired, ("{}", "")]
        written = []

        def fake_write(self, text, encoding=None, errors=None):
            written.append((str(self), text))
            return len(text)

        with patch.dict(sys.modules, {"tkinter": fake_tk, "tkinter.ttk": fake_ttk}), \
                patch.object(subprocess, "Popen", return_value=proc) as popen, \
                patch.object(panel.threading, "Thread", FakeThread), \
                patch.object(Path, "mkdir", lambda self, *a, **k: None), \
                patch.object(Path, "write_text", fake_write):
            panel.open_panel(Mock(), language="it")
            points_label = i18n.t("it", "ps4.points")
            command = next(e.get("command") for e in created
                           if e.get("text") == points_label)
            command()
        self.assertEqual(popen.call_count, 1)
        argv = popen.call_args.args[0]
        timeout = proc.communicate.call_args_list[0].kwargs.get("timeout")
        self.assertEqual(timeout, 60)
        stop_file = argv[argv.index("--stop-file") + 1]
        self.assertTrue(any(path == stop_file and text == "stop\n"
                            for path, text in written))


if __name__ == "__main__":
    unittest.main()
