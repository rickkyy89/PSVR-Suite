"""Bounded camera capture with an explicit selection and a JSON report.

The report is the machine-readable answer to "what did the camera actually
deliver": requested vs negotiated mode, frames captured/dropped, timeouts,
wall duration, achieved frame rate and any errors. A capture never falls back to
another camera index and never runs unbounded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import time
import math
from typing import Callable, Optional

from .camera_backend import BoundedCameraSession, CameraError

CAPTURE_SCHEMA = "psvr.camera.capture"
CAPTURE_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CaptureReport:
    index: int
    backend: Optional[str]
    backend_id: Optional[int]
    requested: dict
    opened: bool
    frames_requested: int
    frames_captured: int = 0
    frames_dropped: int = 0
    timeouts: int = 0
    duration_s: float = 0.0
    negotiated: Optional[dict] = None
    errors: list = field(default_factory=list)
    started_utc: str = field(default_factory=_utc_now)
    ended_utc: Optional[str] = None
    cleanup_pending: bool = False

    @property
    def achieved_fps(self) -> Optional[float]:
        if self.duration_s <= 0.0 or self.frames_captured == 0:
            return None
        return round(self.frames_captured / self.duration_s, 3)

    def to_dict(self) -> dict:
        return {
            "schema": CAPTURE_SCHEMA,
            "version": CAPTURE_VERSION,
            "index": self.index,
            "backend": self.backend,
            "backend_id": self.backend_id,
            "requested": dict(self.requested),
            "negotiated": self.negotiated,
            "opened": self.opened,
            "frames_requested": self.frames_requested,
            "frames_captured": self.frames_captured,
            "frames_dropped": self.frames_dropped,
            "timeouts": self.timeouts,
            "duration_s": round(self.duration_s, 6),
            "achieved_fps": self.achieved_fps,
            "errors": list(self.errors),
            "started_utc": self.started_utc,
            "ended_utc": self.ended_utc,
            "cleanup_pending": self.cleanup_pending,
        }


def capture_frames(
    index: int,
    backend: Optional[str] = None,
    frames: int = 120,
    max_seconds: float = 3.0,
    warmup_frames: int = 2,
    width: Optional[int] = None,
    height: Optional[int] = None,
    fps: Optional[float] = None,
    on_frame: Optional[Callable] = None,
    module=None,
    cleanup_timeout_s: float = 0.05,
) -> CaptureReport:
    """Capture at most ``frames`` frames or ``max_seconds`` seconds.

    Both bounds are enforced: the loop stops at the first one reached. A
    backend call cannot hold the caller past the remaining budget (plus at
    most ``cleanup_timeout_s`` cleanup wait, default 50ms). A stuck owner cleans up when its call returns;
    the report states ``cleanup_pending``. ``on_frame(frame)`` may return ``False`` to
    request an early stop (used by the calibration view collector).
    User callbacks remain on the caller's thread and must return cooperatively;
    the deadline bounds backend calls, not arbitrary callback execution.
    """
    if type(frames) is not int or frames < 1:
        raise CameraError("frames must be a positive integer")
    if not math.isfinite(max_seconds) or max_seconds <= 0:
        raise CameraError("max_seconds must be positive")
    if not math.isfinite(cleanup_timeout_s) or not 0 <= cleanup_timeout_s <= 2:
        raise CameraError("cleanup_timeout_s must be between 0 and 2 seconds")
    report = CaptureReport(
        index=index,
        backend=backend,
        backend_id=None,
        requested={
            "width": width,
            "height": height,
            "fps": fps,
            "frames": frames,
            "max_seconds": max_seconds,
        },
        opened=False,
        frames_requested=frames,
    )
    cap = None
    started = time.perf_counter()
    try:
        cap = BoundedCameraSession(index, backend=backend, width=width, height=height, fps=fps, module=module)
        remaining = lambda: max(0.0, max_seconds - (time.perf_counter() - started))
        descriptor = cap.open(remaining())
        report.opened = True
        report.backend_id = descriptor.backend_id
        report.negotiated = {
            "width": descriptor.width,
            "height": descriptor.height,
            "fps": descriptor.fps,
            "fourcc": descriptor.fourcc,
        }
        for _ in range(max(int(warmup_frames), 0)):
            if time.perf_counter() - started > max_seconds:
                break
            cap.read(remaining())
        while report.frames_captured < frames:
            if time.perf_counter() - started > max_seconds:
                break
            ok, frame = cap.read(remaining())
            if not ok or frame is None:
                report.timeouts += 1
                report.frames_dropped += 1
                if report.timeouts >= max(5, frames):
                    report.errors.append("capture aborted after repeated read timeouts")
                    break
                continue
            report.frames_captured += 1
            if on_frame is not None and on_frame(frame) is False:
                break
    except CameraError as exc:
        report.errors.append(str(exc))
    except Exception as exc:  # noqa: BLE001 - report, do not crash the CLI
        report.errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        if cap is not None:
            try:
                report.cleanup_pending = not cap.release(timeout_s=cleanup_timeout_s)
            except Exception:  # pragma: no cover
                pass
    report.duration_s = max(time.perf_counter() - started, 0.0)
    report.ended_utc = _utc_now()
    return report
