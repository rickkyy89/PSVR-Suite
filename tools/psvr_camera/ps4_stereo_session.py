"""Continuous newest-pair PS4 capture with bounded waits and owner-only cleanup.

Timestamps are host monotonic observations immediately after a completed read,
not hardware/exposure timestamps. A slow consumer drops pairs, never queues a
history. There is no duration limit and no automatic reopen after a stuck driver.

Suite production transport is OpenCV DirectShow legacy unless
``PSVR_PS4_DSHOW_ENABLE=1`` explicitly opts into capture-owned DirectShow.
Legacy startup is non-strict, warns that capture/control identity is unverified,
and never applies native targets without that identity guard. An opted-in owned
open failure is terminal: it never silently downgrades to OpenCV.
"""
from __future__ import annotations

import math
import os
import threading
import time

from .camera_backend import BoundedCameraSession, CameraError, import_cv2
from .ps4_board_preview import _ControlState, _Cv2Proxy, _read_control
from .ps4_camera import MODES, split_stereo
from .ps4_dshow_controls import MANUAL, Ps4DshowControl, choose_led_exposure

_EXPLICIT_SETTLE_STAGES = (4, 16)


class _SuiteControls:
    """PS4 owner controls: legacy startup policy plus fail-closed explicit edits.

    The Suite acquisition owns brightness and manual exposure for its lifetime.
    The measured Suite profile declares exposure -7, gain 8, brightness 1.
    A legacy unspecified exposure is resolved by the source's explicit
    ``led_frame`` policy against the hardware range.
    Unlike the
    temporary calibration wrapper (``_TemporaryControls`` in
    ``ps4_board_preview``) this adapter:

    * gain None remains untouched; exposure None is untouched unless an explicit
      LED policy is supplied. Numeric exposure uses IAMCameraControl Manual;
    * records the same ``before``/``set_ok``/``readback``/
      ``readback_confirmed`` shape in the shared ``_ControlState`` so the
      per-frame session log keeps the calibration tool's field names;
    * on a legacy startup problem (unreadable original, missing property, rejected write,
      mismatching readback) logs an explicit warning containing
      ``"continuing with unconfirmed readback"`` and continues; it never
      raises, preserving the accepted startup policy;
    * on ``release`` only releases the device: previous values are NEVER
      restored on normal shutdown. Explicit edits instead validate every native
      range, confirm Manual readback and rollback or fail closed on error.
    """

    def __init__(self, cap, cv2, state, exposure_factory=None, index=None,
                 exposure_policy=None, fps=60, pending_controls=None,
                 strict_controls=False):
        self._cap, self._cv2, self._state = cap, cv2, state
        self._exposure_factory, self._index = exposure_factory, index
        self._exposure_policy, self._fps = exposure_policy, fps
        self._native_controls = {}
        self._native_device = None
        self._exposure_frames = 0
        self._owner = threading.get_ident()
        self._started = self._released = False
        self._pending_controls = pending_controls
        self._strict_controls = strict_controls

    def __getattr__(self, name):
        return getattr(self._cap, name)

    def _check_owner(self):
        if threading.get_ident() != self._owner:
            raise CameraError("suite controls must run on camera owner thread")

    def _apply(self):
        self._started = True
        requested = self._state.snapshot()["requested"]
        if self._exposure_policy is not None or requested["exposure"] is not None:
            self._apply_native_control(requested["exposure"], "exposure")
        if all(value is None for value in requested.values()):
            return
        for name, target in requested.items():
            # Exposure must use explicit IAMCameraControl Manual flags, never
            # OpenCV's numeric setter (Flags=0). No unsafe fallback on failure.
            if name == "exposure":
                continue
            if target is None:
                continue
            if self._exposure_factory is not None:
                self._apply_native_control(target, name)
                continue
            try:
                prop = getattr(self._cv2, "CAP_PROP_" + name.upper())
            except AttributeError as exc:
                self._state.warning(
                    f"{name} control unavailable: {exc}; "
                    "continuing with unconfirmed readback")
                continue
            try:
                original = _read_control(self._cap, prop, name)
            except Exception as exc:
                self._state.warning(
                    f"cannot save original {name}: {exc}; "
                    "continuing with unconfirmed readback")
                continue
            self._state.put("before", original, name)
            if target == original:
                self._state.put("readback", original, name)
                self._state.put("readback_confirmed", True, name)
                continue
            try:
                accepted = bool(self._cap.set(prop, target))
            except Exception as exc:
                self._state.put("set_ok", False, name)
                self._state.warning(
                    f"{name} request {target} raised {exc}; "
                    "continuing with unconfirmed readback")
                continue
            self._state.put("set_ok", accepted, name)
            try:
                actual = _read_control(self._cap, prop, name)
            except Exception as exc:
                self._state.put("readback_confirmed", False, name)
                self._state.warning(
                    f"{name} unreadable readback after set: {exc}; "
                    "continuing with unconfirmed readback")
                continue
            self._state.put("readback", actual, name)
            confirmed = bool(accepted) and actual == target
            self._state.put("readback_confirmed", confirmed, name)
            if not confirmed:
                self._state.warning(
                    f"{name} request {target} unconfirmed: "
                    f"set={accepted}, readback={actual}; "
                    "continuing with unconfirmed readback")

    def _apply_native_control(self, target, name):
        control = None
        native = {"policy": (self._exposure_policy if name == "exposure" and target is None
                              else "profile"),
                  "target": target,
                  "range": None, "before": None, "readback": None,
                  "set_flags": MANUAL, "confirmed": False,
                  "capture_identity_confirmed": False}
        key = "native_" + name
        self._state.put(key, native.copy())
        try:
            if self._exposure_factory is None:
                raise CameraError("native PS4 exposure controls unavailable for injected capture")
            if self._native_device is not None:
                control = self._native_device.for_property(name)
            else:
                control = (self._exposure_factory(self._index) if name == "exposure" else
                           self._exposure_factory(self._index, property_name=name))
                if hasattr(control, "for_property"):
                    self._native_device = control
                    control = self._native_device.for_property(name)
            native["capture_identity_confirmed"] = (
                getattr(control, "capture_identity_confirmed", False) is True)
            if not native["capture_identity_confirmed"]:
                raise CameraError("capture/control identity not confirmed; no native control calls")
            native["range"] = info = control.range()
            native["before"] = before = control.get()
            self._state.put("before", before["value"], name)
            if target is None:
                target = choose_led_exposure(info, self._fps)
            native["target"] = target
            # Always set Manual even if the numeric value already matches:
            # the device could currently be in Auto mode at that same value.
            self._state.put("set_ok", False, name)
            accepted = bool(control.set_manual(target, info))
            self._state.put("set_ok", accepted, name)
            native["readback"] = actual = control.get()
            self._state.put("readback", actual["value"], name)
            confirmed = (accepted and actual["value"] == target
                         and actual["flags"] == MANUAL)
            native["confirmed"] = confirmed
            self._state.put("readback_confirmed", confirmed, name)
            if not confirmed:
                raise CameraError(f"manual {name} {target} unconfirmed: {actual}")
        except Exception as exc:
            self._state.put("readback_confirmed", False, name)
            self._state.warning(f"PS4 {name} setup: {exc}; "
                                "continuing with unconfirmed readback")
        finally:
            self._state.put(key, native)
            # Some OV580 writes are asynchronously reflected by Get(). Retry
            # after delivered frames, never sleep or reopen the video stream.
            if control is not None and native["capture_identity_confirmed"]:
                self._native_controls[name] = control
            elif control is not None:
                try:
                    control.close()
                finally:
                    self._close_native_device()

    def _check_native_readback(self):
        if not self._native_controls:
            return
        self._exposure_frames += 1
        if self._exposure_frames not in (4, 16, 60):
            return
        for name in list(self._native_controls):
            self._read_native_control(name)

    def _read_native_control(self, name):
        key = "native_" + name
        native = self._state.snapshot()[key]
        try:
            actual = self._native_controls[name].get()
            native["delayed_readback"] = actual
            native["readback_after_frames"] = self._exposure_frames
            native["confirmed"] = (self._state.snapshot()["set_ok"][name] is True
                                   and actual["value"] == native["target"]
                                   and actual["flags"] == MANUAL)
            self._state.put("readback", actual["value"], name)
            self._state.put("readback_confirmed", native["confirmed"], name)
        except Exception as exc:
            self._state.warning(f"PS4 delayed {name} readback: {exc}")
        finally:
            self._state.put(key, native)
            if native["confirmed"] or self._exposure_frames == 60:
                self._close_native_control(name)

    def _close_native_control(self, name):
        control = self._native_controls.pop(name, None)
        if control is not None:
            try:
                control.close()
            except Exception as exc:
                self._state.warning(f"PS4 {name} interface cleanup: {exc}")
        if not self._native_controls:
            self._close_native_device()

    def _close_native_device(self):
        device, self._native_device = self._native_device, None
        if device is not None:
            try:
                device.close()
            except Exception as exc:
                self._state.warning(f"PS4 shared control device cleanup: {exc}")

    def _apply_transaction(self, revision, requested):
        """Validate every native range before writes; rollback or fail closed.

        This explicit-edit path is deliberately stricter than legacy startup.
        Native views, GetRange, Set(Manual), Get and rollback all stay on this
        capture's owner. A failed transaction never yields another frame.
        """
        self._check_owner()
        requested = dict(requested)
        transaction = {"revision": revision, "requested": dict(requested),
                       "state": "applying", "error": None,
                       "rollback_confirmed": None}
        self._state.put("transaction", transaction.copy())
        for name in requested:
            self._state.put("readback_confirmed", False, name)
            self._state.put("set_ok", None, name)
        for name in list(self._native_controls):
            self._close_native_control(name)
        controls, snapshots, ranges, touched = {}, {}, {}, []
        device = None
        try:
            if self._exposure_factory is None:
                raise CameraError("explicit PS4 edits require native device controls")
            for name, target in requested.items():
                if device is not None:
                    control = device.for_property(name)
                else:
                    control = (self._exposure_factory(self._index) if name == "exposure" else
                               self._exposure_factory(self._index, property_name=name))
                    if hasattr(control, "for_property"):
                        device = control
                        control = device.for_property(name)
                controls[name] = control
                if getattr(control, "capture_identity_confirmed", False) is not True:
                    raise CameraError("capture/control identity not confirmed; no native control calls")
                info, before = control.range(), control.get()
                ranges[name], snapshots[name] = info, before
                policy = ("led_frame" if name == "exposure" and target is None
                          and self._exposure_policy == "led_frame" else "explicit")
                if target is None and name == "exposure" and self._exposure_policy == "led_frame":
                    target = choose_led_exposure(info, self._fps)
                    requested[name] = target
                if (any(type(info.get(k)) is not int for k in
                        ("minimum", "maximum", "step", "capabilities"))
                        or not info["capabilities"] & MANUAL or info["step"] <= 0
                        or info["minimum"] > info["maximum"]
                        or type(target) is not int
                        or not info["minimum"] <= target <= info["maximum"]
                        or (target - info["minimum"]) % info["step"]):
                    raise CameraError(f"PS4 {name} target outside native Manual range/step")
                if type(before.get("value")) is not int or type(before.get("flags")) is not int:
                    raise CameraError(f"PS4 {name} original value/flags unreadable")
                self._state.put("native_" + name, {
                    "policy": policy, "target": target, "range": dict(info),
                    "before": dict(before), "readback": None, "confirmed": False,
                    "set_flags": MANUAL,
                    "capture_identity_confirmed": (
                        getattr(control, "capture_identity_confirmed", False) is True)})
            transaction["resolved"] = dict(requested)
            def confirm(name, after_frames=0):
                actual = controls[name].get()
                confirmed = (actual["value"] == requested[name] and actual["flags"] == MANUAL)
                self._state.put("readback", actual["value"], name)
                self._state.put("readback_confirmed", confirmed, name)
                native = self._state.snapshot()["native_" + name]
                native.update(readback=dict(actual), confirmed=confirmed,
                              readback_after_frames=after_frames)
                self._state.put("native_" + name, native)
                return confirmed

            unconfirmed = set()
            for name, control in controls.items():
                target = requested[name]
                touched.append(name)  # even a throwing setter may mutate hardware
                self._state.put("before", snapshots[name]["value"], name)
                self._state.put("readback_confirmed", False, name)
                accepted = bool(control.set_manual(target, ranges[name]))
                self._state.put("set_ok", accepted, name)
                if not accepted:
                    raise CameraError(f"PS4 explicit {name} write rejected")
                if not confirm(name):
                    unconfirmed.add(name)
            # OV580 may latch a write only after delivered frames. Drain on the
            # SAME owner and discard every settling frame: none can become a
            # pose. These are the existing readback checkpoints, bounded within
            # the enclosing BoundedCameraSession read timeout (no sleeps/reopen).
            for count in range(1, _EXPLICIT_SETTLE_STAGES[-1] + 1):
                if not unconfirmed:
                    break
                ok, frame = self._cap.read()
                if not ok or frame is None:
                    raise CameraError("PS4 controls settling read failed")
                transaction["settle_frames"] = count
                if count in _EXPLICIT_SETTLE_STAGES:
                    unconfirmed = {name for name in unconfirmed if not confirm(name, count)}
            if unconfirmed:
                raise CameraError("PS4 explicit readback unconfirmed: " + ", ".join(sorted(unconfirmed)))
            targets = self._state.snapshot()["requested"]
            targets.update(requested)
            self._state.put("requested", targets)
            transaction["state"] = "confirmed"
        except Exception as exc:
            rollback = True
            for name in reversed(touched):
                before = snapshots[name]
                try:
                    # No API can restore Auto flags through set_manual. Do not
                    # pretend a numeric-only rollback restored that device.
                    if before["flags"] != MANUAL:
                        raise CameraError("previous control mode was not Manual")
                    accepted = controls[name].set_manual(before["value"], ranges[name])
                    actual = controls[name].get()
                    restored = bool(accepted) and actual == before
                    rollback = rollback and restored
                    self._state.put("readback", actual["value"], name)
                    self._state.put("readback_confirmed", False, name)
                    native = self._state.snapshot().get("native_" + name, {})
                    native.update(confirmed=False, rollback_readback=dict(actual),
                                  rollback_confirmed=restored)
                    self._state.put("native_" + name, native)
                except Exception:
                    rollback = False
            transaction.update(state="failed", error=str(exc), rollback_confirmed=rollback)
            self._state.error(f"PS4 explicit controls failed: {exc}")
            raise CameraError(f"PS4 explicit controls failed; no frames published: {exc}") from exc
        finally:
            self._state.put("transaction", transaction)
            for control in controls.values():
                try:
                    control.close()
                except Exception as exc:
                    self._state.warning(f"PS4 explicit interface cleanup: {exc}")
            if device is not None:
                device.close()

    def read(self):
        self._check_owner()
        if not self._started:
            if self._strict_controls:
                self._started = True
                targets = {k: v for k, v in self._state.snapshot()["requested"].items()
                           if v is not None or (k == "exposure" and self._exposure_policy == "led_frame")}
                self._apply_transaction(0, targets)
            else:
                # Legacy startup warnings remain non-terminal. Explicit edits
                # above/below intentionally do not pass through this guard.
                try:
                    self._apply()
                except Exception as exc:  # pragma: no cover - defensive legacy startup
                    self._state.warning(f"suite control setup issue: {exc}; "
                                        "continuing with unconfirmed readback")
        pending = self._pending_controls() if self._pending_controls is not None else None
        if pending is not None:
            self._apply_transaction(*pending)
        result = self._cap.read()
        if result[0]:
            self._check_native_readback()
        return result

    def release(self):
        if self._released:
            return
        self._check_owner()
        self._released = True
        # Suite owns brightness persistently: NEVER roll back to the values
        # the device had before (e.g. the checkerboard's brightness 4).
        try:
            for name in list(self._native_controls):
                self._close_native_control(name)
            self._close_native_device()
            self._cap.release()
        finally:
            self._state.put("restoration_pending", False)


