# SPDX-License-Identifier: MIT
"""CLI tests for ``python -m psvr_camera``.

Subprocess-level, using the offline paths only: no camera, no OpenCV, no UDP.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from psvr_camera import geometry as G  # noqa: E402
from psvr_camera.calibration import ChessboardSpec  # noqa: E402

CV2_INSTALLED = importlib.util.find_spec("cv2") is not None


def run(*args, env_extra=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(TOOLS) + os.pathsep + env.get("PYTHONPATH", "")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-B", "-m", "psvr_camera", *args],
        capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT), env=env,
        timeout=120,
    )


def synthetic_detections(path: Path, views=4):
    spec = ChessboardSpec(9, 6, 0.025)
    objects = spec.object_points()
    camera = np.array([[700.0, 0.0, 320.0], [0.0, 690.0, 240.0], [0.0, 0.0, 1.0]])
    poses = [
        ([0.3, 0.2, 0.05], [-0.1, -0.05, 0.5]),
        ([-0.35, 0.25, 0.1], [0.12, -0.08, 0.6]),
        ([0.25, -0.3, 0.15], [0.05, 0.1, 0.55]),
        ([0.4, 0.15, -0.1], [-0.05, -0.1, 0.45]),
    ]
    payload = {
        "image_size": [640, 480],
        "views": [
            G.project_points(camera, G.rodrigues(rvec), t, objects).tolist()
            for rvec, t in poses[:views]
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


class HelpAndProtocolTest(unittest.TestCase):
    def test_help_exits_zero(self):
        proc = run("--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("webcam-side", proc.stdout)

    def test_protocol_is_versioned_json(self):
        proc = run("protocol")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        document = json.loads(proc.stdout)
        self.assertEqual(document["protocol"], "psvr.position")
        self.assertEqual(document["version"], 1)
        self.assertEqual(document["axes"], {"right": "+X", "up": "+Y", "forward": "-Z"})

    def test_probe_requires_explicit_index(self):
        proc = run("probe", "--frames", "1")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--index", proc.stderr)

    def test_unknown_subcommand_is_usage_error(self):
        proc = run("wobble")
        self.assertEqual(proc.returncode, 2)


class OfflineCalibrationTest(unittest.TestCase):
    def test_calibrate_from_detections_writes_strict_intrinsics(self):
        with tempfile.TemporaryDirectory() as tmp:
            detections = Path(tmp) / "detections.json"
            out = Path(tmp) / "intrinsics.json"
            synthetic_detections(detections)
            proc = run(
                "calibrate", "offline",
                "--cols", "9", "--rows", "6", "--square-mm", "25",
                "--detections", str(detections), "--no-opencv",
                "--out", str(out),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            summary = json.loads(proc.stdout)
            self.assertTrue(summary["ok"])
            self.assertTrue(out.exists())
            saved = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(saved["schema"], "psvr.camera.intrinsics")
            self.assertTrue(saved["calibrated"])
            # The generated file must survive the strict loader via the CLI.
            verified = run("verify", "--intrinsics", str(out), "--image-size", "640x480")
            self.assertEqual(verified.returncode, 0, verified.stderr)

    def test_bad_detections_is_calibration_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            detections = Path(tmp) / "bad.json"
            detections.write_text('{"image_size":[640,480],"views":[]}', encoding="utf-8")
            proc = run(
                "calibrate", "offline",
                "--cols", "9", "--rows", "6", "--square-mm", "25",
                "--detections", str(detections), "--no-opencv",
            )
            self.assertEqual(proc.returncode, 3)
            self.assertFalse(json.loads(proc.stdout)["ok"])

    def test_verify_missing_file_is_calibration_error(self):
        proc = run("verify", "--intrinsics", "does-not-exist.json")
        self.assertEqual(proc.returncode, 3)


class TrackPreflightTest(unittest.TestCase):
    def _write_intrinsics(self, path: Path):
        from psvr_camera.calibration import Intrinsics, save_intrinsics

        save_intrinsics(
            path,
            Intrinsics(
                camera_matrix=np.array([[700.0, 0, 320.0], [0, 690.0, 240.0], [0, 0, 1.0]]),
                distortion_coeffs=np.zeros(5),
                image_size=(640, 480),
                calibrated=True,
            ),
        )

    def test_blob_rig_live_tracking_is_usage_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            intrinsics = Path(tmp) / "intrinsics.json"
            geometry = Path(tmp) / "geometry.json"
            self._write_intrinsics(intrinsics)
            geometry.write_text(json.dumps({
                "schema": "psvr.camera.marker_geometry",
                "version": 1,
                "kind": "blob_led",
                "provenance": "user_measured",
                "object_points_m": [[0, 0, 0], [0.1, 0, 0], [0, 0.1, 0], [0, 0, 0.1]],
            }), encoding="utf-8")
            proc = run("track", "--intrinsics", str(intrinsics), "--geometry", str(geometry),
                       "--index", "0", "--no-send")
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn("ArUco", json.loads(proc.stdout)["error"])

    def test_builtin_led_geometry_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            intrinsics = Path(tmp) / "intrinsics.json"
            geometry = Path(tmp) / "geometry.json"
            self._write_intrinsics(intrinsics)
            geometry.write_text(json.dumps({
                "schema": "psvr.camera.marker_geometry",
                "version": 1,
                "kind": "aruco",
                "provenance": "psvr_builtin",
                "marker_length_m": 0.1,
                "object_points_m": [[-0.05, 0.05, 0], [0.05, 0.05, 0], [0.05, -0.05, 0], [-0.05, -0.05, 0]],
            }), encoding="utf-8")
            proc = run("track", "--intrinsics", str(intrinsics), "--geometry", str(geometry),
                       "--index", "0", "--no-send")
            self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
            self.assertIn("not calibrated", json.loads(proc.stdout)["error"])


@unittest.skipIf(CV2_INSTALLED, "cv2 is installed; absence path not applicable")
class InventoryWithoutOpenCvTest(unittest.TestCase):
    def test_inventory_reports_opencv_absence(self):
        proc = run("inventory")
        self.assertEqual(proc.returncode, 2)
        report = json.loads(proc.stdout)
        self.assertFalse(report["opencv"])
        self.assertEqual(report["cameras"], [])

    def test_probe_reports_opencv_absence(self):
        proc = run("probe", "--index", "0", "--frames", "1", "--max-seconds", "0.1")
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(json.loads(proc.stdout)["opened"])


class NegativeLimitTest(unittest.TestCase):
    def test_track_rejects_negative_frames(self):
        proc = run("track", "--intrinsics", "x", "--geometry", "y",
                   "--index", "0", "--frames", "-1")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("non-negative", proc.stderr)

    def test_track_rejects_negative_max_seconds(self):
        proc = run("track", "--intrinsics", "x", "--geometry", "y",
                   "--index", "0", "--max-seconds", "-1")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)

    def test_track_rejects_zero_max_reprojection(self):
        proc = run("track", "--intrinsics", "x", "--geometry", "y",
                   "--index", "0", "--max-reprojection-px", "0")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)

    def test_probe_rejects_zero_frames(self):
        proc = run("probe", "--index", "0", "--frames", "0")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)

    def test_calibrate_live_rejects_bad_views(self):
        proc = run("calibrate", "live", "--cols", "9", "--rows", "6",
                   "--square-mm", "25", "--index", "0", "--views", "2")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertFalse(json.loads(proc.stdout)["ok"])


class PatternCliTest(unittest.TestCase):
    def test_chessboard_svg_is_metric_and_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "board.svg"
            args = ("pattern", "chessboard", "--cols", "9", "--rows", "6",
                    "--square-mm", "25", "--out", str(out))
            proc = run(*args)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            report = json.loads(proc.stdout)
            self.assertTrue(report["ok"])
            self.assertTrue(out.exists())
            text = out.read_text(encoding="utf-8")
            # 10 squares x 25 mm + a 25 mm margin each side.
            self.assertIn('width="300mm"', text)
            self.assertIn('height="225mm"', text)
            self.assertEqual(text.count('fill="#000000"'), 35)
            # Deterministic: a second run with --force is byte-identical.
            forced = run(*args, "--force")
            self.assertEqual(forced.returncode, 0, forced.stdout + forced.stderr)
            self.assertEqual(out.read_text(encoding="utf-8"), text)

    def test_chessboard_refuses_overwrite_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "board.svg"
            out.write_text("existing", encoding="utf-8")
            proc = run("pattern", "chessboard", "--cols", "9", "--rows", "6",
                       "--square-mm", "25", "--out", str(out))
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertEqual(out.read_text(encoding="utf-8"), "existing")

    @unittest.skipIf(CV2_INSTALLED, "cv2 present; absence path not applicable")
    def test_aruco_without_opencv_is_camera_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "marker.png"
            proc = run("pattern", "aruco", "--marker-mm", "100", "--out", str(out))
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertFalse(json.loads(proc.stdout)["ok"])

    @unittest.skipUnless(CV2_INSTALLED, "cv2 not installed")
    def test_aruco_png_and_geometry(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "marker.png"
            geom = Path(tmp) / "geometry.json"
            proc = run("pattern", "aruco", "--dictionary", "DICT_4X4_50",
                       "--id", "0", "--marker-mm", "100", "--out", str(out),
                       "--geometry-out", str(geom))
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertTrue(out.exists() and out.stat().st_size > 0)
            saved = json.loads(geom.read_text(encoding="utf-8"))
            self.assertEqual(saved["provenance"], "user_measured")
            self.assertEqual(saved["marker_length_m"], 0.1)


class PseyeTrackPreflightTest(unittest.TestCase):
    def test_pseye_backend_unavailable_reports_camera_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            from psvr_camera.calibration import Intrinsics, save_intrinsics

            intrinsics = Path(tmp) / "intrinsics.json"
            geometry = Path(tmp) / "geometry.json"
            save_intrinsics(
                intrinsics,
                Intrinsics(
                    camera_matrix=np.array(
                        [[700.0, 0, 320.0], [0, 690.0, 240.0], [0, 0, 1.0]]
                    ),
                    distortion_coeffs=np.zeros(5),
                    image_size=(640, 480),
                    calibrated=True,
                ),
            )
            geometry.write_text(json.dumps({
                "schema": "psvr.camera.marker_geometry",
                "version": 1,
                "kind": "aruco",
                "provenance": "user_measured",
                "marker_length_m": 0.1,
                "object_points_m": [
                    [-0.05, 0.05, 0], [0.05, 0.05, 0],
                    [0.05, -0.05, 0], [-0.05, -0.05, 0],
                ],
            }), encoding="utf-8")
            proc = run(
                "track", "--intrinsics", str(intrinsics), "--geometry", str(geometry),
                "--index", "0", "--backend", "pseye", "--no-send",
                env_extra={
                    "PSVR_PSEYE_DLL": str(Path(tmp) / "missing" / "psvr_pseye.dll")
                },
            )
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertFalse(json.loads(proc.stdout)["ok"])


if __name__ == "__main__":
    unittest.main()
