# SPDX-License-Identifier: MIT
"""Suite tracking worker: live PS Eye blue-LED + driver IMU 6DoF bridge.

Delegation persistente autorizzata: la Suite Tk resta l'unica owner della
preview/camera. Questo modulo riusa SOLO gli import accettati di
``tools.psvr_camera`` (namespace suite ``tools.psvr_suite``) e non modifica
optical tracker/model/calibration accettate, IMU fusion o la camera GUI
esistente.

Regole:
* Solo NumPy/OpenCV esistenti, nessuna nuova dipendenza pesante.
* Sempre vera ``DriverImuSource`` se il driver esporta; mai DiagnosticHID o
  avvii standalone.
* Preview senza driver permessa: solo detector RGB, nessuna posa/pacchetto
  inventato; con IMU fresca si imposta ``ImuLedTracker`` e si calcola PnP
  standard con stessa ROI/regola min-area adattiva e ``include_white_cores``.
* Sender UDP solo se ``tracking_mode == '6dof'`` E IMU driver realmente bound;
  metadati preservati via ``build_driver_position_packet``/``DriverPositionSender``,
  nessuna sessione inventata.
* Filtro esclusivo ``PositionPipeline``; config kalman iniziale/ottimizzata
  immutabili da JSON accettati sotto root; One Euro cutoff/beta da prefs.
* Nessun filtro di orientamento; offset testa applicato via modulo esistente.
* I thread non toccano mai Tk: ``on_event`` e' chiamato dal worker thread.
"""

from __future__ import annotations

import copy
import inspect
import json
import math
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from tools.psvr_camera.blue_leds import BlueLedConfig, BlueLedDetector
from tools.psvr_camera.calibration import load_intrinsics
from tools.psvr_camera.camera_source import open_camera_source
from tools.psvr_suite.cameras import (
    camera_source_ids,
    find_camera_source,
    is_valid_camera_source,
    resolve_camera_source,
)
from tools.psvr_suite.camera_settings import (
    module_for_source, resolve_camera_preferences, validate_camera_profiles,
)
from tools.psvr_camera.driver_imu_source import MAX_AGE_S, DriverImuSource
from tools.psvr_camera.driver_position import DriverPositionSender, build_driver_position_packet
from tools.psvr_camera.imu_led_pose import load_rotation_rig
from tools.psvr_camera.constellation_led_pose import ConstellationLedTracker as ImuLedTracker
from tools.psvr_camera.led_pose import load_led_rig
from tools.psvr_camera.pose_uncertainty import estimate_pose_covariance
from tools.psvr_camera.position_pipeline import PositionPipeline, load_position_config

__all__ = [
    "DEFAULT_PREFS",
    "IMMUTABLE_CAM_KEYS",
    "TRACKING_MODES",
    "POSITION_FILTERS",
    "GUI_PREVIEW_MAX_W",
    "GUI_PREVIEW_MAX_H",
    "default_prefs",
    "normalize_prefs",
    "resolve_dll_path",
    "TrackingWorker",
]

TRACKING_MODES = ("3dof", "6dof")
POSITION_FILTERS = ("one_euro", "kalman_initial", "kalman_optimized")
# Formato camera immutabile: cambio richiede reopen (stesso file di log).
# camera_source e' la scelta canonica suite (registro tools.psvr_suite.cameras);
# backend/index restano i parametri derivati per open_camera_source.
IMMUTABLE_CAM_KEYS = ("width", "height", "fps", "backend", "index",
                      "intrinsicsPath", "rigPath", "imu_rig", "dll", "camera_source")

SOURCE_CAMERA_KEYS = ("width", "height", "fps", "intrinsicsPath", "rigPath",
                      "imu_rig", "min_area", "value_min", "saturation_min",
                      "opening_size", "exposure", "gain")

KALMAN_INITIAL_REL = "configurations/position_kalman_experimental.json"
KALMAN_OPTIMIZED_REL = "configurations/position_kalman_optimized_20261013.json"

FRAME_EMIT_MIN_S = 0.10
GUI_PREVIEW_MAX_W = 640
GUI_PREVIEW_MAX_H = 480
IMU_REBIND_EVERY_S = 2.0


def default_prefs():
    """Default piatti dell'altro agent (JSON-serialisabili)."""
    prefs = {
        "tracking_mode": "6dof",
        "two_led_experimental": False,
        "camera_source": "pseye",
        "head_offset_model_m": [0.0, 0.0, 0.0],
        "position_scale": 1.0,
        "position_filter": "one_euro",
        "min_cutoff_hz": 2.5,
        "beta": 8.0,
    }
    prefs.update(module_for_source("pseye").default_configuration())
    return prefs


DEFAULT_PREFS = default_prefs()


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _as_int(data, key, minimum=None, maximum=None, choices=None):
    value = data[key]
    if type(value) is bool:
        raise ValueError("invalid %s: int required" % key)
    if isinstance(value, float):
        if not math.isfinite(value) or int(value) != value:
            raise ValueError("invalid %s: int required" % key)
        value = int(value)
    if type(value) is not int:
        raise ValueError("invalid %s: int required" % key)
    if choices is not None and value not in choices:
        raise ValueError("invalid %s: not supported" % key)
    if minimum is not None and value < minimum:
        raise ValueError("invalid %s: below minimum" % key)
    if maximum is not None and value > maximum:
        raise ValueError("invalid %s: above maximum" % key)
    return value


def _as_number(data, key, minimum=None, maximum=None):
    value = data[key]
    if not _finite(value):
        raise ValueError("invalid %s: finite number required" % key)
    value = float(value)
    if minimum is not None and value < minimum:
        raise ValueError("invalid %s: below minimum" % key)
    if maximum is not None and value > maximum:
        raise ValueError("invalid %s: above maximum" % key)
    return value


