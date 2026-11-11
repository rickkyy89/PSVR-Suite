# SPDX-License-Identifier: MIT
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tools/psvr_runtime_probe/summarize_capture.py"
spec = importlib.util.spec_from_file_location("presentation_summary", SCRIPT)
summary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(summary)


class CaptureSummaryTests(unittest.TestCase):
    def fixture(self, directory, exit_code=0, valid=10, fullscreen=True, elapsed=30):
        def save(name, data):
            (directory / name).write_text(json.dumps(data), encoding="utf-8")
        save("metadata.json", {"duration_seconds": 30, "synthetic_presence_requested": True})
        save("completion.json", {"elapsed_seconds": elapsed, "samples": 1})
        save("sample-0000.json", {
            "start_utc": "2026-10-06T08:00:00Z", "end_utc": "2026-10-06T08:00:01Z",
            "runtime_probe_exit": exit_code,
            "runtime_probe_lines": [json.dumps({"event": "summary", "valid_count": valid,
                "sample_count": 10, "compositor": {"fullscreen": fullscreen, "can_render_scene": True}})],
            "displays": {"displays": [{"monitor_device_path": "DISPLAY#SNY6A04#fixture",
                "source": {"x": 1920, "y": 492, "width": 1920, "height": 1080},
                "refresh": {"numerator": 90, "denominator": 1},
                "target": {"signal_refresh": {"numerator": 90, "denominator": 1}}}]},
        })

    def test_complete_capture_never_claims_lens_acceptance_and_changes_no_files(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.fixture(directory)
            before = {p.name: p.read_bytes() for p in directory.iterdir()}
            report = summary.summarize(directory)
            self.assertTrue(report["unattended_sampled_checks_passed"])
            self.assertFalse(report["physical_lens_acceptance_verified"])
            self.assertEqual(report["valid_pose_samples"], 10)
            self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir()})

    def test_tracking_failure_is_not_misclassified_as_fullscreen_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.fixture(directory, exit_code=4, valid=0)
            report = summary.summarize(directory)
            self.assertFalse(report["unattended_sampled_checks_passed"])
            self.assertEqual(report["fullscreen_counts"], {"true": 1})
            self.assertEqual(report["first_failed_sample"]["reasons"], ["probe_failed", "invalid_pose_samples"])

    def test_interrupted_capture_cannot_pass_despite_valid_poses(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.fixture(directory, elapsed=10)
            report = summary.summarize(directory)
            self.assertFalse(report["unattended_sampled_checks_passed"])
            self.assertFalse(report["requested_duration_reached"])

    def test_missing_summary_and_missing_completion_cannot_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.fixture(directory)
            path = directory / "sample-0000.json"
            sample = json.loads(path.read_text())
            sample["runtime_probe_lines"] = []
            path.write_text(json.dumps(sample))
            (directory / "completion.json").unlink()
            report = summary.summarize(directory)
            self.assertFalse(report["unattended_sampled_checks_passed"])
            self.assertEqual(report["summaries_parsed"], 0)
            self.assertIn("missing_probe_summary", report["first_failed_sample"]["reasons"])


if __name__ == "__main__":
    unittest.main()
