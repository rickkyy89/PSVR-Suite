"""Opt-in numeric OpenCV edits, NOT capture-owned/native control verification.

Metadata enumeration only: no second filter binding, DLL, or native setters.
One OV580 runtime at the selected DShow index is required before every write.
This reduces index ambiguity but cannot establish the open capture's moniker or
prevent hotplug between enumeration and set. Readback/restore confirmation means
numeric cv2.get observations only, never independent verification/Manual flags.
"""
from __future__ import annotations

import ctypes as ct
import math
import sys
import threading

from .camera_backend import CameraError
from .ps4_dshow_controls import _GUID, _call, _check, _release

CONTRACT = "opencv-nonstrict-single-ps4"
POLICY_BOUNDS = {"exposure": (-13, -1), "gain": (0, 8), "brightness": (0, 8)}
WARNING = ("OpenCV legacy non-strict: capture/control filter identity is NOT verified; "
           "single-PS4-only enumeration is not capture-owned identity. Numeric get() "
           "readback is not independent verification or Manual-mode confirmation.")


def enumerate_dshow_devices():
    """Read complete bounded video-input moniker enumeration; never open filters."""
    if sys.platform != "win32":
        raise CameraError("legacy PS4 enumeration requires Windows DirectShow")
    ole = ct.WinDLL("ole32")
    ole.CoInitializeEx.argtypes = [ct.c_void_p, ct.c_ulong]
    ole.CoInitializeEx.restype = ct.c_long
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    ole.CoCreateInstance.argtypes = [ct.POINTER(_GUID), ct.c_void_p, ct.c_ulong,
                                    ct.POINTER(_GUID), ct.POINTER(ct.c_void_p)]
    ole.CoCreateInstance.restype = ct.c_long
    ole.CreateBindCtx.argtypes = [ct.c_ulong, ct.POINTER(ct.c_void_p)]
    ole.CreateBindCtx.restype = ct.c_long
    ole.CoTaskMemFree.argtypes = [ct.c_void_p]
    ole.CoTaskMemFree.restype = None
    hr = ole.CoInitializeEx(None, 2)
    if hr not in (0, 1, -2147417850):
        _check(hr, "legacy metadata COM initialize")
        raise CameraError("unexpected COM initialization result")
    enum = devices = bind = moniker = None
    try:
        enum, devices, bind = [ct.c_void_p() for _ in range(3)]
        _check(ole.CoCreateInstance(ct.byref(_GUID("62BE5D10-60EB-11D0-BD3B-00A0C911CE86")),
            None, 1, ct.byref(_GUID("29840822-5B84-11D0-BD3B-00A0C911CE86")), ct.byref(enum)),
            "legacy device enumerator")
        if not enum:
            raise CameraError("missing DirectShow device enumerator")
        hr_enum = _call(enum, 3, (ct.POINTER(_GUID), ct.POINTER(ct.c_void_p), ct.c_ulong),
            ct.byref(_GUID("860BB310-5D01-11D0-BD3B-00A0C911CE86")), ct.byref(devices), 0)
        _check(hr_enum, "legacy video enumeration")
        if hr_enum == 1:
            return []
        if hr_enum != 0 or not devices:
            raise CameraError("incomplete DirectShow enumeration")
        _check(ole.CreateBindCtx(0, ct.byref(bind)), "legacy metadata bind context")
        if not bind:
            raise CameraError("missing DirectShow metadata bind context")
        paths = []
        for _ in range(65):
            moniker = ct.c_void_p()
            fetched = ct.c_ulong()
            hr_next = _call(devices, 3, (ct.c_ulong, ct.POINTER(ct.c_void_p), ct.POINTER(ct.c_ulong)),
                            1, ct.byref(moniker), ct.byref(fetched))
            _check(hr_next, "legacy moniker enumeration")
            if hr_next == 1 and fetched.value == 0:
                return paths
            if hr_next != 0 or fetched.value != 1 or not moniker or len(paths) == 64:
                raise CameraError("incomplete/oversized DirectShow enumeration; no writes")
            display = ct.c_void_p()
            try:
                _check(_call(moniker, 20, (ct.c_void_p, ct.c_void_p, ct.POINTER(ct.c_void_p)),
                    bind, None, ct.byref(display)), "legacy device display name")
                if not display:
                    raise CameraError("missing DirectShow device identity")
                paths.append(ct.wstring_at(display).lower())
            finally:
                if display:
                    ole.CoTaskMemFree(display)
                _release(moniker)
                moniker = None
        raise CameraError("DirectShow enumeration did not terminate")
    finally:
        for ptr in (moniker, bind, devices, enum):
            _release(ptr)
        if hr in (0, 1):
            ole.CoUninitialize()


def require_single_ps4(index, expected=None):
    paths = enumerate_dshow_devices()
    if (not isinstance(paths, list) or len(paths) > 64
            or any(not isinstance(p, str) or not p for p in paths)):
        raise CameraError("invalid/incomplete DirectShow inventory; no writes")
    compatible = [(i, p) for i, p in enumerate(paths)
                  if any(f"vid_05a9&pid_{pid}" in p.lower() for pid in ("058a", "058b"))]
    if len(compatible) != 1:
        raise CameraError("single-PS4-only: exactly one OV580 runtime is required; "
                          f"found {len(compatible)} compatible cameras; no controls written")
    selected, path = compatible[0]
    if selected != index or (expected is not None and path != expected):
        raise CameraError("single-PS4-only: selected index/OV580 topology changed or mismatched; no writes")
    return path


def validate_targets(targets):
    if (not isinstance(targets, dict) or set(targets) - set(POLICY_BOUNDS)
            or any(type(v) is not int or not POLICY_BOUNDS[k][0] <= v <= POLICY_BOUNDS[k][1]
                   for k, v in targets.items())):
        raise CameraError("unsupported explicit OpenCV PS4 targets (profile policy, not measured range)")
    return dict(targets)


