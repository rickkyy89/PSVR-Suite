"""Optional OpenCV camera inventory and explicit device opening.

OpenCV is imported lazily. If it is missing the module raises
:class:`OpenCvUnavailable` with an actionable message; it never installs
anything. Tests inject a fake ``cv2`` module through ``import_cv2``'s module
argument so the selection and reporting logic is covered without a camera.

There is deliberately no "first working camera" helper: the inventory lists
every probed index, and opening/probing requires an explicit index and backend.

:class:`LatestFrameCapture` is the bounded-latency adapter for live use.
OpenCV backends keep an internal frame queue (MSMF grabber list, DirectShow
allocator buffers, V4L2 ring), so a consumer slower than the camera would
otherwise process an ever older backlog for the whole session. The adapter
drains that queue on a reader thread and keeps only the newest frame, giving
the OpenCV path the same single-slot contract as the native PS3 Eye backend.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
import math
import queue
from typing import Optional

_BACKEND_ALIASES = {
    "any": "CAP_ANY",
    "dshow": "CAP_DSHOW",
    "msmf": "CAP_MSMF",
    "v4l2": "CAP_V4L2",
    "gstreamer": "CAP_GSTREAMER",
    "ffmpeg": "CAP_FFMPEG",
    "vfw": "CAP_VFW",
    "mjpg": "CAP_MJPG",
}


class CameraError(RuntimeError):
    pass


class OpenCvUnavailable(CameraError):
    pass


class UnknownBackend(CameraError):
    pass


def import_cv2(module=None):
    """Return the OpenCV module or raise :class:`OpenCvUnavailable`.

    ``module`` is an injection point for tests; production callers omit it.
    """
    if module is not None:
        return module
    try:
        import cv2  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise OpenCvUnavailable(
            "OpenCV (cv2) is not installed in this Python environment. "
            "Do not install it globally; the parent environment owns camera "
            "dependencies. Camera features stay disabled until cv2 is present."
        ) from exc
    return cv2


def cv2_available() -> bool:
    try:
        import_cv2()
        return True
    except OpenCvUnavailable:
        return False


def resolve_backend(name: Optional[str], module=None) -> Optional[int]:
    """Map a backend name to a ``cv2.CAP_*`` id. ``None``/``any`` -> CAP_ANY."""
    cv2 = import_cv2(module)
    if name is None or str(name).lower() in ("any", "auto"):
        return int(getattr(cv2, "CAP_ANY", 0))
    key = str(name).lower()
    if key not in _BACKEND_ALIASES:
        raise UnknownBackend(
            f"unknown backend {name!r}; known: {', '.join(sorted(_BACKEND_ALIASES))}"
        )
    attribute = _BACKEND_ALIASES[key]
    value = getattr(cv2, attribute, None)
    if value is None:
        raise UnknownBackend(f"this OpenCV build has no {attribute}")
    return int(value)


def backend_name_from_id(backend_id, module=None) -> Optional[str]:
    cv2 = import_cv2(module)
    for alias, attribute in _BACKEND_ALIASES.items():
        if alias == "any":
            continue
        if int(getattr(cv2, attribute, -1)) == int(backend_id):
            return alias
    if int(backend_id) == int(getattr(cv2, "CAP_ANY", 0)):
        return "any"
    return None


def available_backends(module=None) -> list:
    """List backend names this OpenCV build exposes (best effort, no probing)."""
    cv2 = import_cv2(module)
    result = []
    for alias, attribute in sorted(_BACKEND_ALIASES.items()):
        if alias == "any":
            continue
        if getattr(cv2, attribute, None) is not None:
            result.append({"name": alias, "id": int(getattr(cv2, attribute))})
    return result


def _apply_timeouts(cap, open_timeout_ms: Optional[float], read_timeout_ms: Optional[float], module=None) -> None:
    cv2 = import_cv2(module)
    pairs = (
        ("CAP_PROP_OPEN_TIMEOUT_MSEC", open_timeout_ms),
        ("CAP_PROP_READ_TIMEOUT_MSEC", read_timeout_ms),
    )
    for attribute, value in pairs:
        if value is None:
            continue
        prop = getattr(cv2, attribute, None)
        if prop is None:
            continue
        try:
            cap.set(prop, float(value))
        except Exception:  # pragma: no cover - backend dependent, non-fatal
            pass


def _prefer_smallest_buffer(cap, module=None) -> bool:
    """Ask the backend for the smallest internal queue it supports.

    ``CAP_PROP_BUFFERSIZE`` is honoured by some backends (V4L2, newer MSMF) and
    ignored by others (DirectShow); it is never fatal. It only shrinks the
    driver-side queue. :class:`LatestFrameCapture` is what actually bounds the
    delivered latency for a slow consumer.
    """
    cv2 = import_cv2(module)
    prop = getattr(cv2, "CAP_PROP_BUFFERSIZE", None)
    if prop is None:
        return False
    try:
        return bool(cap.set(prop, 1.0))
    except Exception:  # pragma: no cover - backend dependent, non-fatal
        return False


def _get(cap, attribute: str, module=None, default: float = 0.0) -> float:
    cv2 = import_cv2(module)
    prop = getattr(cv2, attribute, None)
    if prop is None:
        return float(default)
    try:
        return float(cap.get(prop))
    except Exception:  # pragma: no cover - backend dependent
        return float(default)


@dataclass
class CameraDescriptor:
    index: int
    backend: Optional[str]
    backend_id: Optional[int]
    opened: bool
    readable: bool = False
    width: Optional[float] = None
    height: Optional[float] = None
    fps: Optional[float] = None
    fourcc: Optional[str] = None
    error: Optional[str] = None
    cleanup_pending: bool = False

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "backend": self.backend,
            "backend_id": self.backend_id,
            "opened": self.opened,
            "readable": self.readable,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "fourcc": self.fourcc,
            "error": self.error,
            "cleanup_pending": self.cleanup_pending,
        }


def _fourcc_to_text(value: float, module=None) -> Optional[str]:
    if not value:
        return None
    try:
        code = int(value)
    except (TypeError, ValueError):
        return None
    if code <= 0:
        return None
    chars = "".join(chr((code >> (8 * i)) & 0xFF) for i in range(4))
    return chars.strip("\x00 ") or None


def open_camera(
    index: int,
    backend: Optional[str] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    fps: Optional[float] = None,
    open_timeout_ms: float = 2000.0,
    read_timeout_ms: float = 2000.0,
    module=None,
):
    """Open one explicitly selected camera index. Returns ``(cap, descriptor)``."""
    if type(index) is not int or index < 0:
        raise CameraError("camera index must be a non-negative integer")
    cv2 = import_cv2(module)
    backend_id = resolve_backend(backend, module)
    cap = cv2.VideoCapture(index, backend_id)
    try:
        if not cap.isOpened():
            raise CameraError(
                f"camera index {index} with backend {backend or 'any'} did not open"
            )
        return _describe_open_camera(cap, index, backend, backend_id, width, height, fps,
                                     open_timeout_ms, read_timeout_ms, module)
    except Exception:
        try:
            cap.release()
        except Exception:
            pass
        raise


def _describe_open_camera(cap, index, backend, backend_id, width, height, fps,
                          open_timeout_ms, read_timeout_ms, module):
    cv2 = import_cv2(module)
    _apply_timeouts(cap, open_timeout_ms, read_timeout_ms, module)
    _prefer_smallest_buffer(cap, module)
    if width is not None:
        cap.set(getattr(cv2, "CAP_PROP_FRAME_WIDTH", 3), float(width))
    if height is not None:
        cap.set(getattr(cv2, "CAP_PROP_FRAME_HEIGHT", 4), float(height))
    if fps is not None:
        cap.set(getattr(cv2, "CAP_PROP_FPS", 5), float(fps))
    descriptor = CameraDescriptor(
        index=index,
        backend=backend,
        backend_id=backend_id,
        opened=True,
        width=_get(cap, "CAP_PROP_FRAME_WIDTH", module) or None,
        height=_get(cap, "CAP_PROP_FRAME_HEIGHT", module) or None,
        fps=_get(cap, "CAP_PROP_FPS", module) or None,
        fourcc=_fourcc_to_text(_get(cap, "CAP_PROP_FOURCC", module), module),
    )
    return cap, descriptor


class BoundedCameraSession:
    """Own OpenCV on one daemon thread; callers bound open/read/close waits.

    A backend that cannot be interrupted is never closed under its active call.
    On timeout it receives a stop request and releases itself when that call
    returns. ``release`` reports pending cleanup instead of claiming it closed.
    This adapter is for finite probes/capture, not the live newest-frame path.
    """

    def __init__(self, index, **kwargs):
        self._stop = threading.Event()
        self._opened = threading.Event()
        self._closed = threading.Event()
        self._commands = queue.Queue()
        self._descriptor = None
        self._open_error = None
        self._cleanup_error = None
        self._thread = threading.Thread(target=self._run, args=(index, kwargs),
                                        name="psvr-opencv-probe", daemon=True)
        self._thread.start()

    def _run(self, index, kwargs):
        cap = None
        try:
            cap, self._descriptor = open_camera(index, **kwargs)
            self._opened.set()
            while not self._stop.is_set():
                command = self._commands.get()
                if command is None or self._stop.is_set():
                    break
                done, result = command
                try:
                    result.append(cap.read())
                except Exception as exc:
                    result.append(exc)
                finally:
                    done.set()
        except Exception as exc:
            self._open_error = exc
        finally:
            self._opened.set()
            try:
                if cap is not None:
                    cap.release()
            except Exception as exc:
                self._cleanup_error = exc
            finally:
                self._closed.set()

    def open(self, timeout_s):
        if not self._opened.wait(max(timeout_s, 0.0)):
            self.release(0)
            raise CameraError("camera open timed out; cleanup pending on owning thread")
        if self._open_error is not None:
            raise CameraError(f"camera open failed: {self._open_error}") from self._open_error
        return self._descriptor

    def read(self, timeout_s):
        done, result = threading.Event(), []
        self._commands.put((done, result))
        if not done.wait(max(timeout_s, 0.0)):
            self.release(0)
            raise CameraError("camera read timed out; cleanup pending on owning thread")
        if isinstance(result[0], Exception):
            raise CameraError(f"camera read failed: {result[0]}") from result[0]
        return result[0]

    def release(self, timeout_s=0.05):
        self._stop.set()
        self._commands.put(None)
        return self._closed.wait(max(timeout_s, 0.0)) and self._cleanup_error is None


def probe_index(
    index: int,
    backend: Optional[str] = None,
    open_timeout_ms: float = 1500.0,
    read_timeout_ms: float = 1500.0,
    module=None,
) -> CameraDescriptor:
    """Open one index and attempt a single bounded read. Never raises for I/O."""
    cv2 = import_cv2(module)
    backend_id = resolve_backend(backend, module)  # validates the backend name
    descriptor = CameraDescriptor(index=index, backend=backend, backend_id=backend_id, opened=False)
    if not all(math.isfinite(float(t)) and t > 0 for t in (open_timeout_ms, read_timeout_ms)):
        raise CameraError("probe timeouts must be finite and positive")
    session = None
    try:
        session = BoundedCameraSession(index, backend=backend, module=cv2,
            open_timeout_ms=open_timeout_ms, read_timeout_ms=read_timeout_ms)
        descriptor = session.open(open_timeout_ms / 1000.0)
        ok, frame = session.read(read_timeout_ms / 1000.0)
        descriptor.readable = bool(ok) and frame is not None
        if not descriptor.readable:
            descriptor.error = "opened but no frame delivered"
    except Exception as exc:  # noqa: BLE001 - inventory must not abort on one index
        descriptor.error = f"{type(exc).__name__}: {exc}"
    finally:
        if session is not None:
            descriptor.cleanup_pending = not session.release()
    return descriptor


def enumerate_cameras(
    max_index: int = 8,
    backend: Optional[str] = None,
    module=None,
) -> list:
    """Probe indices ``0..max_index-1`` and return every descriptor.

    This is a report, not a selection: it never silently returns the first
    working camera. Callers must pick an index explicitly.
    """
    if type(max_index) is not int or max_index < 1:
        raise CameraError("max_index must be a positive integer")
    return [probe_index(index, backend=backend, module=module) for index in range(max_index)]


# --------------------------------------------------------------------------- #
# Bounded-latency live source
# --------------------------------------------------------------------------- #

class LatestFrameCapture:
    """Single-slot, bounded-latency frame source over a ``VideoCapture``.

    A raw ``cv2.VideoCapture`` hands frames back in arrival order and the
    backend keeps an internal queue (MSMF grabber list, DirectShow allocator
    buffers, V4L2 ring). A live loop that is slower than the camera — detect,
    pose, filter, send — therefore consumes an ever older backlog, and the
    latency grows for the whole session. The native PS3 Eye adapter
    (:class:`psvr_camera.pseye_backend.PseyeCamera`) never accumulates because
    it publishes into one slot; this class gives the OpenCV path the same
    contract:

    * a daemon reader thread drains the backend queue continuously and keeps
      only the newest frame; frames replaced before anyone read them are
      dropped and counted, never replayed later;
    * ``read()`` returns a frame only when its sequence is strictly newer than
      the last one handed out, so the same frame is never returned twice;
    * the publish time travels with the frame as ``last_frame_monotonic``;
    * consecutive read failures stop the reader and surface through ``error``
      instead of busy-spinning;
    * ``release()`` stops the thread and closes the inner capture exactly once,
      reporting ``abandoned`` if a stuck backend read never returned.

    ``read()``/``release()``/``isOpened()`` keep the ``VideoCapture``
    contract; every other attribute is forwarded to the inner capture.
    """

    def __init__(
        self,
        cap,
        read_timeout_s: float = 2.0,
        max_consecutive_failures: int = 30,
        release_join_timeout_s: float = 2.0,
    ) -> None:
        if cap is None:
            raise CameraError("LatestFrameCapture needs an opened VideoCapture")
        self._cap = cap
        self._read_timeout_s = max(float(read_timeout_s), 0.0)
        self._max_consecutive_failures = max(int(max_consecutive_failures), 1)
        self._release_join_timeout_s = max(float(release_join_timeout_s), 0.0)
        # _state guards the frame slot; _device serializes every inner cv2 call.
        self._state = threading.Lock()
        self._device = threading.Lock()
        self._frame = None
        self._frame_monotonic = None
        self.last_frame_monotonic = None
        self._seq = 0
        self._read_seq = 0
        self._frame_event = threading.Event()
        self._stop = threading.Event()
        self._failures = 0
        self._published = 0
        self._dropped = 0
        self._error: Optional[str] = None
        self._released = False
        self._abandoned = False
        self._cleanup_complete = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="psvr-opencv-reader", daemon=True
        )
        self._thread.start()

    @property
    def error(self) -> Optional[str]:
        return self._error

    @property
    def failed(self) -> bool:
        return self._error is not None

    @property
    def abandoned(self) -> bool:
        """True when release() had to leak the capture to avoid a use-after-free."""
        return self._abandoned

    @property
    def cleanup_pending(self) -> bool:
        return self._released and not self._cleanup_complete.is_set()

    @property
    def frames_published(self) -> int:
        with self._state:
            return self._published

    @property
    def frames_dropped(self) -> int:
        with self._state:
            return self._dropped

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with self._device:
                    ok, frame = self._cap.read()
            except Exception as exc:  # noqa: BLE001 - surfaced via .error
                self._note_failure(f"camera read raised {type(exc).__name__}")
                continue
            if not ok or frame is None:
                self._note_failure("camera read failed")
                continue
            with self._state:
                if self._released or self._stop.is_set():
                    break
                self._failures = 0
                if self._frame is not None and self._seq != self._read_seq:
                    # Replacing a slot nobody consumed is the whole point: the
                    # stale frame is counted and never handed out later.
                    self._dropped += 1
                self._frame = frame
                self._frame_monotonic = time.monotonic()
                self._seq += 1
                self._published += 1
                # Set under the lock so it cannot race the consumer's
                # clear-then-read and hand out the same frame twice.
                self._frame_event.set()

    def _note_failure(self, message: str) -> None:
        with self._state:
            self._failures += 1
            if self._failures < self._max_consecutive_failures:
                return
            self._error = f"{message} {self._failures} times in a row"
            # Wake a waiting read() so it can report the failure immediately.
            self._frame_event.set()
        self._stop.set()

    def read(self, timeout_s: Optional[float] = None):
        """Wait up to ``timeout_s`` for the next *new* frame.

        Returns ``(ok, frame)``. A frame already returned by a previous call is
        never returned again: the publisher increments a sequence number and a
        reader only accepts a strictly newer one.
        """
        if self._released:
            return False, None
        timeout = self._read_timeout_s if timeout_s is None else float(timeout_s)
        if timeout <= 0.0:
            return False, None
        deadline = time.monotonic() + timeout
        while True:
            if self._released or self._error is not None:
                return False, None
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return False, None
            if not self._frame_event.wait(remaining):
                return False, None
            with self._state:
                if self._released or self._error is not None:
                    return False, None
                if self._seq == self._read_seq:
                    # Stale wakeup: the slot still holds the frame already
                    # handed out. Clear it and wait for a genuinely new one.
                    self._frame_event.clear()
                    continue
                self._read_seq = self._seq
                frame = self._frame
                self.last_frame_monotonic = self._frame_monotonic
                self._frame_event.clear()
            if frame is None:
                return False, None
            return True, frame

    def isOpened(self) -> bool:
        if self._released or self._error is not None:
            return False
        cap = self._cap
        if cap is None:
            return False
        try:
            with self._device:
                if self._released or cap is not self._cap:
                    return False
                return bool(cap.isOpened())
        except Exception:  # pragma: no cover - backend dependent
            return False

    def set(self, prop, value) -> bool:
        cap = self._cap
        if cap is None:
            return False
        with self._device:
            if self._released or cap is not self._cap:
                return False
            return bool(cap.set(prop, value))

    def get(self, prop) -> float:
        cap = self._cap
        if cap is None:
            return 0.0
        with self._device:
            if self._released or cap is not self._cap:
                return 0.0
            return float(cap.get(prop))

    def release(self) -> bool:
        with self._state:
            if self._released:
                return self._cleanup_complete.is_set() and not self._abandoned
            self._released = True
        self._stop.set()
        self._frame_event.set()
        self._thread.join(timeout=self._release_join_timeout_s)
        if self._thread.is_alive():
            # The inner read() never returned; closing under a live reader
            # would be a use-after-free. Leak it (daemon thread) instead,
            # exactly like the native adapter does for a stuck grab.
            self._abandoned = True
            return False
        with self._device:
            cap, self._cap = self._cap, None
            if cap is not None:
                try:
                    cap.release()
                except Exception:  # pragma: no cover - native close failure
                    self._abandoned = True
        self._cleanup_complete.set()
        return not self._abandoned

    def __getattr__(self, name):
        # Only reached for attributes the wrapper itself does not define.
        cap = self.__dict__.get("_cap")
        if cap is None:
            raise AttributeError(name)
        return getattr(cap, name)

    def __enter__(self) -> "LatestFrameCapture":
        return self

    def __exit__(self, *exc) -> None:
        self.release()
