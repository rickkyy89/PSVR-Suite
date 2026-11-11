"""Native PS3 Eye capture backend (Windows, libusb-win32 binding).

This is an additive backend next to :mod:`camera_backend` (OpenCV). It does not
touch the OpenCV path, does not rebind or install any USB driver, and does not
run as administrator. It talks to the camera through a separately built
``psvr_pseye.dll`` (MIT shim) that dynamically loads the upstream PS3EYEDriver
``ps3eye.dll``; the device must already be bound to libusb-win32, which is the
operator's existing setup.

``ps3eye.dll`` is built from the pinned upstream plus the in-repo bounded-grab
patch, so every native grab has a real timeout. The shim source is MIT, but a
distribution containing both DLLs is a combined distribution that must satisfy
the GPLv2 for ``ps3eye.dll``; the stage ships the full licence texts and
``NOTICE.txt``.

The library is imported lazily and the ctypes function table can be injected
with ``module=`` for hardware-free tests, mirroring the fake-``cv2`` pattern in
:mod:`camera_backend`.
"""

from __future__ import annotations

import ctypes
from datetime import datetime, timezone
import os
import math
from pathlib import Path
import sys
import threading
import time
from typing import Optional

import numpy as np

from .camera_backend import CameraDescriptor, CameraError
from .capture import CaptureReport

BACKEND_NAME = "pseye"

FORMATS = {
    "bayer": 0,
    "bgr": 1,
    "rgb": 2,
    "gray": 3,
}

_DLL_NAME = "psvr_pseye.dll"
_UPSTREAM_DLL_NAME = "ps3eye.dll"
_EXPECTED_ABI = 2
_DEFAULT_GRAB_TIMEOUT_MS = 1000


class PseyeUnavailable(CameraError):
    """The native PS3 Eye backend could not be loaded."""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def candidate_dll_paths() -> list:
    """Ordered list of places ``psvr_pseye.dll`` may live.

    ``PSVR_PSEYE_DLL`` is handled separately by :func:`find_dll` and is
    authoritative when set.
    """
    paths = []
    for base in (
        Path(os.environ["PSVR_PSEYE_DIR"]) if os.environ.get("PSVR_PSEYE_DIR") else None,
        _repo_root() / "out" / "pseye",
        Path(__file__).resolve().parents[1] / "psvr_pseye" / "build",
        _repo_root() / "build" / "pseye",
    ):
        if base is not None:
            paths.append(Path(base) / _DLL_NAME)
    return paths


def find_dll() -> Optional[Path]:
    """Locate ``psvr_pseye.dll``.

    If ``PSVR_PSEYE_DLL`` is set it is returned as-is (authoritative), even if
    it does not exist, so a typo is reported rather than silently ignored.
    """
    explicit = os.environ.get("PSVR_PSEYE_DLL")
    if explicit:
        return Path(explicit)
    for candidate in candidate_dll_paths():
        if candidate.is_file():
            return candidate
    return None


