"""PS4-only standard DirectShow controls; no USB/vendor commands.

OpenCV's numeric exposure setter passes Flags=0. IAMCameraControl requires
CameraControl_Flags_Manual (2) for an explicit manual exposure. Native writes
require an exact moniker obtained FROM the already-open capture, plus the OV580
identity check. A repeated index enumeration/VID-PID check is not such proof.
The current OpenCV adapter supplies no capture moniker, so its index-only native
path is deliberately disabled. All COM references belong to the owner thread.
"""
from __future__ import annotations

import ctypes as ct
import math
import sys
import threading
import uuid

from .camera_backend import CameraError

MANUAL = 2
EXPOSURE = 4  # CameraControlProperty.CameraControl_Exposure (log2 seconds)


class _GUID(ct.Structure):
    _fields_ = [("data", ct.c_ubyte * 16)]

    def __init__(self, value):
        super().__init__()
        self.data[:] = uuid.UUID(value).bytes_le


def _check(hr, operation):
    if hr < 0:
        raise CameraError(f"DirectShow {operation} failed: HRESULT 0x{hr & 0xffffffff:08X}")


def _call(ptr, slot, args, *values):
    table = ct.cast(ptr, ct.POINTER(ct.POINTER(ct.c_void_p))).contents
    method = ct.WINFUNCTYPE(ct.c_long, ct.c_void_p, *args)(table[slot])
    return method(ptr, *values)


def _release(ptr):
    if ptr:
        _call(ptr, 2, ())