class LegacyControls:
    """All snapshots, writes, reads and restoration stay on the capture owner."""

    def __init__(self, cap, cv2, state, index, targets, cancel_event=None):
        self._cap, self._cv2, self._state = cap, cv2, state
        self._index, self._targets = index, validate_targets(targets)
        self._owner = threading.get_ident()
        self._cancel_event = cancel_event
        self._started = self._released = False
        self._path = None
        self._originals, self._touched, self._accepted = {}, [], {}
        self._frames = 0
        self._state.warning(WARNING)
        self._state.put("legacy_opencv", {"contract": CONTRACT, "single_ps4_verified": False,
            "capture_identity_confirmed": False, "readback_scope": "opencv_get_numeric_only",
            "restore_state": "not_requested", "restore_readback_confirmed": {}})

    def __getattr__(self, name):
        return getattr(self._cap, name)

    def _check_owner(self):
        if threading.get_ident() != self._owner:
            raise CameraError("legacy PS4 control calls require the capture owner")

    def _legacy(self, **values):
        info = self._state.snapshot()["legacy_opencv"]
        info.update(values)
        self._state.put("legacy_opencv", info)

    def _get(self, name):
        value = self._cap.get(getattr(self._cv2, "CAP_PROP_" + name.upper()))
        if (type(value) not in (int, float) or not math.isfinite(value)
                or (name in ("gain", "brightness") and value < 0)):
            raise CameraError(f"unreadable OpenCV {name} snapshot/readback")
        return float(value)

    def _check_cancelled(self):
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise CameraError("legacy PS4 apply cancelled; no further target writes")

    def _start(self):
        self._started = True
        try:
            self._check_cancelled()
            self._path = require_single_ps4(self._index)
            self._legacy(single_ps4_verified=True)
            # Snapshot every control before ANY target write. Never invent an
            # original or use the requested target when get() is unreadable.
            for name in POLICY_BOUNDS:
                original = self._get(name)
                self._originals[name] = original
                self._state.put("before", original, name)
                self._state.put("readback", original, name)
            for name, target in self._targets.items():
                require_single_ps4(self._index, self._path)
                self._check_cancelled()
                self._touched.append(name)  # a rejected/throwing write can mutate
                self._state.put("restoration_pending", True)
                self._legacy(restore_state="pending")
                try:
                    accepted = bool(self._cap.set(getattr(self._cv2, "CAP_PROP_" + name.upper()), target))
                except Exception as exc:
                    accepted = False
                    self._state.warning(f"OpenCV {name} write raised: {exc}")
                self._accepted[name] = accepted
                self._state.put("set_ok", accepted, name)
        except Exception as exc:
            self._legacy(single_ps4_verified=False, error=str(exc))
            self._state.warning(f"OpenCV PS4 controls refused: {exc}")
        if self._targets:
            self._state.put("requested", dict(self._targets))
            self._verify()

    def _verify(self):
        confirmed = True
        for name, target in self._targets.items():
            try:
                actual = self._get(name)
            except Exception as exc:
                actual = None
                self._state.warning(f"OpenCV {name} readback unavailable: {exc}")
            good = self._accepted.get(name) is True and actual == target
            self._state.put("readback", actual, name)
            self._state.put("readback_confirmed", good, name)
            confirmed = confirmed and good
        self._state.put("transaction", {"revision": 0,
            "state": "confirmed" if confirmed else "unconfirmed", "requested": dict(self._targets),
            "error": None if confirmed else "OpenCV targets NOT confirmed; inspect each numeric readback",
            "capture_identity_confirmed": False, "confirmation_scope": "opencv_get_numeric_only"})

    def read(self):
        self._check_owner()
        if not self._started:
            self._start()
        if self._cancel_event is not None and self._cancel_event.is_set():
            return False, None
        result = self._cap.read()
        if result[0] and self._targets and self._frames < 16:
            self._frames += 1
            self._verify()
        return result

    def release(self):
        self._check_owner()
        if self._released:
            return
        self._released = True
        restored = {}
        try:
            for name in reversed(self._touched):
                good = False
                try:
                    require_single_ps4(self._index, self._path)
                    original = self._originals[name]
                    accepted = bool(self._cap.set(getattr(self._cv2, "CAP_PROP_" + name.upper()), original))
                    self._state.put("restoration_set_ok", accepted, name)
                    actual = self._get(name)
                    self._state.put("restored", actual, name)
                    good = accepted and actual == original
                except Exception as exc:
                    self._state.warning(f"OpenCV {name} restore unverified: {exc}")
                restored[name] = good
            self._legacy(restore_readback_confirmed=restored,
                restore_state=("readback_confirmed" if all(restored.values()) else "unconfirmed")
                if self._touched else "not_requested")
            self._state.put("restoration_failed", any(not v for v in restored.values()))
        finally:
            try:
                self._cap.release()
            finally:
                self._state.put("restoration_pending", False)


class LegacyCv2Proxy:
    def __init__(self, cv2, state, targets, cancel_event=None):
        self._cv2, self._state, self._targets = cv2, state, targets
        self._cancel_event = cancel_event

    def __getattr__(self, name):
        return getattr(self._cv2, name)

    def VideoCapture(self, index, backend):
        cap = self._cv2.VideoCapture(index, backend)
        try:
            cap.set(self._cv2.CAP_PROP_FOURCC, int.from_bytes(b"YUY2", "little"))
            return LegacyControls(cap, self._cv2, self._state, index, self._targets, self._cancel_event)
        except Exception:
            cap.release()
            raise
