# SPDX-License-Identifier: MIT
"""B13-03: correlate_flashes/summarize_pacing escono 2 pulito su input mancante/malformato."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PACING_SCRIPT = ROOT / "tools/psvr_runtime_probe/summarize_pacing.py"
CORRELATE_SCRIPT = ROOT / "tools/psvr_runtime_probe/correlate_flashes.py"


def run(script, *args):
    return subprocess.run([sys.executable, str(script), *map(str, args)],
                          capture_output=True, text=True, timeout=60, check=False)


def sample_row(utc, valid=True):
    return {"event": "sample", "utc_unix_us": utc, "raw_pose": {"valid": valid},
            "fullscreen": True, "dashboard_visible": False, "poll_gap_ms": 1.0}


def frame_row(utc, index, system_time):
    return {"event": "frame_timing", "utc_unix_us": utc, "frame_index": index,
            "system_time_s": system_time, "dropped": 0, "mispresents": 0,
            "presents": 1, "reprojection_flags": 0, "compositor_gpu_ms": 1.0,
            "compositor_cpu_ms": 1.0, "present_cpu_ms": 0.5}


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(Path(path).parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(Path(path).parent))
    return module


class PacingCliTests(unittest.TestCase):
    def test_missing_file_exits_2_without_traceback(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "missing.jsonl"
            before = set(Path(temp).iterdir())
            proc = run(PACING_SCRIPT, missing)
            self.assertEqual(proc.returncode, 2, (proc.stdout, proc.stderr))
            self.assertNotIn("Traceback", proc.stdout + proc.stderr)
            self.assertEqual(before, set(Path(temp).iterdir()))

    def test_malformed_file_exits_2_without_traceback(self):
        with tempfile.TemporaryDirectory() as temp:
            bad = Path(temp) / "bad.jsonl"
            bad.write_text("not json\n", encoding="utf-8")
            proc = run(PACING_SCRIPT, bad)
            self.assertEqual(proc.returncode, 2, (proc.stdout, proc.stderr))
            self.assertNotIn("Traceback", proc.stdout + proc.stderr)
            self.assertEqual(bad.read_text(encoding="utf-8"), "not json\n")

    def test_valid_input_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "telemetry.jsonl"
            with path.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(sample_row(2_000_000)) + "\n")
                handle.write(json.dumps(sample_row(3_000_000, valid=False)) + "\n")
                handle.write(json.dumps(frame_row(2_000_000, 10, 100.0)) + "\n")
                handle.write(json.dumps(frame_row(3_000_000, 11, 100.011)) + "\n")
            proc = run(PACING_SCRIPT, path, "--seconds", "30")
            self.assertEqual(proc.returncode, 0, (proc.stdout, proc.stderr))
            report = json.loads(proc.stdout)
            self.assertEqual(report["samples"], 2)
            self.assertEqual(report["frame_records"], 2)
            self.assertEqual(report["raw_invalid"], 1)
            self.assertEqual(set(Path(temp).iterdir()), {path})


class CorrelateCliTests(unittest.TestCase):
    def test_missing_video_exits_2_without_traceback_and_no_file(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "report.json"
            proc = run(CORRELATE_SCRIPT, Path(temp) / "missing.mp4", "--out", out)
            self.assertEqual(proc.returncode, 2, (proc.stdout, proc.stderr))
            self.assertNotIn("Traceback", proc.stdout + proc.stderr)
            self.assertFalse(out.exists())

    def test_malformed_metadata_exits_2_without_traceback_and_no_file(self):
        with tempfile.TemporaryDirectory() as temp:
            video = Path(temp) / "rec.mp4"
            video.write_bytes(b"\x00")
            (Path(temp) / "rec.metadata.json").write_text("not json", encoding="utf-8")
            out = Path(temp) / "report.json"
            proc = run(CORRELATE_SCRIPT, video, "--out", out,
                       "--ffmpeg", Path(temp) / "missing-ffmpeg.exe")
            self.assertEqual(proc.returncode, 2, (proc.stdout, proc.stderr))
            self.assertNotIn("Traceback", proc.stdout + proc.stderr)
            self.assertFalse(out.exists())

    def test_valid_input_unchanged_with_mocked_analysis(self):
        correlate = load_module("correlate_flashes", CORRELATE_SCRIPT)
        start = "2026-10-06T08:00:00Z"
        end = "2026-10-06T08:00:01Z"
        start_us = int(datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp() * 1_000_000)
        mid_us = start_us + 500_000
        fake_analysis = {"bright_intervals_above_median_plus_8": [],
                         "candidate_broad_flash_count": 0}
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            video = directory / "rec.mp4"
            video.write_bytes(b"\x00")
            (directory / "rec.metadata.json").write_text(json.dumps(
                {"video_started_utc": start, "video_ended_utc": end,
                 "recording_completed": True}), encoding="utf-8")
            with (directory / "rec.telemetry.jsonl").open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(sample_row(mid_us)) + "\n")
                handle.write(json.dumps({"event": "telemetry_summary"}) + "\n")
            (directory / "rec.vrserver-after.txt").write_text("", encoding="utf-8")
            with mock.patch.object(correlate, "analyze", return_value=fake_analysis):
                report = correlate.correlate(video, directory / "ffmpeg.exe")
            self.assertEqual(report["samples_during_video"], 1)
            self.assertEqual(report["raw_invalid_during_video"], 0)
            self.assertIs(report["video_analysis"], fake_analysis)
            self.assertTrue(report["recording_completed"])


if __name__ == "__main__":
    unittest.main()