def normalize_prefs(prefs):
    """Normalizza prefs piatte (alias inclusi), ritorna dict nuovo.

    Chiavi extra non-tracking (fallback_3dof, optics, imu baseline, ecc.)
    sono ignorate per compatibilita' con entrambi i moduli dell'altro agent.
    Alias accettati per i percorsi e l'indice camera. Mai autocorrezioni.

    Integrazione tendina videocamera: ``camera_source`` (id del registro
    ``tools.psvr_suite.cameras``) e' la scelta canonica suite e determina
    ``backend``/``index`` per ``open_camera_source``. Se il chiamante passa
    anche ``backend``/``index`` espliciti diversi dalla sorgente, la
    normalizzazione fallisce (mai mismatch silenziosi). Senza
    ``camera_source`` vale il percorso legacy ``backend``/``index``, ma solo
    se la coppia corrisponde a una sorgente registrata (fail closed).
    """
    if not isinstance(prefs, dict):
        raise ValueError("prefs must be an object")
    merged = dict(default_prefs())
    # Alias -> chiave canonica.
    aliases = {
        "intrinsics": "intrinsicsPath",
        "intrinsics_path": "intrinsicsPath",
        "intrinsicsPath": "intrinsicsPath",
        "rig": "rigPath",
        "rig_path": "rigPath",
        "rigPath": "rigPath",
        "imu_rig_path": "imu_rig",
        "imuRig": "imu_rig",
        "imu_rig": "imu_rig",
        "camera_index": "index",
        "index": "index",
        "head_offset": "head_offset_model_m",
        "head_offset_model_m": "head_offset_model_m",
        "min_cutoff": "min_cutoff_hz",
        "min_cutoff_hz": "min_cutoff_hz",
        "position_filter": "position_filter",
        "tracking_mode": "tracking_mode",
    }
    for key, value in prefs.items():
        if key in aliases:
            merged[aliases[key]] = value
        elif key in merged:
            merged[key] = value
        # Chiavi extra dell'altro agent: ignorate (mai fallire qui).
        elif key in (
            "fallback_3dof", "reacquire_blend_ms", "reacquire_speed_m_s",
            "initial_offset_m", "optics_profile", "tracking_config_path",
            "calibration_ms", "stale_timeout_ms", "max_sample_gap_us",
            "imu_calibration_ms", "imu_stale_ms", "imu_max_gap_us",
            "tracking_config_path", "optics_profile",
            "intrinsics_path", "rig_path", "imu_rig_path",
        ):
            continue
        else:
            # Tollera anche alias snake gia' coperti sopra; resto ignorato
            # per non rompere la Suite quando l'altro agent aggiunge campi.
            # Solo percorsi/noti extra qui: ignora.
            if key in ("backend", "driver_imu_port", "position_port",
                       "max_rms_px", "log", "dll"):
                merged[key] = value
            continue
    # Backend/porte opzionali con default.
    merged.setdefault("backend", "pseye")
    merged.setdefault("index", 0)
    merged.setdefault("driver_imu_port", 4272)
    merged.setdefault("position_port", 4271)
    merged.setdefault("max_rms_px", 2.5)
    merged.setdefault("log", None)
    merged.setdefault("dll", None)

    out = {}
    if merged["tracking_mode"] not in TRACKING_MODES:
        raise ValueError("invalid tracking_mode")
    out["tracking_mode"] = merged["tracking_mode"]
    if type(merged["two_led_experimental"]) is not bool:
        raise ValueError("invalid two_led_experimental: bool required")
    out["two_led_experimental"] = merged["two_led_experimental"]

    head = merged["head_offset_model_m"]
    if isinstance(head, tuple):
        head = list(head)
    if not isinstance(head, list) or len(head) != 3 or not all(_finite(v) for v in head):
        raise ValueError("invalid head_offset_model_m: list of 3 finite numbers required")
    if any(abs(float(v)) > 10.0 for v in head):
        raise ValueError("invalid head_offset_model_m: out of range")
    out["head_offset_model_m"] = [float(v) for v in head]

    tmp = {"position_scale": merged["position_scale"]}
    out["position_scale"] = _as_number(tmp, "position_scale", minimum=0.0)
    if not out["position_scale"] > 0:
        raise ValueError("invalid position_scale: must be positive")

    filt = merged["position_filter"]
    if filt == "kalman":
        filt = "kalman_initial"
    if filt not in POSITION_FILTERS:
        raise ValueError("invalid position_filter")
    out["position_filter"] = filt

    tmp = {"min_cutoff_hz": merged["min_cutoff_hz"]}
    out["min_cutoff_hz"] = _as_number(tmp, "min_cutoff_hz", minimum=0.0)
    if not out["min_cutoff_hz"] > 0:
        raise ValueError("invalid min_cutoff_hz: must be positive")
    tmp = {"beta": merged["beta"]}
    out["beta"] = _as_number(tmp, "beta", minimum=0.0)

    for key in ("min_area", "value_min", "saturation_min", "opening_size",
                "exposure", "gain", "width", "height"):
        tmp = {key: merged[key]}
        if key == "min_area":
            out[key] = _as_int(tmp, key, minimum=1)
        elif key in ("value_min", "saturation_min"):
            out[key] = _as_int(tmp, key, minimum=0, maximum=255)
        elif key == "opening_size":
            out[key] = _as_int(tmp, key, choices=(0, 3, 5))
        elif key == "exposure":
            out[key] = _as_int(tmp, key, minimum=0, maximum=255)
        elif key == "gain":
            out[key] = _as_int(tmp, key, minimum=0, maximum=63)
        else:
            out[key] = _as_int(tmp, key, minimum=1)
    tmp = {"fps": merged["fps"]}
    fps_raw = merged["fps"]
    if type(fps_raw) is bool:
        raise ValueError("invalid fps: int required")
    if isinstance(fps_raw, float):
        if not math.isfinite(fps_raw) or int(fps_raw) != fps_raw:
            raise ValueError("invalid fps: int required")
        fps_raw = int(fps_raw)
    if type(fps_raw) is not int or fps_raw < 1:
        raise ValueError("invalid fps: positive int required")
    out["fps"] = fps_raw

    for key in ("intrinsicsPath", "rigPath", "imu_rig"):
        value = merged[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("invalid %s: non-empty path required" % key)
        out[key] = value

    has_source = isinstance(prefs, dict) and "camera_source" in prefs
    has_legacy = isinstance(prefs, dict) and any(
        k in prefs for k in ("backend", "index", "camera_index"))
    if has_source:
        source = merged["camera_source"]
        if not isinstance(source, str) or not source.strip():
            raise ValueError("invalid camera_source: non-empty id required")
        if not is_valid_camera_source(source):
            raise ValueError(
                "invalid camera_source: unknown id (known: %s)"
                % ", ".join(camera_source_ids()))
        resolved = resolve_camera_source(source)
        if has_legacy:
            legacy_backend = merged["backend"]
            legacy_index = merged["index"]
            norm_legacy = (None if legacy_backend is None
                           else str(legacy_backend).strip().lower())
            if norm_legacy in ("auto",):
                norm_legacy = "any"
            norm_expected = str(resolved["backend"]).strip().lower()
            if norm_legacy != norm_expected or int(legacy_index) != int(resolved["index"]):
                raise ValueError(
                    "camera_source %r conflicts with explicit backend/index" % source)
        out["camera_source"] = source
        out["backend"] = str(resolved["backend"])
        out["index"] = int(resolved["index"])
    else:
        backend = merged["backend"]
        if backend is not None and (not isinstance(backend, str) or not backend.strip()):
            raise ValueError("invalid backend")
        tmp = {"index": merged["index"]}
        legacy_index = _as_int(tmp, "index", minimum=0)
        matched = find_camera_source(backend, legacy_index)
        if matched is None:
            raise ValueError(
                "backend/index combination is not a registered camera_source "
                "(known: %s); pass camera_source explicitly"
                % ", ".join(camera_source_ids()))
        out["camera_source"] = matched
        out["backend"] = backend if backend is None else str(backend)
        out["index"] = legacy_index
    tmp = {"driver_imu_port": merged["driver_imu_port"]}
    out["driver_imu_port"] = _as_int(tmp, "driver_imu_port", minimum=1, maximum=65535)
    tmp = {"position_port": merged["position_port"]}
    out["position_port"] = _as_int(tmp, "position_port", minimum=1, maximum=65535)
    tmp = {"max_rms_px": merged["max_rms_px"]}
    out["max_rms_px"] = _as_number(tmp, "max_rms_px", minimum=0.0)
    if not out["max_rms_px"] > 0:
        raise ValueError("invalid max_rms_px: must be positive")
    log = merged["log"]
    if log is not None and (not isinstance(log, str) or not log.strip()):
        raise ValueError("invalid log: non-empty path required")
    out["log"] = log
    dll = merged["dll"]
    if dll is not None and (not isinstance(dll, str) or not dll.strip()):
        raise ValueError("invalid dll: non-empty path required")
    out["dll"] = dll
    for preference_key in ("source_preferences", "camera_profiles"):
        if preference_key not in prefs:
            continue
        if preference_key == "camera_profiles":
            out[preference_key] = validate_camera_profiles(prefs[preference_key])
            continue
        entries = prefs[preference_key]
        if not isinstance(entries, dict):
            raise ValueError("%s must be an object" % preference_key)
        for source_id, values in entries.items():
            if not is_valid_camera_source(source_id) or not isinstance(values, dict):
                raise ValueError("invalid %s source" % preference_key)
            allowed = (set(SOURCE_CAMERA_KEYS) if source_id == "pseye" else
                       {"exposure", "gain", "brightness"} if source_id == "ps4-stereo" else
                       {"width", "height", "fps", "intrinsicsPath", "rigPath", "imu_rig"})
            if set(values) - allowed:
                raise ValueError("unsupported source_preferences fields for %s" % source_id)
        out[preference_key] = copy.deepcopy(entries)
    return out


def _resolve_under(root, value):
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate
    return Path(root) / candidate


def _source_preferences(root, requested):
    """Resolve detached source-owned controls/optics; never rewrite legacy prefs."""
    projected = copy.deepcopy(requested)
    source_id = requested["camera_source"]
    overrides = copy.deepcopy(requested.get("source_preferences", {}).get(source_id, {}))
    saved = requested.get("camera_profiles", {}).get(source_id, {})
    if source_id == "ps4-stereo" and saved:
        saved = saved["controls"]
    if any(k in overrides and overrides[k] != v for k, v in saved.items()):
        raise ValueError("conflicting source_preferences/camera_profiles targets")
    overrides.update(copy.deepcopy(saved))
    if source_id == "pseye":
        projected.update(overrides)
        projected = normalize_prefs(projected)
    elif overrides:
        # source_preferences remains a transient backend compatibility input;
        # camera_profiles is the one canonical UI/persistence carrier.
        projected.setdefault("camera_profiles", {})[source_id] = (
            {"controls": overrides} if source_id == "ps4-stereo" else overrides)
    effective, profile = resolve_camera_preferences(root, projected)
    if profile is not None and profile.get("control_overrides"):
        profile["controls_explicit"] = True
    return effective, profile


class _LegacyReopenSettings(dict):
    """Transient owner-bound request metadata, never part of saved preferences."""

    def __init__(self, prefs, owner):
        super().__init__(prefs)
        self.legacy_owner = owner


def _open_effective_camera(root, prefs, profile):
    """Open exactly the resolved snapshot, never reload a drifting PS4 profile."""
    # PS4 has its own ABI-v2 graph DLL, explicitly supplied outside PS3 prefs.
    # Never route the PS3 dll preference into the PS4 capture or auto-build it.
    dll_path = (None if prefs.get("camera_source") == "ps4-stereo"
                else resolve_dll_path(root, prefs.get("dll")))
    kwargs = {}
    if profile is not None:
        kwargs.update(controls=copy.deepcopy(profile["controls"]),
                       strict_controls=bool(profile.get("controls_explicit", False)))
        # Legacy writes are a distinct, explicit source-keyed UI payload. Never
        # turn the default Suite profile into implicit OpenCV target application.
        import os
        if (profile.get("control_overrides") and os.environ.get("PSVR_PS4_DSHOW_ENABLE") != "1"):
            kwargs["legacy_controls"] = copy.deepcopy(profile["control_overrides"])
    return open_camera_source(
        prefs["index"], backend=prefs["backend"], width=prefs["width"],
        height=prefs["height"], fps=prefs["fps"],
        dll_path=str(dll_path) if dll_path else None,
        read_timeout_s=0.5, max_consecutive_failures=10, **kwargs)


def _camera_metadata(requested, effective, profile):
    """Log caller preferences versus resolved configuration, not device state.

    PS4 control targets come from its source profile, not saved PS3 controls.
    These values do not establish write acceptance, Manual flags or readback;
    actual observations/confirmation remain in each pose's camera_controls.
    A None profile target stays unspecified here, even if the source later
    resolves it against the device range during acquisition.
    """
    keys = ("camera_source", "backend", "index", "width", "height", "fps",
            "intrinsicsPath", "rigPath", "imu_rig", "min_area", "value_min",
            "saturation_min", "opening_size", "exposure", "gain")
    actual = {k: effective[k] for k in keys}
    if profile is not None:
        actual.update({k: profile["controls"][k]
                       for k in ("exposure", "gain", "brightness")})
        actual.update(imu_rig=None,
                      eye_order=list(profile["eye_order"]),
                      coordinate_frame="calibration_left_camera")
    return {"requested_camera": {k: requested[k] for k in keys},
            "effective_camera": actual, "source_profile": profile}


def _ps4_publication_failure(captured_at, binding, imu_source):
    """Check processing age and activation identity without rebinding a pose.

    Capture-time freshness alone is insufficient after free stereo PnP or
    covariance/filter work. A newer IMU sample only checks activation state;
    it must NEVER replace the sample associated with this optical observation.
    """
    try:
        latest = imu_source.latest() if imu_source is not None else None
    except Exception:
        latest = None
    # Sample the clock after latest(): even time spent obtaining its snapshot
    # counts against the original observation's publication budget.
    now = time.perf_counter()
    if not _finite(captured_at) or not 0 <= now - captured_at <= MAX_AGE_S:
        return "ps4_stereo_processing_expired"
    if (not isinstance(binding, dict) or binding.get("valid") is not True
            or binding.get("connected", True) is not True
            or not _finite(binding.get("monotonic_s"))
            or not 0 <= now - binding["monotonic_s"] <= MAX_AGE_S):
        return "ps4_stereo_original_imu_expired"
    if (not isinstance(latest, dict) or latest.get("valid") is not True
            or latest.get("connected", True) is not True
            or not binding.get("session_id")
            or latest.get("session_id") != binding.get("session_id")
            or latest.get("recenter_epoch") != binding.get("recenter_epoch")):
        return "ps4_stereo_driver_binding_changed_or_invalid"
    return None


def _reject_ps4_publication(pose, pipeline, sender, reason):
    if pose is not None:
        pose.valid = False
        pose.reason = reason
    # In particular clear One Euro history, rather than retain its brief hold.
    pipeline.reset(reason)
    if sender is not None:
        try:
            sender.invalidate()
        except Exception:
            pass


def _load_optical_setup(root, prefs, profile):
    if profile is not None:
        from tools.psvr_camera.stereo_calibration import load_stereo_calibration
        from tools.psvr_camera.stereo_head_pose import load_front_led_rig
        from tools.psvr_camera.ps4_suite_tracker import Ps4SuiteTracker
        calibration = load_stereo_calibration(
            _resolve_under(root, prefs["intrinsicsPath"]),
            expected_image_size=(prefs["width"], prefs["height"]),
            expected_mode="640x400")
        if not calibration.validation or calibration.validation.get("accepted") is not True:
            raise ValueError("PS4 Suite requires accepted held-out stereo calibration")
        rig = load_front_led_rig(_resolve_under(root, prefs["rigPath"]))
        tracker = Ps4SuiteTracker(calibration, rig,
                                  allow_two_led=prefs["two_led_experimental"])
        # Five real front points only; no fabricated nine-point lateral rig.
        return (calibration.left, rig.to_dict(), rig.object_points_m,
                rig.to_dict(), rig.object_points_m, None, list(rig.labels), tracker)
    intrinsics = load_intrinsics(
        str(_resolve_under(root, prefs["intrinsicsPath"])),
        expected_image_size=(prefs["width"], prefs["height"]))
    front_rig, front_points = load_led_rig(str(_resolve_under(root, prefs["rigPath"])))
    rot_rig, rot_points, rot_normals = load_rotation_rig(
        str(_resolve_under(root, prefs["imu_rig"])))
    rot_labels = list(rot_rig["labels"])
    tracker = ImuLedTracker(intrinsics, np.asarray(rot_points, float),
                            np.asarray(rot_normals, float), rot_labels,
                            prefs["max_rms_px"],
                            allow_two_led=prefs["two_led_experimental"])
    return (intrinsics, front_rig, front_points, rot_rig, rot_points,
            rot_normals, rot_labels, tracker)


def resolve_dll_path(root, prefs_dll=None):
    """Risolvi DLL nativa: esplicita (root-relative) o default lookup.

    Ritorna None per usare il lookup standard di ``pseye_backend`` (env +
    directory root/pseye-based).
    """
    if prefs_dll:
        candidate = Path(prefs_dll)
        if not candidate.is_absolute():
            candidate = Path(root) / candidate
        return candidate
    return None


def _default_log_path(root):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    name = "suite_tracking_%s_%s.jsonl" % (stamp, uuid.uuid4().hex[:8])
    return Path(root) / "logs" / name


def _load_kalman_table(root):
    """Carica config kalman immutabili accettate; mancanti -> {}."""
    table = {}
    for name, rel in (("kalman_initial", KALMAN_INITIAL_REL),
                      ("kalman_optimized", KALMAN_OPTIMIZED_REL)):
        try:
            table[name] = load_position_config(str(_resolve_under(root, rel)))
        except (OSError, ValueError):
            table[name] = {}
    return table


def _pipeline_key(prefs):
    if prefs["position_filter"] == "one_euro":
        return ("one_euro", float(prefs["min_cutoff_hz"]), float(prefs["beta"]))
    return (prefs["position_filter"],)


def _make_pipeline(prefs, kalman_table):
    if prefs["position_filter"] == "one_euro":
        return PositionPipeline(
            "one_euro",
            one_euro={"min_cutoff_hz": float(prefs["min_cutoff_hz"]),
                      "beta": float(prefs["beta"])},
        )
    cfg = dict(kalman_table.get(prefs["position_filter"], {}) or {})
    return PositionPipeline(
        "kalman",
        kalman=dict(cfg.get("kalman", {})),
        uncertainty=dict(cfg.get("uncertainty", {})),
        prediction_output=cfg.get("prediction_output", "camera"),
    )


def _cap_handle_abandoned(cap) -> bool:
    """True se la release ha leakato l'handle nativo (pseye/OpenCV).

    Entrambi i backend espongono ``_abandoned`` (``PseyeCamera`` aggiunge la
    property ``abandoned``); dopo il leak il reader thread e' ancora dentro
    la DLL e riaprire sullo stesso device rischia access violation nativa.
    Il chiamante deve rifiutare il reopen con errore esplicito invece di
    riusare l'handle. Mai sollevare qui: un cap mock/Fake senza flag vale
    non-abbandonato.
    """
    if cap is None:
        return False
    try:
        if bool(getattr(cap, "_abandoned", False)):
            return True
    except Exception:
        pass
    try:
        abandoned = getattr(cap, "abandoned", False)
        if callable(abandoned):
            try:
                return bool(abandoned())
            except Exception:
                return False
        return bool(abandoned)
    except Exception:
        return False


def _encode_ppm(frame):
    try:
        arr = np.asarray(frame)
        if arr.ndim != 3 or arr.shape[2] != 3 or arr.dtype != np.uint8:
            return b""
        height, width = int(arr.shape[0]), int(arr.shape[1])
        if height <= 0 or width <= 0:
            return b""
        rgb = arr[:, :, ::-1].tobytes()
        return ("P6\n%d %d\n255\n" % (width, height)).encode("ascii") + rgb
    except (TypeError, ValueError):
        return b""


def _gui_preview_size(width, height,
                      max_w=GUI_PREVIEW_MAX_W, max_h=GUI_PREVIEW_MAX_H):
    """Dimensione preview full-source 640x480, mai upscale.

    Il worker mantiene la sorgente 640x480 (nessuna perdita di punti);
    il fit continuo smooth avviene in GUI (app._refresh_preview)."""
    try:
        width = int(width)
        height = int(height)
    except (TypeError, ValueError):
        return 0, 0
    if width <= 0 or height <= 0:
        return 0, 0
    scale = min(float(max_w) / float(width), float(max_h) / float(height), 1.0)
    if not math.isfinite(scale) or scale <= 0:
        return width, height
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def _resize_nearest(frame, out_w, out_h):
    """Downscale nearest-neighbor solo NumPy (nessuna nuova dipendenza)."""
    try:
        arr = np.asarray(frame)
        if arr.ndim != 3 or arr.shape[2] != 3:
            return frame
        in_h, in_w = int(arr.shape[0]), int(arr.shape[1])
        out_w, out_h = int(out_w), int(out_h)
        if in_w <= 0 or in_h <= 0 or out_w <= 0 or out_h <= 0:
            return frame
        if (in_w, in_h) == (out_w, out_h):
            return arr
        ys = np.linspace(0, in_h - 1, out_h).astype(int)
        xs = np.linspace(0, in_w - 1, out_w).astype(int)
        return arr[ys][:, xs]
    except (TypeError, ValueError, IndexError):
        return frame


def _overlay_preview_labels(annotated, cv2mod, pose, roi):
    """Overlay solo visualizzazione: ROI + etichette LED, niente filtri/posa.

    Riutilizza il modulo cv2 già posseduto dal detector (nessuna nuova
    dipendenza). Errori ignorati: la preview non deve mai rompere il tracking.
    Sorgente 640x480 invariata; aspect preservato dal resize esistente.
    """
    try:
        if cv2mod is None:
            return annotated
        if roi is not None:
            try:
                x0, y0, x1, y1 = (int(v) for v in roi)
                cv2mod.rectangle(annotated, (x0, y0), (x1, y1), (255, 180, 0), 1)
            except (TypeError, ValueError):
                pass
        if pose is None or not bool(getattr(pose, "valid", False)):
            return annotated
        try:
            labels = list(getattr(pose, "led_labels", None) or [])
        except (TypeError, ValueError):
            return annotated
        if not labels:
            return annotated
        try:
            pts = np.asarray(getattr(pose, "image_points"), float).reshape(-1, 2)
        except (TypeError, ValueError):
            return annotated
        for lab, xy in zip(labels, pts):
            try:
                x = int(float(xy[0]))
                y = int(float(xy[1]))
            except (TypeError, ValueError):
                continue
            try:
                text = {"centre": "C", "upper_left": "UL", "upper_right": "UR",
                        "lower_left": "LL", "lower_right": "LR"}.get(str(lab), str(lab))
            except Exception:
                continue
            if not text:
                continue
            try:
                cv2mod.putText(annotated, text, (x + 6, y - 6),
                               cv2mod.FONT_HERSHEY_SIMPLEX, 0.45,
                               (0, 255, 255), 1, cv2mod.LINE_AA)
            except Exception:
                try:
                    cv2mod.putText(annotated, text, (x + 6, y - 6),
                                   cv2mod.FONT_HERSHEY_SIMPLEX, 0.45,
                                   (0, 255, 255), 1)
                except Exception:
                    pass
        return annotated
    except Exception:
        return annotated


def _compute_mode_actual(requested, imu_bound, fresh, outgoing_valid):
    """Stato camera reale, mai fallback driver inventato.

    Il worker camera NON dichiara fallback driver (authoritative backend).
    Ritorna (mode_actual, fallback_active) con fallback None = sconosciuto.
    """
    if not imu_bound:
        return "await_source", None
    if not fresh:
        return "invalid_imu", None
    if requested == "3dof":
        # Input 3dof valido, output posizione none by design.
        return "3dof_input", None
    if outgoing_valid:
        return "6dof", False
    return "position_missing", None


class TrackingWorker:
    """Worker tracking Suite: owned thread, bounded setup, throttle ~10Hz.

    ``root`` e' la radice repo per percorsi relativi/log/DLL. ``on_event``
    e' chiamato dal worker thread (mai Tk): accetta sia ``on_event(event)``
    con ``event['kind']`` sia ``on_event(kind, payload)`` (Suite Tk).
    """

    camera_profile_contract = 1

    def __init__(self, root, on_event=None):
        self._root = Path(root)
        self._on_event = on_event
        self._two_arg = False
        if on_event is not None:
            try:
                params = list(inspect.signature(on_event).parameters.values())
                kinds = [p.kind for p in params]
                positional = [p for p in params
                              if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
                if len(positional) >= 2 and all(
                        k in (inspect.Parameter.POSITIONAL_ONLY,
                              inspect.Parameter.POSITIONAL_OR_KEYWORD)
                        for k in kinds[:2]):
                    # Bound method (kind, payload) -> 2 posizionali.
                    self._two_arg = len(positional) == 2
                elif len(positional) == 1:
                    self._two_arg = False
                else:
                    self._two_arg = False
            except (TypeError, ValueError):
                self._two_arg = False
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._settings = queue.Queue(maxsize=1)
        self._realign = threading.Event()
        self._recenter = threading.Event()
        self._cap = None
        self._retired_cap = None
        self._retired_uncertain = False
        self._stopping = False
        self._control_edits = queue.Queue(maxsize=1)
        self._imu_source = None
        self._sender = None
        self._status = {
            "running": False,
            "prefs": None,
            "captured_frames": 0,
            "valid_frames": 0,
            "read_failures": 0,
            "fps": 0.0,
            "log": None,
            "error": None,
            "udp_enabled": False,
            "imu_bound": False,
            "mode_requested": None,
            "mode_actual": None,
            "fallback_active": None,
            "diagnostics": {},
            "imu": {},
            "estimated_position_m": None,
            "position_mode": None,
            "preview_ppm": None,
            "telemetry": None,
            "state": "stopped",
            "camera_controls": None,
            "camera_controls_source": None,
            "camera_source": None,
        }

    # -- emit ------------------------------------------------------------
    def _emit(self, kind, payload):
        cb = self._on_event
        if cb is None:
            return
        try:
            if self._two_arg:
                cb(kind, dict(payload))
            else:
                event = {"kind": kind}
                if isinstance(payload, dict):
                    event.update(payload)
                cb(event)
        except Exception:
            pass

    # -- public ----------------------------------------------------------
    def start(self, prefs):
        cleaned = normalize_prefs(prefs)
        with self._lock:
            if self._stopping:
                raise RuntimeError("tracking stop/cleanup is still active")
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("tracking already running")
            if self._retired_cap is not None:
                if (self._retired_uncertain or _cap_handle_abandoned(self._retired_cap)
                        or bool(getattr(self._retired_cap, "cleanup_pending", True))):
                    raise RuntimeError("previous camera owner cleanup pending; no restart")
                self._retired_cap = None
            self._stop.clear()
            while not self._settings.empty():
                try:
                    self._settings.get_nowait()
                except queue.Empty:
                    break
            self._realign.clear()
            self._recenter.clear()
            while not self._control_edits.empty():
                try:
                    self._control_edits.get_nowait()
                except queue.Empty:
                    break
            self._status.update(running=True, prefs=copy.deepcopy(cleaned),
                                captured_frames=0, valid_frames=0,
                                read_failures=0, fps=0.0, log=None,
                                error=None, udp_enabled=False,
                                imu_bound=False,
                                mode_requested=cleaned["tracking_mode"],
                                mode_actual=None, fallback_active=None,
                                diagnostics={}, imu={},
                                estimated_position_m=None,
                                position_mode=cleaned["position_filter"],
                                 preview_ppm=None, telemetry=None,
                                 camera_controls=None, camera_controls_source=None,
                                 camera_source=None, camera_settings_capabilities=None,
                                 control_edit=None,
                                state="starting")
            thread = threading.Thread(target=self._run, args=(cleaned,),
                                      daemon=True, name="suite-tracking")
            self._thread = thread
            # B11-06: start sotto lo stesso lock dell'assegnazione, cosi'
            # stop() non puo' fotografare un thread mai avviato (join su
            # thread non avviato alzerebbe, ingoiato, con True illusorio
            # mentre il worker partirebbe orfano dopo). Lo start non blocca;
            # _run attende il lock in _set_status.
            thread.start()
        return True

    def update_settings(self, prefs, *, force_legacy_reopen=False):
        if type(force_legacy_reopen) is not bool:
            raise ValueError("force_legacy_reopen must be boolean")
        with self._lock:
            target_thread = self._thread
            if (self._stopping or self._stop.is_set()
                    or target_thread is None or not target_thread.is_alive()):
                raise RuntimeError("tracking not running")
        cleaned = normalize_prefs(prefs)
        # B11-08: get_nowait+put_nowait sotto lo stesso lock del controllo
        # di vitalita', cosi' i produttori concorrenti sono serializzati
        # (get+put atomici fra loro) e nessuno puo' intercalarsi fra il
        # controllo e l'accodamento. La normalizzazione resta fuori dal
        # lock (puo' alzare senza tenere risorse).
        with self._lock:
            if (self._stopping or self._stop.is_set()
                    or self._thread is not target_thread
                    or self._thread is None or not self._thread.is_alive()):
                raise RuntimeError("tracking not running")
            if force_legacy_reopen:
                capabilities = self._status.get("camera_settings_capabilities") or {}
                if (self._status.get("state") != "running"
                        or self._status.get("running") is not True
                        or self._status.get("camera_source") != "ps4-stereo"
                        or cleaned.get("camera_source") != "ps4-stereo"
                        or capabilities.get("control_contract") != "opencv-nonstrict-single-ps4"
                        or self._cap is None
                        or not cleaned.get("camera_profiles", {}).get("ps4-stereo", {}).get("controls")):
                    raise RuntimeError("explicit legacy reopen requires the connected single-PS4 source")
                cleaned = _LegacyReopenSettings(cleaned, self._cap)
                self._status["control_edit"] = None  # retire any earlier rejected request
            try:
                self._settings.put_nowait(cleaned)
            except queue.Full:
                try:
                    self._settings.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._settings.put_nowait(cleaned)
                except queue.Full:
                    pass
        return True

    def realign(self):
        """Segnala riallineamento al worker (solo documentazione B11-08).

        Imposta soltanto l'evento consumato dal loop del worker (reset
        tracker/filtro, vedi ``_run``); non sospende/joina il thread e non
        solleva se il worker e' fermo (nessun guard qui per accordo con i
        chiamanti Tk: cambiare il comportamento richiede decisione
        condivisa, non e' parte di questo intervento meccanico).
        """
        self._realign.set()
        return True

    def apply_source_controls(self, source_id, controls):
        """Queue an explicit PS4 edit for the current resource, not just its ID.

        Returns a request identity. Completion/owner readback is published in
        status()['control_edit'] and status()['camera_controls']; no preference
        file is written. A source switch invalidates queued edits, even if it
        switches back to the same source ID before the edit is consumed.
        """
        if source_id != "ps4-stereo":
            raise ValueError("explicit source controls currently require ps4-stereo")
        if (not isinstance(controls, dict) or not controls
                or set(controls) - {"exposure", "gain", "brightness"}
                or any(type(v) is not int for v in controls.values())):
            raise ValueError("PS4 controls require integer native targets")
        request_id = uuid.uuid4().hex
        with self._lock:
            current = self._status.get("effective_camera") or {}
            if (self._stopping or self._thread is None or not self._thread.is_alive()
                    or self._stop.is_set() or self._status.get("state") != "running"
                    or self._status.get("running") is not True
                    or self._status.get("camera_source") != source_id
                    or self._cap is None or current.get("camera_source") != source_id):
                raise RuntimeError("requested camera owner is not active")
            edit = self._status.get("control_edit")
            if isinstance(edit, dict) and edit.get("state") in ("pending", "applying"):
                raise RuntimeError("camera controls transaction already active")
            try:
                self._control_edits.put_nowait((request_id, self._cap, dict(controls)))
            except queue.Full as exc:
                raise RuntimeError("camera controls request already queued") from exc
        return request_id

    def reset_relative(self):
        """Segnala reset relativo al worker (solo documentazione B11-08).

        Come ``realign``: imposta soltanto l'evento consumato dal loop del
        worker (azzeramento origine, vedi ``_run``), senza guard sullo stato
        del thread. Nessun cambiamento di comportamento qui.
        """
        self._recenter.set()
        return True

    def status(self):
        with self._lock:
            restore = self._status.get("camera_restore")
            retired = getattr(self, "_retired_cap", None)
            refresh_restore = isinstance(restore, dict) and restore.get("cleanup_pending") is True
        if refresh_restore and retired is not None:
            self._publish_camera_restore(retired)  # read-only late owner completion
        with self._lock:
            return copy.deepcopy(self._status)

    def stop(self, timeout=5.0):
        """Segnala stop, join limitato, abort DLL se il reader e' bloccato.

        Chiude solo risorse possedute (sender invalid, poi cap, poi source).
        Ritorna True a worker terminato, False se il worker resta vivo dopo
        il join: in quel caso lo status resta running=True (mai finto stop).

        B11-08: ``timeout`` e' validato prima del join, fuori dal try del
        join stesso: deve essere un numero finito >= 0, altrimenti
        ``ValueError`` (mai silenziato in False/True illusori).
        """
        # B11-08: validazione fuori dal try del join; float() copre int e
        # stringhe numeriche, bool resta 0.0/1.0 (innocuo per join).
        try:
            timeout_s = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "invalid timeout: finite number >= 0 required") from exc
        if not math.isfinite(timeout_s) or timeout_s < 0:
            raise ValueError("invalid timeout: finite number >= 0 required")
        thread = None
        with self._lock:
            if self._stopping:
                return False
            self._stopping = True
            self._stop.set()
            thread = self._thread
        if thread is not None:
            try:
                thread.join(timeout=timeout_s)
            except Exception:
                pass
        # Abort sicuro anche se il thread e' bloccato in read/grab nativo:
        # release() sveglia il grab (Pseye abort) e non leakca l'handle.
        with self._lock:
            sender, cap, source = self._sender, self._cap, self._imu_source
            alive = self._thread is not None and self._thread.is_alive()
        for closer in (
            (lambda: sender.close() if sender is not None else None),
            (lambda: cap.release() if cap is not None else None),
            (lambda: source.close() if source is not None else None),
        ):
            try:
                closer()
            except Exception:
                pass
        with self._lock:
            alive = self._thread is not None and self._thread.is_alive()
            if alive:
                # Worker ancora vivo: non dichiarare stop.
                self._status.update(running=True)
                self._stopping = False
                return False
            # Preserva state/error del worker (error se fallito), chiudi running.
            self._status.update(running=False)
            if self._status.get("state") == "running":
                self._status.update(state="stopped")
            # B11-05: il worker fallito su mkdir non passa dal finally che
            # azzera _thread; pulisci il riferimento morto fotografato, solo
            # se ancora quello (mai un restart successivo).
            if self._thread is thread:
                self._thread = None
            self._stopping = False
        return True

    # -- internals -------------------------------------------------------
    def _set_status(self, **fields):
        with self._lock:
            self._status.update(fields)

    def _clear_camera_context(self, tracker, pipeline, sender, reason):
        """Invalidate the old optical frame before a potentially blocking close."""
        if sender is not None:
            sender.invalidate()
        if tracker is not None:
            tracker.reset()
        if pipeline is not None:
            try:
                pipeline.reset(reason)
            except TypeError:
                pipeline.reset()
        self._set_status(camera_controls=None, camera_controls_source=None,
                         camera_source=None, camera_settings_capabilities=None,
                         estimated_position_m=None, telemetry=None,
                         diagnostics={}, preview_ppm=None, mode_actual="position_missing")
        if reason == "camera_transition":
            with self._lock:
                edit = self._status.get("control_edit")
                if isinstance(edit, dict) and edit.get("state") in ("pending", "applying"):
                    self._status["control_edit"] = {**edit, "state": "rejected",
                        "error": "camera owner changed before confirmation"}

    def _publish_camera_controls(self, cap, prefs):
        try:
            controls = cap.controls_status() if hasattr(cap, "controls_status") else None
        except Exception:
            controls = None
        controls = controls if isinstance(controls, dict) else None
        if prefs["camera_source"] not in ("pseye", "ps4-stereo"):
            controls = None
        source_error = getattr(cap, "error", None)
        pending = (controls or {}).get("transaction", {}).get("state") in ("pending", "applying")
        capabilities = None
        if prefs["camera_source"] == "ps4-stereo" and controls is not None:
            try:
                candidate = cap.settings_capabilities()
                if isinstance(candidate, dict):
                    capabilities = copy.deepcopy(candidate)
            except (AttributeError, TypeError):
                pass
        with self._lock:
            self._status.update(camera_source=prefs["camera_source"],
                camera_controls_source=prefs["camera_source"],
                camera_controls=(None if pending or source_error else copy.deepcopy(controls)),
                camera_settings_capabilities=capabilities)
            edit = self._status.get("control_edit")
            transaction = (controls or {}).get("transaction")
            if (isinstance(edit, dict) and isinstance(transaction, dict)
                    and edit.get("revision") == transaction.get("revision")):
                self._status["control_edit"] = {**edit, **copy.deepcopy(transaction)}
        return controls

    def _publish_camera_restore(self, cap):
        """Keep close diagnostics separate from unavailable live controls."""
        getter = getattr(cap, "release_diagnostics", None)
        if not callable(getter):
            return
        try:
            diagnostic = getter()
        except Exception as exc:
            diagnostic = {"source": "ps4-stereo", "error": str(exc),
                          "verification": "unavailable"}
        if isinstance(diagnostic, dict):
            self._set_status(camera_restore=diagnostic)

    def _drain_settings_latest(self):
        latest = None
        while True:
            try:
                latest = self._settings.get_nowait()
            except queue.Empty:
                break
        return latest

    def _run(self, initial):
        prefs = dict(initial)
        requested = dict(initial)
        source_profile = None
        log_path = None
        if prefs.get("log"):
            log_path = _resolve_under(self._root, prefs["log"])
        else:
            log_path = _default_log_path(self._root)
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._set_status(running=False, error=str(exc), state="error")
            self._emit("error", {"error": "log directory unavailable: %s" % exc})
            # B11-05: early-return prima del try/finally esterno che azzera
            # _thread; pulisci il riferimento morto, solo se ancora nostro.
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None
            return
        self._set_status(log=str(log_path))
        stream = None
        cap = None
        imu_source = None
        sender = None
        pipeline = None
        intrinsics = None
        front_rig = None
        front_points = None
        rot_rig = None
        rot_points = None
        rot_normals = None
        rot_labels = None
        detector = None
        tracker = None
        kalman_table = {}
        origin = None
        first_valid = None
        last_good = None
        position_seq = 0
        captured = 0
        valid = 0
        read_failures = 0
        consecutive_failures = 0
        failures_note = None
        last_controls = None
        last_detector = None
        current_key = None
        current_immutable = {k: prefs[k] for k in IMMUTABLE_CAM_KEYS}
        last_emit = 0.0
        started_at = time.perf_counter()
        summary = {"schema": "psvr.suite.tracking", "captured_frames": 0,
                   "valid_frames": 0, "read_failures": 0, "ok": False}
        try:
            try:
                stream = log_path.open("x", encoding="utf-8")
            except FileExistsError as exc:
                raise ValueError("log already exists: %s" % log_path) from exc
            # Setup limitato: calibrazioni e tabelle filtri.
            prefs, source_profile = _source_preferences(self._root, requested)
            (intrinsics, front_rig, front_points, rot_rig, rot_points,
             rot_normals, rot_labels, tracker) = _load_optical_setup(
                 self._root, prefs, source_profile)
            current_immutable = {k: prefs[k] for k in IMMUTABLE_CAM_KEYS}
            self._set_status(**_camera_metadata(requested, prefs, source_profile))
            kalman_table = _load_kalman_table(self._root)

            # Sempre vera DriverImuSource se il driver esporta.
            # Mai HID/processi propri. Se la porta e' posseduta dalla vecchia
            # GUI: status await_source + retry periodico limitato dopo chiusura.
            imu_bind_error = None
            try:
                imu_source = DriverImuSource(port=prefs["driver_imu_port"])
            except OSError as exc:
                imu_bind_error = str(exc)
                imu_source = None
            last_rebind_attempt = time.perf_counter()
            with self._lock:
                self._imu_source = imu_source
                self._status.update(imu_bound=bool(imu_source is not None))
            if imu_bind_error is not None:
                # Porta 4272 gia' posseduta dalla vecchia camera: errore
                # esplicito, mai riuso silenzioso del bind. La preview
                # continua senza posa; il rebind e' periodico e limitato.
                self._emit("error", {"error": "driver IMU bind failed: %s" % imu_bind_error,
                                     "port": prefs["driver_imu_port"],
                                     "status": "await_source"})
                self._set_status(error="driver IMU bind failed: %s" % imu_bind_error,
                                 mode_actual="await_source", fallback_active=None)

            want_udp = (prefs["tracking_mode"] == "6dof" and imu_source is not None)
            if want_udp:
                sender = DriverPositionSender(port=prefs["position_port"])
            with self._lock:
                self._cap = None
                self._sender = sender
            self._set_status(udp_enabled=bool(sender is not None))

            cap, _ = _open_effective_camera(self._root, prefs, source_profile)
            with self._lock:
                self._cap = cap

            detector = BlueLedDetector(BlueLedConfig(
                min_area=prefs["min_area"], value_min=prefs["value_min"],
                saturation_min=prefs["saturation_min"],
                opening_size=prefs["opening_size"],
                include_white_cores=True))
            pipeline = _make_pipeline(prefs, kalman_table)
            current_key = _pipeline_key(prefs)
            last_detector = (prefs["min_area"], prefs["value_min"])

            udp_enabled = bool(sender is not None)
            kalman_cfg = dict(kalman_table.get(prefs["position_filter"], {}) or {})
            session = {
                "event": "session",
                "schema": "psvr.suite.tracking.session",
                "version": 1,
                # Nomi canonici backend Suite, risolti da ROOT.
                "intrinsicsPath": str(prefs["intrinsicsPath"]),
                "rigPath": str(prefs["rigPath"]),
                "imu_rig": prefs["imu_rig"],
                "intrinsics": str(prefs["intrinsicsPath"]),
                "rig": front_rig,
                "imu_rig_full": rot_rig,
                "image_size": [prefs["width"], prefs["height"]],
                "tracking_mode": prefs["tracking_mode"],
                "udp_enabled": udp_enabled,
                "head_offset_model_m": list(prefs["head_offset_model_m"]),
                "position_scale": float(prefs["position_scale"]),
                "position_config": {
                    "filter": ("kalman" if prefs["position_filter"].startswith("kalman")
                               else "one_euro"),
                    "filter_name": prefs["position_filter"],
                    "one_euro": {"min_cutoff_hz": float(prefs["min_cutoff_hz"]),
                                 "beta": float(prefs["beta"])},
                    "kalman": dict(kalman_cfg.get("kalman", {})),
                    "uncertainty": dict(kalman_cfg.get("uncertainty", {})),
                    "prediction_output": kalman_cfg.get("prediction_output", "camera"),
                },
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "notes": ("monotonic_s uses perf_counter capture clock; "
                          "host_time_ns is wall clock; no synthetic pose; "
                          "telemetry rms/led are real optical measures, not renderer"),
            }
            session.update(_camera_metadata(requested, prefs, source_profile))
            stream.write(json.dumps(session, allow_nan=False) + "\n")
            stream.flush()
            self._emit("started", {"log": str(log_path)})
            self._set_status(state="running")

            while not self._stop.is_set():
                # Rebind periodico limitato se la porta era posseduta dalla
                # vecchia GUI: un tentativo ogni 2s, mai busy-loop. Nessun
                # riuso silenzioso: status await_source finche' non libera;
                # la UI puo' anche stop/start dopo aver chiuso la vecchia GUI.
                if imu_source is None:
                    now_try = time.perf_counter()
                    if now_try - last_rebind_attempt >= IMU_REBIND_EVERY_S:
                        last_rebind_attempt = now_try
                        try:
                            candidate = DriverImuSource(port=prefs["driver_imu_port"])
                        except OSError:
                            candidate = None
                        if candidate is not None:
                            imu_source = candidate
                            with self._lock:
                                self._imu_source = imu_source
                                self._status.update(imu_bound=True)
                            self._set_status(error=None, mode_actual=None,
                                             fallback_active=None)
                            stream.write(json.dumps({
                                "event": "position_reset", "reason": "imu_rebound",
                                "monotonic_s": time.perf_counter(),
                            }) + "\n")
                            if prefs["tracking_mode"] == "6dof" and sender is None:
                                try:
                                    sender = DriverPositionSender(
                                        port=prefs["position_port"])
                                    with self._lock:
                                        self._sender = sender
                                    self._set_status(
                                        udp_enabled=True)
                                except (OSError, ValueError) as exc:
                                    self._emit("error", {
                                        "error": "position sender failed: %s" % exc})
                        else:
                            self._set_status(imu_bound=False,
                                             mode_actual="await_source",
                                             fallback_active=None)
                pending = self._drain_settings_latest()
                if pending is not None:
                    reopen_owner = getattr(pending, "legacy_owner", None)
                    if reopen_owner is not None and reopen_owner is not cap:
                        message = "PS4 Apply rejected: camera owner changed before reopen"
                        self._set_status(control_edit={"state": "rejected", "error": message,
                                                       "apply_mode": "reopen"})
                        self._emit("error", {"error": message})
                        continue
                    requested_pending = dict(pending)
                    pending, pending_profile = _source_preferences(self._root, requested_pending)
                    new_immutable = {k: pending[k] for k in IMMUTABLE_CAM_KEYS}
                    if (new_immutable != current_immutable or pending_profile != source_profile
                            or reopen_owner is not None):
                        self._clear_camera_context(tracker, pipeline, sender, "camera_transition")
                        origin = first_valid = last_good = None
                        last_controls = None
                        consecutive_failures = 0
                        self._set_status(state="reopening")
                        # Reopen camera, stesso file di log, con rollback.
                        # 1) Handle abbandonato (release con leak nativo pseye/
                        #    OpenCV): mai riaprire sullo stesso handle, che
                        #    rischia access violation nativa. Errore esplicito
                        #    e sessione chiusa con messaggio, mai reopen cieco.
                        if _cap_handle_abandoned(cap):
                            refused = ValueError(
                                "camera handle abandoned (native leak); "
                                "restart tracking instead of reopen on same handle")
                            self._emit("error", {"error": "camera reopen refused: %s" % refused})
                            self._set_status(error="camera reopen refused: %s" % refused)
                            raise refused
                        old_prefs = dict(prefs)
                        old_requested = dict(requested)
                        new_cap = None
                        optical_setup = None
                        release_complete = False
                        try:
                            try:
                                closed = cap.release()
                            finally:
                                self._publish_camera_restore(cap)
                            # release() is where a backend can discover an
                            # abandoned reader. Check AFTER release as well:
                            # neither reopen nor rollback may compete with an
                            # owner that still holds the device.
                            if (closed is False or _cap_handle_abandoned(cap)
                                    or bool(getattr(cap, "cleanup_pending", False))):
                                raise ValueError("camera owner cleanup pending or handle abandoned; no camera reopen")
                            release_complete = True
                            optical_setup = _load_optical_setup(self._root, pending, pending_profile)
                            new_cap, _ = _open_effective_camera(self._root, pending, pending_profile)
                        except Exception as exc:
                            if bool(getattr(exc, "cleanup_pending", False)):
                                failed_source = getattr(exc, "source", None)
                                if failed_source is not None:
                                    cap = failed_source
                                    with self._lock:
                                        self._cap = cap
                                raise ValueError("new camera startup cleanup pending; no rollback") from exc
                            if not release_complete:
                                message = "camera release failed; no reopen or rollback: %s" % exc
                                self._emit("error", {"error": message})
                                self._set_status(error=message)
                                raise ValueError(message) from exc
                            # 2) Rollback: il nuovo open (o il setup ottico
                            #    dopo la release) ha fallito. Prova a riaprire
                            #    la camera precedente; solo se fallisce anche
                            #    quello esci con errore chiaro. Mai lasciare
                            #    la sessione senza camera senza messaggio.
                            try:
                                rb_cap, _ = _open_effective_camera(self._root, old_prefs, source_profile)
                            except Exception as rb_exc:
                                message = ("camera reopen failed: %s; rollback also failed: %s"
                                           % (exc, rb_exc))
                                self._emit("error", {"error": message})
                                self._set_status(error=message)
                                # The outer handler writes str(exc) into both
                                # final status and the end record. Preserve BOTH
                                # concrete failures, not just a generic label.
                                # Keep rollback as cause for failed-owner cleanup.
                                raise ValueError(message) from rb_exc
                            cap = rb_cap
                            with self._lock:
                                self._cap = cap
                            try:
                                stream.write(json.dumps({
                                    "event": "camera_rollback",
                                    "monotonic_s": time.perf_counter(),
                                    "reason": "camera reopen failed: %s" % exc,
                                    **_camera_metadata(old_requested, old_prefs, source_profile),
                                }, allow_nan=False) + "\n")
                            except (TypeError, ValueError, OSError):
                                pass
                            self._emit("error", {"error": "camera reopen failed: %s; "
                                                          "restored previous camera" % exc})
                            self._set_status(error="camera reopen failed: %s; "
                                                    "restored previous camera" % exc,
                                             state="running")
                            continue
                        with self._lock:
                            cap = new_cap
                            self._cap = cap
                        try:
                            (intrinsics, front_rig, front_points, rot_rig, rot_points,
                             rot_normals, rot_labels, tracker) = optical_setup
                            pipeline = _make_pipeline(pending, kalman_table)
                            current_key = _pipeline_key(pending)
                            origin = None
                            last_controls = None
                            last_detector = (pending["min_area"], pending["value_min"])
                            stream.write(json.dumps({
                                "event": "camera_open", "monotonic_s": time.perf_counter(),
                                **_camera_metadata(requested_pending, pending, pending_profile),
                            }, allow_nan=False) + "\n")
                            stream.write(json.dumps({
                                "event": "position_reset",
                                "reason": "camera_format",
                                "monotonic_s": time.perf_counter(),
                            }) + "\n")
                        except Exception as exc:
                            self._emit("error", {"error": "camera reopen failed: %s" % exc})
                            self._set_status(error="camera reopen failed: %s" % exc)
                            raise ValueError("camera reopen setup failed: %s" % exc) from exc
                        prefs = dict(pending)
                        requested = requested_pending
                        source_profile = pending_profile
                        current_immutable = new_immutable
                        detector = BlueLedDetector(BlueLedConfig(
                            min_area=prefs["min_area"], value_min=prefs["value_min"],
                            saturation_min=prefs["saturation_min"],
                            opening_size=prefs["opening_size"],
                            include_white_cores=True))
                        # Sender segue il modo + bind reale.
                        want = (prefs["tracking_mode"] == "6dof" and imu_source is not None)
                        if want and sender is None:
                            try:
                                sender = DriverPositionSender(port=prefs["position_port"])
                                with self._lock:
                                    self._sender = sender
                            except (OSError, ValueError) as exc:
                                self._emit("error", {"error": "position sender failed: %s" % exc})
                        elif not want and sender is not None:
                            try:
                                sender.close()
                            except Exception:
                                pass
                            sender = None
                            with self._lock:
                                self._sender = None
                        self._set_status(udp_enabled=bool(sender is not None),
                                         prefs=copy.deepcopy(requested),
                                         **_camera_metadata(requested, prefs, source_profile),
                                         mode_requested=prefs["tracking_mode"],
                                         position_mode=prefs["position_filter"], state="running")
                        continue
                    # Live: detector/filtro/controlli/trasformata senza restart file.
                    # head_offset/scale cambiano la trasformata: la storia nel
                    # vecchio frame non deve contaminare il nuovo.
                    detector_changed = ((pending["min_area"], pending["value_min"],
                                         pending["saturation_min"], pending["opening_size"])
                                        != (prefs["min_area"], prefs["value_min"],
                                            prefs["saturation_min"], prefs["opening_size"]))
                    filter_changed = (_pipeline_key(pending) != current_key)
                    head_changed = (list(pending["head_offset_model_m"])
                                    != list(prefs["head_offset_model_m"]))
                    scale_changed = (float(pending["position_scale"])
                                     != float(prefs["position_scale"]))
                    controls_changed = ((pending["exposure"], pending["gain"])
                                        != (prefs["exposure"], prefs["gain"]))
                    mode_changed = (pending["tracking_mode"] != prefs["tracking_mode"]
                                    or pending["position_port"] != prefs["position_port"])
                    prefs = dict(pending)
                    requested = requested_pending
                    source_profile = pending_profile
                    self._set_status(prefs=copy.deepcopy(requested),
                                     **_camera_metadata(requested, prefs, source_profile),
                                     mode_requested=prefs["tracking_mode"],
                                     position_mode=prefs["position_filter"])
                    if filter_changed:
                        pipeline = _make_pipeline(prefs, kalman_table)
                        current_key = _pipeline_key(prefs)
                        stream.write(json.dumps({
                            "event": "position_reset", "reason": "filter_switch",
                            "filter": prefs["position_filter"],
                            "monotonic_s": time.perf_counter(),
                        }) + "\n")
                    # Reset storia su cambio trasformata testa/scala.
                    if head_changed and not filter_changed:
                        try:
                            pipeline.reset("head_offset")
                        except TypeError:
                            pipeline.reset()
                        stream.write(json.dumps({
                            "event": "position_reset", "reason": "head_offset",
                            "head_offset_model_m": list(prefs["head_offset_model_m"]),
                            "monotonic_s": time.perf_counter(),
                        }) + "\n")
                    if scale_changed and not filter_changed:
                        try:
                            pipeline.reset("position_scale")
                        except TypeError:
                            pipeline.reset()
                        stream.write(json.dumps({
                            "event": "position_reset", "reason": "position_scale",
                            "position_scale": float(prefs["position_scale"]),
                            "monotonic_s": time.perf_counter(),
                        }) + "\n")
                    if detector_changed:
                        tracker.reset()
                        if getattr(pipeline, "mode", None) == "kalman":
                            try:
                                pipeline.reset("detector_settings")
                            except TypeError:
                                pipeline.reset()
                        detector = BlueLedDetector(BlueLedConfig(
                            min_area=prefs["min_area"], value_min=prefs["value_min"],
                            saturation_min=prefs["saturation_min"],
                            opening_size=prefs["opening_size"],
                            include_white_cores=True))
                        last_detector = (prefs["min_area"], prefs["value_min"])
                        stream.write(json.dumps({
                            "event": "position_reset", "reason": "detector_settings",
                            "monotonic_s": time.perf_counter(),
                        }) + "\n")
                    if mode_changed:
                        want = (prefs["tracking_mode"] == "6dof" and imu_source is not None)
                        if want and sender is None:
                            try:
                                sender = DriverPositionSender(port=prefs["position_port"])
                                with self._lock:
                                    self._sender = sender
                            except (OSError, ValueError) as exc:
                                self._emit("error", {"error": "position sender failed: %s" % exc})
                        elif not want and sender is not None:
                            try:
                                sender.close()
                            except Exception:
                                pass
                            sender = None
                            with self._lock:
                                self._sender = None
                        self._set_status(udp_enabled=bool(sender is not None))
                    _ = controls_changed  # applicato sotto per-frame come trial accettato.

                # Realign: reset tracker A/filtro; solo display reset relativo.
                if self._realign.is_set():
                    tracker.reset()
                    try:
                        pipeline.reset("realign")
                    except TypeError:
                        pipeline.reset()
                    origin = None
                    self._realign.clear()
                    stream.write(json.dumps({
                        "event": "position_reset", "reason": "realign",
                        "monotonic_s": time.perf_counter(),
                    }) + "\n")
                if self._recenter.is_set():
                    origin = None
                    self._recenter.clear()
                    stream.write(json.dumps({
                        "event": "position_reset", "reason": "reset_relative",
                        "monotonic_s": time.perf_counter(),
                    }) + "\n")

                try:
                    edit_id, edit_cap, edit_controls = self._control_edits.get_nowait()
                except queue.Empty:
                    pass
                else:
                    if edit_cap is not cap or prefs["camera_source"] != "ps4-stereo":
                        self._set_status(control_edit={"id": edit_id, "state": "rejected",
                            "error": "camera owner changed before apply"})
                    else:
                        self._clear_camera_context(tracker, pipeline, sender, "camera_controls")
                        origin = first_valid = last_good = None
                        try:
                            revision = cap.request_controls(edit_controls)
                        except Exception as exc:
                            self._set_status(control_edit={"id": edit_id, "state": "failed",
                                                           "error": str(exc)})
                            raise
                        self._set_status(control_edit={"id": edit_id, "revision": revision,
                                                      "state": "pending"})
                # Controlli manuali PS Eye (come trial): cambio resetta stato.
                # Solo il backend che espone il metodo (PS3 Eye nativo, o un
                # futuro nativo registrato) li riceve: cap generico OpenCV
                # non ha controlli proprietari PSeye e non viene mai chiamato.
                requested_controls = (prefs["exposure"], prefs["gain"])
                if requested_controls != last_controls:
                    if (str(prefs["backend"]).strip().lower() == "pseye"
                            and hasattr(cap, "set_manual_controls")):
                        try:
                            cap.set_manual_controls(*requested_controls)
                        except Exception as exc:
                            raise ValueError(str(exc)) from exc
                        tracker.reset()
                        if getattr(pipeline, "mode", None) == "kalman":
                            try:
                                pipeline.reset("camera_controls")
                            except TypeError:
                                pipeline.reset()
                        stream.write(json.dumps({
                            "event": "position_reset", "reason": "camera_controls",
                            "monotonic_s": time.perf_counter(),
                        }) + "\n")
                    last_controls = requested_controls

                ok, frame = cap.read()
                observed_controls = self._publish_camera_controls(cap, prefs)
                transaction = (observed_controls or {}).get("transaction", {})
                if source_profile is not None:
                    # Defense in depth: an adapter must not turn an old frame
                    # into a pose while a PS4 owner transaction is unsettled.
                    if transaction.get("state") == "failed":
                        raise ValueError("PS4 owner controls failed: " + str(transaction.get("error")))
                    if transaction.get("state") in ("pending", "applying", "cancelled"):
                        ok, frame = False, None
                    pair = getattr(cap, "last_pair", None)
                    if (ok and isinstance(pair, dict) and "controls_revision" in pair and
                            pair["controls_revision"] != transaction.get("revision", 0)):
                        ok, frame = False, None
                if (source_profile is not None and transaction.get("state") == "confirmed"
                        and transaction.get("revision", 0) > 0):
                    targets = observed_controls.get("requested", {})
                    if targets != source_profile["controls"]:
                        source_profile["controls"] = copy.deepcopy(targets)
                        source_profile["controls_explicit"] = True
                        prefs.update(targets)
                        self._set_status(**_camera_metadata(requested, prefs, source_profile))
                if not ok or frame is None:
                    read_failures += 1
                    consecutive_failures += 1
                    if getattr(cap, "error", None):
                        raise ValueError(cap.error)
                    if consecutive_failures >= 10:
                        raise ValueError("camera read failed ten times consecutively")
                    if pipeline is not None and sender is not None:
                        failed_at = time.perf_counter()
                        try:
                            pipeline.invalid(failed_at)
                        except Exception:
                            pass
                        try:
                            sender.invalidate()
                        except Exception:
                            pass
                        stream.write(json.dumps({
                            "event": "position_reset",
                            "reason": "camera_read_failure",
                            "monotonic_s": failed_at,
                        }) + "\n")
                    continue
                consecutive_failures = 0
                if (int(frame.shape[1]), int(frame.shape[0])) != tuple(intrinsics.image_size):
                    raise ValueError("live camera resolution differs from calibration")
                captured_at = getattr(cap, "last_frame_monotonic", None) or time.perf_counter()

                imu_sample = None
                if imu_source is not None:
                    try:
                        imu_sample = imu_source.at(captured_at)
                    except ValueError:
                        imu_sample = None
                fresh = False
                age = None
                if imu_sample is not None:
                    try:
                        mono = imu_sample.get("monotonic_s")
                        if _finite(mono):
                            age = float(captured_at) - float(mono)
                            fresh = bool(
                                imu_sample.get("valid") is True
                                and imu_sample.get("connected", True) is True
                                and 0 <= age <= MAX_AGE_S)
                    except (TypeError, ValueError):
                        fresh = False
                        age = None

                # ROI + min-area adattiva, stessa regola del trial accettato.
                roi, size_ratio = tracker.search_region(prefs["width"], prefs["height"], captured_at)
                with self._lock:
                    pass
                base_settings = (prefs["min_area"], prefs["value_min"])
                if base_settings != last_detector:
                    tracker.reset()
                    if getattr(pipeline, "mode", None) == "kalman":
                        try:
                            pipeline.reset("detector_settings")
                        except TypeError:
                            pipeline.reset()
                    stream.write(json.dumps({
                        "event": "position_reset", "reason": "detector_settings",
                        "monotonic_s": captured_at,
                    }) + "\n")
                    roi, size_ratio = None, 1.0
                    last_detector = base_settings
                try:
                    ratio = float(size_ratio)
                except (TypeError, ValueError):
                    ratio = 1.0
                if not math.isfinite(ratio) or ratio <= 0:
                    ratio = 1.0
                effective_area = (base_settings[0] if roi is None
                                  else max(3, int(base_settings[0] * min(1.0, ratio ** 2) * 0.5)))
                config = BlueLedConfig(
                    min_area=effective_area, value_min=base_settings[1],
                    saturation_min=prefs["saturation_min"],
                    opening_size=prefs["opening_size"] if roi is None else 0,
                    include_white_cores=True)
                detector.config = config
                if roi is None:
                    candidates = detector.detect(frame)
                else:
                    x0, y0, x1, y1 = (int(v) for v in roi)
                    x0 = max(0, x0)
                    y0 = max(0, y0)
                    x1 = min(int(frame.shape[1]), x1)
                    y1 = min(int(frame.shape[0]), y1)
                    if x1 <= x0 or y1 <= y0:
                        candidates = detector.detect(frame)
                        roi = None
                    else:
                        candidates = detector.detect(frame[y0:y1, x0:x1])
                        for candidate in candidates:
                            candidate["center_px"][0] += x0
                            candidate["center_px"][1] += y0
                            candidate["bbox_px"][0] += x0
                            candidate["bbox_px"][1] += y0

                pose = None
                if fresh:
                    tracker.allow_two_led = prefs["two_led_experimental"]
                    tracker.set_imu(imu_sample)
                    if source_profile is not None:
                        pair = cap.last_pair
                        if pair is None or pair["host_monotonic"] != captured_at:
                            raise ValueError("PS4 atomic pair/timestamp mismatch")
                        right_candidates = detector.detect(pair["right"])
                        pose = tracker.update_stereo(candidates, right_candidates, captured_at)
                    else:
                        pose = tracker.update(candidates, captured_at)
                # Senza IMU fresca: solo detector RGB, nessuna posa/pacchetto.
                ps4_publication_failure = None
                if source_profile is not None and pose is not None:
                    ps4_publication_failure = _ps4_publication_failure(
                        captured_at, imu_sample, imu_source)
                    if ps4_publication_failure:
                        _reject_ps4_publication(pose, pipeline, sender, ps4_publication_failure)
                        fresh = False

                raw_packet = None
                outgoing = None
                estimator_input = None
                filt_diag = None
                if pose is not None and fresh and prefs["tracking_mode"] == "3dof":
                    # 3dof richiesto: input IMU valido ma output posizione
                    # none by design (solo preview ottica + stato).
                    try:
                        filt_diag = pipeline.diagnostics()
                    except Exception:
                        filt_diag = None
                elif pose is not None and fresh:
                    binding = imu_sample if imu_sample is not None else imu_source.latest()
                    try:
                        seq_floor = 0
                        if sender is not None and getattr(sender, "last_packet", None):
                            try:
                                seq_floor = int(sender.last_packet.get("seq", -1)) + 1
                            except (TypeError, ValueError):
                                seq_floor = 0
                        position_seq = max(position_seq, seq_floor)
                    except Exception:
                        pass
                    raw_packet = build_driver_position_packet(
                        pose, binding, tracker.alignment, position_seq,
                        head_offset_model_m=tuple(prefs["head_offset_model_m"]),
                        scale=float(prefs["position_scale"]), now=captured_at)
                    covariance = None
                    quality = {"reason": "not_requested", "reliable": False}
                    if getattr(pipeline, "mode", None) == "kalman" or stream is not None:
                        covariance, quality = estimate_pose_covariance(
                            pose, intrinsics, np.asarray(rot_points, float), rot_labels,
                            tracker.alignment,
                            scale=float(prefs["position_scale"]),
                            head_offset_model_m=tuple(prefs["head_offset_model_m"]),
                            guided=pose.reason in ("imu_guided_pattern",
                                                   "imu_guided_front_reacquired",
                                                   "imu_guided_front_anchor",
                                                    "imu_guided_constellation"))
                        if source_profile is not None:
                            quality["covariance_model"] = "reference_eye_front5_approximation"
                    if source_profile is not None:
                        ps4_publication_failure = _ps4_publication_failure(
                            captured_at, binding, imu_source)
                        if ps4_publication_failure:
                            _reject_ps4_publication(pose, pipeline, sender, ps4_publication_failure)
                            fresh = False
                            if raw_packet is not None:
                                raw_packet = {**raw_packet, "valid": False,
                                              "state": "lost", "position_m": None}
                    context_valid = bool(
                        raw_packet is not None and imu_sample is not None
                        and imu_sample.get("valid") is True
                        and imu_sample.get("connected", True) is True
                        and tracker.alignment is not None
                        and age is not None and 0 <= age <= MAX_AGE_S
                        and (source_profile is None or ps4_publication_failure is None))
                    optical_prediction = bool(
                        pose.valid or pose.reason.startswith("Pattern parziale:")
                        or pose.reason.startswith("Pattern non associato:"))
                    prediction_allowed = bool(context_valid and optical_prediction)
                    estimator_input = {
                        "packet": raw_packet,
                        "alignment": (None if tracker.alignment is None
                                      else np.asarray(tracker.alignment, float).tolist()),
                        "covariance": (None if covariance is None
                                       else np.asarray(covariance, float).tolist()),
                        "quality": quality,
                        "prediction_allowed": prediction_allowed,
                        "context_valid": bool(context_valid),
                    }
                    outgoing = pipeline.filter_packet(
                        raw_packet, tracker.alignment, captured_at,
                        covariance=covariance, quality=quality,
                        prediction_allowed=prediction_allowed,
                        context_valid=bool(context_valid))
                    filt_diag = pipeline.diagnostics()
                    if source_profile is not None and ps4_publication_failure is None:
                        # Last check after filtering/diagnostics, directly before
                        # publication: no valid One Euro bypass on an old binding.
                        ps4_publication_failure = _ps4_publication_failure(
                            captured_at, binding, imu_source)
                        if ps4_publication_failure:
                            _reject_ps4_publication(pose, pipeline, sender, ps4_publication_failure)
                            fresh = False
                    if sender is not None:
                        if ps4_publication_failure:
                            pass  # Already invalidated; never send this estimate.
                        elif outgoing is not None:
                            try:
                                if not sender.send(outgoing):
                                    failures_note = getattr(sender, "last_error", None)
                            except Exception as exc:
                                failures_note = str(exc)
                            position_seq += 1
                        else:
                            try:
                                sender.invalidate()
                            except Exception:
                                pass
                else:
                    # Mai promuovere binding vecchio; mai posa valida inventata.
                    # Se il sender 6dof esiste ma l'IMU non e' fresca (runtime
                    # spento), pubblica comunque un invalid con il binding
                    # corrente cosi' il driver non trattiene pose stale.
                    try:
                        pipeline.invalid(captured_at)
                    except Exception:
                        pass
                    try:
                        filt_diag = pipeline.diagnostics()
                    except Exception:
                        filt_diag = None
                    if sender is not None:
                        binding = None
                        try:
                            binding = (imu_sample if source_profile is not None
                                       and imu_sample is not None else
                                       imu_source.latest() if imu_source is not None else None)
                        except Exception:
                            binding = None
                        if binding is not None:
                            try:
                                seq_floor = 0
                                if getattr(sender, "last_packet", None):
                                    try:
                                        seq_floor = int(
                                            sender.last_packet.get("seq", -1)) + 1
                                    except (TypeError, ValueError):
                                        seq_floor = 0
                                position_seq = max(position_seq, seq_floor)
                            except Exception:
                                pass
                            try:
                                invalid_packet = build_driver_position_packet(
                                    None, binding,
                                    getattr(tracker, "alignment", None),
                                    position_seq,
                                    head_offset_model_m=tuple(
                                        prefs["head_offset_model_m"]),
                                    scale=float(prefs["position_scale"]),
                                    now=captured_at)
                            except (ValueError, TypeError):
                                invalid_packet = None
                            if invalid_packet is not None:
                                try:
                                    if not sender.send(invalid_packet):
                                        failures_note = getattr(
                                            sender, "last_error", None)
                                except Exception as exc:
                                    failures_note = str(exc)
                                position_seq += 1
                                raw_packet = invalid_packet
                                outgoing = invalid_packet
                            else:
                                try:
                                    sender.invalidate()
                                except Exception:
                                    pass
                        else:
                            try:
                                sender.invalidate()
                            except Exception:
                                pass

                if source_profile is not None and ps4_publication_failure:
                    # Keep logs, telemetry and estimator input coherent with the
                    # packet veto, including failures discovered after filtering.
                    if raw_packet is not None:
                        raw_packet = {**raw_packet, "valid": False,
                                      "state": "lost", "position_m": None}
                    if outgoing is not None:
                        outgoing = {**outgoing, "valid": False,
                                    "state": "lost", "position_m": None}
                    estimator_input = {**(estimator_input or {}), "packet": raw_packet,
                                       "context_valid": False, "prediction_allowed": False,
                                       "publication_rejection": ps4_publication_failure}
                    filt_diag = pipeline.diagnostics()

                captured += 1
                if pose is not None and pose.valid:
                    valid += 1
                    last_good = pose
                    if origin is None or self._recenter.is_set():
                        try:
                            origin = np.asarray(pose.tvec, float).reshape(3).copy()
                        except (TypeError, ValueError):
                            origin = None
                        self._recenter.clear()
                    if first_valid is None:
                        try:
                            first_valid = pose.to_dict()
                        except Exception:
                            first_valid = None
                relative = None
                if pose is not None and pose.valid and origin is not None:
                    try:
                        relative = (np.asarray(pose.tvec, float).reshape(3) - origin).tolist()
                    except (TypeError, ValueError):
                        relative = None

                controls_status = observed_controls
                pose_dict = None
                if pose is not None:
                    try:
                        pose_dict = pose.to_dict()
                    except Exception:
                        pose_dict = {"valid": bool(getattr(pose, "valid", False)),
                                     "reason": str(getattr(pose, "reason", "?"))}
                else:
                    pose_dict = {"valid": False,
                                 "reason": ("waiting for fresh driver IMU export "
                                            "(no invented pose)")}

                row = {"event": "pose", "monotonic_s": captured_at,
                       "candidates": candidates,
                       "candidate_count": len(candidates),
                       "relative_camera_m": relative, "search_roi": roi,
                       "detector": {"value_min": config.value_min,
                                    "min_area": config.min_area,
                                    "saturation_min": config.saturation_min,
                                    "opening_size": config.opening_size},
                       "camera_controls": controls_status}
                if source_profile is not None:
                    row["stereo_sequence"] = cap.last_pair["sequence"]
                    row["camera_source"] = "ps4-stereo"
                row.update(pose_dict)
                if hasattr(tracker, "diagnostics"):
                    row["optical_tracking"] = tracker.diagnostics()
                if filt_diag is not None:
                    row["position_filter"] = filt_diag
                if estimator_input is not None:
                    row["estimator_input"] = estimator_input
                    row["position_output"] = outgoing
                if imu_sample is not None:
                    row["imu"] = {k: imu_sample.get(k) for k in
                                  ("valid", "state", "monotonic_s",
                                   "quaternion_sensor_wxyz", "quaternion_wxyz",
                                   "corrected_gyro_rad_s", "recenter_count",
                                   "interpolated", "session_id",
                                   "recenter_epoch", "seq", "sample_time_s",
                                   "world_rotation_wxyz")}
                else:
                    row["imu"] = None
                stream.write(json.dumps(row, allow_nan=False, default=str) + "\n")
                if captured % 30 == 0:
                    try:
                        stream.flush()
                    except OSError:
                        pass

                # Diagnostica ~10Hz verso callback; tracking resta a 75Hz.
                now_wall = time.perf_counter()
                elapsed = now_wall - started_at
                fps_now = (captured / elapsed) if elapsed > 0 else 0.0
                if pose is not None and pose.valid:
                    status_text = ("TRACKING | RMS %.2f / %.2f px | candidati %d"
                                   % (pose.rms_px or 0.0, pose.rms_limit_px or 0.0,
                                      len(candidates)))
                elif pose is not None:
                    status_text = str(pose.reason)
                else:
                    status_text = ("preview camera: in attesa di export IMU driver fresco "
                                   "(%d candidati, nessuna posa)" % len(candidates))
                raw_pos = None
                est_pos = None
                try:
                    if raw_packet is not None:
                        raw_pos = raw_packet.get("position_m")
                    if outgoing is not None:
                        est_pos = outgoing.get("position_m")
                except AttributeError:
                    pass
                tracking_state = "lost"
                try:
                    if isinstance(filt_diag, dict) and filt_diag.get("tracking_state"):
                        tracking_state = str(filt_diag["tracking_state"])
                    elif outgoing is not None and outgoing.get("valid"):
                        tracking_state = "tracking"
                    elif pose is not None and pose.valid:
                        tracking_state = "tracking"
                    elif pose is None and not fresh:
                        tracking_state = "preview"
                except Exception:
                    tracking_state = "lost"
                telemetry = {
                    # Misure ottiche reali, mai rendering: rms/labels dal PnP.
                    "optical_valid": bool(pose is not None and pose.valid),
                    "imu_valid": bool(fresh),
                    "rms_px": getattr(pose, "rms_px", None) if pose is not None else None,
                    "rms_limit_px": getattr(pose, "rms_limit_px", None) if pose is not None else None,
                    "led_count": len(candidates),
                    "led_labels": (list(pose.led_labels) if pose is not None
                                   and getattr(pose, "led_labels", None) else []),
                    "position_raw_m": raw_pos,
                    "position_estimated_m": est_pos,
                    "tracking_state": tracking_state,
                    "filter": prefs["position_filter"],
                    "age_ms": (None if age is None else float(age * 1000.0)),
                    "session_id": (imu_sample.get("session_id") if imu_sample else None),
                    "epoch": (imu_sample.get("recenter_epoch") if imu_sample else None),
                    "fps": float(fps_now),
                    "reason": (pose.reason if pose is not None
                               else "waiting for fresh driver IMU"),
                }
                outgoing_valid = bool(
                    outgoing is not None and outgoing.get("valid") is True)
                mode_actual, fallback_active = _compute_mode_actual(
                    prefs["tracking_mode"], bool(imu_source is not None),
                    bool(fresh), outgoing_valid)
                try:
                    cap_h, cap_w = int(frame.shape[0]), int(frame.shape[1])
                except (AttributeError, TypeError, ValueError):
                    cap_h, cap_w = prefs["height"], prefs["width"]
                ppm_w, ppm_h = _gui_preview_size(cap_w, cap_h)
                self._set_status(
                    captured_frames=captured, valid_frames=valid,
                    read_failures=read_failures, fps=float(fps_now),
                    udp_enabled=bool(sender is not None),
                    imu_bound=bool(imu_source is not None),
                    mode_actual=mode_actual,
                    fallback_active=fallback_active,
                    diagnostics={"led_count": len(candidates),
                                 "rms_px": telemetry["rms_px"],
                                 "rms_limit_px": telemetry["rms_limit_px"],
                                 "age_s": (None if age is None else float(age)),
                                 "reason": telemetry["reason"],
                                 "filter": prefs["position_filter"]},
                    imu={"session_id": telemetry["session_id"],
                         "recenter_epoch": telemetry["epoch"],
                         "valid": telemetry["imu_valid"],
                         "age_ms": telemetry["age_ms"]},
                    estimated_position_m=est_pos,
                    position_mode=prefs["position_filter"],
                    preview_ppm=("capture %dx%d preview %dx%d %.1ffps"
                                 % (cap_w, cap_h, ppm_w, ppm_h, fps_now)),
                    telemetry=copy.deepcopy(telemetry),
                    error=(failures_note if failures_note is not None
                           else (None if imu_source is not None
                                 else self._status.get("error"))),
                    state="running")
                if now_wall - last_emit >= FRAME_EMIT_MIN_S:
                    last_emit = now_wall
                    try:
                        annotated = detector.annotate(frame, candidates)
                    except Exception:
                        annotated = frame
                    # Etichette LED + ROI per leggibilità (solo display,
                    # nessun filtro/orientamento; riusa cv2 del detector).
                    try:
                        cv2mod = getattr(detector, "cv2", None)
                        annotated = _overlay_preview_labels(
                            annotated, cv2mod, pose, roi)
                    except Exception:
                        pass
                    try:
                        preview = _resize_nearest(annotated, ppm_w, ppm_h)
                    except Exception:
                        preview = annotated
                    ppm = _encode_ppm(preview)
                    detail = ("candidati %d | area minima %d px | %s"
                              % (len(candidates), effective_area, status_text))
                    self._emit("frame", {"ppm": ppm, "status": status_text,
                                         "detail": detail, "telemetry": copy.deepcopy(telemetry)})

            summary.update(captured_frames=captured, valid_frames=valid,
                           read_failures=read_failures,
                           duration_s=round(time.perf_counter() - started_at, 3),
                           ok=True)
        except Exception as exc:
            # Startup can fail before open_camera_source returns a capture.
            # Preserve that source (also through a wrapped rollback error) for
            # finally/retirement instead of losing a still-active owner.
            failure = exc
            for _ in range(8):
                failed_source = getattr(failure, "source", None)
                if failed_source is not None:
                    cap = failed_source
                    with self._lock:
                        self._cap = cap
                    break
                failure = getattr(failure, "__cause__", None)
                if failure is None:
                    break
            summary.update(captured_frames=captured, valid_frames=valid,
                           read_failures=read_failures,
                           duration_s=round(time.perf_counter() - started_at, 3),
                           ok=False, error=str(exc))
            self._set_status(error=str(exc), state="error")
            self._emit("error", {"error": str(exc)})
        finally:
            # Ordine: filtro reset, sender final invalid, cap release, source close.
            if pipeline is not None:
                try:
                    pipeline.reset("shutdown")
                except TypeError:
                    try:
                        pipeline.reset()
                    except Exception:
                        pass
                except Exception:
                    pass
            if sender is not None:
                try:
                    sender.close()
                except Exception:
                    pass
            with self._lock:
                self._sender = None
            if cap is not None:
                closed = False
                close_failed = False
                try:
                    closed = cap.release()
                except Exception:
                    close_failed = True
                finally:
                    self._publish_camera_restore(cap)
                if (closed is False or _cap_handle_abandoned(cap)
                        or bool(getattr(cap, "cleanup_pending", False))):
                    with self._lock:
                        self._retired_cap = cap
                        self._retired_uncertain = (close_failed or
                            (closed is False and not hasattr(cap, "cleanup_pending")))
            with self._lock:
                self._cap = None
            if imu_source is not None:
                try:
                    imu_source.close()
                except Exception:
                    pass
            with self._lock:
                self._imu_source = None
                self._status.update(running=False,
                                    udp_enabled=False, imu_bound=False,
                                    camera_source=None, camera_controls=None,
                                    camera_controls_source=None,
                                    camera_settings_capabilities=None,
                                    estimated_position_m=None, preview_ppm=None,
                                    telemetry=None,
                                    state=("error" if not summary.get("ok") else "stopped"))
                edit = self._status.get("control_edit")
                if isinstance(edit, dict) and edit.get("state") in ("pending", "applying"):
                    self._status["control_edit"] = {**edit, "state": "aborted",
                        "error": summary.get("error") or "owner stopped before confirmation"}
            if stream is not None:
                try:
                    summary["camera_restore"] = copy.deepcopy(self._status.get("camera_restore"))
                    stream.write(json.dumps({"event": "end", "valid": False,
                                             **summary}) + "\n")
                except (TypeError, ValueError, OSError):
                    pass
                try:
                    stream.close()
                except OSError:
                    pass
            try:
                self._emit("stopped", {"log": str(log_path), "summary": dict(summary)})
            except Exception:
                pass
            with self._lock:
                self._thread = None
