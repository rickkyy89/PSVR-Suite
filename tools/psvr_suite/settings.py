"""Persistent suite preferences and SteamVR driver settings ownership.

Only ``driver_psvrpc`` keys owned by this suite are ever written; display,
projection and other apps are preserved (verified by hash) unless the caller
explicitly passes ``apply_optics=True``. Optics stay on ``'identity'`` by
default (never reset). IMU tuning never edits the global ``config.json``:
the baseline is exposed read-only and overrides go to a NEW derivative file,
because the driver exposes no live IMU config path.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile

from . import cameras as cameras_module
from .camera_settings import validate_camera_profiles, module_for_source

ROOT = Path(__file__).resolve().parents[2]
DRIVER_SECTION = "driver_psvrpc"
DEFAULT_VRSETTINGS = Path("C:/Program Files (x86)/Steam/config/steamvr.vrsettings")
DEFAULT_OPENVR_DLL = Path("C:/Program Files (x86)/Steam/steamapps/common/SteamVR/bin/win64/openvr_api.dll")
IMU_BASELINE_SOURCE = "config.json"
TARGET_HMD_SERIAL = "PSVRPC-HMD-0"

TRACKING_MODES = ("3dof", "6dof")
POSITION_FILTERS = ("one_euro", "kalman_initial", "kalman_optimized")
OPTICS_CURRENT = "current"
OPTICS_ALIASES = ("current", "identity", "mild", "medium", "peripheral", "stronger")
OPTICS_PROFILES = {
    "mild": "configurations/optics_radial_mild_20261014.json",
    "medium": "configurations/optics_radial_medium_20261014.json",
    "stronger": "configurations/optics_radial_stronger_20261014.json",
    "peripheral": "configurations/optics_radial_peripheral.json",
}

# Exact driver keys owned by this suite. positionFallback3DofEnabled is
# optional: omitted (driver default false) unless fallback is enabled.
OWNED_KEYS = (
    "positionEnabled",
    "imuExportEnabled",
    "positionFallback3DofEnabled",
    "positionAngularCenterEnabled",
    "positionReacquireBlendMs",
    "positionReacquireMaxSpeedMps",
    "positionInitialOffsetX",
    "positionInitialOffsetY",
    "positionInitialOffsetZ",
    "imuCalibrationMs",
    "imuStaleTimeoutMs",
    "imuMaxSampleGapUs",
)
OPTICS_KEYS = ("distortionMode", "distortionProfilePath")


class _Absent:
    pass


_ABSENT = _Absent()


def default_preferences():
    """Flat UI schema with live defaults (JSON-serialisable)."""
    prefs = {
        "tracking_mode": "6dof",
        "fallback_3dof": False,
        "two_led_experimental": False,
        "reacquire_blend_ms": 150,
        "reacquire_speed_m_s": 1.5,
        "initial_offset_m": [0.0, 0.0, 0.0],
        "head_offset_model_m": [0.0, 0.0, 0.0],
        "position_scale": 1.0,
        "position_filter": "one_euro",
        "min_cutoff_hz": 2.5,
        "beta": 8.0,
        "optics_profile": "identity",
        "tracking_config_path": "config.json",
        "imu_calibration_ms": 8000,
        "imu_stale_ms": 100,
        "imu_max_gap_us": 5000,
        "camera_source": cameras_module.DEFAULT_CAMERA_SOURCE,
    }
    prefs.update(module_for_source("pseye").default_configuration())
    return prefs


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _checked_bool(data, key):
    value = data[key]
    if type(value) is not bool:
        raise ValueError("invalid %s: bool required" % key)
    return value


def _checked_int(data, key, minimum=None, maximum=None, choices=None):
    value = data[key]
    if type(value) is not int:
        raise ValueError("invalid %s: int required" % key)
    if choices is not None and value not in choices:
        raise ValueError("invalid %s: not supported" % key)
    if minimum is not None and value < minimum:
        raise ValueError("invalid %s: below minimum" % key)
    if maximum is not None and value > maximum:
        raise ValueError("invalid %s: above maximum" % key)
    return value


def _checked_number(data, key, minimum=None, maximum=None, allow_zero=True):
    value = data[key]
    if not _finite(value):
        raise ValueError("invalid %s: finite number required" % key)
    if minimum is not None and (value < minimum or (not allow_zero and value == 0)):
        raise ValueError("invalid %s: below minimum" % key)
    if maximum is not None and value > maximum:
        raise ValueError("invalid %s: above maximum" % key)
    return value


def _checked_vector(data, key, size, minimum=None, maximum=None):
    value = data[key]
    if not isinstance(value, list) or len(value) != size or not all(_finite(v) for v in value):
        raise ValueError("invalid %s: list of %d finite numbers required" % (key, size))
    if minimum is not None and any(v < minimum for v in value):
        raise ValueError("invalid %s: below minimum" % key)
    if maximum is not None and any(v > maximum for v in value):
        raise ValueError("invalid %s: above maximum" % key)
    return list(value)


def _checked_path(data, key):
    value = data[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError("invalid %s: non-empty path required" % key)
    return value


def validate_preferences(data):
    """Validate a flat preferences dict, returning a NEW dict.

    Unknown keys, NaN/inf and out-of-range values raise ValueError; nothing
    is autocorrected or clamped. Integral FPS numbers (e.g. 75.0) are
    losslessly represented as int; fractional rates are rejected.
    """
    if not isinstance(data, dict):
        raise ValueError("preferences must be an object")
    required = set(default_preferences())
    known = required | {"camera_profiles"}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ValueError("unknown preferences: " + ", ".join(unknown))
    missing = sorted(required - set(data))
    if missing:
        raise ValueError("missing preferences: " + ", ".join(missing))
    out = {}
    if data["tracking_mode"] not in TRACKING_MODES:
        raise ValueError("invalid tracking_mode")
    out["tracking_mode"] = data["tracking_mode"]
    out["fallback_3dof"] = _checked_bool(data, "fallback_3dof")
    out["two_led_experimental"] = _checked_bool(data, "two_led_experimental")
    out["reacquire_blend_ms"] = _checked_int(data, "reacquire_blend_ms", 0, 2000)
    out["reacquire_speed_m_s"] = _checked_number(data, "reacquire_speed_m_s", 0.01, 10.0)
    out["initial_offset_m"] = _checked_vector(data, "initial_offset_m", 3, -10.0, 10.0)
    out["head_offset_model_m"] = _checked_vector(data, "head_offset_model_m", 3, -10.0, 10.0)
    out["position_scale"] = _checked_number(data, "position_scale", 0.0, None, allow_zero=False)
    if data["position_filter"] not in POSITION_FILTERS:
        raise ValueError("invalid position_filter")
    out["position_filter"] = data["position_filter"]
    out["min_cutoff_hz"] = _checked_number(data, "min_cutoff_hz", 0.0, None, allow_zero=False)
    out["beta"] = _checked_number(data, "beta", 0.0, None)
    out["min_area"] = _checked_int(data, "min_area", 1, None)
    out["value_min"] = _checked_int(data, "value_min", 0, 255)
    out["saturation_min"] = _checked_int(data, "saturation_min", 0, 255)
    out["opening_size"] = _checked_int(data, "opening_size", choices=(0, 3, 5))
    out.update(module_for_source("pseye").validate_controls(
        {key: data[key] for key in ("exposure", "gain")}))
    fps = _checked_number(data, "fps", 1)
    if int(fps) != fps:
        raise ValueError("invalid fps: positive integer required")
    out["fps"] = int(fps)
    out["width"] = _checked_int(data, "width", 1, None)
    out["height"] = _checked_int(data, "height", 1, None)
    out["intrinsicsPath"] = _checked_path(data, "intrinsicsPath")
    out["rigPath"] = _checked_path(data, "rigPath")
    out["imu_rig"] = _checked_path(data, "imu_rig")
    profile = data["optics_profile"]
    if not isinstance(profile, str) or not profile.strip():
        raise ValueError("invalid optics_profile")
    out["optics_profile"] = profile
    out["tracking_config_path"] = _checked_path(data, "tracking_config_path")
    out["imu_calibration_ms"] = _checked_int(data, "imu_calibration_ms", 500, 10000)
    out["imu_stale_ms"] = _checked_int(data, "imu_stale_ms", 20, 1000)
    out["imu_max_gap_us"] = _checked_int(data, "imu_max_gap_us", 1000, 50000)
    source = data["camera_source"]
    if not isinstance(source, str) or not source.strip():
        raise ValueError("invalid camera_source: non-empty id required")
    if not cameras_module.is_valid_camera_source(source):
        raise ValueError(
            "invalid camera_source: unknown id (known: %s)"
            % ", ".join(cameras_module.camera_source_ids()))
    out["camera_source"] = source
    if "camera_profiles" in data:
        out["camera_profiles"] = validate_camera_profiles(data["camera_profiles"])
    return out


def load_preferences(path):
    """Load prefs file over defaults; known values absent from file are kept.

    Unknown file keys raise ValueError. A missing file yields defaults.
    """
    merged = default_preferences()
    path = Path(path)
    if not path.is_file():
        return merged
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("unreadable preferences: %s" % exc) from exc
    if not isinstance(data, dict):
        raise ValueError("preferences file must hold an object")
    unknown = sorted(set(data) - (set(merged) | {"camera_profiles"}))
    if unknown:
        raise ValueError("unknown preferences: " + ", ".join(unknown))
    merged.update(data)
    return validate_preferences(merged)


def _atomic_write_json(path, obj):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix="psvr-suite-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(obj, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save_preferences(path, prefs):
    """Validate and atomically save prefs; backs up local existing file only.

    Never overwrites the accepted global IMU baseline (config.json).
    """
    cleaned = validate_preferences(prefs)
    path = Path(path)
    if path.resolve() == (ROOT / IMU_BASELINE_SOURCE).resolve():
        raise ValueError("refusing to overwrite the accepted global config; save prefs elsewhere")
    if not path.parent.is_dir():
        raise ValueError("preferences parent must exist")
    if path.is_file():
        backup = path.with_name(path.name + ".bak")
        backup.write_bytes(path.read_bytes())
    _atomic_write_json(path, cleaned)
    return path


def check_paths(prefs, root=ROOT):
    """Report referenced files missing under root (read-only, for UI)."""
    missing = []
    for key in ("intrinsicsPath", "rigPath", "imu_rig"):
        candidate = Path(prefs[key])
        target = candidate if candidate.is_absolute() else Path(root) / candidate
        if not target.is_file():
            missing.append(key)
    profile = prefs.get("optics_profile", OPTICS_CURRENT)
    mode, resolved = resolve_optics_profile(profile, root=root)
    if mode == "profile":
        target = Path(resolved) if Path(resolved).is_absolute() else Path(root) / resolved
        if not target.is_file():
            missing.append("optics_profile")
    return missing


def resolve_optics_profile(value, root=ROOT):
    """Resolve an optics alias/path to (mode, profile_path).

    ``'current'`` means no optics operation (None path). ``'identity'``
    maps to identity mode with a blank path. Other aliases resolve to
    frozen configs under root.
    """
    if value == OPTICS_CURRENT:
        return OPTICS_CURRENT, None
    if value == "identity":
        return "identity", ""
    if value in OPTICS_PROFILES:
        return "profile", str((Path(root) / OPTICS_PROFILES[value]).resolve())
    if isinstance(value, str) and value.strip():
        return "profile", value
    raise ValueError("invalid optics_profile")


def baseline_imu_config(source=None):
    """Read-only IMU baseline from the existing global config (never writes)."""
    path = Path(source or IMU_BASELINE_SOURCE)
    if not path.is_absolute():
        path = ROOT / path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("unreadable IMU baseline: %s" % exc) from exc
    tracking = (data.get("tracking") or {})
    if not isinstance(tracking, dict):
        raise ValueError("IMU baseline has no tracking section")
    return copy.deepcopy(tracking)


def derive_imu_config(prefs, dest):
    """Write a NEW native-schema derivative IMU config (baseline + overrides).

    The baseline source is never modified; dest resolving to it is refused.
    No suite metadata is added: the file stays directly usable by native code
    (or readable as a diagnostic). Driver-only gap values above 20000 us
    remain valid preferences but cannot be exported to psvr_diag; reject
    before creating dest rather than silently clamping the user's value.
    """
    cleaned = validate_preferences(prefs)
    if cleaned["imu_max_gap_us"] > 20000:
        raise ValueError("psvr_diag derivative requires imu_max_gap_us 1000..20000; "
                         "the driver supports up to 50000 separately")
    source = (ROOT / IMU_BASELINE_SOURCE).resolve()
    dest = Path(dest)
    if not dest.is_absolute():
        dest = ROOT / dest
    if dest.resolve() == source:
        raise ValueError("refusing to overwrite the global IMU baseline; pick a derivative path")
    if not dest.parent.is_dir():
        raise ValueError("derivative parent must exist")
    if dest.exists():
        raise FileExistsError("derivative config already exists: %s" % dest)
    baseline = baseline_imu_config()
    merged = dict(baseline)
    merged["calibration_ms"] = cleaned["imu_calibration_ms"]
    merged["stale_timeout_ms"] = cleaned["imu_stale_ms"]
    merged["max_sample_gap_us"] = cleaned["imu_max_gap_us"]
    payload = {"schema_version": 2,
               "diagnostic": {"log_dir": "logs", "logging_verbosity": "info"},
               "tracking": merged}
    with dest.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
    return dest


def driver_keys_for(prefs):
    """Map validated prefs to the exact owned driver_psvrpc keys.

    ``camera_source`` is host-side (which camera the tracking worker opens)
    and is never written to steamvr.vrsettings: no driver key, no mapping.
    """
    cleaned = validate_preferences(prefs)
    mapped = {
        "positionEnabled": cleaned["tracking_mode"] == "6dof",
        "positionAngularCenterEnabled": cleaned["tracking_mode"] == "6dof" and not cleaned["two_led_experimental"],
        "imuExportEnabled": True,
        "positionReacquireBlendMs": cleaned["reacquire_blend_ms"],
        "positionReacquireMaxSpeedMps": cleaned["reacquire_speed_m_s"],
        "positionInitialOffsetX": cleaned["initial_offset_m"][0],
        "positionInitialOffsetY": cleaned["initial_offset_m"][1],
        "positionInitialOffsetZ": cleaned["initial_offset_m"][2],
        "imuCalibrationMs": cleaned["imu_calibration_ms"],
        "imuStaleTimeoutMs": cleaned["imu_stale_ms"],
        "imuMaxSampleGapUs": cleaned["imu_max_gap_us"],
    }
    if cleaned["fallback_3dof"]:
        mapped["positionFallback3DofEnabled"] = True
    return mapped


def preferences_from_driver(source=None, path=None):
    """Derive UI prefs from an existing driver section + defaults.

    Optics always stays the ``'current'`` alias (never reverse-mapped).
    Missing positionEnabled follows the native 3DoF default, independently
    of the suite's fresh-setup defaults; a supplied flag must be bool.
    """
    if source is None:
        try:
            raw = Path(path).read_bytes() if path is not None else None
        except OSError as exc:
            raise FileNotFoundError("driver settings unreadable: %s" % exc) from exc
        if raw is None:
            raise ValueError("preferences_from_driver needs source or path")
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise ValueError("driver settings malformed: %s" % exc) from exc
        source = data.get(DRIVER_SECTION, {})
    if not isinstance(source, dict):
        raise ValueError("driver section must hold an object")
    prefs = default_preferences()
    # Native position input is opt-in; importing a partial driver section
    # must not inherit the suite's fresh-setup default of 6DoF.
    enabled = source.get("positionEnabled", False)
    if type(enabled) is not bool:
        raise ValueError("invalid positionEnabled: bool required")
    prefs["tracking_mode"] = "6dof" if enabled else "3dof"
    prefs["fallback_3dof"] = source.get("positionFallback3DofEnabled", False) is True
    if "positionAngularCenterEnabled" in source:
        if type(source["positionAngularCenterEnabled"]) is not bool:
            raise ValueError("invalid positionAngularCenterEnabled")
        prefs["two_led_experimental"] = enabled and not source["positionAngularCenterEnabled"]
    for pref_key, driver_key, low, high in (
            ("reacquire_blend_ms", "positionReacquireBlendMs", 0, 2000),
            ("imu_calibration_ms", "imuCalibrationMs", 500, 10000),
            ("imu_stale_ms", "imuStaleTimeoutMs", 20, 1000),
            ("imu_max_gap_us", "imuMaxSampleGapUs", 1000, 50000)):
        value = source.get(driver_key)
        if type(value) is int and low <= value <= high:
            prefs[pref_key] = value
    speed = source.get("positionReacquireMaxSpeedMps")
    if type(speed) in (int, float) and math.isfinite(speed) and 0.01 <= speed <= 10.0:
        prefs["reacquire_speed_m_s"] = speed
    offset = [source.get("positionInitialOffsetX", 0.0),
              source.get("positionInitialOffsetY", 0.0),
              source.get("positionInitialOffsetZ", 0.0)]
    if all(type(v) in (int, float) and math.isfinite(v) and -10.0 <= v <= 10.0 for v in offset):
        prefs["initial_offset_m"] = [float(v) for v in offset]
    prefs["optics_profile"] = OPTICS_CURRENT
    # Camera host-side: mai reverse-mapped dal driver (nessuna chiave driver
    # la descrive); resta il default del registro (oggi "pseye").
    prefs["camera_source"] = cameras_module.DEFAULT_CAMERA_SOURCE
    return validate_preferences(prefs)


def default_running_processes():
    """Query runtime processes; unknown state raises instead of meaning stopped."""
    # Enumerate first, then filter: an absent -Name target is itself a
    # Get-Process error, whereas an empty successful inventory is legitimate.
    script = (
        "$ErrorActionPreference = 'Stop'; try { "
        "Get-Process -ErrorAction Stop"
        " | Where-Object { $_.ProcessName -in @('vrserver', 'vrcompositor') }"
        " | ForEach-Object { $_.ProcessName }"
        " } catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }")
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise RuntimeError("SteamVR process query failed: %s" % exc) from exc
    if result.returncode != 0 or (result.stderr or "").strip():
        reason = (result.stderr or result.stdout or "no diagnostic output").strip()
        raise RuntimeError("SteamVR process query failed (exit %s): %s"
                           % (result.returncode, reason))
    names = {line.strip().lower() for line in (result.stdout or "").splitlines()
             if line.strip()}
    if names - {"vrserver", "vrcompositor"}:
        raise RuntimeError("SteamVR process query returned unexpected output")
    return names


def validate_optics_profile(path, validator=None, root=ROOT):
    """Run the native optics validator BEFORE any config write (read-only)."""
    exe = Path(validator) if validator is not None else ROOT / "out" / "diag" / "psvr_optics.exe"
    if not exe.is_file():
        raise RuntimeError("optics validator unavailable: %s" % exe)
    run = subprocess.run([str(exe), "--validate", str(path), "--json"],
                         capture_output=True, text=True, timeout=15,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if run.returncode != 0:
        raise RuntimeError("optics profile rejected: %s%s" % (run.stdout, run.stderr))
    try:
        return json.loads(run.stdout)
    except ValueError as exc:
        raise RuntimeError("optics validator output unreadable: %s" % exc) from exc


class DriverSettingStore:
    """Owns ONLY mapped driver keys inside steamvr.vrsettings.

    Everything else (display/projection/other apps, plus optics unless
    explicitly requested) is preserved and hash-verified. Every apply writes
    a raw backup plus a before/after journal; rollback restores owned keys
    only (values or absence), rejecting stale/conflicting owned keys and edits
    detected after its snapshot. Replacement uses optimistic byte comparisons,
    not an interprocess lock shared with other writers.
    """

    SCHEMA = "psvr.suite.driver_apply"

    def __init__(self, path=None, backup_root=None, running_checker=None,
                 optics_validator=None, root=ROOT):
        self.root = Path(root)
        self.path = Path(path) if path is not None else DEFAULT_VRSETTINGS
        self.explicit_backup_root = Path(backup_root) if backup_root is not None else None
        self.running_checker = running_checker or default_running_processes
        self.optics_validator = optics_validator

    def _default_backup_root(self):
        base = self.root / "logs" / "suite_backups"
        base.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        candidate = base / ("%s-%d" % (stamp, os.getpid()))
        counter = 0
        while candidate.exists():
            counter += 1
            candidate = base / ("%s-%d-%d" % (stamp, os.getpid(), counter))
        return candidate

    def _read_snapshot(self):
        """Return the exact bytes and the object decoded from that snapshot."""
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            raise FileNotFoundError("driver settings unreadable: %s" % exc) from exc
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise ValueError("driver settings malformed: %s" % exc) from exc
        if not isinstance(data, dict):
            raise ValueError("driver settings must hold an object")
        return raw, data

    def read(self):
        return self._read_snapshot()[1]

    def _require_runtime_stopped(self):
        names = self.running_checker()
        if names is None:
            raise RuntimeError("SteamVR process state is unknown; refusing driver settings write")
        if {name.lower() for name in names} & {"vrserver", "vrcompositor"}:
            raise RuntimeError("SteamVR runtime must be stopped before driver apply")

    def _preserved_hash(self, section, owned):
        rest = {k: copy.deepcopy(v) for k, v in section.items() if k not in owned}
        return hashlib.sha256(
            json.dumps(rest, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest()

    def apply(self, prefs, apply_optics=False):
        cleaned = validate_preferences(prefs)
        self._require_runtime_stopped()
        raw, settings = self._read_snapshot()
        section = settings.get(DRIVER_SECTION)
        if not isinstance(section, dict):
            section = {}
            settings[DRIVER_SECTION] = section
        mapped = driver_keys_for(cleaned)
        owned = list(OWNED_KEYS)
        if apply_optics:
            mode, profile = resolve_optics_profile(cleaned["optics_profile"], root=self.root)
            if mode != OPTICS_CURRENT:
                if mode == "profile":
                    target = Path(profile)
                    if not target.is_absolute():
                        target = self.root / target
                    if not target.is_file():
                        raise FileNotFoundError("optics profile missing: %s" % target)
                    if callable(self.optics_validator):
                        self.optics_validator(target)
                    else:
                        validate_optics_profile(target, validator=self.optics_validator,
                                                root=self.root)
                    mapped["distortionMode"] = "profile"
                    mapped["distortionProfilePath"] = str(target)
                else:
                    mapped["distortionMode"] = "identity"
                    mapped["distortionProfilePath"] = ""
                owned = list(OWNED_KEYS) + list(OPTICS_KEYS)
        before = {k: (copy.deepcopy(section[k]) if k in section else _ABSENT) for k in owned}
        for key, value in mapped.items():
            section[key] = value
        if not cleaned["fallback_3dof"]:
            section.pop("positionFallback3DofEnabled", None)
        after = {k: (copy.deepcopy(section[k]) if k in section else _ABSENT) for k in owned}
        changed = sorted(k for k in owned if not _same(before[k], after[k]))
        preserved = {app: copy.deepcopy(content) for app, content in settings.items() if app != DRIVER_SECTION}
        backup_root = self.explicit_backup_root if self.explicit_backup_root is not None else self._default_backup_root()
        if backup_root.exists():
            raise FileExistsError("backup directory must be new: %s" % backup_root)
        if not backup_root.parent.is_dir():
            raise ValueError("backup parent must exist")
        backup_root.mkdir()
        (backup_root / "settings_before.json").write_bytes(raw)
        journal = {
            "schema": self.SCHEMA,
            "utc": datetime.now(timezone.utc).isoformat(),
            "settings": str(self.path),
            "backup": str((backup_root / "settings_before.json").resolve()),
            "owned_keys": list(owned),
            "before": {k: (None if v is _ABSENT else v) for k, v in before.items()},
            "before_absent": sorted(k for k, v in before.items() if v is _ABSENT),
            "after": {k: (None if v is _ABSENT else v) for k, v in after.items()},
            "after_absent": sorted(k for k, v in after.items() if v is _ABSENT),
            "changed_keys": changed,
            "apply_optics": bool(apply_optics),
            "preserved_sha256": hashlib.sha256(
                json.dumps(preserved, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest(),
            "section_preserved_sha256": self._preserved_hash(section, owned),
            "applied": False,
        }
        (backup_root / "journal.json").write_text(
            json.dumps(journal, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        self._replace_guarded(raw, settings)
        journal["applied"] = True
        (backup_root / "journal.json").write_text(
            json.dumps(journal, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        return journal

    def _replace_guarded(self, original, settings):
        if self.path.read_bytes() != original:
            raise RuntimeError("driver settings changed concurrently; refusing overwrite")
        self._require_runtime_stopped()
        fd, name = tempfile.mkstemp(prefix="psvr-suite-", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(settings, stream, indent=3, ensure_ascii=False, allow_nan=False)
                stream.write("\n")
            if self.path.read_bytes() != original:
                raise RuntimeError("driver settings changed concurrently; refusing overwrite")
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise RuntimeError("driver settings readback failed: %s" % exc) from exc
        if loaded != settings:
            raise RuntimeError("driver settings readback mismatch; inspect backup/journal")

    def rollback(self, journal):
        """Restore owned keys from a journal; refuses when others moved first."""
        if not isinstance(journal, dict) or journal.get("schema") != self.SCHEMA:
            raise ValueError("foreign journal")
        if not journal.get("applied"):
            raise ValueError("journal was never applied")
        if Path(journal["settings"]) != self.path:
            raise ValueError("journal targets another settings file")
        # Keep the comparison bytes tied to this decoded snapshot. Re-reading
        # them later would approve a concurrent writer's bytes while replacing
        # them with our stale object, including unrelated application settings.
        raw, current = self._read_snapshot()
        section = current.get(DRIVER_SECTION)
        if not isinstance(section, dict):
            raise RuntimeError("driver section missing; refusing rollback")
        owned = list(journal.get("owned_keys", OWNED_KEYS))
        expected_after = dict(journal.get("after", {}))
        expected_absent = set(journal.get("after_absent", []))
        for key in owned:
            if key in expected_absent:
                if key in section:
                    raise RuntimeError("owned keys no longer match this journal; refusing stale rollback")
            elif key in expected_after and (key not in section or section[key] != expected_after[key]):
                raise RuntimeError("owned keys no longer match this journal; refusing stale rollback")
        if self._preserved_hash(section, owned) != journal.get("section_preserved_sha256"):
            raise RuntimeError("unrelated driver keys changed; refusing rollback")
        before = dict(journal.get("before", {}))
        before_absent = set(journal.get("before_absent", []))
        for key in owned:
            if key in before_absent:
                section.pop(key, None)
            elif key in before:
                section[key] = copy.deepcopy(before[key])
        self._replace_guarded(raw, current)
        return {"schema": "psvr.suite.driver_rollback",
                "restored_keys": sorted(k for k in owned if k in before or k in before_absent),
                "settings": str(self.path)}


def _same(first, second):
    if first is _ABSENT or second is _ABSENT:
        return first is second
    return first == second
