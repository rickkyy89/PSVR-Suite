"""Capture-owned PS4 graph and bounded multi-property owner transactions.

Used by real persistent StereoSession/Suite acquisition. No capture fallback,
device install, profile writes, driver binding or automatic reopen. Callers must
coordinate release of ALL existing camera owners before opting in. Native COM
open/Set/Stop cannot be preempted safely: deadlines quarantine the owner, never
free its objects from a foreign thread. Hardware evaluation remains a separate
coordinated step; fake injection does not prove a driver works.
"""
from __future__ import annotations

import copy
from contextlib import nullcontext
import ctypes as ct
import json
from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import queue
import sys
import threading
import time

from .camera_backend import CameraError, import_cv2

TRANSPORT_BYTES = 1748 * 408 * 2
PROPERTIES = {"exposure": 0, "gain": 1, "brightness": 2}
_reservation_lock = threading.Lock()
_active_owner = None  # strong reference; NEVER discard a timed-out native owner


class CancellationToken:
    """Admission Event plus lock-free native commit/cancel decisions.

    Stop never acquires the owned condition. A registered decision's CAS is
    authoritative; Event admits no new work. Registration is serialized by the
    owned condition and rechecks Event, so stop cannot miss a newly queued ACK.
    """
    def __init__(self, event=None):
        self.lock = threading.RLock()
        self.event = event if event is not None else threading.Event()
        self._decisions = ()

    def set(self):
        self.event.set()
        for decision in self._decisions:
            decision.cancel()  # pure lock-free C state, NEVER COM/owner wait

    def register(self, decision):
        self._decisions = tuple(item for item in self._decisions if item.status()[0] == 0) + (decision,)
        if self.event.is_set():
            decision.cancel()

    def unregister(self, decision):
        # Admission rollback only; caller holds the owned condition. Stop may
        # still hold an older immutable tuple, whose cancelled ticket is safe.
        self._decisions = tuple(item for item in self._decisions if item is not decision)

    def is_set(self):
        return self.event.is_set()

    def wait(self, timeout=None):
        return self.event.wait(timeout)


