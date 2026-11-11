"""Bounded PS4 checkerboard visibility preview, NOT calibration.

Only explicit exposure/gain/brightness arguments change controls. Values are backend-native,
not portable units. No preferences, calibration or camera configuration are saved.
Restoration needs a responsive driver: a timed-out read leaves cleanup pending on
the capture owner, never releases the device underneath its active read.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import threading

from .camera_backend import CameraError, import_cv2
from .ps4_camera import capture_stereo


class _ControlState:
    def __init__(self, exposure, gain, brightness=None, allow_delayed_readback=False):
        self.lock = threading.Lock()
        requested = {"exposure": exposure, "gain": gain, "brightness": brightness}
        self.data = {
            "requested": requested,
            **{key: dict.fromkeys(requested) for key in
               ("before", "set_ok", "readback", "readback_confirmed", "restored",
                "restoration_set_ok", "restore_write_accepted")},
            "allow_delayed_readback": allow_delayed_readback,
            "restoration_readback_is_observation": True,
            "restoration_independently_verified": False,
            "restoration_failed": False,
            "restoration_pending": False,
            "errors": [],
            "warnings": [],
        }

    def put(self, key, value, name=None):
        with self.lock:
            if name is None:
                self.data[key] = value
            else:
                self.data[key][name] = value

    def error(self, message, restoration=False):
        with self.lock:
            self.data["errors"].append(message)
            if restoration:
                self.data["restoration_failed"] = True

    def warning(self, message):
        with self.lock:
            self.data["warnings"].append(message)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)


def _read_control(cap, prop, name):
    value = float(cap.get(prop))
    # DirectShow exposure -1 is legitimate; gain -1 is not a usable original.
    if not math.isfinite(value) or (name == "gain" and value < 0):
        raise CameraError(f"unreadable {name}: {value}")
    return value


class _TemporaryControls:
    """Keep control changes and rollback on the backend's existing owner thread."""

    def __init__(self, cap, cv2, state):
        self._cap, self._cv2, self._state = cap, cv2, state
        self._owner = threading.get_ident()
        self._started = self._released = False
        self._originals = {}
        self._attempted = []
        self._allow_delayed_readback = state.snapshot()["allow_delayed_readback"]

    def __getattr__(self, name):
        return getattr(self._cap, name)

    def _check_owner(self):
        if threading.get_ident() != self._owner:
            raise CameraError("temporary controls must run on camera owner thread")

    def _apply(self):
        self._started = True
        requested = self._state.snapshot()["requested"]
        if all(value is None for value in requested.values()):
            return
        # Snapshot controls AFTER width/height/fps negotiation, before ANY
        # requested mutation. If a requested original is unreadable, fail closed.
        failures = []
        for name in requested:
            try:
                prop = getattr(self._cv2, "CAP_PROP_" + name.upper())
                original = _read_control(self._cap, prop, name)
                self._originals[name] = (prop, original)
                self._state.put("before", original, name)
            except Exception as exc:
                if requested[name] is not None:
                    failures.append(f"cannot save original {name}: {exc}")
        if failures:
            raise CameraError("; ".join(failures))
        for name, target in requested.items():
            if target is None:
                continue
            prop, original = self._originals[name]
            if target == original:
                self._state.put("readback", original, name)
                self._state.put("readback_confirmed", True, name)
                continue
            # Even a rejected/raising set can mutate a buggy driver. Register
            # rollback BEFORE the call. Cached get() cannot prove no mutation;
            # EVERY attempted write must receive an original-value rollback.
            self._attempted.append(name)
            self._state.put("restoration_pending", True)
            accepted = bool(self._cap.set(prop, target))
            self._state.put("set_ok", accepted, name)
            actual = _read_control(self._cap, prop, name)
            self._state.put("readback", actual, name)
            self._state.put("readback_confirmed", actual == target, name)
            message = (f"{name} request {target} rejected or mismatched: "
                       f"set={accepted}, readback={actual}")
            if not accepted:
                raise CameraError(message)
            if actual != target:
                if not self._allow_delayed_readback:
                    raise CameraError(message)
                self._state.warning(message + "; continuing with unconfirmed readback")

    def read(self):
        self._check_owner()
        if not self._started:
            try:
                self._apply()
            except Exception as exc:
                self._state.error(f"control setup failed: {exc}")
                raise
        return self._cap.read()

    def release(self):
        if self._released:
            return
        self._check_owner()
        self._released = True
        try:
            for name in reversed(self._attempted):
                prop, original = self._originals[name]
                try:
                    accepted = bool(self._cap.set(prop, original))
                    self._state.put("restoration_set_ok", accepted, name)
                    self._state.put("restore_write_accepted", accepted, name)
                    actual = _read_control(self._cap, prop, name)
                    # This is an immediate driver observation, NOT independent
                    # proof of rollback. Never reopen the camera to verify here.
                    self._state.put("restored", actual, name)
                    message = f"set={accepted}, readback={actual}, expected={original}"
                    if not accepted:
                        raise CameraError(message)
                    if actual != original:
                        if not self._allow_delayed_readback:
                            raise CameraError(message)
                        self._state.warning(f"{name} restoration readback mismatch: {message}")
                except Exception as exc:
                    self._state.error(f"{name} restoration failed: {exc}", restoration=True)
        finally:
            try:
                self._cap.release()
            except Exception as exc:
                self._state.error(f"camera release failed: {exc}")
                raise
            finally:
                self._state.put("restoration_pending", False)