def _configure(module) -> None:
    """Set ctypes signatures on a real loaded DLL."""
    c = ctypes
    module.psvr_pseye_abi_version.restype = c.c_int
    module.psvr_pseye_abi_version.argtypes = []
    module.psvr_pseye_load.argtypes = [c.c_char_p, c.c_char_p, c.c_int]
    module.psvr_pseye_load.restype = c.c_int
    module.psvr_pseye_count.restype = c.c_int
    module.psvr_pseye_count.argtypes = []
    module.psvr_pseye_open.argtypes = [c.c_int, c.c_int, c.c_int, c.c_int, c.c_int]
    module.psvr_pseye_open.restype = c.c_void_p
    module.psvr_pseye_close.argtypes = [c.c_void_p]
    module.psvr_pseye_close.restype = None
    module.psvr_pseye_grab.argtypes = [c.c_void_p, c.c_void_p, c.c_int]
    module.psvr_pseye_grab.restype = c.c_int
    if hasattr(module, "psvr_pseye_grab_timeout"):
        module.psvr_pseye_grab_timeout.argtypes = [
            c.c_void_p, c.c_void_p, c.c_int, c.c_int
        ]
        module.psvr_pseye_grab_timeout.restype = c.c_int
    if hasattr(module, "psvr_pseye_abort"):
        module.psvr_pseye_abort.argtypes = [c.c_void_p]
        module.psvr_pseye_abort.restype = None
    if hasattr(module, "psvr_pseye_set_grab_timeout"):
        module.psvr_pseye_set_grab_timeout.argtypes = [c.c_void_p, c.c_int]
        module.psvr_pseye_set_grab_timeout.restype = None
    module.psvr_pseye_width.argtypes = [c.c_void_p]
    module.psvr_pseye_width.restype = c.c_int
    module.psvr_pseye_height.argtypes = [c.c_void_p]
    module.psvr_pseye_height.restype = c.c_int
    module.psvr_pseye_fps.argtypes = [c.c_void_p]
    module.psvr_pseye_fps.restype = c.c_int
    module.psvr_pseye_bytes_per_pixel.argtypes = [c.c_void_p]
    module.psvr_pseye_bytes_per_pixel.restype = c.c_int
    if hasattr(module, "psvr_pseye_set_parameter"):
        module.psvr_pseye_set_parameter.argtypes = [c.c_void_p,c.c_int,c.c_int]
        module.psvr_pseye_set_parameter.restype = c.c_int
    if hasattr(module, "psvr_pseye_get_parameter"):
        module.psvr_pseye_get_parameter.argtypes = [c.c_void_p,c.c_int]
        module.psvr_pseye_get_parameter.restype = c.c_int
    module.psvr_pseye_unique_id.argtypes = [c.c_void_p, c.c_char_p, c.c_int]
    module.psvr_pseye_unique_id.restype = c.c_int
    for name in (
        "psvr_pseye_upstream_id",
        "psvr_pseye_license_note",
        "psvr_pseye_libusb_note",
    ):
        getattr(module, name).restype = c.c_char_p
        getattr(module, name).argtypes = []