def _timeout(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 30:
        raise CameraError("timeout must be finite and in (0, 30] seconds")
    return float(value)


def _check(hr, operation):
    if hr < 0:
        raise CameraError(f"owned DirectShow {operation}: HRESULT 0x{hr & 0xffffffff:08X}")


def resolve_owned_dll_path(value=None):
    """Separate PS4 DLL, never the PS3 preference's dll path. No auto-build."""
    if os.environ.get("PSVR_PS4_DSHOW_ENABLE") != "1":
        raise CameraError("PS4 capture-owned hardware gate is closed. Explicit owned capture requires "
                          "review before enabling PSVR_PS4_DSHOW_ENABLE=1; this API has no fallback. "
                          "Suite selects non-strict OpenCV legacy while the gate is closed, "
                          "without verified capture/control identity or native target application.")
    selected = value or os.environ.get("PSVR_PS4_DSHOW_DLL")
    path = Path(selected) if selected else Path(__file__).resolve().parents[1] / "psvr_ps4_dshow" / "psvr_ps4_dshow.dll"
    if not path.is_absolute() or path.suffix.lower() != ".dll" or not path.is_file():
        raise CameraError("PS4 capture-owned DLL unavailable; explicitly supply a verified ABI-v2 DLL via "
                          "PSVR_PS4_DSHOW_DLL. No OpenCV capture/control fallback and no auto-build.")
    return path


class _Control(ct.Structure):
    _fields_ = [(name, ct.c_int32) for name in (
        "minimum", "maximum", "step", "default", "capabilities", "value", "flags")]


class _Decision:
    """Native 16-byte atomic phase; storage/DLL retained by every user.

    Phase 1 publishes immutable success, independently of Python/Event notify.
    Cross-thread methods here touch ONLY atomics, never session/COM pointers.
    """
    def __init__(self, dll):
        self._dll = dll
        self._storage = (ct.c_uint64 * 2)()
        self.pointer = ct.cast(self._storage, ct.c_void_p)
        _check(dll.psvr_dshow_decision_init(self.pointer, ct.sizeof(self._storage)), "decision init")

    def status(self):
        error = ct.c_int32()
        return self._dll.psvr_dshow_decision_status(self.pointer, ct.byref(error)), error.value

    def cancel(self):
        return self._dll.psvr_dshow_decision_cancel(self.pointer)


class _Native:
    """Session/COM ABI stays on the owner; decision atomics are thread-safe.

    Creating/querying/cancelling a decision never touches a session/COM pointer.
    """

    def __init__(self, dll_path):
        if sys.platform != "win32" or ct.sizeof(ct.c_void_p) != 8:
            raise CameraError("owned DirectShow PoC requires Windows x64 Python")
        path = resolve_owned_dll_path(dll_path)
        self._dll = ct.CDLL(str(path), winmode=0x1100)
        self._handle = ct.c_void_p()
        self._buffer = (ct.c_uint8 * TRANSPORT_BYTES)()
        dll = self._dll
        dll.psvr_dshow_abi.argtypes = []
        dll.psvr_dshow_abi.restype = ct.c_uint32
        if dll.psvr_dshow_abi() != 2:
            raise CameraError("unsupported owned DirectShow ABI")
        signatures = {
            "open": [ct.c_wchar_p, ct.POINTER(ct.c_void_p)],
            "open_index": [ct.c_int32, ct.POINTER(ct.c_void_p)],
            "inspect_index_exact_moniker": [ct.c_int32, ct.c_wchar_p, ct.POINTER(ct.c_void_p)],
            "diagnostics": [ct.c_void_p, ct.POINTER(ct.c_char), ct.c_uint32],
            "read": [ct.c_void_p, ct.POINTER(ct.c_uint8), ct.c_uint32, ct.c_uint32, ct.POINTER(ct.c_uint64)],
            "get_control": [ct.c_void_p, ct.c_uint32, ct.POINTER(_Control)],
            "set_manual": [ct.c_void_p, ct.c_uint32, ct.c_int32],
            "begin_controls": [ct.c_void_p, ct.c_uint32, ct.POINTER(ct.c_int32), ct.POINTER(ct.c_uint64)],
            "commit_controls": [ct.c_void_p, ct.POINTER(ct.c_uint64)],
            "finish_controls": [ct.c_void_p, ct.c_uint32],
            "accept_persistence": [ct.c_void_p, ct.c_uint64],
            "accept_persistence_decided": [ct.c_void_p, ct.c_uint64, ct.c_void_p],
            "accept_readiness_decided": [ct.c_void_p, ct.c_uint64, ct.c_uint32, ct.c_void_p],
            "finish_controls_decided": [ct.c_void_p, ct.c_uint32, ct.c_void_p],
            "decision_init": [ct.c_void_p, ct.c_uint32],
            "decision_status": [ct.c_void_p, ct.POINTER(ct.c_int32)],
            "decision_cancel": [ct.c_void_p],
            "rollback_controls": [ct.c_void_p, ct.POINTER(ct.c_uint32)],
            "close": [ct.c_void_p],
        }
        for name, args in signatures.items():
            try:
                function = getattr(dll, "psvr_dshow_" + name)
            except AttributeError as exc:
                raise CameraError("PS4 DLL lacks required owned-graph diagnostics/readiness API; rebuild verified sources before use") from exc
            function.argtypes = args
            function.restype = ct.c_uint32 if name in ("decision_status", "decision_cancel") else ct.c_int32

    def make_decision(self):
        return _Decision(self._dll)

    def open(self, display_name):
        # A failed open may still return a handle requiring owner-only cleanup.
        _check(self._dll.psvr_dshow_open(display_name, ct.byref(self._handle)), "open")

    def open_index(self, index):
        _check(self._dll.psvr_dshow_open_index(index, ct.byref(self._handle)), "open selected capture index")

    def inspect_index(self, index, expected_moniker):
        _check(self._dll.psvr_dshow_inspect_index_exact_moniker(index, expected_moniker,
            ct.byref(self._handle)), "read-only exact-moniker candidate inspection")

    def diagnostics(self):
        if not self._handle:
            return []
        text = ct.create_string_buffer(65536)
        _check(self._dll.psvr_dshow_diagnostics(self._handle, text, ct.sizeof(text)), "diagnostic metadata copy")
        # Native JSONL is fixed-capacity, literal stages/GUIDs/numbers only.
        return [json.loads(line) for line in text.value.decode("ascii").splitlines()[:512] if line]

    def read(self, timeout_ms):
        sequence = ct.c_uint64()
        hr = self._dll.psvr_dshow_read(self._handle, self._buffer,
            TRANSPORT_BYTES, timeout_ms, ct.byref(sequence))
        completed = time.perf_counter()  # immediately after native read completion
        _check(hr, "read")
        if hr == 1:
            return None
        if hr != 0:
            raise CameraError("unexpected owned DirectShow read status")
        return bytes(self._buffer), sequence.value, completed

    def get_control(self, name):
        out = _Control()
        _check(self._dll.psvr_dshow_get_control(self._handle, PROPERTIES[name], ct.byref(out)), "GetRange/Get")
        return {name: getattr(out, name) for name, _ in out._fields_}

    def set_manual(self, name, value):
        _check(self._dll.psvr_dshow_set_manual(self._handle, PROPERTIES[name], value), "Set(Manual)/readback")
        return self.get_control(name)

    def begin_controls(self, targets):
        values = (ct.c_int32 * 3)(*(targets.get(name, 0) for name in PROPERTIES))
        mask = sum(1 << PROPERTIES[name] for name in targets)
        boundary = ct.c_uint64()
        _check(self._dll.psvr_dshow_begin_controls(self._handle, mask, values, ct.byref(boundary)), "begin transaction")
        return boundary.value

    def commit_controls(self):
        boundary = ct.c_uint64()
        _check(self._dll.psvr_dshow_commit_controls(self._handle, ct.byref(boundary)), "commit transaction readback/barrier")
        return boundary.value

    def finish_controls(self, persist=False, decision=None):
        if decision is None:
            hr = self._dll.psvr_dshow_finish_controls(self._handle, int(persist))
        else:
            hr = self._dll.psvr_dshow_finish_controls_decided(self._handle, int(persist), decision.pointer)
        _check(hr, "finish transaction")

    def accept_persistence(self, sequence, decision):
        _check(self._dll.psvr_dshow_accept_persistence_decided(self._handle, sequence, decision.pointer),
               "accept decoded acquisition readiness")

    def accept_readiness_nonpersistent(self, sequence, decision):
        _check(self._dll.psvr_dshow_accept_readiness_decided(self._handle, sequence, 0, decision.pointer),
               "accept nonpersistent decoded readiness")

    def rollback_controls(self):
        confirmed = ct.c_uint32()
        hr = self._dll.psvr_dshow_rollback_controls(self._handle, ct.byref(confirmed))
        return {"confirmed": [name for name, prop in PROPERTIES.items() if confirmed.value & (1 << prop)],
                "error": f"HRESULT 0x{hr & 0xffffffff:08X}" if hr < 0 else None,
                "active": hr != 1}

    def close(self):
        if self._handle:
            _check(self._dll.psvr_dshow_close(self._handle), "Stop/restore/cleanup")
            self._handle = ct.c_void_p()


@dataclass
class _Command:
    operation: str
    name: str
    value: object
    deadline: float
    done: threading.Event = field(default_factory=threading.Event)
    result: object = None
    error: Exception | None = None
    revision: int = 0
    decision: object = None
    prepared: object = None  # immutable success payload, BEFORE decision CAS
    published: bool = False


class OwnedDshowOpenError(CameraError):
    def __init__(self, message, source):
        super().__init__(message)
        self.source = source  # caller can inspect pending cleanup; never discard owner


class OwnedDshowCapture:
    """Bounded caller waits; newest immutable YUY2 sample; explicit control RPC.

    Default raw-PoC open writes nothing and restores changes on close. Suite
    passes initial_controls and restore_controls=False explicitly: only a fully
    settled startup followed by a decoded/pump-accepted post-commit frame can
    persist. open() returns a candidate; accept_readiness() is the owner RPC
    completing readiness. Cleanup failure retains the real owner and apartment.
    """

    @classmethod
    def open(cls, display_name, dll_path=None, *, owner_release_confirmed=False,
             timeout_s=4.0, _native_factory=None, initial_controls=None,
             restore_controls=True, _selection_index=None, cancel_token=None, _inspect_only=False,
             _expected_moniker=None):
        # Compatibility name for capture callers: OPERATOR ATTESTATION ONLY.
        # The reservation below is intra-process, not OS/global exclusivity.
        timeout_s = _timeout(timeout_s)
        if owner_release_confirmed is not True:
            raise CameraError("operator must attest existing owner release; no cross-process exclusivity is enforced")
        if not isinstance(display_name, str) or not display_name.strip() or "\0" in display_name:
            raise CameraError("exact moniker display name required; indices are not accepted")
        if (initial_controls is not None and
                (not isinstance(initial_controls, dict) or not initial_controls
                 or set(initial_controls) - PROPERTIES.keys()
                 or any(value is not None and type(value) is not int for value in initial_controls.values()))):
            raise CameraError("invalid PS4 initial control targets")
        if cancel_token is not None and not isinstance(cancel_token, CancellationToken):
            raise CameraError("shared PS4 CancellationToken required")
        if cancel_token is not None and cancel_token.is_set():
            raise CameraError("PS4 open cancelled before owner creation")
        if _inspect_only and (initial_controls is not None or _selection_index is None):
            raise CameraError("read-only inspection accepts only an explicit index and no control targets")
        if _inspect_only and (not isinstance(_expected_moniker, str)
                or not _expected_moniker.strip() or "\0" in _expected_moniker):
            raise CameraError("expected exact IMoniker::GetDisplayName required before probe; DevicePath is not accepted")
        global _active_owner
        # Check a loaded Suite's earlier startup/cleanup latch as well as the
        # native adapter reservation. Same lock order as Suite.open; never hold
        # the Suite lock while waiting on COM/open/cleanup. No import side effects.
        suite = sys.modules.get(__package__ + ".ps4_suite_source") if _inspect_only else None
        with suite._owner_lock if suite is not None else nullcontext():
            session = suite._active_session if suite is not None else None
            if session is not None and (not session.done.is_set() or session.report()["cleanup_pending"]):
                raise CameraError("internal PS4 Suite owner active or cleanup pending; no probe")
            with _reservation_lock:
                if _active_owner is not None:
                    raise CameraError("previous owned DirectShow owner active or cleanup pending; no reopen")
                source = cls(display_name, dll_path, _native_factory, initial_controls,
                             restore_controls, time.perf_counter() + timeout_s, _selection_index, cancel_token,
                             _inspect_only, _expected_moniker)
                _active_owner = source
                try:
                    source._thread.start()
                except Exception:
                    _active_owner = None
                    raise
        # Do not await the entire open deadline after the outer session was
        # stopped. The still-blocked COM owner remains reserved/retained.
        deadline = time.perf_counter() + timeout_s
        while not source._ready.wait(min(0.02, max(0, deadline - time.perf_counter()))):
            if source._stop.is_set():
                source._poison("PS4 open cancelled; native owner retained until actual cleanup")
                raise OwnedDshowOpenError(source.error, source)
            if time.perf_counter() >= deadline:
                source._poison("owned DirectShow open/first-frame deadline expired")
                raise OwnedDshowOpenError(source.error, source)
        if source._stop.is_set() and source.error is None:
            source._poison("PS4 open cancelled before returning acquisition candidate")
        if source.error:
            raise OwnedDshowOpenError(source.error, source)
        return source

    @classmethod
    def open_index(cls, index, dll_path=None, **kwargs):
        if type(index) is not int or not 0 <= index < 2**31:
            raise CameraError("explicit non-negative DirectShow selection index required")
        return cls.open("selection-index", dll_path, _selection_index=index, **kwargs)

    @classmethod
    def inspect_index(cls, index, dll_path=None, *, expected_moniker=None,
                      operator_owner_release_attested=False, timeout_s=4.0,
                      _native_factory=None, cancel_token=None):
        """Selected-filter metadata only; NEVER controls/SetFormat/connect/Run.

        expected_moniker is an exact IMoniker::GetDisplayName from prior
        metadata-only DirectShow enumeration, NOT IPropertyBag DevicePath/PnP ID.
        Operator attestation is NOT a cross-process lock or proof of external
        owner absence. Parent must check processes/legacy owners separately.
        Returns its retained source for diagnostics/cleanup inspection. Success
        has already closed the graph on its owner; a timeout carries that source
        in OwnedDshowOpenError and never releases it on the requesting thread.
        """
        source = cls.open_index(index, dll_path, owner_release_confirmed=operator_owner_release_attested,
            timeout_s=timeout_s, _native_factory=_native_factory, cancel_token=cancel_token,
            _inspect_only=True, _expected_moniker=expected_moniker)
        try:
            source.release(timeout_s=min(_timeout(timeout_s), 0.5))
        except Exception as exc:
            raise OwnedDshowOpenError(str(exc), source) from exc
        return source

    def __init__(self, display_name, dll_path, native_factory, initial_controls=None,
                 restore_controls=True, startup_deadline=None, selection_index=None, cancel_token=None,
                 inspect_only=False, expected_moniker=None):
        self._display_name, self._dll_path = display_name, dll_path
        self._native_factory = native_factory or _Native
        if type(restore_controls) is not bool:
            raise CameraError("restore_controls must be boolean")
        self._restore_controls = restore_controls
        self._initial_controls = dict(initial_controls) if initial_controls is not None else None
        self._startup_deadline = startup_deadline
        self._selection_index = selection_index
        self._inspect_only = inspect_only
        self._expected_moniker = expected_moniker
        self._negotiation_diagnostics = []
        self._stop = cancel_token if cancel_token is not None else CancellationToken()
        self._condition = threading.Condition(self._stop.lock)
        self._ready, self._done = threading.Event(), threading.Event()
        self._cleanup_attempted = threading.Event()
        self._retry_cleanup = threading.Event()
        self._commands = queue.Queue(maxsize=8)
        self._sample = None
        self._consumed = 0
        self._error = None
        self._cleanup_error = None
        self._cleanup_ok = False
        self._native = None  # kept alive if Stop/restore failed
        self._last_native_sequence = 0
        self._last_completed = float("-inf")
        self._revision = 0
        self._transaction_deadline = None
        self._startup_pending = False
        self._decoded_startup_candidate = None
        self._accepted_readiness = False
        self._pending_faults = queue.SimpleQueue()
        self._active_decision = None
        self._controls = {"requested": dict.fromkeys(PROPERTIES),
                          "transaction": {"state": "idle", "revision": 0},
                          "capture_identity_confirmed": False, "warnings": [], "errors": [],
                          **{key: dict.fromkeys(PROPERTIES) for key in
                             ("before", "set_ok", "readback", "readback_confirmed")}}
        self._thread = threading.Thread(target=self._run, name="ps4-owned-dshow", daemon=True)

    @property
    def error(self):
        with self._condition:
            self._take_faults_locked()
            return self._error

    def _take_faults_locked(self):
        while True:
            try:
                message = self._pending_faults.get_nowait()
            except queue.Empty:
                return
            if self._error is None:
                self._error = message

    def _cancel_without_wait(self, message):
        # Deadline path: no owner/session lock, no COM, no foreign close.
        self._pending_faults.put(message)
        self._stop.set()
        self._ready.set()
        if self._condition.acquire(blocking=False):
            try:
                self._take_faults_locked()
                self._sample = None
                self._condition.notify_all()
            finally:
                self._condition.release()

    @property
    def cleanup_pending(self):
        with self._condition:
            return not self._cleanup_ok

    def report(self):
        self._check_deadline()
        with self._condition:
            return {"backend": "ps4-owned-dshow-poc", "error": self._error,
                    "cleanup_pending": not self._cleanup_ok, "cleanup_error": self._cleanup_error,
                    "owner_done": self._done.is_set(), "transport_size": [1748, 408],
                    "pixel_format": "YUY2", "fps_nominal": 60,
                    "timing_source": "perf_counter_host_native_read_completion_not_exposure",
                    "controls_restore_on_close": self._restore_controls, "hardware_verified": False,
                    "readiness_accepted": self._accepted_readiness,
                    "read_only_inspection": self._inspect_only,
                    "ownership_scope": "intra_process_only_operator_attestation_no_external_exclusivity",
                    "probe_identity_kind": "IMoniker::GetDisplayName" if self._inspect_only else None,
                    "negotiation_diagnostics": copy.deepcopy(self._negotiation_diagnostics),
                    "controls": copy.deepcopy(self._controls)}

    def controls_status(self):
        self._check_deadline()
        with self._condition:
            return copy.deepcopy(self._controls)

    def _check_deadline(self):
        # Watchdog never waits for the owner. In particular, a pending status
        # sample is NOT authority to poison: commit can win immediately after
        # that sample. The same cancel/commit CAS used by RPC waiters arbitrates.
        if not self._condition.acquire(blocking=False):
            return
        try:
            if (self._active_decision is not None and
                    self._active_decision.decision.status()[0] == 1):
                self._publish_decided_locked(self._active_decision)
            if (self._transaction_deadline is not None
                    and time.perf_counter() >= self._transaction_deadline
                    and self._controls["transaction"]["state"] in
                        ("pending", "applying", "awaiting_frame", "awaiting_acceptance")):
                command = self._active_decision
                if command is not None:
                    phase = command.decision.cancel()
                    if phase == 1:
                        self._publish_decided_locked(command)
                        return  # completed success: no timeout error or stop
                    if phase not in (2, 3):
                        return  # no terminal arbitration result; never attest success
                # No decision means startup is still rollbackable, before an
                # acceptance command exists. Otherwise cancel/failure won CAS.
                self._poison("owned DirectShow transaction deadline expired; owner quarantined")
        finally:
            self._condition.release()

    def _deadline(self, command):
        if self._stop.is_set() or time.perf_counter() >= command.deadline:
            raise CameraError("owned DirectShow transaction stopped or deadline expired")

    def _observe(self, targets, after_frames):
        failures = []
        all_confirmed = True
        for name, target in targets.items():
            try:
                actual = self._native.get_control(name)
                confirmed = actual["value"] == target and actual["flags"] == 2
                with self._condition:
                    self._controls["readback"][name] = actual["value"]
                    self._controls["readback_confirmed"][name] = confirmed
                    self._controls["native_" + name].update(
                        readback=dict(actual), confirmed=confirmed, readback_after_frames=after_frames)
                all_confirmed = all_confirmed and confirmed
            except Exception as exc:
                failures.append(f"{name}: {exc}")
        if failures:
            raise CameraError("PS4 readback failed: " + "; ".join(failures))
        return all_confirmed

    def _transaction(self, command, *, startup=False):
        targets = dict(command.value)
        tx = {"revision": command.revision, "state": "applying", "requested": dict(targets),
              "error": None, "rollback_confirmed": None, "settle_frames": 0,
              "settle_sequences": [], "checkpoints": []}
        attempted = False
        with self._condition:
            self._controls["transaction"] = tx
            self._sample = None
        try:
            self._deadline(command)
            from .ps4_dshow_controls import choose_led_exposure
            snapshots = {}
            # All observations and validation precede ANY native write. The C++
            # begin primitive independently repeats ALL validation on this same
            # capture filter; a test seam is not a bypass in the actual DLL.
            for name in PROPERTIES:
                self._deadline(command)
                snapshots[name] = self._native.get_control(name)
            resolved = {}
            for name, target in targets.items():
                info = snapshots[name]
                if target is None:
                    if name == "exposure":
                        target = choose_led_exposure(info, 60)
                    else:
                        continue
                if (any(type(info.get(k)) is not int for k in
                        ("minimum", "maximum", "step", "capabilities", "value", "flags"))
                        or not info["capabilities"] & 2 or info["step"] <= 0
                        or type(target) is not int or not -(2**31) <= target < 2**31
                        or not info["minimum"] <= target <= info["maximum"]
                        or (target - info["minimum"]) % info["step"] or info["flags"] not in (1, 2)):
                    raise CameraError(f"PS4 {name} target outside native Manual range/step")
                resolved[name] = target
            if not resolved:
                raise CameraError("PS4 transaction has no controls to apply")
            with self._condition:
                tx["resolved"] = dict(resolved)
                for name, info in snapshots.items():
                    target = resolved.get(name, self._controls["requested"].get(name))
                    unchanged_confirmed = (name not in resolved and target is not None and
                                           info["value"] == target and info["flags"] == 2)
                    self._controls["native_" + name] = {
                        "range": {k: info[k] for k in ("minimum", "maximum", "step", "default", "capabilities")},
                        "target": target, "before": dict(info),
                        "readback": None if name in resolved else dict(info),
                        "confirmed": unchanged_confirmed, "set_flags": 2, "capture_identity_confirmed": True}
                    if name not in resolved:
                        self._controls["readback"][name] = info["value"]
                        self._controls["readback_confirmed"][name] = unchanged_confirmed
                for name in resolved:
                    self._controls["before"][name] = snapshots[name]["value"]
                    self._controls["set_ok"][name] = None
                    self._controls["readback_confirmed"][name] = False
            self._deadline(command)
            attempted = True
            boundary = self._native.begin_controls(resolved)
            self._last_native_sequence = max(self._last_native_sequence, boundary)
            with self._condition:
                tx["write_boundary_sequence"] = boundary
                for name in resolved:
                    self._controls["set_ok"][name] = True
            self._deadline(command)
            self._observe(resolved, 0)
            fresh = 0
            confirmed = False
            while fresh < 16:
                self._deadline(command)
                sample = self._native.read(20)
                if sample is None:
                    continue
                raw, sequence, completed = sample
                # Duplicates/pre-barrier frames NEVER advance the settle count.
                if sequence <= self._last_native_sequence:
                    continue
                self._validate_sample(sample)
                fresh += 1
                with self._condition:
                    tx["settle_frames"] = fresh
                    tx["settle_sequences"].append(sequence)
                if fresh in (4, 16):
                    confirmed = self._observe(resolved, fresh)
                    with self._condition:
                        tx["checkpoints"].append({"frames": fresh, "sequence": sequence, "confirmed": confirmed})
            if not confirmed:
                raise CameraError("PS4 transaction readback unconfirmed after 16 unique new frames")
            self._deadline(command)
            # Revalidate all properties natively and flush the final settling
            # slot. Only a strictly NEW post-commit sequence can be published.
            boundary = self._native.commit_controls()
            self._last_native_sequence = max(self._last_native_sequence, boundary)
            with self._condition:
                self._deadline(command)
                self._startup_pending = startup
                tx["verified_readback"] = {
                    name: dict(self._controls["native_" + name]["readback"]) for name in resolved}
                command.prepared = copy.deepcopy(dict(tx, state="awaiting_frame" if startup else "confirmed",
                    commit_boundary_sequence=boundary, readiness_accepted=False if startup else None))
                command.result = copy.deepcopy(command.prepared)  # preallocated before native CAS
            # No external/native call holds the Python condition. For a runtime
            # commit the C CAS publishes completed success BEFORE discarding
            # originals. Startup remains nonpersistent until its separate ACK.
            self._native.finish_controls(persist=not self._restore_controls and not startup,
                                         decision=None if startup else command.decision)
            with self._condition:
                if startup:
                    self._deadline(command)
                    self._controls["requested"].update(resolved)
                    tx.update(command.prepared)
                    self._condition.notify_all()
                else:
                    self._publish_decided_locked(command)
            return command.result
        except Exception as exc:
            rollback = {"confirmed": [], "error": None, "active": False}
            if attempted:
                try:
                    rollback = self._native.rollback_controls()
                except Exception as rollback_exc:
                    rollback["error"] = str(rollback_exc)
            with self._condition:
                for name in targets:
                    self._controls["readback_confirmed"][name] = False
                    native = self._controls.get("native_" + name)
                    if native is not None:
                        native.update(confirmed=False, rollback_confirmed=name in rollback["confirmed"])
                tx.update(state="failed", error=str(exc), rollback=rollback,
                          rollback_confirmed=(not attempted or (rollback["error"] is None and
                              (not rollback["active"] or all(name in rollback["confirmed"] for name in resolved)))))
                self._controls["errors"].append(str(exc))
            self._poison(f"PS4 transaction failed; no frames published: {exc}")
            raise

    def _validate_sample(self, sample):
        raw, sequence, completed = sample
        if (not isinstance(raw, bytes) or len(raw) != TRANSPORT_BYTES
                or type(sequence) is not int or sequence <= self._last_native_sequence
                or not math.isfinite(completed) or completed < self._last_completed):
            raise CameraError("invalid owned DirectShow transport sample")
        self._last_native_sequence, self._last_completed = sequence, completed

    def _poison(self, message):
        with self._condition:
            self._take_faults_locked()
            if self._error is None:
                self._error = str(message)
            self._sample = None
            self._stop.set()
            self._ready.set()
            self._condition.notify_all()

    def _run(self):
        global _active_owner
        try:
            if self._stop.is_set():
                raise CameraError("PS4 owner cancelled before DLL creation")
            self._native = self._native_factory(self._dll_path)
            if self._stop.is_set():
                raise CameraError("PS4 owner cancelled before capture open")
            if self._inspect_only:
                self._native.inspect_index(self._selection_index, self._expected_moniker)
                self._copy_diagnostics_owner()
                return  # finally closes; no frames/transaction/controls ever run
            if self._selection_index is None:
                self._native.open(self._display_name)
            else:
                self._native.open_index(self._selection_index)
            self._copy_diagnostics_owner()
            if self._stop.is_set():
                raise CameraError("PS4 owner cancelled while capture open was blocked; no controls applied")
            with self._condition:
                self._controls["capture_identity_confirmed"] = True
            if self._initial_controls is not None:
                self._transaction_deadline = self._startup_deadline
                self._transaction(_Command("transaction", "", self._initial_controls, self._startup_deadline), startup=True)
            while not self._stop.is_set():
                # At most one command per frame poll prevents control floods
                # starving acquisition; native read wait is only 20 ms.
                try:
                    with self._condition:
                        command = self._commands.get_nowait()
                except queue.Empty:
                    command = None
                if command is not None:
                    try:
                        if self._stop.is_set() or time.perf_counter() >= command.deadline:
                            raise CameraError("control deadline expired before execution; no write")
                        if command.operation == "get":
                            command.result = self._native.get_control(command.name)
                        elif command.operation == "accept":
                            self._accept_readiness_owner(command)
                        else:
                            # Flush the Python slot; native sequence remains
                            # honest (pre-setting in-flight samples still exist).
                            with self._condition:
                                self._sample = None
                            self._transaction(command)
                    except Exception as exc:
                        command.error = exc
                        if command.decision is not None:
                            command.decision.cancel()
                        raise
                    finally:
                        with self._condition:
                            if command.decision is not None and command.decision.status()[0] == 1:
                                self._publish_decided_locked(command)
                            else:
                                command.done.set()
                if self._stop.is_set():
                    break
                self._check_deadline()
                if self._stop.is_set():
                    break
                sample = self._native.read(20)
                if sample is None:
                    continue
                raw, sequence, completed = sample
                self._validate_sample(sample)
                with self._condition:
                    if self._stop.is_set():
                        break
                    if self._controls["transaction"]["state"] in ("pending", "applying", "failed"):
                        continue
                    if self._startup_pending:
                        self._controls["transaction"]["state"] = "awaiting_acceptance"
                    self._sample = (raw, sequence, completed)
                    self._ready.set()
                    self._condition.notify_all()
        except Exception as exc:
            self._copy_diagnostics_owner()  # cache metadata BEFORE owner cleanup; preserve original HRESULT
            self._poison(exc)
        finally:
            with self._condition:
                self._take_faults_locked()
                if self._startup_pending:
                    self._controls["transaction"].update(state="failed", error=self._error or "startup cancelled before acceptance",
                                                           readiness_accepted=False, rollback_confirmed=None)
                    for name in self._controls["transaction"].get("resolved", {}):
                        self._controls["readback_confirmed"][name] = False
                        self._controls["native_" + name]["confirmed"] = False
            while True:
                try:
                    with self._condition:
                        command = self._commands.get_nowait()
                except queue.Empty:
                    break
                command.error = CameraError(self.error or "owned DirectShow closed")
                if command.decision is not None:
                    command.decision.cancel()
                if command.operation == "transaction":
                    with self._condition:
                        self._controls["transaction"].update(state="failed", error=str(command.error),
                                                               rollback_confirmed=True)
                command.done.set()
            while True:
                try:
                    if self._native is not None:
                        self._native.close()
                    with self._condition:
                        self._cleanup_ok = True
                        self._cleanup_error = None
                        if self._startup_pending:
                            self._controls["transaction"]["rollback_confirmed"] = True
                    break
                except Exception as exc:
                    self._copy_diagnostics_owner()
                    with self._condition:
                        self._cleanup_error = str(exc)
                    self._poison(f"owned DirectShow cleanup failed: {exc}")
                    self._cleanup_attempted.set()
                    # Retain the actual COM owner APARTMENT as well as the
                    # DLL/handle. A later explicit release retries on THIS
                    # thread; no automatic retries and no foreign Release.
                    self._retry_cleanup.wait()
                    self._retry_cleanup.clear()
            self._ready.set()
            if self._cleanup_ok:
                with _reservation_lock:
                    if _active_owner is self:
                        _active_owner = None
            self._done.set()
            self._cleanup_attempted.set()
            with self._condition:
                self._condition.notify_all()

    def _copy_diagnostics_owner(self):
        """Owner-only native memory copy; report() never re-queries the device."""
        getter = getattr(self._native, "diagnostics", None)
        if getter is None:
            return  # offline fake seam, no native diagnostics ABI involved
        try:
            records = getter()
            if not isinstance(records, list) or len(records) > 512:
                raise CameraError("invalid bounded diagnostic record list")
        except Exception as exc:
            records = self._negotiation_diagnostics[-511:] + [
                {"event": "diagnostic_copy_failed", "error_type": type(exc).__name__}]
        with self._condition:
            self._negotiation_diagnostics = records

    def read_raw(self, timeout_s=0.5, *, _terminal=True, _allow_unaccepted=False):
        """Return immutable YUY2 bytes, sequence, monotonic read completion.

        A frame deadline is terminal for this PoC; no retry/reopen over a stuck
        driver. Call release and inspect cleanup_pending before any new owner.
        """
        deadline = time.perf_counter() + _timeout(timeout_s)
        with self._condition:
            while not self._stop.is_set() and not self._done.is_set():
                self._check_deadline()
                if self._stop.is_set():
                    break
                if (self._controls["transaction"]["state"] not in ("pending", "applying", "failed")
                        and (not self._startup_pending or _allow_unaccepted)
                        and self._sample is not None and self._sample[1] > self._consumed):
                    self._consumed = self._sample[1]
                    return self._sample
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
        if not _terminal and not self.error:
            return None
        self._poison(self.error or "owned DirectShow frame deadline expired or source closed")
        raise CameraError(self.error)

    def read_pair(self, timeout_s=0.5, *, module=None):
        with self._condition:
            revision = self._revision
        raw, sequence, completed = self.read_raw(timeout_s, _allow_unaccepted=True)
        try:
            left, right = decode_pair(raw, module=module)
        except Exception as exc:
            self._poison(f"PS4 decode failed before readiness acceptance: {exc}")
            raise
        with self._condition:
            if revision != self._revision or self._stop.is_set():
                return None
            if self._startup_pending:
                self._decoded_startup_candidate = (sequence, revision)
            return {"left": left, "right": right, "sequence": sequence,
                    "host_monotonic": completed, "controls_revision": revision,
                    "readiness_pending": self._startup_pending}

    def next_pair(self, timeout_s=0.5, *, module=None):
        with self._condition:
            revision = self._revision
        sample = self.read_raw(timeout_s, _terminal=False, _allow_unaccepted=True)
        if sample is None:
            return None
        raw, sequence, completed = sample
        try:
            left, right = decode_pair(raw, module=module)
        except Exception as exc:
            self._poison(f"PS4 decode failed before readiness acceptance: {exc}")
            raise
        with self._condition:
            if (revision != self._revision or self._stop.is_set() or
                    self._controls["transaction"]["state"] in ("pending", "applying", "failed")):
                return None
            if self._startup_pending:
                self._decoded_startup_candidate = (sequence, revision)
            return {"left": left, "right": right, "sequence": sequence,
                    "host_monotonic": completed, "controls_revision": revision,
                    "readiness_pending": self._startup_pending}

    def _accept_readiness_owner(self, command):
        with self._condition:
            self._deadline(command)
            if not self._startup_pending or self._decoded_startup_candidate != command.value:
                raise CameraError("PS4 readiness acceptance has no matching decoded candidate")
            sequence, revision = command.value
            if revision != self._revision:
                raise CameraError("PS4 readiness candidate belongs to a stale revision")
            command.prepared = True  # immutable, prepared BEFORE native CAS
            command.result = True
        if not self._restore_controls:
            self._native.accept_persistence(sequence, command.decision)
        else:
            # No persistence for raw experiments, but use the SAME atomic
            # outcome/notification protocol. Native validates the pending
            # persistence record and keeps originals when persist=False.
            self._native.accept_readiness_nonpersistent(sequence, command.decision)
        with self._condition:
            self._publish_decided_locked(command)

    def _publish_decided_locked(self, command):
        """Materialize an ALREADY committed atomic result. No native/COM work.

        Caller may help if owner is suspended after CAS but before notification.
        Result, state and done are published under this one condition lock.
        """
        if command.published:
            return
        if command.decision.status()[0] != 1:
            raise CameraError("cannot publish an uncommitted PS4 decision")
        if command.operation == "accept":
            sequence, _revision = command.value
            self._startup_pending = False
            self._accepted_readiness = True
            self._transaction_deadline = None
            self._controls["transaction"].update(state="confirmed", readiness_accepted=True,
                accepted_sequence=sequence, persistence_accepted=not self._restore_controls)
        else:
            self._controls["requested"].update(command.prepared["resolved"])
            self._controls["transaction"].update(command.prepared)
            self._transaction_deadline = None
        command.published = True
        if self._active_decision is command:
            self._active_decision = None
        command.done.set()  # NOT deferred to dispatcher finally/outside lock
        self._condition.notify_all()

    def _await_decided(self, command, label):
        while True:
            phase, error = command.decision.status()
            if phase != 0:
                break
            remaining = command.deadline - time.perf_counter()
            if remaining <= 0:
                break
            # Notification is only a wakeup optimization, not completion truth.
            # Observe native completion even if the owner never reaches set().
            command.done.wait(min(0.005, remaining))
        if phase == 0:
            phase = command.decision.cancel()  # atomic timeout-vs-commit CAS
            if phase == 3:
                _phase, error = command.decision.status()
        if phase == 1:
            # Completed success wins over a LATER timeout/cancel/stream error.
            # No owner lock is required to know this immutable outcome. Help
            # notification/state if immediately available; never wait past the
            # deadline on a lock held by a paused owner.
            if self._condition.acquire(blocking=False):
                try:
                    self._publish_decided_locked(command)
                finally:
                    self._condition.release()
            return command.result  # preallocated; no post-commit deepcopy/fallible notification
        message = (str(command.error) if command.error else
                   f"{label} failed: HRESULT 0x{error & 0xffffffff:08X}" if phase == 3 else
                   f"{label} deadline expired or cancelled; owner quarantined")
        self._cancel_without_wait(message)
        raise CameraError(message)

    def accept_readiness(self, sequence, revision, timeout_s=0.5):
        """Pump calls ONLY after validating/decoding its startup candidate.

        No caller-thread COM. Stop shares the lock/event from before open, so
        acceptance and cancellation have one unambiguous linearization point.
        """
        command = _Command("accept", "", (sequence, revision), time.perf_counter() + _timeout(timeout_s))
        with self._condition:
            if self._stop.is_set() or self._done.is_set():
                raise CameraError(self._error or "PS4 readiness was cancelled")
            if not self._startup_pending:
                raise CameraError("PS4 has no pending startup acceptance")
            if self._active_decision is not None:
                raise CameraError("PS4 readiness acceptance already pending")
            command.deadline = min(command.deadline, self._startup_deadline)
            self._admit_decided_locked(command)
        return self._await_decided(command, "PS4 readiness acceptance")

    def _admit_decided_locked(self, command):
        """No observable admission metadata changes until queue acceptance.

        All producers and the owner dequeue use this condition. A non-full
        queue therefore reserves capacity across decision preparation. Stop is
        independent: recheck it after registration; a later stop cancels the
        already-registered accepted command, never authorizes a late commit.
        """
        if self._commands.full():
            raise CameraError("owned DirectShow control queue full")
        command.decision = self._native.make_decision()  # pure atomic storage, no COM
        try:
            self._stop.register(command.decision)
            if self._stop.is_set() or self._done.is_set() or time.perf_counter() >= command.deadline:
                raise CameraError("owned DirectShow admission cancelled or expired before queue acceptance")
            self._commands.put_nowait(command)
        except BaseException as exc:
            command.decision.cancel()
            self._stop.unregister(command.decision)
            if isinstance(exc, queue.Full):
                raise CameraError("owned DirectShow control queue full") from exc
            raise
        self._active_decision = command

    def _submit_controls(self, controls, timeout_s=1.5):
        if (not isinstance(controls, dict) or not controls or set(controls) - PROPERTIES.keys()
                or any(type(v) is not int or not -(2**31) <= v < 2**31 for v in controls.values())):
            raise CameraError("PS4 transaction requires integer exposure/gain/brightness targets")
        command = _Command("transaction", "", dict(controls), time.perf_counter() + _timeout(timeout_s))
        with self._condition:
            if self._stop.is_set() or self._done.is_set():
                raise CameraError(self._error or "owned DirectShow is closed")
            if self._startup_pending:
                raise CameraError("PS4 acquisition readiness has not been accepted")
            if self._controls["transaction"]["state"] in ("pending", "applying"):
                raise CameraError("PS4 transaction already pending")
            command.revision = self._revision + 1
            pending = {"revision": command.revision, "state": "pending",
                       "requested": dict(controls), "error": None, "rollback_confirmed": None}
            self._admit_decided_locked(command)
            self._revision = command.revision
            self._sample = None
            self._transaction_deadline = command.deadline
            self._controls["transaction"] = pending
            self._condition.notify_all()
        return command

    def request_controls(self, controls, timeout_s=1.5):
        return self._submit_controls(controls, timeout_s).revision

    def apply_controls(self, controls, timeout_s=1.5):
        command = self._submit_controls(controls, timeout_s)
        return self._await_decided(command, "owned DirectShow control")

    def _control(self, operation, name, value=None, timeout_s=0.5):
        timeout_s = _timeout(timeout_s)
        if name not in PROPERTIES:
            raise CameraError("unsupported PS4 control")
        if operation == "set" and (type(value) is not int or not -(2**31) <= value < 2**31):
            raise CameraError("native PS4 integer control value required")
        command = _Command(operation, name, value, time.perf_counter() + timeout_s)
        # Queue and stop check atomic against owner cleanup/queue drain.
        with self._condition:
            if self._stop.is_set() or self._done.is_set():
                raise CameraError(self._error or "owned DirectShow is closed")
            try:
                self._commands.put_nowait(command)
            except queue.Full as exc:
                raise CameraError("owned DirectShow control queue full") from exc
        if not command.done.wait(max(0, command.deadline - time.perf_counter())):
            self._poison("owned DirectShow control deadline expired; owner quarantined")
            raise CameraError(self.error)
        if command.error:
            raise CameraError(str(command.error)) from command.error
        if self.error:
            raise CameraError(self.error)
        return command.result

    def get_control(self, name, timeout_s=0.5):
        return self._control("get", name, timeout_s=timeout_s)

    def set_manual(self, name, value, timeout_s=0.5):
        """Return its preallocated, verified snapshot; never a post-commit RPC.

        This is the frame-16 readback revalidated by native commit, not a new
        current-device poll which could falsely fail an already committed Set.
        """
        transaction = self.apply_controls({name: value}, timeout_s)
        return transaction["verified_readback"][name]

    def release(self, timeout_s=0.5):
        timeout_s = _timeout(timeout_s)
        with self._condition:
            self._stop.set()
            self._sample = None
            if self._cleanup_error is not None:
                self._cleanup_attempted.clear()
                self._retry_cleanup.set()
            self._condition.notify_all()
        if not self._cleanup_attempted.wait(timeout_s):
            self._poison("owned DirectShow cleanup deadline expired; no reopen")
            raise CameraError(self.error)
        if self.cleanup_pending:
            raise CameraError(self._cleanup_error or "owned DirectShow cleanup pending")


def decode_pair(raw, *, module=None):
    """Keep transport geometry byte-for-byte until OpenCV YUY2 -> BGR.

    YUY2 is top-down regardless of positive biHeight. Reuse the existing crop
    contract: each row starts at x=48; two 640x400 eyes; never half-frame split.
    This preserves the conversion choice, not a claim of tested OV580 parity.
    """
    if not isinstance(raw, bytes) or len(raw) != TRANSPORT_BYTES:
        raise CameraError("PS4 YUY2 transport must be exactly 1748x408x2 bytes")
    import numpy as np
    from .ps4_camera import split_stereo
    cv2 = import_cv2(module)
    yuy2 = np.frombuffer(raw, dtype=np.uint8).reshape(408, 1748, 2)
    bgr = cv2.cvtColor(yuy2, cv2.COLOR_YUV2BGR_YUY2)
    return split_stereo(bgr, "640x400")