class _Cv2Proxy:
    def __init__(self, cv2, state):
        self._cv2, self._state = cv2, state

    def __getattr__(self, name):
        return getattr(self._cv2, name)

    def VideoCapture(self, *args, **kwargs):
        return _TemporaryControls(self._cv2.VideoCapture(*args, **kwargs),
                                  self._cv2, self._state)


def run_preview(index, seconds=10.0, exposure=None, gain=None, preview=False,
                module=None, brightness=None, on_pair=None, allow_delayed_readback=False):
    """Inspect visibility without calibration/persistence.

    ``allow_delayed_readback`` permits accepted writes with mismatching immediate
    observations; it does not turn rejected writes into success. Matching reads
    may also be cached, so rollback is never claimed independently verified.
    """
    if type(index) is not int or index < 0:
        raise CameraError("an explicit non-negative camera index is required")
    if not math.isfinite(seconds) or not 0 < seconds <= 60:
        raise CameraError("preview duration must be in (0, 60] seconds")
    for name, target in (("exposure", exposure), ("gain", gain),
                         ("brightness", brightness)):
        if target is not None and (not math.isfinite(target)
                                   or (name == "gain" and target < 0)):
            raise CameraError(f"invalid {name} target")
    state = _ControlState(exposure, gain, brightness, allow_delayed_readback)
    capture = None
    try:
        proxy = _Cv2Proxy(import_cv2(module), state)
        capture = capture_stereo(index=index, backend="dshow", mode="640x400",
                                 fps=60, seconds=seconds, preview=preview, module=proxy,
                                 on_pair=on_pair)
    except Exception as exc:
        state.error(f"preview failed: {type(exc).__name__}: {exc}")
    controls = state.snapshot()
    errors = list(capture["errors"]) if capture is not None else []
    errors.extend(controls["errors"])
    if capture is not None and capture.get("cleanup_pending"):
        errors.append("camera cleanup pending; control restoration is not confirmed")
    return {"schema": "psvr.camera.ps4.board_preview", "version": 1,
            "calibrated": False, "tracking_6dof": False,
            "purpose": "checkerboard_visibility_only", "capture": capture,
            **controls, "errors": errors}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", required=True, type=int)
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--exposure", type=float, help="Temporary backend-native exposure")
    parser.add_argument("--gain", type=float, help="Temporary backend-native gain")
    parser.add_argument("--brightness", type=float, help="Temporary backend-native brightness")
    parser.add_argument("--allow-delayed-readback", action="store_true",
                        help="Experimental: allow accepted writes with unconfirmed readback")
    parser.add_argument("--preview", action="store_true", help="Show Tk stereo preview")
    args = parser.parse_args(argv)
    try:
        report = run_preview(args.index, args.seconds, args.exposure, args.gain,
                             args.preview, brightness=args.brightness,
                             allow_delayed_readback=args.allow_delayed_readback)
    except (CameraError, ValueError, OSError) as exc:
        print(json.dumps({"errors": [str(exc)]}))
        return 2
    print(json.dumps(report, indent=2, allow_nan=False))
    return 2 if report["errors"] or report["restoration_failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