def _text(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


class PseyeLibrary:
    """Thin typed wrapper around ``psvr_pseye.dll``."""

    def __init__(self, module, dll_path=None):
        self._m = module
        self.dll_path = str(dll_path) if dll_path else None

    @classmethod
    def load(cls, dll_path=None, module=None):
        if module is not None:
            return cls(module, dll_path=dll_path)
        if sys.platform != "win32":
            raise PseyeUnavailable("the PS3 Eye native backend is Windows-only")
        path = Path(dll_path) if dll_path else find_dll()
        if path is None:
            raise PseyeUnavailable(
                "psvr_pseye.dll was not found. Build it with "
                "tools/psvr_pseye/build.ps1, or set PSVR_PSEYE_DLL to the DLL."
            )
        if not path.is_file():
            raise PseyeUnavailable(f"psvr_pseye.dll not found at {path}")
        try:
            raw = ctypes.WinDLL(str(path))
        except OSError as exc:
            raise PseyeUnavailable(f"cannot load {path}: {exc}") from exc
        _configure(raw)
        reason = ctypes.create_string_buffer(512)
        directory = str(path.parent).encode("utf-8")
        result = raw.psvr_pseye_load(directory, reason, len(reason))
        if result != 0:
            raise PseyeUnavailable(
                _text(reason.value)
                or f"psvr_pseye_load failed with code {result}"
            )
        abi = int(raw.psvr_pseye_abi_version())
        if abi != _EXPECTED_ABI:
            raise PseyeUnavailable(
                f"psvr_pseye.dll ABI {abi} is not supported "
                f"(expected {_EXPECTED_ABI})"
            )
        return cls(raw, dll_path=path)

    def abi_version(self) -> int:
        return int(self._m.psvr_pseye_abi_version())

    def count(self) -> int:
        return int(self._m.psvr_pseye_count())

    def open(self, index: int, width: int, height: int, fps: int, fmt: int):
        return self._m.psvr_pseye_open(index, width, height, fps, fmt)

    def close(self, cam) -> None:
        self._m.psvr_pseye_close(cam)

    def grab(self, cam, buffer, size: int,
             timeout_ms: Optional[int] = None) -> int:
        """Bounded native grab.

        Uses the patched ``psvr_pseye_grab_timeout`` export when present so the
        wait is always bounded; falls back to the shim default otherwise.
        Returns bytes written, or -1 on timeout/error.
        """
        fn = getattr(self._m, "psvr_pseye_grab_timeout", None)
        if fn is not None:
            if timeout_ms is None:
                timeout_ms = _DEFAULT_GRAB_TIMEOUT_MS
            return int(fn(cam, buffer, size, int(timeout_ms)))
        return int(self._m.psvr_pseye_grab(cam, buffer, size))

    def set_grab_timeout(self, cam, timeout_ms: int) -> None:
        fn = getattr(self._m, "psvr_pseye_set_grab_timeout", None)
        if fn is not None:
            fn(cam, int(timeout_ms))

    def abort(self, cam) -> None:
        """Wake a grab blocked inside the DLL so it returns promptly."""
        fn = getattr(self._m, "psvr_pseye_abort", None)
        if fn is not None:
            fn(cam)

    def manual_controls(self, cam, exposure, gain):
        if not 0 <= exposure <= 255 or not 0 <= gain <= 63:
            raise CameraError("exposure must be 0..255; gain must be 0..63")
        setter = getattr(self._m,"psvr_pseye_set_parameter",None)
        getter = getattr(self._m,"psvr_pseye_get_parameter",None)
        if setter is None or getter is None:
            raise CameraError("PS Eye shim lacks manual controls; rebuild psvr_pseye.dll")
        requested = ((0,0),(2,0),(1,gain),(3,exposure))
        for parameter,value in requested:
            if setter(cam,parameter,value) != 0:
                raise CameraError("PS Eye rejected manual control %d" % parameter)
        actual = {"auto_gain":int(getter(cam,0)),"auto_white_balance":int(getter(cam,2)),
                  "gain":int(getter(cam,1)),"exposure":int(getter(cam,3))}
        if actual != {"auto_gain":0,"auto_white_balance":0,"gain":gain,"exposure":exposure}:
            raise CameraError("PS Eye control readback differs from requested settings")
        return actual

    def width(self, cam) -> int:
        return int(self._m.psvr_pseye_width(cam))

    def height(self, cam) -> int:
        return int(self._m.psvr_pseye_height(cam))

    def fps(self, cam) -> int:
        return int(self._m.psvr_pseye_fps(cam))

    def bytes_per_pixel(self, cam) -> int:
        return int(self._m.psvr_pseye_bytes_per_pixel(cam))

    def unique_id(self, cam) -> Optional[str]:
        buf = ctypes.create_string_buffer(256)
        if int(self._m.psvr_pseye_unique_id(cam, buf, len(buf))) == 0:
            return _text(buf.value)
        return None

    def info(self) -> dict:
        return {
            "backend": BACKEND_NAME,
            "dll": self.dll_path,
            "abi_version": self.abi_version(),
            "upstream": _text(self._m.psvr_pseye_upstream_id()),
            "license": _text(self._m.psvr_pseye_license_note()),
            "libusb": _text(self._m.psvr_pseye_libusb_note()),
        }


def _library(library=None, dll_path=None, module=None) -> PseyeLibrary:
    if library is not None:
        return library
    return PseyeLibrary.load(dll_path=dll_path, module=module)


def inventory(library=None, dll_path=None, module=None) -> dict:
    """Read-only backend inventory: DLL provenance and connected count."""
    try:
        lib = _library(library, dll_path, module)
    except PseyeUnavailable as exc:
        return {
            "schema": "psvr.camera.pseye_inventory",
            "version": 1,
            "backend": BACKEND_NAME,
            "available": False,
            "error": str(exc),
            "count": None,
        }
    count = lib.count()
    report = lib.info()
    report.update({
        "schema": "psvr.camera.pseye_inventory",
        "version": 1,
        "available": True,
        "count": count,
        "note": "read-only; opening a camera is an explicit --index action",
        "refresh_policy": "refresh when no native handles are open; otherwise preserve active-index snapshot",
    })
    return report


def capture_frames_pseye(
    index: int,
    width: int = 640,
    height: int = 480,
    fps: int = 60,
    fmt: str = "bgr",
    frames: int = 120,
    max_seconds: float = 3.0,
    warmup_frames: int = 2,
    library=None,
    dll_path=None,
    module=None,
) -> CaptureReport:
    """Bounded native capture of one explicit PS Eye index.

    Both bounds are enforced. Each native grab is bounded by the smaller of the
    remaining ``max_seconds`` budget and the shim's grab timeout, so an
    unplugged or stalled device returns instead of blocking forever. The report
    always states the real duration, the negotiated (actual) fps and achieved
    fps.
    """
    if type(index) is not int or index < 0:
        raise CameraError("camera index must be a non-negative integer")
    if type(frames) is not int or frames < 1:
        raise CameraError("frames must be a positive integer")
    if max_seconds <= 0:
        raise CameraError("max_seconds must be positive")
    key = str(fmt).lower()
    if key not in FORMATS:
        raise CameraError(
            f"unknown format {fmt!r}; known: {', '.join(sorted(FORMATS))}"
        )

    report = CaptureReport(
        index=index,
        backend=BACKEND_NAME,
        backend_id=None,
        requested={
            "width": width,
            "height": height,
            "fps": fps,
            "format": key,
            "frames": frames,
            "max_seconds": max_seconds,
        },
        opened=False,
        frames_requested=frames,
    )
    opened_at = time.perf_counter()
    loop_started = None
    cam = None
    try:
        lib = _library(library, dll_path, module)
        report.backend_id = lib.abi_version()
        cam = lib.open(index, width, height, fps, FORMATS[key])
        if not cam:
            report.errors.append(
                f"pseye camera index {index} did not open "
                f"(connected={lib.count()})"
            )
            return report
        report.opened = True
        neg_w = lib.width(cam)
        neg_h = lib.height(cam)
        bpp = lib.bytes_per_pixel(cam)
        report.negotiated = {
            "width": neg_w,
            "height": neg_h,
            "fps": lib.fps(cam),
            "fourcc": None,
            "format": key,
            "bytes_per_pixel": bpp,
        }
        needed = neg_w * neg_h * bpp
        if needed <= 0:
            report.errors.append("pseye reported an empty frame geometry")
            return report
        buffer = ctypes.create_string_buffer(needed)

        def _grab_timeout_ms() -> int:
            # Never wait past the max_seconds budget: the native grab itself is
            # bounded so an unplugged device cannot block the caller.
            remaining = max_seconds - (time.perf_counter() - opened_at)
            if remaining <= 0:
                return 0
            return max(1, min(_DEFAULT_GRAB_TIMEOUT_MS, int(remaining * 1000)))

        for _ in range(max(int(warmup_frames), 0)):
            timeout_ms = _grab_timeout_ms()
            if timeout_ms <= 0:
                break
            if lib.grab(cam, buffer, needed, timeout_ms) != needed:
                report.timeouts += 1
                break

        # Measure only the steady-state capture loop: open/init/warmup happen
        # before this point and would otherwise deflate the achieved fps.
        loop_started = time.perf_counter()
        while report.frames_captured < frames:
            timeout_ms = _grab_timeout_ms()
            if timeout_ms <= 0:
                break
            written = lib.grab(cam, buffer, needed, timeout_ms)
            if written != needed:
                # Timeout, abort or a short frame: never count a partial buffer.
                report.timeouts += 1
                report.frames_dropped += 1
                if report.timeouts >= max(5, frames):
                    report.errors.append("capture aborted after repeated grab errors")
                    break
                continue
            report.frames_captured += 1
    except PseyeUnavailable as exc:
        report.errors.append(str(exc))
    except CameraError as exc:
        report.errors.append(str(exc))
    except Exception as exc:  # noqa: BLE001 - report, never crash the CLI
        report.errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        if cam is not None:
            try:
                lib.close(cam)
            except Exception:  # pragma: no cover - best effort
                pass
        # duration_s is the steady-state capture loop when it ran, otherwise
        # the whole open attempt; ended_utc always closes the report.
        reference = loop_started if loop_started is not None else opened_at
        report.duration_s = max(time.perf_counter() - reference, 0.0)
        report.ended_utc = datetime.now(timezone.utc).isoformat()
    return report


# --------------------------------------------------------------------------- #
# OpenCV-compatible frame source over the native backend
# --------------------------------------------------------------------------- #

def _frame_shape(width: int, height: int, bytes_per_pixel: int) -> tuple:
    if int(bytes_per_pixel) == 3:
        return (int(height), int(width), 3)
    return (int(height), int(width))


class PseyeCamera:
    """``read()``/``release()`` adapter over one open native PS3 Eye.

    This lets ``track`` and ``calibrate live`` use the real PS Eye exactly like
    an OpenCV ``VideoCapture``: ``read()`` returns ``(ok, frame)`` and
    ``release()`` closes the device. The frame is a NumPy array shaped from the
    *negotiated* mode, so a caller can never mistake a frame for another
    resolution.

    The native grab is bounded (the patched shim always has a timeout), so it
    runs on a daemon reader thread while ``read()`` waits with its own bounded
    timeout (``read_timeout_s``). A stalled device therefore surfaces as read
    failures instead of hanging the tracking loop. ``release()`` calls the
    shim's abort hook to wake any in-flight grab and then joins the reader, so
    the handle is closed normally rather than abandoned.

    Frames are published with a monotonically increasing sequence number. A
    ``read()`` only returns a frame whose sequence is newer than the last one
    it returned, so the same frame can never be handed out twice (which would
    otherwise let a stale pose be treated as fresh).
    """

    def __init__(
        self,
        library: PseyeLibrary,
        cam,
        index: int,
        requested: dict,
        negotiated: dict,
        fmt: str,
        buffer,
        read_timeout_s: float = 2.0,
        max_consecutive_failures: int = 30,
        grab_timeout_s: float = 0.5,
    ) -> None:
        self._lib = library
        self._cam = cam
        self.index = int(index)
        self.format = str(fmt)
        self.requested = dict(requested)
        self.negotiated = dict(negotiated)
        self._bpp = int(negotiated["bytes_per_pixel"])
        self._needed = int(negotiated["width"]) * int(negotiated["height"]) * self._bpp
        self._shape = _frame_shape(
            negotiated["width"], negotiated["height"], self._bpp
        )
        self._buffer = buffer
        self._read_timeout_s = max(float(read_timeout_s), 0.0)
        self._max_consecutive_failures = max(int(max_consecutive_failures), 1)
        self._grab_timeout_ms = max(int(float(grab_timeout_s) * 1000), 1)
        self._lock = threading.Lock()
        self._controls_requested = None
        self._controls_applied = None
        self._controls_revision = self._applied_revision = 0
        self._cleanup_complete = threading.Event()
        self._frame = None
        self._frame_monotonic = None
        self.last_frame_monotonic = None
        self._seq = 0
        self._read_seq = 0
        self._frame_event = threading.Event()
        self._stop = threading.Event()
        self._released = False
        self._failures = 0
        self._total_failures = 0
        self._error: Optional[str] = None
        self._abandoned = False
        self.descriptor = CameraDescriptor(
            index=self.index,
            backend=BACKEND_NAME,
            backend_id=library.abi_version(),
            opened=True,
            readable=True,
            width=float(negotiated["width"]),
            height=float(negotiated["height"]),
            fps=(float(negotiated["fps"]) if negotiated.get("fps") else None),
            fourcc=None,
        )
        self._thread = threading.Thread(
            target=self._run, name="psvr-pseye-reader", daemon=True
        )
        self._thread.start()

    @classmethod
    def open(
        cls,
        index: int,
        width: Optional[int] = None,
        height: Optional[int] = None,
        fps: Optional[int] = None,
        fmt: str = "bgr",
        library: Optional[PseyeLibrary] = None,
        dll_path=None,
        module=None,
        read_timeout_s: float = 2.0,
        max_consecutive_failures: int = 30,
        grab_timeout_s: float = 0.5,
    ) -> "PseyeCamera":
        """Open one explicit index. Refuses a mode the shim snapped elsewhere.

        The shim resolves the mode to exactly 640x480 or 320x240. If the caller
        asked for a different size, the device would silently deliver another
        resolution, so opening fails closed instead of returning a frame whose
        real size contradicts the requested one.
        """
        if type(index) is not int or index < 0:
            raise CameraError("camera index must be a non-negative integer")
        key = str(fmt).lower()
        if key not in FORMATS:
            raise CameraError(
                f"unknown format {fmt!r}; known: {', '.join(sorted(FORMATS))}"
            )
        request_width = 640 if width is None else int(width)
        request_height = 480 if height is None else int(height)
        request_fps = 60 if fps is None else int(fps)
        if request_width <= 0 or request_height <= 0:
            raise CameraError("width and height must be positive integers")
        if request_fps <= 0:
            raise CameraError("fps must be a positive integer")
        try:
            read_timeout_s, grab_timeout_s = float(read_timeout_s), float(grab_timeout_s)
            max_consecutive_failures = max(int(max_consecutive_failures), 1)
            if not all(math.isfinite(t) and t >= 0 for t in (read_timeout_s, grab_timeout_s)):
                raise ValueError("non-finite or negative timeout")
        except (TypeError, ValueError, OverflowError) as exc:
            raise CameraError(f"invalid capture wait configuration: {exc}") from exc

        lib = _library(library, dll_path, module)
        cam = lib.open(index, request_width, request_height, request_fps, FORMATS[key])
        if not cam:
            raise CameraError(
                f"pseye camera index {index} did not open (connected={lib.count()})"
            )
        try:
            neg_w = int(lib.width(cam))
            neg_h = int(lib.height(cam))
            bpp = int(lib.bytes_per_pixel(cam))
            neg_fps = int(lib.fps(cam))
            if neg_w <= 0 or neg_h <= 0 or bpp <= 0:
                raise CameraError("pseye reported an empty frame geometry")
            if (request_width, request_height) != (neg_w, neg_h):
                raise CameraError(
                    f"pseye requested {request_width}x{request_height} but the "
                    f"device negotiated {neg_w}x{neg_h}; refusing to capture a "
                    "different resolution (use 640x480 or 320x240)"
                )
            needed = neg_w * neg_h * bpp
            negotiated = {
                "width": neg_w, "height": neg_h, "fps": neg_fps,
                "fourcc": None, "format": key, "bytes_per_pixel": bpp,
            }
            buffer = ctypes.create_string_buffer(needed)
            return cls(lib, cam, index,
                {"width": request_width, "height": request_height,
                 "fps": request_fps, "format": key},
                negotiated, key, buffer, read_timeout_s=read_timeout_s,
                max_consecutive_failures=max_consecutive_failures,
                grab_timeout_s=grab_timeout_s)
        except Exception as exc:  # ownership transfers only after construction
            try:
                lib.close(cam)
            except Exception:
                pass  # preserve the original open/construction failure
            if isinstance(exc, CameraError):
                raise
            raise CameraError(f"pseye open failed: {exc}") from exc

    @property
    def width(self) -> int:
        return int(self.negotiated["width"])

    @property
    def height(self) -> int:
        return int(self.negotiated["height"])

    @property
    def fps(self) -> int:
        return int(self.negotiated["fps"])

    @property
    def failed(self) -> bool:
        return self._error is not None

    @property
    def error(self) -> Optional[str]:
        return self._error

    @property
    def abandoned(self) -> bool:
        """True when release() had to leak the handle to avoid a use-after-free."""
        return self._abandoned

    @property
    def cleanup_pending(self) -> bool:
        return self._released and not self._cleanup_complete.is_set()

    def _run(self) -> None:
        try:
            self._read_loop()
        except Exception as exc:
            with self._lock:
                self._error = f"native reader raised {type(exc).__name__}: {exc}"
        finally:
            self._stop.set()
            self._frame_event.set()

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                requested = self._controls_requested
                self._controls_requested = None
                revision = self._controls_revision
            if requested is not None:
                try:
                    applied = self._lib.manual_controls(self._cam,*requested)
                    with self._lock:
                        self._applied_revision = revision
                        if revision == self._controls_revision and not self._released:
                            self._controls_applied = applied
                except Exception as exc:
                    self._error = str(exc)
                    self._stop.set()
                    self._frame_event.set()
                    break
            written = self._lib.grab(
                self._cam, self._buffer, self._needed, self._grab_timeout_ms
            )
            if self._stop.is_set():
                break
            if written != self._needed:
                # Timeout, abort or a short frame: never publish a partial
                # buffer as if it were a complete frame.
                self._failures += 1
                self._total_failures += 1
                if self._failures >= self._max_consecutive_failures:
                    self._error = (
                        f"native grab failed {self._failures} times in a row"
                    )
                    self._stop.set()
                    break
                # Never busy-spin a failing device.
                time.sleep(0.01)
                continue
            self._failures = 0
            frame = (
                np.frombuffer(self._buffer, dtype=np.uint8)
                .reshape(self._shape)
                .copy()
            )
            with self._lock:
                if self._released or self._applied_revision != self._controls_revision:
                    continue
                self._frame = frame
                self._frame_monotonic = time.perf_counter()
                self._seq += 1
                # Set the event under the lock so it cannot race the consumer's
                # clear-then-read and hand out the same frame twice.
                self._frame_event.set()

    def set_manual_controls(self, exposure, gain):
        if type(exposure) is not int or type(gain) is not int or not 0 <= exposure <= 255 or not 0 <= gain <= 63:
            raise CameraError("exposure must be integer 0..255; gain integer 0..63")
        with self._lock:
            if self._released or self._stop.is_set() or self._error is not None:
                raise CameraError("PS3 Eye owner is not active")
            self._controls_requested = (exposure,gain)
            self._controls_revision += 1
            self._controls_applied = None
            self._frame = None
            self._read_seq = self._seq
            self._frame_event.clear()

    def controls_status(self):
        with self._lock:
            return (None if self._released or self._controls_applied is None
                    else dict(self._controls_applied))

    def read(self, timeout_s: Optional[float] = None, expected_size=None):
        """Wait up to ``timeout_s`` for the next *new* frame.

        Returns ``(ok, frame)``. A frame already returned by a previous call is
        never returned again: the publisher increments a sequence number and a
        reader only accepts a strictly newer one, so a stale event cannot make
        the caller treat an old frame as fresh.

        When ``expected_size`` is given, a frame is only returned if the
        negotiated resolution matches it; a wrong-resolution stream can never
        report success.
        """
        if self._released or self._cam is None:
            return False, None
        if expected_size is not None:
            expected = (int(expected_size[0]), int(expected_size[1]))
            if (self.width, self.height) != expected:
                return False, None
        timeout = self._read_timeout_s if timeout_s is None else float(timeout_s)
        if timeout <= 0.0:
            return False, None
        deadline = time.perf_counter() + timeout
        while True:
            if self._released or self._error is not None:
                return False, None
            remaining = deadline - time.perf_counter()
            if remaining <= 0.0:
                return False, None
            if not self._frame_event.wait(remaining):
                return False, None
            with self._lock:
                if self._released or self._error is not None:
                    return False, None
                if self._seq == self._read_seq:
                    # Event was stale (already-consumed frame); drop it and
                    # keep waiting for a genuinely new one.
                    self._frame_event.clear()
                    continue
                self._read_seq = self._seq
                frame = self._frame
                self.last_frame_monotonic = self._frame_monotonic
                self._frame_event.clear()
            if frame is None:
                return False, None
            return True, frame

    def release(self) -> bool:
        with self._lock:
            if self._released:
                return self._cleanup_complete.is_set() and not self._abandoned
            self._released = True
            self._controls_applied = None
            self._controls_requested = None
        self._stop.set()
        self._frame_event.set()
        cam = self._cam
        if cam is not None:
            # Wake a grab blocked inside the DLL so the reader returns promptly
            # and the handle can be closed instead of abandoned.
            try:
                self._lib.abort(cam)
            except Exception:  # pragma: no cover - best effort
                pass
        self._thread.join(timeout=2.0)
        if self._thread.is_alive():
            # Still inside the DLL despite the abort: leaking is the only safe
            # option (the daemon thread cannot block interpreter exit).
            self._abandoned = True
            return False
        self._cam = None
        if cam is not None:
            try:
                self._lib.close(cam)
            except Exception:  # pragma: no cover - native close failure
                self._abandoned = True
        self._cleanup_complete.set()
        return not self._abandoned

    def __enter__(self) -> "PseyeCamera":
        return self

    def __exit__(self, *exc) -> None:
        self.release()
