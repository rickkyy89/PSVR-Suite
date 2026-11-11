# SPDX-License-Identifier: MIT
"""Native PS3 Eye backend tests (hardware-free).

The ctypes function table is injected as a fake module, so selection, bounding,
reporting and provenance are exercised without a camera, a DLL or OpenCV. A
real capture is available opt-in through ``PSVR_PSEYE_HARDWARE=1``.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from psvr_camera import pseye_backend as PB  # noqa: E402
from psvr_camera.camera_backend import CameraError  # noqa: E402
from psvr_camera.camera_source import open_camera_source  # noqa: E402


class FakePseye:
    """Minimal stand-in for a loaded psvr_pseye.dll function table."""

    def __init__(self, count=1, open_indices=(0,), frame_limit=10, width=640,
                 height=480, bpp=3, fps=60, fill_sequence=False,
                 short_write=False, block_until_abort=False):
        self.count_value = count
        self.open_indices = set(open_indices)
        self.frame_limit = frame_limit
        self.width_value = width
        self.height_value = height
        self.bpp = bpp
        self.fps_value = fps
        self.fill_sequence = fill_sequence
        self.short_write = short_write
        self.block_until_abort = block_until_abort
        self.grabs = 0
        self.closed = []
        self.aborted = 0
        self.set_timeouts = []
        self.abort_event = threading.Event()

    # provenance
    def psvr_pseye_abi_version(self):
        return 2

    def psvr_pseye_upstream_id(self):
        return b"fake/PS3EYEDriver@deadbeef"

    def psvr_pseye_license_note(self):
        return b"fake license note"

    def psvr_pseye_libusb_note(self):
        return b"fake libusb note"

    # device ops
    def psvr_pseye_count(self):
        return self.count_value

    def psvr_pseye_open(self, index, width, height, fps, fmt):
        return (1000 + index) if index in self.open_indices else None

    def psvr_pseye_close(self, cam):
        self.closed.append(cam)

    def psvr_pseye_grab_timeout(self, cam, buf, size, timeout_ms):
        self.grabs += 1
        if self.block_until_abort:
            # Mimic a native grab that only returns when abort() is called.
            self.abort_event.wait(min(max(timeout_ms, 1) / 1000.0, 5.0))
            return -1
        if self.grabs > self.frame_limit:
            return -1
        if self.short_write:
            return size - 1
        if self.fill_sequence:
            ctypes.memset(buf, self.grabs & 0xFF, min(size, 8))
        return size

    def psvr_pseye_grab(self, cam, buf, size):
        return self.psvr_pseye_grab_timeout(cam, buf, size, 1000)

    def psvr_pseye_abort(self, cam):
        self.aborted += 1
        self.abort_event.set()

    def psvr_pseye_set_grab_timeout(self, cam, timeout_ms):
        self.set_timeouts.append(timeout_ms)

    def psvr_pseye_width(self, cam):
        return self.width_value

    def psvr_pseye_height(self, cam):
        return self.height_value

    def psvr_pseye_fps(self, cam):
        return self.fps_value

    def psvr_pseye_bytes_per_pixel(self, cam):
        return self.bpp

    def psvr_pseye_unique_id(self, cam, out, out_len):
        return 0


class InventoryTest(unittest.TestCase):
    def test_fake_inventory_reports_count_and_provenance(self):
        fake = FakePseye(count=2)
        report = PB.inventory(module=fake)
        self.assertTrue(report["available"])
        self.assertEqual(report["count"], 2)
        self.assertEqual(report["backend"], "pseye")
        self.assertIn("PS3EYEDriver", report["upstream"])
        self.assertEqual(report["abi_version"], 2)

    @unittest.skipUnless(sys.platform == "win32", "Windows-only backend")
    def test_missing_explicit_dll_reports_unavailable(self):
        report = PB.inventory(dll_path=str(Path(ROOT) / "does-not-exist" / "psvr_pseye.dll"))
        self.assertFalse(report["available"])
        self.assertTrue(report["error"])

    def test_env_override_is_authoritative(self):
        old = os.environ.get("PSVR_PSEYE_DLL")
        try:
            os.environ["PSVR_PSEYE_DLL"] = r"Z:\nope\psvr_pseye.dll"
            self.assertEqual(PB.find_dll(), Path(r"Z:\nope\psvr_pseye.dll"))
        finally:
            if old is None:
                os.environ.pop("PSVR_PSEYE_DLL", None)
            else:
                os.environ["PSVR_PSEYE_DLL"] = old

    def test_candidate_paths_include_staging(self):
        paths = [str(p) for p in PB.candidate_dll_paths()]
        self.assertTrue(any("out" in p and "pseye" in p for p in paths))


class CaptureTest(unittest.TestCase):
    def test_bounded_capture_reports_frames_and_fps(self):
        fake = FakePseye(frame_limit=3)
        report = PB.capture_frames_pseye(
            index=0, frames=8, max_seconds=5.0, warmup_frames=0, module=fake
        ).to_dict()
        self.assertTrue(report["opened"])
        self.assertEqual(report["frames_captured"], 3)
        self.assertGreaterEqual(report["timeouts"], 1)
        self.assertIsNotNone(report["achieved_fps"])
        self.assertEqual(report["backend"], "pseye")
        self.assertEqual(report["schema"], "psvr.camera.capture")
        self.assertEqual(report["negotiated"]["width"], 640)
        self.assertTrue(fake.closed)

    def test_unopened_index_reports_error_without_raising(self):
        fake = FakePseye(count=1, open_indices=set())
        report = PB.capture_frames_pseye(
            index=3, frames=5, max_seconds=1.0, module=fake
        ).to_dict()
        self.assertFalse(report["opened"])
        self.assertTrue(report["errors"])

    def test_invalid_bounds_rejected(self):
        fake = FakePseye()
        with self.assertRaises(CameraError):
            PB.capture_frames_pseye(index=0, frames=0, module=fake)
        with self.assertRaises(CameraError):
            PB.capture_frames_pseye(index=0, frames=1, max_seconds=0.0, module=fake)
        with self.assertRaises(CameraError):
            PB.capture_frames_pseye(index=-1, frames=1, module=fake)
        with self.assertRaises(CameraError):
            PB.capture_frames_pseye(index=0, frames=1, fmt="nonsense", module=fake)

    def test_fake_library_load_skips_native_loading(self):
        fake = FakePseye()
        lib = PB.PseyeLibrary.load(module=fake)
        self.assertEqual(lib.abi_version(), 2)
        self.assertEqual(lib.count(), 1)

    def test_bounded_capture_honors_max_seconds_when_grab_stalls(self):
        # A native grab that never delivers a frame must not make the
        # synchronous probe outlive max_seconds.
        fake = FakePseye(block_until_abort=True)
        started = time.perf_counter()
        report = PB.capture_frames_pseye(
            index=0, frames=120, max_seconds=0.6, warmup_frames=0, module=fake
        ).to_dict()
        elapsed = time.perf_counter() - started
        self.assertTrue(report["opened"], report["errors"])
        self.assertEqual(report["frames_captured"], 0)
        self.assertGreaterEqual(report["timeouts"], 1)
        self.assertLess(elapsed, 3.0)

    def test_short_frame_is_not_counted_as_captured(self):
        fake = FakePseye(frame_limit=100, short_write=True)
        report = PB.capture_frames_pseye(
            index=0, frames=4, max_seconds=5.0, warmup_frames=0, module=fake
        ).to_dict()
        self.assertTrue(report["opened"], report["errors"])
        self.assertEqual(report["frames_captured"], 0)
        self.assertGreaterEqual(report["timeouts"], 1)
        self.assertTrue(report["errors"])

    def test_per_grab_timeout_is_bounded_by_remaining_budget(self):
        seen = []

        class Recording(FakePseye):
            def psvr_pseye_grab_timeout(self, cam, buf, size, timeout_ms):
                seen.append(timeout_ms)
                return super().psvr_pseye_grab_timeout(
                    cam, buf, size, timeout_ms
                )

        fake = Recording(frame_limit=2)
        PB.capture_frames_pseye(
            index=0, frames=5, max_seconds=10.0, warmup_frames=0, module=fake
        )
        self.assertTrue(seen)
        self.assertTrue(all(0 < t <= PB._DEFAULT_GRAB_TIMEOUT_MS for t in seen))


class AdapterTest(unittest.TestCase):
    """The read()/release() adapter used by track and calibrate live."""

    def _open(self, fake, **kwargs):
        kwargs.setdefault("read_timeout_s", 0.5)
        return PB.PseyeCamera.open(index=0, module=fake, **kwargs)

    def test_read_returns_negotiated_frame_and_release_closes(self):
        fake = FakePseye(frame_limit=100, width=640, height=480)
        cam = self._open(fake)
        try:
            self.assertEqual((cam.width, cam.height), (640, 480))
            self.assertEqual(cam.descriptor.backend, "pseye")
            ok, frame = cam.read(timeout_s=1.0)
            self.assertTrue(ok)
            self.assertEqual(frame.shape, (480, 640, 3))
            self.assertEqual(str(frame.dtype), "uint8")
        finally:
            cam.release()
        self.assertTrue(fake.closed)
        self.assertFalse(cam.abandoned)

    def test_gray_format_has_single_channel_shape(self):
        fake = FakePseye(frame_limit=100, width=320, height=240, bpp=1)
        cam = PB.PseyeCamera.open(
            index=0, width=320, height=240, fmt="gray", module=fake,
            read_timeout_s=0.5,
        )
        try:
            ok, frame = cam.read(timeout_s=1.0)
            self.assertTrue(ok)
            self.assertEqual(frame.shape, (240, 320))
        finally:
            cam.release()

    def test_wrong_requested_resolution_is_refused(self):
        fake = FakePseye(width=320, height=240)
        with self.assertRaises(CameraError):
            PB.PseyeCamera.open(index=0, width=640, height=480, module=fake)
        self.assertTrue(fake.closed)

    def test_read_never_succeeds_for_wrong_expected_size(self):
        fake = FakePseye(frame_limit=100, width=640, height=480)
        cam = self._open(fake)
        try:
            ok, frame = cam.read(timeout_s=0.2, expected_size=(320, 240))
            self.assertFalse(ok)
            self.assertIsNone(frame)
        finally:
            cam.release()

    def test_read_times_out_without_frames(self):
        fake = FakePseye(frame_limit=0)
        cam = self._open(fake, max_consecutive_failures=3)
        try:
            ok, frame = cam.read(timeout_s=0.1)
            self.assertFalse(ok)
            self.assertIsNone(frame)
        finally:
            cam.release()

    def test_frames_are_fresh_and_never_repeated(self):
        # The reader publishes a distinct sequence byte per frame; read() must
        # never hand the same frame out twice, and must time out once the
        # producer stops.
        fake = FakePseye(frame_limit=5, fill_sequence=True)
        cam = self._open(fake, max_consecutive_failures=3)
        seen = []
        try:
            deadline = time.perf_counter() + 3.0
            while time.perf_counter() < deadline:
                ok, frame = cam.read(timeout_s=0.2)
                if not ok:
                    break
                seen.append(int(frame[0, 0, 0]))
            self.assertGreaterEqual(len(seen), 1)
            self.assertEqual(len(set(seen)), len(seen))
            self.assertLessEqual(max(seen), 5)
            # Producer is done: a further read must fail rather than repeat.
            ok, frame = cam.read(timeout_s=0.3)
            self.assertFalse(ok)
            self.assertIsNone(frame)
        finally:
            cam.release()

    def test_short_native_frame_is_never_published(self):
        fake = FakePseye(frame_limit=100, short_write=True)
        cam = self._open(fake, max_consecutive_failures=3)
        try:
            ok, frame = cam.read(timeout_s=0.2)
            self.assertFalse(ok)
            self.assertIsNone(frame)
        finally:
            cam.release()

    def test_release_is_bounded_and_does_not_abandon(self):
        # A native grab that only returns on abort() must not force release()
        # to leak the handle: abort wakes it and the camera is closed.
        fake = FakePseye(block_until_abort=True)
        cam = self._open(fake, read_timeout_s=0.2, grab_timeout_s=0.1)
        started = time.perf_counter()
        try:
            ok, _ = cam.read(timeout_s=0.1)
            self.assertFalse(ok)
        finally:
            cam.release()
        self.assertLess(time.perf_counter() - started, 3.0)
        self.assertFalse(cam.abandoned)
        self.assertGreaterEqual(fake.aborted, 1)
        self.assertIn(1000, fake.closed)

    def test_unopened_index_raises(self):
        fake = FakePseye(count=1, open_indices=set())
        with self.assertRaises(CameraError):
            PB.PseyeCamera.open(index=3, module=fake)

    def test_open_camera_source_dispatches_to_native(self):
        fake = FakePseye(frame_limit=100)
        source, descriptor = open_camera_source(
            0, backend="pseye", width=640, height=480, fps=60, module=fake
        )
        try:
            self.assertEqual(descriptor.backend, "pseye")
            self.assertEqual((source.width, source.height), (640, 480))
        finally:
            source.release()

    def test_open_camera_source_is_case_insensitive(self):
        fake = FakePseye(frame_limit=100)
        source, _ = open_camera_source(
            0, backend="PSEye", width=640, height=480, module=fake
        )
        source.release()


class CliTest(unittest.TestCase):
    def _run_cli(self, *args, env_extra=None):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(TOOLS) + os.pathsep + env.get("PYTHONPATH", "")
        # Force the native backend to look at a missing DLL: deterministic and
        # hardware-free.
        env["PSVR_PSEYE_DLL"] = str(Path(ROOT) / "does-not-exist" / "psvr_pseye.dll")
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [sys.executable, "-B", "-m", "psvr_camera", *args],
            capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT), env=env,
            timeout=120,
        )

    def test_pseye_requires_explicit_index(self):
        proc = self._run_cli("pseye", "probe", "--frames", "1")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--index", proc.stderr)

    @unittest.skipUnless(sys.platform == "win32", "Windows-only backend")
    def test_pseye_inventory_reports_missing_dll(self):
        import json

        proc = self._run_cli("pseye", "inventory")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        report = json.loads(proc.stdout)
        self.assertFalse(report["available"])
        self.assertEqual(report["backend"], "pseye")


@unittest.skipUnless(
    os.environ.get("PSVR_PSEYE_HARDWARE") == "1",
    "set PSVR_PSEYE_HARDWARE=1 to exercise a real PS3 Eye",
)
class HardwareTest(unittest.TestCase):
    def test_real_capture(self):
        report = PB.capture_frames_pseye(
            index=0, width=640, height=480, fps=60, frames=60, max_seconds=5.0
        ).to_dict()
        self.assertTrue(report["opened"], report["errors"])
        self.assertGreater(report["frames_captured"], 0)
        self.assertGreater(report["achieved_fps"], 0.0)

    def test_real_negotiated_fps_is_the_actual_rate(self):
        # 640x480 @ 45 is snapped by the sensor to 40; the report must state the
        # actual negotiated rate, not the requested one.
        report = PB.capture_frames_pseye(
            index=0, width=640, height=480, fps=45, frames=5, max_seconds=5.0,
            warmup_frames=0,
        ).to_dict()
        self.assertTrue(report["opened"], report["errors"])
        self.assertEqual(report["requested"]["fps"], 45)
        self.assertEqual(report["negotiated"]["fps"], 40)

    def test_real_probe_is_bounded_by_max_seconds(self):
        report = PB.capture_frames_pseye(
            index=0, width=640, height=480, fps=60, frames=100000,
            max_seconds=0.5, warmup_frames=0,
        ).to_dict()
        self.assertTrue(report["opened"], report["errors"])
        self.assertLess(report["duration_s"], 2.0)


if __name__ == "__main__":
    unittest.main()
