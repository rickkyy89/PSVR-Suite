"""Explicit PS4 Suite profile and read/release adapter; no UI, HID or UDP.

Eye order is a profile decision, NOT inferred from disparity. The reference
image is raw crop ``right`` and matches the pinned Suite calibration artifact.
Saved Suite/PS3 preferences are never modified by this module.

With the experimental owned gate closed, the Suite uses bounded OpenCV DShow
legacy at 1748x408/YUY2/60 (requested transport), with non-strict startup and
explicit warnings. This does not establish capture/control identity: native
targets remain forbidden. Explicit ``legacy_controls`` permit numeric OpenCV
edits only with unique OV580 enumeration at the selected index, readback and
restore on release. UI limits are profile policy, NOT measured native ranges.
Gate value ``1`` opts into owned capture with no fallback on open failure.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
import threading

from .camera_backend import CameraError
from .ps4_stereo_session import StereoSession, _number

PROFILE_PATH = "configurations/ps4_suite_source.json"
EYE_ORDER = ("right", "left")
_owner_lock = threading.Lock()
_active_session = None


class Ps4SourceOpenError(CameraError):
    """Failed startup retains its resource so callers cannot rollback over it."""

    def __init__(self, message, source, cleanup_failed=False):
        super().__init__(message)
        self.source = source
        self._cleanup_failed = cleanup_failed

    @property
    def cleanup_pending(self):
        try:
            return self._cleanup_failed or self.source.cleanup_pending
        except Exception:
            return True  # unknown ownership must not permit rollback


def _profile_controls_valid(value):
    # Measured OV580 DirectShow units, NOT PS3 sensor units. The live adapter
    # still validates the selected device's actual range/step/capabilities.
    bounds = {"brightness": (0, 8), "exposure": (-13, -1), "gain": (0, 8)}
    return (isinstance(value, dict) and set(value) == set(bounds)
            and value["brightness"] is not None
            and all((target is None and name != "brightness")
                    or (type(target) is int and bounds[name][0] <= target <= bounds[name][1])
                    for name, target in value.items()))


def load_suite_profile(root):
    """Load the one explicit PS4 profile, with auditable artifact hashes."""
    root = Path(root)
    path = root / PROFILE_PATH
    if path.stat().st_size > 65536:
        raise ValueError("PS4 Suite profile too large")
    raw = path.read_bytes()
    profile = json.loads(raw)
    expected = {"schema", "version", "source", "width", "height", "fps",
                "eye_order", "calibration_path", "rig_path", "detector", "controls"}
    if (not isinstance(profile, dict) or set(profile) != expected
            or profile["schema"] != "psvr.camera.suite_source"
            or type(profile["version"]) is not int or profile["version"] != 1
            or profile["source"] != "ps4-stereo"
            or any(type(profile[k]) is not int for k in ("width", "height", "fps"))
            or (profile["width"], profile["height"], profile["fps"]) != (640, 400, 60)
            or profile["eye_order"] != list(EYE_ORDER)
            or profile["detector"] != {"value_min": 160, "saturation_min": 90,
                                       "min_area": 20, "opening_size": 0}
             or not _profile_controls_valid(profile["controls"])):
        raise ValueError("unsupported PS4 Suite profile (no automatic fallback)")
    profile["profile_path"] = PROFILE_PATH
    profile["profile_sha256"] = hashlib.sha256(raw).hexdigest()
    for field, digest in (("calibration_path", "calibration_sha256"),
                          ("rig_path", "rig_sha256")):
        value = profile[field]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("invalid PS4 profile artifact path")
        target = (root / value).resolve()
        if not target.is_relative_to(root.resolve()):
            raise ValueError("PS4 profile artifact must be inside the repository")
        if target.stat().st_size > 256 * 1024:
            raise ValueError("PS4 profile artifact too large")
        profile[digest] = hashlib.sha256(target.read_bytes()).hexdigest()
    rig = json.loads((root / profile["rig_path"]).read_bytes())
    if rig.get("refinement", {}).get("calibration_sha256") != profile["calibration_sha256"]:
        raise ValueError("PS4 rig/calibration provenance mismatch")
    return profile


class Ps4SuiteSource:
    """One newest atomic stereo pair; reference image retains read() contract.

    ``last_pair`` is a detached pair snapshot, updated only by successful read;
    its left/right are calibration eyes, NOT the raw transport crop labels.
    No PS3 set_manual_controls API is exposed. All camera calls remain on
    StereoSession's bounded owner thread. Verified native controls own profile
    brightness and manual exposure persistently; the unverified OpenCV legacy
    transport does not apply those targets implicitly. Explicit OpenCV targets
    have separate numeric-only verification and restore semantics.
    """

    @classmethod
    def open(cls, index, width=None, height=None, fps=None, module=None,
              read_timeout_s=0.5, controls=None, root=None,
               exposure_control_factory=None, strict_controls=False,
               ready_timeout_s=4.0, owned_capture_factory=None,
                owned_dll_path=None, owned_native_factory=None, legacy_controls=None, **unused):
        ready_timeout_s = _number(ready_timeout_s, "source ready timeout", positive=True)
        if ((width is not None and width != 640)
                or (height is not None and height != 400)
                or (fps is not None and fps != 60)):
            raise CameraError("PS4 Suite requires per-eye 640x400 at 60 FPS")
        global _active_session
        with _owner_lock:
            if (_active_session is not None
                    and (not _active_session.done.is_set()
                         or _active_session.report()["cleanup_pending"])):
                raise CameraError("previous PS4 owner still active or cleanup pending; no reopen")
            if controls is None:
                base = Path(root) if root is not None else Path(__file__).resolve().parents[2]
                controls = dict(load_suite_profile(base)["controls"])
            try:
                suite_controls = {"exposure": controls["exposure"],
                                "gain": controls["gain"],
                                "brightness": controls["brightness"]}
            except (TypeError, KeyError, AttributeError) as exc:
                raise CameraError(f"invalid PS4 Suite controls: {exc}") from exc
            if not _profile_controls_valid(suite_controls):
                raise CameraError("unsupported PS4 Suite control targets")
            # Brightness is not exposure. An unspecified profile exposure must
            # not inherit the long checkerboard exposure: select a supported
            # manual exposure <= one frame (60 FPS -> -6 log2 seconds), keeping
            # more signal than -7 while still respecting the nominal interval.
            # Numeric overrides remain native PS4 values, never PS3 units.
            session = StereoSession(index, mode="640x400", fps=60,
                                    exposure=suite_controls["exposure"],
                                    gain=suite_controls["gain"],
                                    brightness=suite_controls["brightness"],
                                    restore_controls=False,
                                    exposure_policy="led_frame",
                                     exposure_control_factory=exposure_control_factory,
                                     strict_controls=strict_controls,
                                    owned_capture_factory=owned_capture_factory,
                                    owned_dll_path=owned_dll_path,
                                     owned_native_factory=owned_native_factory,
                                     legacy_controls=legacy_controls,
                                    module=module, read_timeout_s=read_timeout_s,
                                    clock=time.perf_counter)
            _active_session = session.start()
        source = cls(session, read_timeout_s)
        try:
            session.wait_ready(ready_timeout_s)
        except Exception as exc:
            cleanup_failed = False
            try:
                source.release()
            except Exception:
                cleanup_failed = True
            raise Ps4SourceOpenError(f"PS4 startup failed (selected transport only; "
                                     f"no retry or automatic transport downgrade): {exc}", source,
                                     cleanup_failed=cleanup_failed) from exc
        transport = session.report()["descriptor"] or {}
        source.descriptor.update(capture_backend=transport.get("transport", transport.get("backend")),
            capture_identity_confirmed=transport.get("capture_identity_confirmed", False))
        return source

    def __init__(self, session, read_timeout_s=0.5):
        self._session = session
        self._timeout = read_timeout_s
        self._released = False
        self._cleanup_uncertain = False
        self._access_lock = threading.RLock()
        self.last_pair = None
        self.last_frame_monotonic = None
        self.descriptor = {"backend": "ps4-stereo", "eye_size": [640, 400],
                           "transport_size": [1748, 408], "fps": 60,
                           "eye_order": list(EYE_ORDER),
                           "timing_source": "perf_counter_host_read_completion_not_exposure"}

    @property
    def error(self):
        return self._session.report()["error"]

    @property
    def cleanup_pending(self):
        return self._cleanup_uncertain or self._session.report()["cleanup_pending"]

    def read(self):
        # Linearize a delivered pair against request_controls: an accepted edit
        # clears the source snapshot and no concurrent old read can republish it.
        with self._access_lock:
            return self._read_locked()

    def _read_locked(self):
        if self._released:
            return False, None
        if self.error:
            self.last_pair = None
            return False, None
        sample = self._session.next_pair(timeout_s=self._timeout)
        if sample is None:
            self.last_pair = None
            return False, None
        # Copies detach the consumed pair from both the pump and external users.
        pair = {"left": sample[EYE_ORDER[0]].copy(),
                "right": sample[EYE_ORDER[1]].copy(),
                 "sequence": sample["sequence"],
                 "host_monotonic": sample["host_monotonic"]}
        if "controls_revision" in sample:
            pair["controls_revision"] = sample["controls_revision"]
        self.last_pair = pair
        self.last_frame_monotonic = pair["host_monotonic"]
        return True, pair["left"]

    def controls_status(self):
        """Current source controls, or None once release has been requested.

        Release invalidates availability, not the session's diagnostic history.
        Never publish a historical readback as current controls/capabilities for
        a detached source, including while owner cleanup is still pending.
        """
        if self._released:
            return None
        return self._session.report()["controls"]

    def request_controls(self, controls):
        """Latch an explicit PS4-only edit; the camera owner validates/applies it.

        Returned revision is a request identity, NOT write confirmation. Inspect
        controls_status()['transaction'] for owner readback and completion.
        No profile file is changed and no caller thread touches DirectShow.
        """
        with self._access_lock:
            if self._released:
                raise CameraError("PS4 source is released")
            revision = self._session.request_controls(controls)
            self.last_pair = self.last_frame_monotonic = None
            return revision

    def settings_capabilities(self):
        """Native measured ranges OR explicitly labelled legacy numeric policy.

        Legacy availability requires owner-enumerated unique OV580 selection,
        not native/capture identity confirmation. The two contracts never alias.
        """
        from .ps4_dshow_controls import MANUAL
        controls = self.controls_status()
        if controls is None or self.error:
            return None
        legacy = controls.get("legacy_opencv", {})
        if legacy.get("single_ps4_verified") is True:
            from .ps4_legacy_controls import CONTRACT, POLICY_BOUNDS
            return {"source": "ps4-stereo", "preference_key": "camera_profiles",
                "apply_mode": "reopen", "control_contract": CONTRACT,
                "capture_identity_confirmed": False,
                "controls": {name: {"minimum": low, "maximum": high, "step": 1,
                    "validation": "profile_policy_not_measured"}
                    for name, (low, high) in POLICY_BOUNDS.items()}}
        if controls.get("transaction", {}).get("state") in ("pending", "applying", "failed"):
            return None
        ranges = {}
        for name in ("exposure", "gain", "brightness"):
            native = controls.get("native_" + name)
            info = native.get("range") if isinstance(native, dict) else None
            if (not isinstance(info, dict)
                    or native.get("capture_identity_confirmed") is not True
                    or any(type(info.get(k)) is not int for k in
                           ("minimum", "maximum", "step", "capabilities"))
                    or info["step"] <= 0 or info["minimum"] > info["maximum"]
                    or not info["capabilities"] & MANUAL):
                return None
            ranges[name] = dict(info)
        return {"source": "ps4-stereo", "preference_key": "camera_profiles",
                 "apply_mode": "reopen", "controls": ranges}

    def release_diagnostics(self):
        """Historical restore observations, never active control capabilities."""
        report = self._session.report()
        controls = report["controls"]
        if "legacy_opencv" not in controls:
            return None
        return {"source": "ps4-stereo", "cleanup_pending": report["cleanup_pending"],
            "legacy_opencv": controls["legacy_opencv"], "before": controls["before"],
            "restored": controls["restored"], "restoration_pending": controls["restoration_pending"],
            "restoration_failed": controls["restoration_failed"]}

    def release(self):
        self._released = True
        # The worker may still be finishing the consumed pair when stop() asks
        # for cleanup from another thread. Never replace that atomic snapshot.
        try:
            return self._session.stop(timeout_s=0.75)
        except Exception:
            self._cleanup_uncertain = True
            raise