class _SuiteCv2Proxy:
    """Wrap ``VideoCapture`` with the non-restoring Suite controls adapter."""

    def __init__(self, cv2, state, exposure_factory=None, exposure_policy=None, fps=60,
                 pending_controls=None, strict_controls=False):
        self._cv2, self._state = cv2, state
        self._exposure_factory, self._exposure_policy = exposure_factory, exposure_policy
        self._fps = fps
        self._pending_controls, self._strict_controls = pending_controls, strict_controls

    def __getattr__(self, name):
        return getattr(self._cv2, name)

    def VideoCapture(self, *args, **kwargs):
        return _SuiteControls(self._cv2.VideoCapture(*args, **kwargs),
                              self._cv2, self._state, self._exposure_factory,
                              args[0], self._exposure_policy, self._fps,
                              self._pending_controls, self._strict_controls)


def _number(value, name, *, minimum=None, positive=False):
    try:
        valid = (not isinstance(value, (bool, str, bytes)) and math.isfinite(value)
                 and (minimum is None or value >= minimum)
                 and (not positive or value > 0))
    except (TypeError, ValueError, OverflowError):
        valid = False
    if not valid:
        raise CameraError(f"invalid {name}: a finite {'positive ' if positive else ''}number is required")
    return float(value)


class StereoSession:
    """One-shot session; call ``start``, consume pairs, then ``stop``.

    ``done`` signals that the pump has finished, NOT that a timed-out driver's
    owner has finished restoration/release. Check ``stop`` or ``cleanup_pending``
    for that distinction. ``module`` injects a cv2-compatible fake for tests.
    """

    def __init__(self, index, mode="640x400", fps=60, gain=8, brightness=8,
                  exposure=None, allow_delayed_readback=True, module=None,
                   read_timeout_s=1.0, clock=None, restore_controls=True,
                    exposure_policy=None, exposure_control_factory=None,
                    strict_controls=False, owned_capture_factory=None,
                     owned_dll_path=None, owned_native_factory=None, legacy_controls=None):
        if type(index) is not int or index < 0:
            raise CameraError("an explicit non-negative camera index is required")
        if not isinstance(mode, str) or mode not in MODES:
            raise CameraError("unknown PS4 mode")
        fps = _number(fps, "fps", positive=True)
        if fps not in MODES[mode].rates:
            raise CameraError(f"unsupported fps for {mode}: {MODES[mode].rates}")
        read_timeout_s = _number(read_timeout_s, "read timeout", positive=True)
        for name, target in (("gain", gain), ("brightness", brightness),
                             ("exposure", exposure)):
            if target is not None:
                _number(target, name, minimum=0 if name == "gain" else None)
        if type(allow_delayed_readback) is not bool:
            raise CameraError("allow_delayed_readback must be boolean")
        if type(restore_controls) is not bool:
            raise CameraError("restore_controls must be boolean")
        if type(strict_controls) is not bool or (strict_controls and restore_controls):
            raise CameraError("strict_controls requires persistent Suite controls")
        if exposure_policy not in (None, "led_frame"):
            raise CameraError("unknown PS4 exposure policy")
        if exposure_policy is not None and restore_controls:
            raise CameraError("LED exposure policy requires persistent Suite controls")
        self.index, self.mode, self.fps = index, mode, fps
        if clock is not None and not callable(clock):
            raise CameraError("clock must be callable")
        self._clock = time.monotonic if clock is None else clock
        self._module, self._read_timeout_s = module, read_timeout_s
        self._restore_controls = restore_controls
        self._strict_controls = strict_controls
        # Select once, before open: the closed experimental gate permits the
        # existing bounded OpenCV transport, NOT owner-bound control claims.
        # Explicit factory injections retain the offline owned test seam.
        real_suite = (not restore_controls
                      and (module is None or getattr(module, "__name__", None) == "cv2")
                      and exposure_control_factory is None)
        self._legacy_fallback = (real_suite and owned_capture_factory is None
                                 and os.environ.get("PSVR_PS4_DSHOW_ENABLE") != "1")
        self._use_owned = (not restore_controls and
                           (owned_capture_factory is not None or
                            (real_suite and not self._legacy_fallback)))
        if self._legacy_fallback:
            # Suite supplies strict=True for explicit profiles; legacy cannot
            # prove filter identity. Preserve preview with no target writes.
            self._strict_controls = False
        self._owned_factory = owned_capture_factory
        self._owned_dll_path = owned_dll_path
        self._owned_native_factory = owned_native_factory
        from .ps4_legacy_controls import validate_targets
        self._legacy_targets = validate_targets({} if legacy_controls is None else legacy_controls)
        if self._legacy_targets and not self._legacy_fallback:
            raise CameraError("OpenCV legacy targets require the closed-gate legacy transport")
        self._exposure_policy = exposure_policy
        # OpenCV does not expose its capture moniker. The real native adapter
        # therefore rejects this index-only call before binding/writing; do not
        # replace that guard with a second enumeration, even for one device.
        # Injected factories are owner-adapter/test seams, not preference data.
        self._exposure_factory = (exposure_control_factory if exposure_control_factory is not None
                                  else Ps4DshowControl if module is None or self._legacy_fallback else None)
        self._controls = _ControlState(exposure, gain, brightness, allow_delayed_readback)
        if self._legacy_fallback:
            self._controls.warning(
                "PS4 Suite using OpenCV DirectShow legacy (non-strict): "
                "PSVR_PS4_DSHOW_ENABLE is not 1; capture/control identity is not "
                "confirmed; native targets are never applied. Explicit OpenCV "
                "numeric edits require unique OV580 enumeration and are restored on release; "
                "continuing with unconfirmed readback")
        self._condition = threading.Condition()
        self._stop = threading.Event()
        from .ps4_owned_dshow import CancellationToken
        self._owned_cancel = CancellationToken(self._stop)
        self.done = threading.Event()
        self._thread = None
        self._session = None
        self._descriptor = None
        self._slot = None
        self._frames_published = self._dropped = 0
        self._ready = False
        self._error = None
        self._cleanup_pending = False
        self._pending_controls = None
        self._control_revision = 0

    def request_controls(self, controls):
        """Queue one PS4 edit, never execute COM from the requesting thread.

        One pending transaction at a time: no silent dropping/replacing an edit.
        Device range/step/Manual validation remains on the native owner.
        """
        if (not isinstance(controls, dict) or not controls
                or set(controls) - {"exposure", "gain", "brightness"}
                or any(type(v) is not int for v in controls.values())):
            raise CameraError("PS4 edit requires integer exposure/gain/brightness targets")
        with self._condition:
            if (self._restore_controls or self._thread is None or self._stop.is_set()
                    or self.done.is_set() or self._error is not None):
                raise CameraError("PS4 Suite owner is not active")
            if self._legacy_fallback:
                raise CameraError("PS4 OpenCV legacy controls unavailable: capture/control identity "
                                  "not confirmed; no live targets applied. Use Suite Apply/reopen "
                                  "for explicit guarded non-strict numeric edits (preview remains available)")
            if self._use_owned:
                if self._session is None:
                    raise CameraError("PS4 capture-owned source is not ready")
                revision = self._session.request_controls(dict(controls), timeout_s=1.5)
                self._control_revision = revision
                self._slot = None
                self._condition.notify_all()
                return revision
            transaction = self._controls.snapshot().get("transaction", {})
            if self._pending_controls is not None or transaction.get("state") in ("pending", "applying"):
                raise CameraError("PS4 controls transaction already pending")
            self._control_revision += 1
            revision = self._control_revision
            self._pending_controls = revision, dict(controls)
            self._slot = None
            self._controls.put("transaction", {"revision": revision,
                "state": "pending", "requested": dict(controls), "error": None,
                "rollback_confirmed": None})
            self._condition.notify_all()
            return revision

    def _take_controls(self):
        with self._condition:
            pending, self._pending_controls = self._pending_controls, None
            if self._stop.is_set():
                return None
            if pending is not None:
                self._slot = None
            return pending

    def start(self):
        """Launch a daemon pump and return self (opening happens asynchronously)."""
        with self._condition:
            if self._stop.is_set() or self.done.is_set():
                raise CameraError("a stopped stereo session cannot be restarted")
            if self._thread is None:
                self._cleanup_pending = True
                self._thread = threading.Thread(target=self._pump,
                    name="psvr-ps4-stereo-pump", daemon=True)
                self._thread.start()
        return self

    def wait_ready(self, timeout_s):
        """Wait for the first valid pair without consuming its queued slot.

        Suite controls run before the first raw read. On a strict transport,
        readiness also proves setup/readback succeeded; on non-strict legacy
        it proves frame availability only, not control confirmation.
        Asynchronous failures become synchronous source-open errors. The caller
        still owns the bounded stop/cleanup decision on timeout.
        """
        timeout_s = _number(timeout_s, "ready timeout", minimum=0)
        deadline = time.monotonic() + timeout_s
        with self._condition:
            while True:
                if self._error is not None:
                    raise CameraError(f"PS4 session failed before first frame: {self._error}")
                if self.done.is_set() or self._stop.is_set() or self._thread is None:
                    raise CameraError("PS4 session stopped before first frame")
                transaction = self._controls_snapshot().get("transaction", {})
                if (self._ready and self._slot is not None
                        and transaction.get("state") not in ("pending", "applying", "failed", "cancelled")):
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CameraError("PS4 session readiness timed out; no frame confirmed")
                self._condition.wait(remaining)

    def _pump(self):
        if self._use_owned:
            return self._pump_owned()
        cap = None
        try:
            spec = MODES[self.mode]
            if self._restore_controls:
                proxy = _Cv2Proxy(import_cv2(self._module), self._controls)
            elif self._legacy_fallback:
                from .ps4_legacy_controls import LegacyCv2Proxy
                proxy = LegacyCv2Proxy(import_cv2(self._module), self._controls,
                                       self._legacy_targets, self._stop)
            else:
                proxy = _SuiteCv2Proxy(import_cv2(self._module), self._controls,
                                       self._exposure_factory, self._exposure_policy, self.fps,
                                       self._take_controls, self._strict_controls)
            # BoundedCameraSession alone owns the wrapped VideoCapture. The
            # pump never calls raw cv2 reads, control writes, or driver release.
            cap = BoundedCameraSession(self.index, backend="dshow", module=proxy,
                width=spec.width, height=spec.height, fps=self.fps,
                open_timeout_ms=3000.0, read_timeout_ms=self._read_timeout_s * 1000)
            with self._condition:
                self._session = cap
            descriptor = cap.open(timeout_s=3.0)
            with self._condition:
                self._descriptor = descriptor.to_dict()
                if self._legacy_fallback:
                    self._descriptor.update(transport="ps4-opencv-legacy",
                        strict_controls=False, capture_identity_confirmed=False)
            failures = 0
            while not self._stop.is_set():
                ok, frame = cap.read(timeout_s=self._read_timeout_s)
                host_monotonic = self._clock()
                if self._stop.is_set():
                    break
                if not ok or frame is None:
                    failures += 1
                    if failures >= 5:
                        raise CameraError("camera read failed 5 times in a row")
                    self._stop.wait(0.02)
                    continue
                failures = 0
                left, right = split_stereo(frame, self.mode)
                with self._condition:
                    if self._stop.is_set():
                        break
                    if self._slot is not None:
                        self._dropped += 1
                    self._frames_published += 1
                    self._descriptor["readable"] = True
                    self._slot = {"sequence": self._frames_published,
                                  "host_monotonic": host_monotonic,
                                  "left": left, "right": right}
                    self._ready = True
                    self._condition.notify_all()
        except Exception as exc:
            # Any exception, notably an open/read timeout, is terminal: retrying
            # could queue work behind a still-active native driver call.
            if self._legacy_fallback:
                self._stop.set()  # a delayed enumeration must not apply late targets
            with self._condition:
                self._error = f"{type(exc).__name__}: {exc}"
                self._condition.notify_all()
        finally:
            closed = cap is None
            if cap is not None:
                try:
                    closed = bool(cap.release(timeout_s=0.75))
                except Exception as exc:
                    with self._condition:
                        self._error = self._error or f"camera cleanup failed: {exc}"
            with self._condition:
                self._cleanup_pending = not closed
                self.done.set()
                self._condition.notify_all()

    def _controls_snapshot(self):
        if self._use_owned and self._session is not None:
            return self._session.controls_status()
        return self._controls.snapshot()

    def _pump_owned(self):
        from .ps4_owned_dshow import OwnedDshowCapture, OwnedDshowOpenError
        cap = None
        closed = True
        try:
            if self.mode != "640x400" or self.fps != 60:
                raise CameraError("owned PS4 Suite transport is fixed to 640x400 eyes at nominal 60 Hz")
            # OpenCV is only a decoder here. Dependency failure occurs BEFORE
            # native capture creation; never substitute cv2.VideoCapture.
            cv2 = import_cv2(self._module)
            if self._stop.is_set():
                raise CameraError("PS4 session cancelled before capture-owned open")
            factory = self._owned_factory or OwnedDshowCapture.open_index
            try:
                cap = factory(self.index, self._owned_dll_path,
                    owner_release_confirmed=True, timeout_s=4.0,
                    initial_controls=self._controls.snapshot()["requested"],
                    restore_controls=False, _native_factory=self._owned_native_factory,
                    cancel_token=self._owned_cancel)
            except OwnedDshowOpenError as exc:
                cap = exc.source
                raise
            with self._condition:
                self._session = cap
                self._descriptor = {"backend": "ps4-owned-dshow", "width": 1748,
                    "height": 408, "fps": 60, "fourcc": "YUY2", "readable": False,
                    "capture_identity_confirmed": True, "hardware_verified": False}
            last_frame = time.monotonic()
            while not self._stop.is_set():
                sample = cap.next_pair(timeout_s=0.05, module=cv2)
                controls = cap.controls_status()
                transaction = controls.get("transaction", {})
                if cap.error:
                    raise CameraError(cap.error)
                if sample is None:
                    if transaction.get("state") not in ("pending", "applying") and time.monotonic() - last_frame > self._read_timeout_s:
                        raise CameraError("PS4 capture-owned frame deadline expired")
                    continue
                last_frame = time.monotonic()
                # Decode/pump validation precedes persistent acceptance. Never
                # hold the session condition across an owner RPC: stop() must
                # remain bounded even when open/driver/acceptance is delayed.
                with self._condition:
                    if self._stop.is_set():
                        break
                    candidate_matches = sample.get("controls_revision", 0) == self._control_revision
                if not candidate_matches:
                    continue
                if any(getattr(sample.get(eye), "shape", None) != (400, 640, 3) or
                       str(getattr(sample.get(eye), "dtype", "")) != "uint8" for eye in ("left", "right")):
                    raise CameraError("PS4 decoded startup pair violates the fixed 640x400 BGR contract")
                if transaction.get("state") == "awaiting_acceptance":
                    cap.accept_readiness(sample["sequence"], sample.get("controls_revision", 0), timeout_s=0.5)
                    transaction = cap.controls_status().get("transaction", {})
                    sample = dict(sample, readiness_pending=False)
                with self._condition:
                    if self._stop.is_set():
                        break
                    if (sample.get("controls_revision", 0) != self._control_revision or
                            transaction.get("state") in ("pending", "applying", "failed", "awaiting_frame", "awaiting_acceptance")):
                        continue
                    if self._slot is not None:
                        self._dropped += 1
                    self._frames_published += 1
                    self._descriptor["readable"] = True
                    self._slot = sample
                    self._ready = True
                    self._condition.notify_all()
        except Exception as exc:
            with self._condition:
                self._error = f"{type(exc).__name__}: {exc}"
                self._slot = None
                self._condition.notify_all()
        finally:
            if cap is not None:
                with self._condition:
                    self._session = cap  # retain failed-startup or pending owner
                try:
                    cap.release(timeout_s=0.75)
                except Exception as exc:
                    with self._condition:
                        self._error = self._error or f"camera cleanup failed: {exc}"
                closed = not cap.cleanup_pending
            with self._condition:
                self._cleanup_pending = not closed
                self.done.set()
                self._condition.notify_all()

    def next_pair(self, timeout_s=0.5):
        """Consume the single newest pair, at most once, or return None."""
        timeout_s = _number(timeout_s, "pair timeout", minimum=0)
        deadline = time.monotonic() + timeout_s
        with self._condition:
            while self._slot is None:
                if self._thread is None or self.done.is_set() or self._stop.is_set() or self._error:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
            if self._stop.is_set() or self._error or self.done.is_set():
                self._slot = None
                return None
            transaction = self._controls_snapshot().get("transaction", {})
            if transaction.get("state") in ("pending", "applying", "failed", "awaiting_frame", "awaiting_acceptance"):
                self._slot = None
                return None
            sample, self._slot = self._slot, None
            return sample

    def _refresh_cleanup(self):
        # release(0) only checks/requests owner cleanup, never touches the native
        # capture. It lets reports observe late cleanup after the pump has exited.
        with self._condition:
            cap = self._session if self.done.is_set() and self._cleanup_pending else None
        if cap is not None:
            if self._use_owned:
                # Read-only observation, never release(0), no false detach when
                # the pump exited but native Stop/rollback is still pending.
                with self._condition:
                    self._cleanup_pending = bool(cap.cleanup_pending)
                return
            # BoundedCameraSession exposes no public non-mutating close query.
            # Its completion Event is safe to observe across threads. Avoid
            # repeatedly enqueueing release sentinels while a driver is stuck
            # (a status UI can poll indefinitely); retain the public release
            # check for injected compatible session factories without the Event.
            closed_event = getattr(cap, "_closed", None)
            if isinstance(closed_event, threading.Event):
                closed = closed_event.is_set()
                cleanup_error = getattr(cap, "_cleanup_error", None)
                if closed and cleanup_error is not None:
                    with self._condition:
                        self._error = self._error or f"camera cleanup failed: {cleanup_error}"
                    # Owner completion does not establish successful release:
                    # a throwing native close leaves handle ownership unknown.
                    closed = False
            else:
                try:
                    closed = bool(cap.release(timeout_s=0))
                except Exception:
                    closed = False
            if closed:
                with self._condition:
                    self._cleanup_pending = False

    def stop(self, timeout_s=2.0):
        """Request stop, join only up to the budget, report finished cleanup.

        A stuck read is never released underneath its owner. Repeated calls are
        safe, including before start; a stopped instance cannot be restarted.
        """
        timeout_s = _number(timeout_s, "stop timeout", minimum=0)
        deadline = time.monotonic() + timeout_s
        if self._use_owned:
            # Shared BEFORE factory/open begins. Setting this never waits for
            # a driver call or an owner RPC, and wins any later persistent ACK.
            self._owned_cancel.set()
        with self._condition:
            self._stop.set()
            self._slot = None
            if self._pending_controls is not None:
                revision, targets = self._pending_controls
                self._pending_controls = None
                self._controls.put("transaction", {"revision": revision,
                    "requested": dict(targets), "state": "cancelled",
                    "error": "owner stopped before applying controls",
                    "rollback_confirmed": None})
            thread = self._thread
            if thread is None:
                self.done.set()
            self._condition.notify_all()
        if thread is not None:
            thread.join(timeout_s)
        if self._use_owned and self.done.is_set() and self._cleanup_pending and timeout_s > 0:
            try:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self._session.release(timeout_s=min(remaining, 0.75))
            except Exception:
                pass
        self._refresh_cleanup()
        with self._condition:
            return self.done.is_set() and not self._cleanup_pending

    def report(self):
        """Return an independent status snapshot, never a hardware-time claim."""
        self._refresh_cleanup()
        with self._condition:
            result = {"index": self.index, "mode": self.mode, "fps": self.fps,
                      "descriptor": (dict(self._descriptor)
                                     if self._descriptor is not None else None),
                       "error": self._error, "cleanup_pending": self._cleanup_pending,
                       "ready": self._ready and not self._stop.is_set() and self._error is None,
                      "frames_published": self._frames_published,
                      "dropped": self._dropped,
                      "timing_source": "host_read_completion_not_exposure"}
            if result["descriptor"] is not None:
                result["descriptor"]["cleanup_pending"] = self._cleanup_pending
        result["controls"] = self._controls_snapshot()
        return result
