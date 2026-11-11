"""Backend-agnostic camera source selection.

``track`` and ``calibrate live`` need to read frames from either the OpenCV
stack or the native PS3 Eye backend, and both must look the same to the caller:
an object with ``read() -> (ok, frame)`` and ``release()``. This module is the
single dispatch point.

* ``backend="pseye"`` (explicit, case-insensitive) selects the native PS3 Eye
  adapter :class:`psvr_camera.pseye_backend.PseyeCamera`.
* ``backend="ps4-stereo"`` selects the source-owned PS4 Suite stereo adapter
  (per-eye 640x400/60, explicit reference-eye order, no PS3 controls).
* every other value (including ``None``) selects the optional-OpenCV
  :func:`psvr_camera.camera_backend.open_camera`, wrapped by
  :class:`psvr_camera.camera_backend.LatestFrameCapture` unless
  ``latest_frame=False`` is passed explicitly.

The wrapper matters for live tracking: OpenCV backends queue frames internally,
so a consumer slower than the camera would otherwise process an ever older
backlog and latency would grow for the whole session. It drains the queue on a
reader thread, keeps only the newest frame and stamps it, giving the OpenCV
path the same single-slot contract as the native backend. Callers keep the
plain ``read()``/``release()`` interface.

There is no implicit selection: the backend name is always explicit at the CLI,
and no backend silently falls back to another.
"""

from __future__ import annotations

from typing import Optional

from .camera_backend import CameraError, LatestFrameCapture, open_camera

PSEYE_BACKEND = "pseye"


class CameraSourceError(CameraError):
    pass


def is_pseye_backend(backend) -> bool:
    return backend is not None and str(backend).strip().lower() == PSEYE_BACKEND


def open_camera_source(
    index: int,
    backend: Optional[str] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    fps: Optional[float] = None,
    dll_path=None,
    module=None,
    library=None,
    read_timeout_s: Optional[float] = None,
    max_consecutive_failures: int = 30,
    latest_frame: bool = True,
    controls=None,
    strict_controls: bool = False,
    legacy_controls=None,
):
    """Open one explicit camera and return ``(source, descriptor)``.

    The returned source has ``read()`` and ``release()``. ``module`` is the
    OpenCV injection point for the non-native path (and the native ctypes table
    when ``backend="pseye"``); ``library`` is the native library injection point
    used by tests.

    ``latest_frame`` (OpenCV path only, default on) wraps the capture in
    :class:`LatestFrameCapture` so a slow consumer never inherits a growing
    backend queue. ``read_timeout_s`` becomes the wrapper's bounded read wait;
    the native path already reads the latest frame by design.
    """
    if backend is not None and str(backend).strip().lower() == "ps4-stereo":
        from .ps4_suite_source import Ps4SuiteSource

        cam = Ps4SuiteSource.open(index, width=width, height=height, fps=fps,
                                  module=module,
                                   controls=controls, strict_controls=strict_controls,
                                   legacy_controls=legacy_controls,
                                  read_timeout_s=(0.5 if read_timeout_s is None
                                                  else read_timeout_s))
        return cam, cam.descriptor
    if controls is not None or strict_controls or legacy_controls is not None:
        raise CameraSourceError("source-owned PS4 controls require backend='ps4-stereo'")
    if is_pseye_backend(backend):
        from .pseye_backend import PseyeCamera

        kwargs = dict(
            index=index,
            width=width,
            height=height,
            fps=fps,
            library=library,
            dll_path=dll_path,
            module=module,
            max_consecutive_failures=max_consecutive_failures,
        )
        if read_timeout_s is not None:
            kwargs["read_timeout_s"] = read_timeout_s
        cam = PseyeCamera.open(**kwargs)
        return cam, cam.descriptor
    cap, descriptor = open_camera(
        index, backend=backend, width=width, height=height, fps=fps, module=module
    )
    if latest_frame:
        cap = LatestFrameCapture(
            cap,
            read_timeout_s=(2.0 if read_timeout_s is None else read_timeout_s),
            max_consecutive_failures=max_consecutive_failures,
        )
    return cap, descriptor


__all__ = [
    "PSEYE_BACKEND",
    "CameraSourceError",
    "is_pseye_backend",
    "open_camera_source",
]