def choose_led_exposure(info, fps=60):
    """Longest supported exposure <= one frame, never silently clamp long.

    This is an acquisition starting point, not proof of successful LED tracking.
    DirectShow exposure uses log2 seconds, unlike PS3 Eye native units.
    """
    low, high, step = (info[k] for k in ("minimum", "maximum", "step"))
    if (not info["capabilities"] & MANUAL or step <= 0 or low > high
            or not math.isfinite(fps) or fps <= 0):
        raise CameraError("PS4 exposure range has no usable manual mode")
    ceiling = min(high, math.floor(math.log2(1.0 / fps)))
    target = low + ((ceiling - low) // step) * step
    if target < low:
        raise CameraError("PS4 exposure range cannot supply a frame-bounded LED exposure")
    return target


class Ps4DshowControl:
    """Dependency-free IAMCameraControl/VideoProcAmp adapter, PS4-only."""

    def __init__(self, index, property_name="exposure", *, capture_device_path=None):
        if sys.platform != "win32":
            raise CameraError("PS4 DirectShow controls require Windows")
        if type(index) is not int or index < 0:
            raise CameraError("explicit non-negative DirectShow index required")
        if property_name not in ("exposure", "gain", "brightness"):
            raise CameraError("unsupported PS4 DirectShow control")
        # This MUST originate from the already-open capture, never a second
        # enumeration or a preference. cv2.VideoCapture currently exposes no
        # such identity: its index-only Suite path cannot safely enable native
        # writes (not even with one device, since hotplug can replace it).
        if not isinstance(capture_device_path, str) or not capture_device_path.strip():
            raise CameraError("capture-owned DirectShow identity unavailable; index/VID-PID is not proof; no controls written")
        self.capture_identity_confirmed = False
        expected_device_path = capture_device_path.lower()
        self._property = {"exposure": EXPOSURE, "gain": 9, "brightness": 0}[property_name]
        self.property_name = property_name
        self._owner = threading.get_ident()
        self._control = ct.c_void_p()
        self._extra_controls = {}
        self._initialized = False
        # WinDLL + explicit HRESULT returns, not OleDLL's automatic exception
        # conversion: RPC_E_CHANGED_MODE is a supported preinitialized case.
        self._ole = ct.WinDLL("ole32")
        # Explicit signatures prevent 64-bit pointer truncation.
        self._ole.CoInitializeEx.argtypes = [ct.c_void_p, ct.c_ulong]
        self._ole.CoInitializeEx.restype = ct.c_long
        self._ole.CoCreateInstance.argtypes = [ct.POINTER(_GUID), ct.c_void_p,
            ct.c_ulong, ct.POINTER(_GUID), ct.POINTER(ct.c_void_p)]
        self._ole.CoCreateInstance.restype = ct.c_long
        self._ole.CreateBindCtx.argtypes = [ct.c_ulong, ct.POINTER(ct.c_void_p)]
        self._ole.CreateBindCtx.restype = ct.c_long
        self._ole.CoTaskMemFree.argtypes = [ct.c_void_p]
        # OpenCV normally initialized STA on this owner already. Changed-mode
        # means COM is initialized in another apartment; do not uninitialize it.
        hr = self._ole.CoInitializeEx(None, 2)
        if hr not in (0, 1, -2147417850):  # RPC_E_CHANGED_MODE
            _check(hr, "CoInitializeEx")
        self._initialized = hr in (0, 1)
        enum = devices = moniker = bind = filt = None
        succeeded = False
        try:
            enum, devices, moniker, bind, filt = [ct.c_void_p() for _ in range(5)]
            _check(self._ole.CoCreateInstance(ct.byref(_GUID("62BE5D10-60EB-11D0-BD3B-00A0C911CE86")),
                None, 1, ct.byref(_GUID("29840822-5B84-11D0-BD3B-00A0C911CE86")),
                ct.byref(enum)), "device enumerator")
            hr = _call(enum, 3, (ct.POINTER(_GUID), ct.POINTER(ct.c_void_p), ct.c_ulong),
                ct.byref(_GUID("860BB310-5D01-11D0-BD3B-00A0C911CE86")), ct.byref(devices), 0)
            _check(hr, "video enumeration")
            if hr != 0 or not devices:
                raise CameraError("no DirectShow camera devices")
            for number in range(index + 1):
                _release(moniker)
                moniker = ct.c_void_p()
                fetched = ct.c_ulong()
                hr = _call(devices, 3, (ct.c_ulong, ct.POINTER(ct.c_void_p), ct.POINTER(ct.c_ulong)),
                           1, ct.byref(moniker), ct.byref(fetched))
                _check(hr, "moniker enumeration")
                if hr != 0 or fetched.value != 1:
                    raise CameraError(f"DirectShow index {index} not present")
            _check(self._ole.CreateBindCtx(0, ct.byref(bind)), "CreateBindCtx")
            display = ct.c_void_p()
            try:
                _check(_call(moniker, 20, (ct.c_void_p, ct.c_void_p, ct.POINTER(ct.c_void_p)),
                             bind, None, ct.byref(display)), "device identity")
                self.device_path = ct.wstring_at(display).lower()
            finally:
                if display:
                    self._ole.CoTaskMemFree(display)
            if not any(f"vid_05a9&pid_{pid}" in self.device_path for pid in ("058a", "058b")):
                raise CameraError("explicit DirectShow index is not a PS4 runtime camera; no controls written")
            if self.device_path != expected_device_path:
                raise CameraError("DirectShow device identity differs from open capture; no controls written")
            self.capture_identity_confirmed = True
            _check(_call(moniker, 8, (ct.c_void_p, ct.c_void_p, ct.POINTER(_GUID), ct.POINTER(ct.c_void_p)),
                         bind, None, ct.byref(_GUID("56A86895-0AD4-11CE-B03A-0020AF0BA770")),
                         ct.byref(filt)), "bind PS4 filter")
            _check(_call(filt, 0, (ct.POINTER(_GUID), ct.POINTER(ct.c_void_p)),
                         ct.byref(_GUID("C6E13370-30AC-11D0-A18C-00A0C9118956"
                                        if property_name == "exposure" else
                                        "C6E13360-30AC-11D0-A18C-00A0C9118956")),
                         ct.byref(self._control)), "IAMCameraControl/VideoProcAmp")
            succeeded = True
        finally:
            for ptr in (filt, bind, moniker, devices, enum):
                _release(ptr)
            if not succeeded:
                # Release every COM reference before balancing CoInitializeEx.
                self.close()

    def _check_owner(self):
        if threading.get_ident() != self._owner:
            raise CameraError("PS4 DirectShow controls must run on camera owner thread")

    def _interface(self, name):
        if not self._control:
            raise CameraError("PS4 native controls are released")
        if name is None or name == self.property_name:
            return self._control, self._property
        if name not in ("exposure", "gain", "brightness"):
            raise CameraError("unsupported PS4 DirectShow control")
        if name not in self._extra_controls:
            interface = ct.c_void_p()
            iid = ("C6E13370-30AC-11D0-A18C-00A0C9118956" if name == "exposure" else
                   "C6E13360-30AC-11D0-A18C-00A0C9118956")
            _check(_call(self._control, 0, (ct.POINTER(_GUID), ct.POINTER(ct.c_void_p)),
                         ct.byref(_GUID(iid)), ct.byref(interface)), "control QueryInterface")
            self._extra_controls[name] = interface
        return self._extra_controls[name], {"exposure": EXPOSURE, "gain": 9, "brightness": 0}[name]

    def for_property(self, name):
        """Share this bound device filter, never rebind/reset for each control."""
        self._check_owner()
        self._interface(name)
        return _ControlView(self, name)

    def range(self, property_name=None):
        self._check_owner()
        interface, prop = self._interface(property_name)
        values = [ct.c_long() for _ in range(5)]
        _check(_call(interface, 3, (ct.c_long,) + (ct.POINTER(ct.c_long),) * 5,
                     prop, *(ct.byref(v) for v in values)), "control GetRange")
        return dict(zip(("minimum", "maximum", "step", "default", "capabilities"),
                        (v.value for v in values)))

    def get(self, property_name=None):
        self._check_owner()
        interface, prop = self._interface(property_name)
        value, flags = ct.c_long(), ct.c_long()
        _check(_call(interface, 5, (ct.c_long, ct.POINTER(ct.c_long), ct.POINTER(ct.c_long)),
                     prop, ct.byref(value), ct.byref(flags)), "control Get")
        return {"value": value.value, "flags": flags.value}

    def set_manual(self, target, info, property_name=None):
        self._check_owner()
        if getattr(self, "capture_identity_confirmed", False) is not True:
            raise CameraError("capture/control identity not confirmed; no controls written")
        if (type(target) not in (int, float) or not math.isfinite(target)
                or int(target) != target or not info["capabilities"] & MANUAL
                or info["step"] <= 0 or not info["minimum"] <= target <= info["maximum"]
                or (target - info["minimum"]) % info["step"]):
            raise CameraError("PS4 control target is outside supported manual range/step")
        interface, prop = self._interface(property_name)
        _check(_call(interface, 4, (ct.c_long, ct.c_long, ct.c_long),
                     prop, int(target), MANUAL), "control Set(Manual)")
        return True

    def close(self):
        self._check_owner()
        try:
            for interface in self._extra_controls.values():
                _release(interface)
            self._extra_controls.clear()
            _release(self._control)
            self._control = ct.c_void_p()
        finally:
            self.capture_identity_confirmed = False
            if self._initialized:
                self._ole.CoUninitialize()
                self._initialized = False


class _ControlView:
    """Non-owning view; device releases all interface references together."""
    def __init__(self, device, name):
        self._device, self._name = device, name
    @property
    def capture_identity_confirmed(self):
        return self._device.capture_identity_confirmed
    def range(self):
        return self._device.range(self._name)
    def get(self):
        return self._device.get(self._name)
    def set_manual(self, target, info):
        return self._device.set_manual(target, info, self._name)
    def close(self):
        self._device._check_owner()


# Kept for the bounded diagnostic scripts written alongside this adapter.
Ps4ExposureControl = Ps4DshowControl
