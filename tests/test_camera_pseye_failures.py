"""Failure paths on the real native-camera adapter; DLL functions are fake."""
import unittest
import threading
import time
from unittest import mock
from test_camera_pseye import PB, FakePseye, CameraError


class OpenCleanupTest(unittest.TestCase):
    def test_thread_start_failure_closes_once(self):
        fake = FakePseye()
        with mock.patch.object(PB.threading.Thread, "start", side_effect=RuntimeError("cannot start new thread")):
            with self.assertRaisesRegex(CameraError, "cannot start new thread"):
                PB.PseyeCamera.open(0, module=fake)
        self.assertEqual(fake.closed, [1000])

    def test_buffer_allocation_failure_closes_once(self):
        fake = FakePseye()
        with mock.patch.object(PB.ctypes, "create_string_buffer", side_effect=MemoryError("buffer allocation")):
            with self.assertRaisesRegex(CameraError, "buffer allocation"):
                PB.PseyeCamera.open(0, module=fake)
        self.assertEqual(fake.closed, [1000])

    def test_close_failure_does_not_mask_original_error(self):
        fake = FakePseye()
        fake.psvr_pseye_close = mock.Mock(side_effect=OSError("close failure"))
        with mock.patch.object(PB.threading.Thread, "start", side_effect=RuntimeError("start failure")):
            with self.assertRaisesRegex(CameraError, "start failure") as caught:
                PB.PseyeCamera.open(0, module=fake)
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        fake.psvr_pseye_close.assert_called_once_with(1000)

    def test_invalid_waits_rejected_before_native_open(self):
        for value in (float("nan"), float("inf"), -1, "invalid"):
            with self.subTest(value=value):
                fake = FakePseye()
                fake.psvr_pseye_open = mock.Mock()
                with self.assertRaises(CameraError):
                    PB.PseyeCamera.open(0, module=fake, read_timeout_s=value)
                fake.psvr_pseye_open.assert_not_called()


class ReaderFailureTest(unittest.TestCase):
    def test_grab_exception_is_terminal_and_wakes_reader(self):
        class Raises(FakePseye):
            def psvr_pseye_grab_timeout(self, *args):
                raise OSError("native fault")
        fake = Raises()
        cam = PB.PseyeCamera.open(0, module=fake)
        try:
            cam._thread.join(1)
            self.assertFalse(cam._thread.is_alive())
            self.assertTrue(cam.failed)
            self.assertIn("OSError: native fault", cam.error)
            start = time.monotonic()
            self.assertEqual(cam.read(timeout_s=2), (False, None))
            self.assertLess(time.monotonic()-start, .5)
        finally: cam.release()
        self.assertEqual(fake.closed, [1000])

    def test_conversion_exception_also_surfaces(self):
        fake = FakePseye()
        with mock.patch.object(PB.np, "frombuffer", side_effect=ValueError("bad frame")):
            cam = PB.PseyeCamera.open(0, module=fake)
            try:
                cam._thread.join(1)
                self.assertTrue(cam.failed)
                self.assertIn("bad frame", cam.error)
            finally: cam.release()

    def test_release_wakes_waiting_consumer(self):
        fake = FakePseye(block_until_abort=True)
        cam = PB.PseyeCamera.open(0, module=fake)
        result = []
        waiter = threading.Thread(target=lambda: result.append(cam.read(timeout_s=5)))
        waiter.start()
        cam.release()
        waiter.join(1)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(result, [(False, None)])

    def test_transient_native_failures_still_allow_a_new_frame(self):
        delivered = threading.Event()
        class Transient(FakePseye):
            def psvr_pseye_grab_timeout(self, cam, buf, size, timeout):
                self.grabs += 1
                if self.grabs <= 2: return -1
                if self.grabs == 3:
                    delivered.set()
                    return size
                self.abort_event.wait(1)
                return -1
        fake = Transient()
        cam = PB.PseyeCamera.open(0, module=fake, max_consecutive_failures=3)
        try:
            self.assertTrue(delivered.wait(1))
            self.assertTrue(cam.read(timeout_s=1)[0])
            self.assertFalse(cam.failed)
        finally: cam.release()
