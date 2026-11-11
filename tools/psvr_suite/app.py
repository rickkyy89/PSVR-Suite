"""Suite utente persistente Tk per PSVR (Sessione/Tracking/Camera/Posizione/Ottica/Diagnostica).

Schema canonico backend (settings.validate_preferences, strict):
tracking_mode, fallback_3dof, reacquire_blend_ms, reacquire_speed_m_s,
initial_offset_m, head_offset_model_m, position_scale, position_filter,
min_cutoff_hz, beta, min_area, value_min, saturation_min, opening_size,
exposure, gain, fps, width, height, intrinsicsPath, rigPath, imu_rig,
optics_profile, tracking_config_path, imu_calibration_ms, imu_stale_ms,
imu_max_gap_us, camera_source.

Regole:
- Nessuna alterazione Python dell'orientamento; solo UI, validazione e orchestrazione.
- Solo stdlib + Tk (Tk importato solo dentro SuiteApp, mai in headless).
- Chiamate bloccanti su SerialWorker seriale, mai sul main Tk; eventi display
  in coda con throttle; poll stato ogni 2 s senza overlap e senza freeze (8 s cal).
- Stato da backend status(); campi mancanti -> "sconosciuto", mai inventati.
  Runtime.status()['driver_tracking'] e' autoritativo per modo/fallback/recovery;
  la stima worker non e' fidata quando l'IMU non e' valida.
- File utente NUOVO configurations/psvr_suite_user.json solo su salvataggio
   esplicito; baseline mai riscritte. Senza file utente si leggono i live driver
   prefs via preferences_from_driver(path=DEFAULT_VRSETTINGS) se esposto,
   altrimenti i default.
- Lock singleton OS-held; chiusura rilascia runtime.close() + tracking.stop()
  posseduti, senza fermare SteamVR esterno (solo Stop esplicito).
"""

from __future__ import annotations

import argparse
import copy
import json
import inspect
import math
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import cameras as cameras_module
from .camera_settings import module_for_source, validate_camera_profiles
from .camera_settings.common import native_manual_range
from . import i18n as i18n_module
from . import settings as settings_module
from .runtime import RuntimeController
from .tracking import TrackingWorker


ROOT_DEFAULT = Path(__file__).resolve().parents[2]
USER_PREFS_REL = Path("configurations") / "psvr_suite_user.json"
LOCK_REL = Path("logs") / "psvr_suite.lock"
STATUS_POLL_S = 2.0
DISPLAY_DRAIN_MAX = 32
SCONOSCIUTO = "sconosciuto"

# Avviso evidente prima di ogni apply freddo (driver/ottica): il tracking
# viene fermato e SteamVR riavviato con nuova calibrazione IMU.
# Costante storica in italiano (compat test); la GUI usa il catalogue i18n
# (calibration.notice) secondo la lingua corrente.
CALIBRATION_APPLY_NOTICE = (
    "Avviso: l'applicazione ferma il tracking e riavvia SteamVR; "
    "appoggia il visore e tienilo FERMO fino al termine della calibrazione IMU."
)

# Titolo finestra OS invariante per compat launcher (FindWindowW) e
# ui_snapshot: la traduzione riguarda solo l'header interno (header.title).
WINDOW_TITLE = i18n_module.WINDOW_TITLE


def _txt_lang(value, lang):
    """Variante localizzata di _txt (sconosciuto/unknown, si/yes, float 3f).

    Il marker storico backend 'sconosciuto' (e 'unknown') e' mappato al
    termine della lingua corrente; altri testi passano verbatim (mai
    inventare dati, mai tradurre reason tecnici).
    """
    if value is None:
        return i18n_module.unknown(lang)
    if isinstance(value, bool):
        return i18n_module.bool_text(value, lang)
    if isinstance(value, float):
        if not math.isfinite(value):
            return i18n_module.unknown(lang)
        return f"{value:.3f}"
    try:
        text = str(value).strip()
    except Exception:
        return i18n_module.unknown(lang)
    # B12-09: riusa il set canonico dei marker (case-insensitive).
    if text.casefold() in i18n_module.MARKERS_UNKNOWN:
        return i18n_module.unknown(lang)
    return text

# Chiavi canoniche attese dal backend strict (ordine stabile per i report).
CANONICAL_KEYS = (
    "tracking_mode", "fallback_3dof", "two_led_experimental", "reacquire_blend_ms",
    "reacquire_speed_m_s", "initial_offset_m", "head_offset_model_m",
    "position_scale", "position_filter", "min_cutoff_hz", "beta",
    "min_area", "value_min", "saturation_min", "opening_size",
    "exposure", "gain", "fps", "width", "height",
    "intrinsicsPath", "rigPath", "imu_rig",
    "optics_profile", "tracking_config_path",
    "imu_calibration_ms", "imu_stale_ms", "imu_max_gap_us",
    "camera_source",
)


# ---------------------------------------------------------------------------
# Registro videocamera 6DoF: etichetta umana (tendina) <-> id tecnico (prefs)
# ---------------------------------------------------------------------------

def camera_source_label(source_id):
    """Etichetta umana dell'id registrato da mostrare in tendina.

    Id sconosciuto: ritorna il testo stesso (nessuna camera inventata; la
    validazione segnalerà il valore come non supportato).
    """
    try:
        return cameras_module.get_camera_source(source_id)["label"]
    except ValueError:
        return str(source_id)


def camera_source_from_choice(choice):
    """Scelta della tendina (etichetta umana o id tecnico) -> id registrato.

    None se sconosciuto: il chiamante lascia il testo grezzo alla validazione,
    che lo rifiuta con messaggio esplicito (mai fallback silenzioso).
    """
    if not isinstance(choice, str):
        return None
    if cameras_module.is_valid_camera_source(choice):
        return choice
    return cameras_module.find_camera_source_by_label(choice)


def camera_settings_view(root, choice, tracking_status=None, requested=None):
    """UI-only dispatch. Module values never overwrite canonical PS3 drafts."""
    module = module_for_source(camera_source_from_choice(choice))
    return module.view(root, requested if requested is not None else {}, tracking_status)


def camera_settings_text(view, lang):
    return module_for_source(view["source"]).render(view, lang)


# ---------------------------------------------------------------------------
# Modello puro (senza Tk/camera/hardware; valida sullo schema canonico)
# ---------------------------------------------------------------------------

class AppModel:
    """Parsing tipato delle widget string + validazione strict backend."""

    TRACKING_MODES = ("3dof", "6dof")
    FILTERS = ("one_euro", "kalman_initial", "kalman_optimized")
    OPTICS_PROFILES = ("current", "identity", "mild", "medium",
                        "peripheral", "stronger")
    # Nessuna lista frozen di qui: la scelta videocamera 6DoF e' il registro
    # live (tools.psvr_suite.cameras). Id tecnici invarianti (mai tradotti);
    # la tendina mostra le etichette umane del registro. Le voci registrate
    # dopo l'import (register_camera_source) sono gia' accettate da validate,
    # in coerenza con settings e tracking.

    def __init__(self, prefs=None):
        cleaned = self.validate(dict(prefs) if prefs else self.default_prefs())
        self.values = cleaned

    @staticmethod
    def default_prefs():
        """Default live dal backend (percorsi file reali, mai vuoti)."""
        return copy.deepcopy(settings_module.default_preferences())

    # -- parsing scalari ----------------------------------------------------
    @staticmethod
    def parse_float(text, campo="valore"):
        if isinstance(text, bool):
            raise ValueError(f"{campo}: deve essere un numero finito")
        if isinstance(text, (int, float)):
            valore = float(text)
        elif isinstance(text, str):
            s = text.strip()
            if not s:
                raise ValueError(f"{campo}: valore vuoto")
            try:
                valore = float(s)
            except ValueError:
                raise ValueError(f"{campo}: numero non valido ({text!r})") from None
        else:
            raise ValueError(f"{campo}: numero non valido")
        if not math.isfinite(valore):
            raise ValueError(f"{campo}: deve essere finito")
        return valore

    @staticmethod
    def parse_int(text, campo="valore"):
        if isinstance(text, bool):
            raise ValueError(f"{campo}: deve essere un intero")
        if isinstance(text, int):
            return text
        if isinstance(text, float):
            if not math.isfinite(text) or int(text) != text:
                raise ValueError(f"{campo}: deve essere un intero")
            return int(text)
        if isinstance(text, str):
            s = text.strip()
            if not s:
                raise ValueError(f"{campo}: valore vuoto")
            try:
                f = float(s)
            except ValueError:
                raise ValueError(f"{campo}: intero non valido ({text!r})") from None
            if not math.isfinite(f) or int(f) != f:
                raise ValueError(f"{campo}: deve essere un intero")
            return int(f)
        raise ValueError(f"{campo}: intero non valido")

    @staticmethod
    def parse_bool(value, campo="opzione"):
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            s = value.strip().lower()
            if s in ("1", "true", "vero", "si", "sì", "on", "yes"):
                return True
            if s in ("0", "false", "falso", "no", "off"):
                return False
        raise ValueError(f"{campo}: deve essere vero/falso")

    @staticmethod
    def parse_vector3(value, campo="vettore"):
        if isinstance(value, (list, tuple)):
            parti = list(value)
        elif isinstance(value, str):
            s = value.strip().replace(";", " ").replace(",", " ")
            parti = s.split()
        else:
            raise ValueError(f"{campo}: servono 3 numeri (XYZ metri)")
        if len(parti) != 3:
            raise ValueError(f"{campo}: servono 3 numeri (XYZ metri)")
        try:
            numeri = [AppModel.parse_float(p, campo) for p in parti]
        except ValueError as exc:
            raise ValueError(str(exc)) from None
        return [float(v) for v in numeri]

    @staticmethod
    def format_vector3(vec):
        try:
            x, y, z = float(vec[0]), float(vec[1]), float(vec[2])
            return f"{x:.3f}, {y:.3f}, {z:.3f}"
        except (TypeError, ValueError, IndexError):
            return SCONOSCIUTO

    # -- validazione canonica strict ----------------------------------------
    @classmethod
    def validate(cls, data):
        """Tipizza stringhe UI -> canonico, poi backend strict (unknown reject).

        Ritorna dict nuovo validato. Solleva ValueError in italiano o dal backend.
        """
        if not isinstance(data, dict):
            raise ValueError("preferenze: deve essere un oggetto")
        noto = set(CANONICAL_KEYS)
        sconosciute = sorted(set(data) - (noto | {"camera_profiles"}))
        if sconosciute:
            raise ValueError("preferenze sconosciute: " + ", ".join(sconosciute))
        mancanti = sorted(noto - set(data))
        if mancanti:
            raise ValueError("preferenze mancanti: " + ", ".join(mancanti))
        tipizzato = {}
        if "camera_profiles" in data:
            tipizzato["camera_profiles"] = validate_camera_profiles(data["camera_profiles"])
        tipizzato["tracking_mode"] = data["tracking_mode"]
        try:
            tipizzato["fallback_3dof"] = cls.parse_bool(data["fallback_3dof"], "fallback_3dof")
            tipizzato["two_led_experimental"] = cls.parse_bool(data["two_led_experimental"], "two_led_experimental")
        except ValueError as exc:
            raise ValueError(str(exc)) from None
        tipizzato["reacquire_blend_ms"] = cls.parse_int(data["reacquire_blend_ms"], "reacquire_blend_ms")
        tipizzato["reacquire_speed_m_s"] = cls.parse_float(data["reacquire_speed_m_s"], "reacquire_speed_m_s")
        try:
            tipizzato["initial_offset_m"] = cls.parse_vector3(data["initial_offset_m"], "initial_offset_m")
        except ValueError as exc:
            raise ValueError(str(exc)) from None
        try:
            tipizzato["head_offset_model_m"] = cls.parse_vector3(data["head_offset_model_m"], "head_offset_model_m")
        except ValueError as exc:
            raise ValueError(str(exc)) from None
        tipizzato["position_scale"] = cls.parse_float(data["position_scale"], "position_scale")
        tipizzato["position_filter"] = data["position_filter"]
        tipizzato["min_cutoff_hz"] = cls.parse_float(data["min_cutoff_hz"], "min_cutoff_hz")
        tipizzato["beta"] = cls.parse_float(data["beta"], "beta")
        for chiave in ("min_area", "value_min", "saturation_min", "opening_size",
                       "exposure", "gain", "width", "height"):
            tipizzato[chiave] = cls.parse_int(data[chiave], chiave)
        # Stesso contratto del worker: intero positivo, anche da "75.0".
        tipizzato["fps"] = cls.parse_int(data["fps"], "fps")
        for chiave in ("intrinsicsPath", "rigPath", "imu_rig",
                       "optics_profile", "tracking_config_path",
                       "camera_source"):
            v = data[chiave]
            if not isinstance(v, str):
                raise ValueError(f"{chiave}: deve essere una stringa")
            tipizzato[chiave] = v
        for chiave in ("imu_calibration_ms", "imu_stale_ms", "imu_max_gap_us"):
            tipizzato[chiave] = cls.parse_int(data[chiave], chiave)
        # Range UI coerenti col backend strict + correzioni integrazione:
        # min_cutoff <= 40, opening solo 0/3/5, reacquire_speed >= 0.01.
        if tipizzato["tracking_mode"] not in cls.TRACKING_MODES:
            raise ValueError("tracking_mode: deve essere 3dof o 6dof")
        if not 0 <= tipizzato["reacquire_blend_ms"] <= 2000:
            raise ValueError("reacquire_blend_ms: deve essere 0..2000 ms")
        if not 0.01 <= tipizzato["reacquire_speed_m_s"] <= 10.0:
            raise ValueError("reacquire_speed_m_s: deve essere 0.01..10 m/s")
        for chiave in ("initial_offset_m", "head_offset_model_m"):
            if any(not -10.0 <= c <= 10.0 for c in tipizzato[chiave]):
                raise ValueError(f"{chiave}: ogni asse deve essere -10..10 m")
        if not tipizzato["position_scale"] > 0:
            raise ValueError("position_scale: deve essere positiva")
        if tipizzato["position_filter"] not in cls.FILTERS:
            raise ValueError("position_filter: deve essere one_euro, kalman_initial o kalman_optimized")
        if not 0 < tipizzato["min_cutoff_hz"] <= 40.0:
            raise ValueError("min_cutoff_hz: deve essere 0..40 Hz")
        if not tipizzato["beta"] >= 0:
            raise ValueError("beta: deve essere >= 0")
        if tipizzato["min_area"] < 1:
            raise ValueError("min_area: deve essere >= 1")
        for chiave in ("value_min", "saturation_min"):
            if not 0 <= tipizzato[chiave] <= 255:
                raise ValueError(f"{chiave}: deve essere 0..255")
        if tipizzato["opening_size"] not in (0, 3, 5):
            raise ValueError("opening_size: deve essere 0, 3 o 5")
        # Canonical exposure/gain remain the legacy PS3 draft even with PS4
        # selected. PS4 profile controls have a separate module and validator.
        module_for_source("pseye").validate_controls(
            {key: tipizzato[key] for key in ("exposure", "gain")})
        if not tipizzato["fps"] > 0:
            raise ValueError("fps: deve essere positivo")
        for chiave in ("width", "height"):
            if tipizzato[chiave] < 1:
                raise ValueError(f"{chiave}: deve essere >= 1")
        for chiave in ("intrinsicsPath", "rigPath", "imu_rig", "tracking_config_path"):
            if not tipizzato[chiave].strip():
                raise ValueError(f"{chiave}: percorso non vuoto richiesto")
        if tipizzato["optics_profile"] not in cls.OPTICS_PROFILES:
            raise ValueError("optics_profile: profilo non supportato")
        sorgente = tipizzato.get("camera_source")
        if not isinstance(sorgente, str) or not sorgente.strip():
            raise ValueError("camera_source: scelta videocamera richiesta")
        if not cameras_module.is_valid_camera_source(sorgente):
            raise ValueError(
                "camera_source: videocamera non supportata "
                "(disponibili: %s)" % ", ".join(cameras_module.camera_source_ids()))
        tipizzato["camera_source"] = sorgente
        for chiave in ("imu_calibration_ms", "imu_stale_ms", "imu_max_gap_us"):
            if tipizzato[chiave] < 1:
                raise ValueError(f"{chiave}: deve essere >= 1")
        # Validatore strict finale del backend (schema attuale atteso).
        try:
            return settings_module.validate_preferences(tipizzato)
        except ValueError as exc:
            raise ValueError(f"backend strict: {exc}") from None

    def to_prefs(self):
        return copy.deepcopy(self.values)

    def set_filter(self, nome):
        if nome not in self.FILTERS:
            raise ValueError("position_filter non supportato")
        self.values["position_filter"] = nome

    def set_tracking_mode(self, modo):
        if modo not in self.TRACKING_MODES:
            raise ValueError("tracking_mode non supportato")
        self.values["tracking_mode"] = modo


def _result_ok(result):
    """False backend -> task fallito (mai 'Completato' ingannevole)."""
    if result is True:
        return True
    if result is False or result is None:
        return False
    if isinstance(result, dict):
        if result.get("restart_required"):
            return False
        return bool(result.get("ok", False))
    if isinstance(result, int) and not isinstance(result, bool):
        return result == 0
    return bool(result)


# ---------------------------------------------------------------------------
# SerialWorker: un solo task alla volta, mai sul main Tk
# ---------------------------------------------------------------------------

class SerialWorker:
    """Esegue task bloccanti in serie su un thread dedicato."""

    def __init__(self, on_error=None):
        self._tasks: queue.Queue = queue.Queue()
        self._closing = False
        self._on_error = on_error
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="psvr-suite-worker")

    def start(self):
        self._thread.start()

    def submit(self, label, func, drop_if_pending=False):
        """Accoda un task; status usa drop_if_pending per non accodare oltre."""
        if self._closing:
            return False
        if drop_if_pending and not self._tasks.empty():
            return False
        try:
            self._tasks.put((label, func), block=False)
            return True
        except queue.Full:
            return False

    def pending_count(self):
        return self._tasks.qsize()

    def _loop(self):
        while True:
            item = self._tasks.get()
            if item is None:
                self._tasks.task_done()
                return
            label, func = item
            try:
                func()
            except Exception as exc:  # noqa: BLE001 - inoltrato come evento display
                if self._on_error is not None:
                    try:
                        self._on_error(label, exc)
                    except Exception:
                        pass
            finally:
                self._tasks.task_done()

    def shutdown(self, timeout=5.0):
        self._closing = True
        try:
            self._tasks.put(None, block=False)
        except queue.Full:
            pass
        self._thread.join(timeout=timeout)


# ---------------------------------------------------------------------------
# Lock singleton OS-held (stessa politica del monitor)
# ---------------------------------------------------------------------------

class SuiteLock:
    """Lock esclusivo OS-held: seconda istanza non puo' partire/USB."""

    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError(
                "Suite già attiva o discovery occupata; chiudere l'altra istanza"
            ) from exc
        self.file = handle

    def close(self):
        if self.file is not None:
            try:
                self.file.close()
            finally:
                self.file = None


# ---------------------------------------------------------------------------
# Helpers status "sconosciuto se manca" (mai inventare dati)
# ---------------------------------------------------------------------------

def _txt(value):
    if value is None:
        return SCONOSCIUTO
    if isinstance(value, bool):
        return "sì" if value else "no"
    if isinstance(value, float):
        if not math.isfinite(value):
            return SCONOSCIUTO
        return f"{value:.3f}"
    text = str(value).strip()
    return text if text else SCONOSCIUTO


def summarize_status(runtime_status, tracking_status):
    """Modo/fallback autoritativi da Runtime driver_tracking; mai stimati.

    Se driver_tracking manca e l'IMU non e' valida, modo/fallback restano
    'sconosciuto' (la stima worker 3dof non e' fidata da sola).
    """
    rt = runtime_status if isinstance(runtime_status, dict) else {}
    tw = tracking_status if isinstance(tracking_status, dict) else {}
    dt = rt.get("driver_tracking")
    dt = dt if isinstance(dt, dict) else {}
    diag = tw.get("diagnostics", {}) if isinstance(tw.get("diagnostics"), dict) else {}
    imu = tw.get("imu", {}) if isinstance(tw.get("imu"), dict) else {}
    imu_valid = imu.get("valid")
    if dt:
        modo_richiesto = dt.get("mode_requested", dt.get("tracking_mode",
                                rt.get("mode_requested", tw.get("mode_requested", SCONOSCIUTO))))
        modo_effettivo = dt.get("mode_actual", dt.get("mode",
                                rt.get("mode_actual", SCONOSCIUTO)))
        fallback = dt.get("fallback_active", dt.get("fallback", None))
        recovery = dt.get("recovery", dt.get("reacquire", dt.get("recovery_state", None)))
    else:
        modo_richiesto = rt.get("mode_requested", tw.get("mode_requested", SCONOSCIUTO))
        if imu_valid is False:
            modo_effettivo = SCONOSCIUTO
            fallback = None
        else:
            modo_effettivo = rt.get("mode_actual", tw.get("mode_actual", SCONOSCIUTO))
            fallback = tw.get("fallback_active", tw.get("fallback_attivo", None))
        recovery = None
    return {
        "stato": rt.get("state", rt.get("stato", rt.get("mode", SCONOSCIUTO))),
        "sorgente": rt.get("source", rt.get("sorgente", SCONOSCIUTO)),
        "readiness": rt.get("readiness", rt.get("prontezza", SCONOSCIUTO)),
        "modo_richiesto": modo_richiesto,
        "modo_effettivo": modo_effettivo,
        "fallback_attivo": fallback,
        "recovery": recovery,
        "led": diag.get("led_count", diag.get("led", None)),
        "rms": diag.get("rms_px", diag.get("rms", None)),
        "age": diag.get("age_s", tw.get("age_s", None)),
        "imu_session": imu.get("session_id", tw.get("session_id", None)),
        "imu_epoch": imu.get("recenter_epoch", tw.get("recenter_epoch", None)),
        "pos": dt.get("position_m",tw.get("estimated_position_m", tw.get("pos_m", None))),
        "pos_mode": tw.get("position_mode", tw.get("mode", None)),
    }


def _call_apply_driver(runtime, prefs, apply_optics):
    """Chiama apply_driver con apply_optics=True solo dal pulsante esplicito.

    I mock accettano il kwarg; il backend reale in aggiornamento lo espone,
    quello precedente solleva TypeError e si ripiega senza flag.
    """
    if apply_optics:
        try:
            return runtime.apply_driver(prefs, apply_optics=True)
        except TypeError:
            return runtime.apply_driver(prefs)
    try:
        params = inspect.signature(runtime.apply_driver).parameters
    except (TypeError, ValueError):
        params = {}
    if "apply_optics" in params:
        return runtime.apply_driver(prefs, apply_optics=False)
    return runtime.apply_driver(prefs)


# ---------------------------------------------------------------------------
# Anteprima Camera responsive (Tk + resize smooth con lazy import in metodo)
# ---------------------------------------------------------------------------
# Nessun import cv2/numpy a livello modulo (headless resta senza dipendenze
# display): lo scaling continuo usa lazy import DENTRO _refresh_preview,
# con fallback a zoom/subsample interi PhotoImage se cv2 non disponibile.

GUI_FIT_MAX_W = 1600
GUI_FIT_MAX_H = 1200

def _suite_icon_path(root_path):
    """Percorso icona suite se presente (generata da altro agente)."""
    try:
        base = Path(root_path) / "tools" / "psvr_suite" / "assets" / "psvr_suite.ico"
        if base.is_file():
            return base
        local = Path(__file__).resolve().parent / "assets" / "psvr_suite.ico"
        if local.is_file():
            return local
    except Exception:
        pass
    return None


def _apply_suite_icon(tk_root, root_path):
    """Applica iconbitmap solo se il file .ico esiste; mai fallire."""
    try:
        ico = _suite_icon_path(root_path)
        if ico is not None:
            tk_root.iconbitmap(str(ico))
    except Exception:
        pass


def _apply_initial_geometry(tk_root, default="1280x820"):
    """Finestra iniziale più grande, adattata allo schermo se leggibile."""
    try:
        sw = int(tk_root.winfo_screenwidth())
        sh = int(tk_root.winfo_screenheight())
    except Exception:
        try:
            tk_root.geometry(default)
        except Exception:
            pass
        return
    try:
        w = min(max(1080, int(sw * 0.82)), max(960, sw - 40))
        h = min(max(760, int(sh * 0.82)), max(640, sh - 60))
        if w < 960 or h < 640:
            tk_root.geometry(default)
            return
        tk_root.geometry(f"{w}x{h}")
    except Exception:
        try:
            tk_root.geometry(default)
        except Exception:
            pass


def _preview_zoom_for(base_w, base_h, avail_w, avail_h, max_zoom=3):
    """Fallback intero PhotoImage (usato solo se cv2 non disponibile).

    Ritorna (zoom, subsample): mai entrambi >1. (1, 1) se non stimabile.
    Il percorso principale e' lo scaling continuo smooth (_preview_fit_size
    + cv2.resize lazy in _refresh_preview); questo resta per fake-Tk/test
    e ambienti senza OpenCV. Aspect mai stirato.
    """
    try:
        bw = int(base_w)
        bh = int(base_h)
        aw = int(avail_w)
        ah = int(avail_h)
    except (TypeError, ValueError):
        return (1, 1)
    if bw <= 0 or bh <= 0 or aw <= 0 or ah <= 0:
        return (1, 1)
    try:
        mz = int(max_zoom)
    except (TypeError, ValueError):
        mz = 3
    if mz < 1:
        mz = 1
    if mz > 4:
        mz = 4
    if bw > aw or bh > ah:
        s = max((bw + aw - 1) // aw, (bh + ah - 1) // ah)
        if s < 1:
            s = 1
        if s > 8:
            s = 8
        return (1, s)
    try:
        z = min(aw // bw, ah // bh)
    except Exception:
        return (1, 1)
    if z < 1:
        z = 1
    if z > mz:
        z = mz
    return (z, 1)


def _preview_fit_size(base_w, base_h, avail_w, avail_h,
                      max_w=GUI_FIT_MAX_W, max_h=GUI_FIT_MAX_H):
    """Fit continuo exact-fit preservando l'aspect (default bound 1600x1200).

    Ritorna (target_w, target_h) interi >=1 che inscrivono la base nel
    contenitore senza stirarla. A differenza di _preview_zoom_for (solo
    fattori interi, es. 480x360 fermo in un pannello 900x660 perche' 2x
    non entra), qui lo scaling e' continuo: es. 640x480 in 900x660 ->
    880x660. (0, 0) se non stimabile.
    """
    try:
        bw = int(base_w)
        bh = int(base_h)
        aw = int(avail_w)
        ah = int(avail_h)
        mw = int(max_w)
        mh = int(max_h)
    except (TypeError, ValueError):
        return (0, 0)
    if bw <= 0 or bh <= 0 or aw <= 0 or ah <= 0:
        return (0, 0)
    if mw < 1:
        mw = GUI_FIT_MAX_W
    if mh < 1:
        mh = GUI_FIT_MAX_H
    try:
        scale = min(float(aw) / float(bw), float(ah) / float(bh),
                    float(mw) / float(bw), float(mh) / float(bh))
    except (TypeError, ValueError, ZeroDivisionError):
        return (0, 0)
    if not scale or scale <= 0 or scale != scale:  # NaN guard senza math
        return (0, 0)
    tw = int(round(bw * scale))
    th = int(round(bh * scale))
    if tw < 1:
        tw = 1
    if th < 1:
        th = 1
    if tw > mw:
        tw = mw
    if th > mh:
        th = mh
    return (tw, th)


def _decode_ppm_to_rgb(raw):
    """Decodifica PPM P6 (stdlib, niente numpy): ritorna (w, h, rgb_bytes).

    Conserva la decodifica PPM esistente; tollera header 'P6\\nW H\\n255\\n'
    con commenti/spazi extra. (0, 0, b'') in caso di dati non validi.
    """
    try:
        if isinstance(raw, bytearray):
            data = bytes(raw)
        elif isinstance(raw, bytes):
            data = raw
        else:
            return (0, 0, b"")
        if len(data) < 11 or not data.startswith(b"P6"):
            return (0, 0, b"")
        pos = 2
        n = len(data)

        def _skip():
            nonlocal pos
            while pos < n:
                c = data[pos:pos + 1]
                if c in b" \t\n\r":
                    pos += 1
                    continue
                if c == b"#":
                    while pos < n and data[pos:pos + 1] not in b"\n":
                        pos += 1
                    continue
                break

        def _token():
            nonlocal pos
            _skip()
            start = pos
            while pos < n and data[pos:pos + 1] not in b" \t\n\r":
                if data[pos:pos + 1] == b"#":
                    break
                pos += 1
            return data[start:pos]

        wtok = _token()
        htok = _token()
        maxtok = _token()
        if not wtok or not htok or not maxtok:
            return (0, 0, b"")
        try:
            w = int(wtok)
            h = int(maxtok and htok)
            vmax = int(maxtok)
        except (TypeError, ValueError):
            return (0, 0, b"")
        if w <= 0 or h <= 0 or vmax != 255:
            return (0, 0, b"")
        # Un singolo whitespace dopo maxval precede i pixel.
        if pos < n and data[pos:pos + 1] in b" \t\n\r":
            pos += 1
        else:
            return (0, 0, b"")
        need = w * h * 3
        if need <= 0 or len(data) - pos < need:
            return (0, 0, b"")
        return (w, h, data[pos:pos + need])
    except Exception:
        return (0, 0, b"")


class _UseBaseFallback(Exception):
    """Segnale interno: il fit smooth coincide col nativo, usa la base."""


def _encode_rgb_to_ppm(width, height, rgb):
    """Codifica pixel RGB in PPM P6 per Tk.PhotoImage(data=..., format='PPM')."""
    try:
        w = int(width)
        h = int(height)
        if w <= 0 or h <= 0:
            return b""
        if isinstance(rgb, bytearray):
            pix = bytes(rgb)
        elif isinstance(rgb, bytes):
            pix = rgb
        else:
            return b""
        if len(pix) < w * h * 3:
            return b""
        pix = pix[:w * h * 3]
        return ("P6\n%d %d\n255\n" % (w, h)).encode("ascii") + pix
    except Exception:
        return b""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

# Nomi tab canonici (IT): ordine fissato, usato anche da main per
# tabs.select(tab_names.index(args.tab)). Non riordinare.
_TAB_CANONICAL = ('Sessione', 'Tracking', 'Camera', 'Posizione', 'Ottica', 'Diagnostica')

# Alias EN->IT per --tab (UI bilingue, CLI solo IT prima di B12-07).
# Chiavi minuscole: confronto case-insensitive, banale e senza effetti
# su label UI/i18n/ordine tab.
_TAB_ALIASES = {
    'sessione': 'Sessione',
    'session': 'Sessione',
    'tracking': 'Tracking',
    'camera': 'Camera',
    'posizione': 'Posizione',
    'position': 'Posizione',
    'ottica': 'Ottica',
    'optics': 'Ottica',
    'diagnostica': 'Diagnostica',
    'diagnostics': 'Diagnostica',
}


def normalize_tab_argv(argv):
    """Sostituisce gli alias EN di --tab con il nome IT prima di argparse.

    Gestisce '--tab VALORE' e '--tab=VALORE', case-insensitive.
    Ritorna una nuova lista; argv=None passa attraverso invariato.
    Valori ignoti o assenti restano invariati (argparse li rifiuta come prima).
    """
    if argv is None:
        return None
    out = list(argv)
    i = 0
    while i < len(out):
        item = out[i]
        if item == '--tab':
            if i + 1 < len(out):
                canon = _TAB_ALIASES.get(str(out[i + 1]).lower())
                if canon is not None:
                    out[i + 1] = canon
            i += 2
        elif isinstance(item, str) and item.startswith('--tab='):
            val = item[len('--tab='):]
            canon = _TAB_ALIASES.get(val.lower())
            if canon is not None:
                out[i] = '--tab=' + canon
            i += 1
        else:
            i += 1
    return out


def build_arg_parser():
    p = argparse.ArgumentParser(
        description="Suite utente persistente PSVR (Tk). Nessuna USB implicita.")
    p.add_argument("--headless-check", action="store_true",
                   help="Valida import completi + schema attuale e esce.")
    p.add_argument('--tab',choices=_TAB_CANONICAL,default='Sessione')
    p.add_argument("--root", default=str(ROOT_DEFAULT),
                   help="Radice repo (default: radice rilevata).")
    p.add_argument("--preferences", default=None,
                   help="File preferenze esplicito da caricare.")
    p.add_argument("--start-vr", action="store_true",
                   help="Avvia VR dopo init (runtime poi tracking, con guardia).")
    p.add_argument("--attach", action="store_true",
                   help="Solo tracking verso VR esistente, nessun cambio modo hardware.")
    return p


def headless_check(root_path, preferences_path=None):
    """Import reali + schema attuale; nessun Tk/hardware. Ritorna exit code."""
    print("Controllo headless suite: import reali + schema attuale, nessun Tk/hardware.")
    if "tkinter" in sys.modules:
        print("  ERRORE: tkinter già importato in headless")
        return 2
    # Import reali obbligatori: assenti -> nonzero chiaro, niente GUI finta.
    try:
        import tools.psvr_suite.settings as st
        import tools.psvr_suite.runtime as rt
        import tools.psvr_suite.tracking as tw
    except Exception as exc:
        print(f"  ERRORE import backend: {exc}")
        return 2
    for nome in ("default_preferences", "validate_preferences",
                 "load_preferences", "save_preferences"):
        if not hasattr(st, nome):
            print(f"  ERRORE: settings.{nome} mancante")
            return 2
        print(f"  OK: settings.{nome}")
    for nome in ("status", "start_vr", "stop_vr", "to_cinema",
                 "recenter", "recalibrate", "show_grid", "apply_driver", "close"):
        if not hasattr(rt.RuntimeController, nome):
            print(f"  ERRORE: RuntimeController.{nome} mancante")
            return 2
        print(f"  OK: RuntimeController.{nome}")
    for nome in ("status", "start", "update_settings", "realign",
                 "reset_relative", "stop"):
        if not hasattr(tw.TrackingWorker, nome):
            print(f"  ERRORE: TrackingWorker.{nome} mancante")
            return 2
        print(f"  OK: TrackingWorker.{nome}")
    try:
        prefs = st.default_preferences()
        cleaned = st.validate_preferences(copy.deepcopy(prefs))
    except Exception as exc:
        print(f"  ERRORE schema default backend: {exc}")
        return 2
    if sorted(cleaned.keys()) != sorted(CANONICAL_KEYS):
        print("  ERRORE chiavi canoniche: atteso "
              f"{sorted(CANONICAL_KEYS)}, trovato {sorted(cleaned.keys())}")
        return 2
    # Default percorsi reali non vuoti + valori integrazione.
    for chiave in ("intrinsicsPath", "rigPath", "imu_rig"):
        if not isinstance(cleaned.get(chiave), str) or not cleaned[chiave].strip():
            print(f"  ERRORE default {chiave}: percorso reale richiesto")
            return 2
    attesi = {"tracking_mode": "6dof", "fallback_3dof": False,
              "reacquire_blend_ms": 150, "reacquire_speed_m_s": 1.5,
              "position_scale": 1.0, "position_filter": "one_euro",
              "min_cutoff_hz": 2.5, "beta": 8.0, "min_area": 6,
              "value_min": 60, "saturation_min": 90, "opening_size": 0,
              "exposure": 13, "gain": 0, "fps": 75, "width": 640,
               "height": 480, "optics_profile": "identity",
               "imu_calibration_ms": 8000, "imu_stale_ms": 100,
               "imu_max_gap_us": 5000, "camera_source": "pseye"}
    for chiave, valore in attesi.items():
        if cleaned.get(chiave) != valore:
            print(f"  ERRORE default {chiave}: atteso {valore!r}, trovato "
                  f"{cleaned.get(chiave)!r}")
            return 2
    # AppModel sullo stesso schema canonico (round-trip incluse stringhe UI).
    try:
        model_ok = AppModel.validate(copy.deepcopy(cleaned))
        ui_strings = copy.deepcopy(cleaned)
        ui_strings["reacquire_blend_ms"] = str(ui_strings["reacquire_blend_ms"])
        ui_strings["reacquire_speed_m_s"] = str(ui_strings["reacquire_speed_m_s"])
        ui_strings["initial_offset_m"] = "0.000, 0.000, 0.000"
        ui_strings["head_offset_model_m"] = "0.000, 0.000, 0.000"
        AppModel.validate(ui_strings)
        _ = model_ok
    except Exception as exc:
        print(f"  ERRORE AppModel vs backend: {exc}")
        return 2
    if preferences_path:
        try:
            loaded = st.load_preferences(preferences_path)
            st.validate_preferences(loaded)
            print(f"  OK preferenze caricate: {preferences_path}")
        except Exception as exc:
            print(f"  ERRORE preferenze {preferences_path}: {exc}")
            return 2
    print("  OK: import reali + schema canonico validi, nessun hardware toccato.")
    _ = root_path
    return 0


# ---------------------------------------------------------------------------
# SuiteApp Tk (import Tk solo qui: headless resta senza display)
# ---------------------------------------------------------------------------

class SuiteApp:
    """Finestra principale persistente. Costruita solo con Tk disponibile."""

    def __init__(self, tk_root, root_path, preferences_path=None,
                 start_vr=False, attach=False, runtime=None, tracking=None,
                 settings_mod=None, language=None):
        import tkinter as tk
        from tkinter import ttk
        self.tk = tk
        self.ttk = ttk
        self.root = tk_root
        self.root_path = Path(root_path)
        self.preferences_arg = preferences_path
        self.user_path = self.root_path / USER_PREFS_REL
        self.logs_dir = self.root_path / "logs"
        self._settings = settings_mod if settings_mod is not None else settings_module
        self.display: queue.Queue = queue.Queue(maxsize=512)
        # Lingua UI (file separato configurations/psvr_suite_ui.json, mai
        # dentro settings.py / schema canonico / driver). Default italiano;
        # valore sconosciuto -> fallback grazioso a italiano.
        if language is None:
            try:
                self.lang = i18n_module.load_language(self.root_path)
            except Exception:
                self.lang = i18n_module.DEFAULT
        else:
            self.lang = i18n_module.normalize_language(language)
        self._static: list = []
        self._tab_order: list = []
        self._lang_updating = False
        self.serial = SerialWorker(on_error=self._on_worker_error)
        self.serial.start()
        rt_cls = runtime if runtime is not None else RuntimeController
        tw_cls = tracking if tracking is not None else TrackingWorker
        # Callback unica compatibile con entrambe le firme backend:
        # Runtime on_event(event-dict), Tracking on_event(kind, payload).
        self.runtime = rt_cls(self.root_path, self._on_backend_event)
        self.tracking = tw_cls(self.root_path, self._on_backend_event)
        initial = self._load_initial_prefs()
        self.model = AppModel(initial)
        self.closing = False
        self._pending = 0
        self._pending_lock = threading.Lock()
        self._status_pending = False
        self._poll_stop = threading.Event()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True,
                                             name="psvr-suite-poll")
        self._last_status = ({}, {})
        self._preview_img = None
        self._preview_base = None
        self._preview_base_size = (0, 0)
        self._preview_ppm_raw = None
        self._preview_resize_after = None
        self.preview_frame = None
        self._build_widgets()
        self._apply_prefs_to_widgets(self.model.to_prefs())
        try:
            self.var_preview_info.set(i18n_module.t(self.lang, "camera.preview_waiting"))
        except Exception:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(120, self._drain_display)
        self._poll_thread.start()
        if attach and start_vr:
            self.var_msg.set(i18n_module.t(self.lang, "header.attach_incompatible"))
            start_vr = False
        if attach:
            self.root.after(200, self.on_attach)
        elif start_vr:
            self.root.after(200, self.on_start_vr)

    # -- prefs iniziali ------------------------------------------------------
    def _load_initial_prefs(self):
        """--preferences > user.json > live driver prefs > default. Mai scrivere."""
        candidates = []
        if self.preferences_arg:
            candidates.append(Path(self.preferences_arg))
        candidates.append(self.user_path)
        for path in candidates:
            try:
                if path.is_file():
                    return self._settings.load_preferences(str(path))
            except Exception as exc:
                self._note(i18n_module.t(
                    getattr(self, "lang", i18n_module.DEFAULT),
                    "header.prefs_note_bad_file", path=str(path), errore=str(exc)))
                break
        getter = getattr(self._settings, "preferences_from_driver", None)
        if callable(getter):
            try:
                live = getter(path=self._settings.DEFAULT_VRSETTINGS)
                return self._settings.validate_preferences(live)
            except Exception as exc:
                self._note(i18n_module.t(
                    getattr(self, "lang", i18n_module.DEFAULT),
                    "header.prefs_note_live_failed", errore=str(exc)))
        return self._settings.default_preferences()

    def _note(self, text):
        try:
            self.display.put_nowait(("nota", {"testo": str(text)}))
        except queue.Full:
            pass

    # -- i18n: catalogue statico, live switch senza hardware --------------------
    def _L(self, key, **kwargs):
        return i18n_module.t(getattr(self, "lang", i18n_module.DEFAULT), key, **kwargs)

    def _register(self, widget, key):
        """Registra un widget con testo statico per il live-retranslate."""
        try:
            self._static.append((widget, str(key)))
        except Exception:
            pass
        return widget

    def _set_text(self, widget, text):
        try:
            widget.configure(text=text)
        except Exception:
            pass

    def _tab_key(self, key):
        """Crea un tab registrando la chiave per il retranslate live."""
        frame = self.ttk.Frame(self.tabs, padding=12)
        self.tabs.add(frame, text=self._L(key))
        try:
            self._tab_order.append(str(key))
        except Exception:
            pass
        return frame

    def _row_key(self, parent, r, key, widget, note_key=None):
        """Riga label+widget con chiavi catalogue (live-retranslate)."""
        lbl = self.ttk.Label(parent, text=self._L(key))
        lbl.grid(row=r, column=0, sticky="w", pady=3, padx=(0, 8))
        self._register(lbl, key)
        widget.grid(row=r, column=1, sticky="ew", pady=3)
        parent.columnconfigure(1, weight=1)
        if note_key:
            note = self.ttk.Label(parent, text=self._L(note_key), foreground="#5a6b80")
            note.grid(row=r, column=2, sticky="w", padx=(8, 0))
            self._register(note, note_key)

    def _action_button_key(self, parent, chiave, text_key, comando):
        btn = self.ttk.Button(parent, text=self._L(text_key), command=comando)
        self.buttons[chiave] = btn
        self._register(btn, text_key)
        return btn

    def _guide_label(self, parent, key, **grid):
        lbl = self.ttk.Label(parent, text=self._L(key), foreground=grid.pop("foreground", "#5a6b80"),
                              wraplength=grid.pop("wraplength", 900))
        lbl.grid(**grid)
        self._register(lbl, key)
        # Conserva il colore originale per il retranslate (che riusa configure).
        return lbl

    def _guide_warning(self, parent, key, **grid):
        grid.setdefault("foreground", "#8a2b00")
        return self._guide_label(parent, key, **grid)

    def language_display(self):
        return "Italiano" if getattr(self, "lang", "it") == "it" else "English"

    @staticmethod
    def _display_to_lang(value):
        try:
            s = str(value).strip().lower()
        except Exception:
            return i18n_module.DEFAULT
        if s.startswith("ital"):
            return "it"
        if s.startswith("engl"):
            return "en"
        return i18n_module.normalize_language(value)

    def _on_language_option(self, value=None):
        """Callback selector lingua (main thread Tk): live switch, no hardware."""
        if getattr(self, "_lang_updating", False):
            return
        try:
            current = self.var_language.get() if value is None else value
        except Exception:
            return
        self.set_language(self._display_to_lang(current), save=True)

    def set_language(self, lang, save=True):
        """Cambia lingua GUI live: solo testi statici, mai prefs/hardware/SteamVR.

        Non tocca tracking/runtime, non rilegge ne' riscrive le preferenze
        utente canoniche e non resetta le widget (prefs non salvate intatte).
        Persistenza atomica separata in configurations/psvr_suite_ui.json.
        Ritorna la lingua normalizzata. Mai solleva per widget mancanti
        (shell fake-Tk nei test): aggiorna best-effort.
        """
        code = i18n_module.normalize_language(lang)
        self.lang = code
        if save:
            try:
                i18n_module.save_language(self.root_path, code)
            except Exception as exc:
                try:
                    self.var_msg.set(self._L("header.language_save_failed", errore=str(exc)))
                except Exception:
                    pass
        # Evita ricorsione OptionMenu -> set_language -> var set -> callback.
        self._lang_updating = True
        try:
            try:
                self.var_language.set("Italiano" if code == "it" else "English")
            except Exception:
                pass
        finally:
            self._lang_updating = False
        self._retranslate()
        try:
            self.var_msg.set(self._L("header.language_switched"))
        except Exception:
            pass
        return code

    def _retranslate(self):
        """Riapplica tutti i testi statici registrati + tabs/pulsanti speciali."""
        for widget, key in list(getattr(self, "_static", [])):
            try:
                widget.configure(text=self._L(key))
            except Exception:
                pass
        # Tabs (ordine di creazione).
        try:
            for idx, key in enumerate(list(getattr(self, "_tab_order", []))):
                try:
                    self.tabs.tab(idx, text=self._L(key))
                except Exception:
                    pass
        except Exception:
            pass
        # Radio/check con testi statici (non i valori tecnici).
        try:
            if getattr(self, "rb_3dof", None) is not None:
                self.rb_3dof.configure(text="3DoF")
            if getattr(self, "rb_6dof", None) is not None:
                self.rb_6dof.configure(text="6DoF")
            if getattr(self, "chk_fallback", None) is not None:
                self.chk_fallback.configure(text=self._L("tracking.fallback_check"))
            if getattr(self, "chk_two_led", None) is not None:
                self.chk_two_led.configure(text=self._L("tracking.two_led_check"))
            for rb, key in list(getattr(self, "_filter_radios", [])):
                try:
                    rb.configure(text=self._L(key))
                except Exception:
                    pass
        except Exception:
            pass
        # Header + pulsante salva (fuori da _static per compat shell vecchie).
        for attr, key in (("lbl_header", "header.title"),
                          ("lbl_lang", "header.language"),
                          ("lbl_session_guide", "session.guide"),
                          ("lbl_camera_guide", "camera.guide"),
                          ("lbl_camera_warning", "camera.warning"),
                          ("lbl_position_guide", "position.guide"),
                          ("lbl_optics_guide", "optics.guide"),
                          ("lbl_diag_guide", "diag.guide")):
            try:
                w = getattr(self, attr, None)
                if w is not None:
                    w.configure(text=self._L(key))
            except Exception:
                pass
        # Mappatura pulsanti -> chiavi catalogue.
        btn_keys = {"save": "footer.save", "start": "session.btn_start",
                    "attach": "session.btn_attach", "stop": "session.btn_stop",
                    "cinema": "session.btn_cinema", "apply_driver": "session.btn_apply_driver",
                    "apply_tracking": "tracking.btn_apply", "reset_relative": "tracking.btn_reset",
                    "attach_camera": "camera.btn_attach", "apply_camera": "camera.btn_apply",
                    "ps4_camera": "ps4.open",
                    "apply_position": "position.btn_apply", "grid": "optics.btn_grid",
                    "apply_optics": "optics.btn_apply", "recenter": "diag.btn_recenter",
                    "realign": "diag.btn_realign", "logs": "diag.btn_logs"}
        for chiave, tkey in btn_keys.items():
            try:
                btn = self.buttons.get(chiave)
                if btn is not None:
                    btn.configure(text=self._L(tkey))
            except Exception:
                pass
        # Placeholder anteprima solo se nessuna immagine live (non clobbera il backend).
        try:
            if getattr(self, "_preview_base", None) is None:
                self.preview_widget.configure(text=self._L("camera.preview_placeholder"))
        except Exception:
            pass
        # Info anteprima iniziale: aggiorna solo se e' il messaggio di attesa
        # (in una delle due lingue) o vuoto, mai sopra testo live del backend.
        try:
            cur = self.var_preview_info.get()
            waiting = {i18n_module.t("it", "camera.preview_waiting"),
                       i18n_module.t("en", "camera.preview_waiting"),
                       i18n_module.t("it", "camera.preview_unavailable"),
                       i18n_module.t("en", "camera.preview_unavailable"), ""}
            if cur in waiting:
                self.var_preview_info.set(self._L("camera.preview_waiting"))
        except Exception:
            pass

        self._refresh_camera_settings(reload_profile=False)

    # -- costruzione UI -------------------------------------------------------
    def _build_widgets(self):
        tk, ttk = self.tk, self.ttk
        camera_initial = self.model.to_prefs()
        # Invariante compat: titolo finestra OS Cercato da launcher/snapshot.
        self.root.title(WINDOW_TITLE)
        try:
            _apply_initial_geometry(self.root, default="1280x820")
        except Exception:
            try:
                self.root.geometry("1280x820")
            except Exception:
                pass
        self.root.minsize(960, 640)
        try:
            _apply_suite_icon(self.root, self.root_path)
        except Exception:
            pass
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)
        outer.rowconfigure(2, weight=0)
        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        self.lbl_header = ttk.Label(header, text=self._L("header.title"),
                                    font=("Segoe UI", 16, "bold"))
        self.lbl_header.grid(row=0, column=0, sticky="w")
        lang_box = ttk.Frame(header)
        lang_box.grid(row=0, column=1, sticky="e", padx=(12, 0))
        self.lbl_lang = ttk.Label(lang_box, text=self._L("header.language"))
        self.lbl_lang.grid(row=0, column=0, sticky="e", padx=(0, 6))
        _lang_initial = "Italiano" if getattr(self, "lang", "it") == "it" else "English"
        self.var_language = tk.StringVar(value=_lang_initial)
        try:
            self.opt_language = ttk.OptionMenu(
                lang_box, self.var_language, _lang_initial,
                "Italiano", "English", command=self._on_language_option)
        except Exception:
            # Shell fake-Tk nei test: OptionMenu non disponibile, resta la var.
            self.opt_language = None
        else:
            self.opt_language.grid(row=0, column=1, sticky="e")
        self.tabs = ttk.Notebook(outer)
        self.tabs.grid(row=1, column=0, sticky="nsew", pady=(8, 8))
        _unk = i18n_module.unknown(getattr(self, "lang", i18n_module.DEFAULT))
        self.var_stato = tk.StringVar(value=_unk)
        self.var_sorgente = tk.StringVar(value=_unk)
        self.var_readiness = tk.StringVar(value=_unk)
        self.var_modo_richiesto = tk.StringVar(value=_unk)
        self.var_modo_effettivo = tk.StringVar(value=_unk)
        self.var_fallback_diag = tk.StringVar(value=_unk)
        self.var_recovery = tk.StringVar(value=_unk)
        self.var_tracking_mode = tk.StringVar(value="6dof")
        self.var_fallback = tk.BooleanVar(value=False)
        self.var_two_led = tk.BooleanVar(value=False)
        self.var_blend = tk.StringVar(value="150")
        self.var_speed = tk.StringVar(value="1.5")
        self.var_cal = tk.StringVar(value="8000")
        self.var_stale = tk.StringVar(value="100")
        self.var_gap = tk.StringVar(value="5000")
        self.var_tracking_path = tk.StringVar(value="config.json")
        self.var_value_min = tk.IntVar(value=camera_initial["value_min"])
        self.var_min_area = tk.IntVar(value=camera_initial["min_area"])
        self.var_exposure = tk.IntVar(value=camera_initial["exposure"])
        self.var_gain = tk.IntVar(value=camera_initial["gain"])
        self.var_saturation = tk.StringVar(value=str(camera_initial["saturation_min"]))
        self.var_opening = tk.StringVar(value=str(camera_initial["opening_size"]))
        self.var_fps = tk.StringVar(value=str(camera_initial["fps"]))
        self.var_width = tk.StringVar(value=str(camera_initial["width"]))
        self.var_height = tk.StringVar(value=str(camera_initial["height"]))
        self.var_intrinsics = tk.StringVar(value=camera_initial["intrinsicsPath"])
        self.var_rig = tk.StringVar(value=camera_initial["rigPath"])
        self.var_imu_rig = tk.StringVar(value=camera_initial["imu_rig"])
        self.var_preview_info = tk.StringVar(value=self._L("camera.preview_unavailable"))
        self.var_preview_detail = tk.StringVar(value="")
        self.var_filter = tk.StringVar(value="one_euro")
        self.var_cutoff = tk.StringVar(value="2.5")
        self.var_beta = tk.StringVar(value="8")
        self.var_scale = tk.StringVar(value="1.0")
        self.var_head_offset = tk.StringVar(value="0.000, 0.000, 0.000")
        self.var_initial_offset = tk.StringVar(value="0.000, 0.000, 0.000")
        self.var_optics = tk.StringVar(value="identity")
        # La tendina videocamera espone etichette umane; le prefs conservano
        # l'id tecnico (mappatura camera_source_label/from_choice).
        self.var_camera_source = tk.StringVar(value=camera_source_label(
            cameras_module.DEFAULT_CAMERA_SOURCE))
        self.var_led = tk.StringVar(value=_unk)
        self.var_rms = tk.StringVar(value=_unk)
        self.var_age = tk.StringVar(value=_unk)
        self.var_imu_session = tk.StringVar(value=_unk)
        self.var_imu_epoch = tk.StringVar(value=_unk)
        self.var_pos = tk.StringVar(value=_unk)
        self.var_pos_mode = tk.StringVar(value=_unk)
        self.var_msg = tk.StringVar(value=self._L("header.ready"))
        self.buttons: dict = {}
        self._build_session_tab()
        self._build_tracking_tab()
        self._build_camera_tab()
        self._build_position_tab()
        self._build_optics_tab()
        self._build_diagnostics_tab()
        bottom = ttk.Frame(outer)
        bottom.grid(row=2, column=0, sticky="ew")
        bottom.columnconfigure(0, weight=1)
        ttk.Label(bottom, textvariable=self.var_msg, wraplength=900,
                  foreground="#0b5cad").grid(row=0, column=0, sticky="w")
        self.buttons["save"] = ttk.Button(bottom, text=self._L("footer.save"),
                                           command=self.on_save)
        self._register(self.buttons["save"], "footer.save")
        self.buttons["save"].grid(row=0, column=1, padx=(8, 0))

    def _tab(self, titolo):
        frame = self.ttk.Frame(self.tabs, padding=12)
        self.tabs.add(frame, text=titolo)
        return frame

    def _row(self, parent, r, etichetta, widget, nota=""):
        self.ttk.Label(parent, text=etichetta).grid(row=r, column=0, sticky="w",
                                                    pady=3, padx=(0, 8))
        widget.grid(row=r, column=1, sticky="ew", pady=3)
        parent.columnconfigure(1, weight=1)
        if nota:
            self.ttk.Label(parent, text=nota, foreground="#5a6b80").grid(
                row=r, column=2, sticky="w", padx=(8, 0))

    def _action_button(self, parent, chiave, testo, comando):
        # Compat: testo letterale (vecchi caller/testi). I nuovi tab usano
        # _action_button_key con chiavi catalogue per il live-retranslate.
        btn = self.ttk.Button(parent, text=testo, command=comando)
        self.buttons[chiave] = btn
        return btn

    def _build_session_tab(self):
        f = self._tab_key("tab.session")
        self._row_key(f, 0, "session.state", self.ttk.Label(f, textvariable=self.var_stato))
        self._row_key(f, 1, "session.source", self.ttk.Label(f, textvariable=self.var_sorgente))
        self._row_key(f, 2, "session.readiness", self.ttk.Label(f, textvariable=self.var_readiness))
        self._row_key(f, 3, "session.mode_requested", self.ttk.Label(f, textvariable=self.var_modo_richiesto))
        self._row_key(f, 4, "session.mode_actual", self.ttk.Label(f, textvariable=self.var_modo_effettivo))
        self._row_key(f, 5, "session.fallback",
                  self.ttk.Label(f, textvariable=self.var_fallback_diag))
        self._row_key(f, 6, "session.recovery",
                  self.ttk.Label(f, textvariable=self.var_recovery))
        box = self.ttk.Frame(f)
        box.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        for i in range(5):
            box.columnconfigure(i, weight=1)
        self._action_button_key(box, "start", "session.btn_start", self.on_start_vr).grid(
            row=0, column=0, padx=(0, 6), sticky="ew")
        self._action_button_key(box, "attach", "session.btn_attach",
                            self.on_attach).grid(row=0, column=1, padx=(0, 6), sticky="ew")
        self._action_button_key(box, "stop", "session.btn_stop",
                            self.on_stop_vr).grid(row=0, column=2, padx=(0, 6), sticky="ew")
        self._action_button_key(box, "cinema", "session.btn_cinema", self.on_cinema).grid(
            row=0, column=3, padx=(0, 6), sticky="ew")
        self._action_button_key(box, "apply_driver", "session.btn_apply_driver",
                            self.on_apply_driver).grid(row=0, column=4, sticky="ew")
        self.lbl_session_guide = self.ttk.Label(
            f, text=self._L("session.guide"), foreground="#5a6b80", wraplength=900)
        self.lbl_session_guide.grid(row=8, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def _build_tracking_tab(self):
        f = self._tab_key("tab.tracking")
        mode_box = self.ttk.Frame(f)
        mode_box.grid(row=0, column=1, sticky="w")
        # Valori tecnici 3dof/6dof invarianti; solo l'etichetta e' tradotta.
        self.rb_3dof = self.ttk.Radiobutton(mode_box, text="3DoF", value="3dof",
                             variable=self.var_tracking_mode)
        self.rb_3dof.pack(side="left")
        self.rb_6dof = self.ttk.Radiobutton(mode_box, text="6DoF", value="6dof",
                             variable=self.var_tracking_mode)
        self.rb_6dof.pack(side="left", padx=(12, 0))
        _mode_lbl = self.ttk.Label(f, text=self._L("tracking.mode"))
        _mode_lbl.grid(row=0, column=0, sticky="w")
        self._register(_mode_lbl, "tracking.mode")
        self.chk_fallback = self.ttk.Checkbutton(f, text=self._L("tracking.fallback_check"),
                                   variable=self.var_fallback)
        self._row_key(f, 1, "tracking.fallback", self.chk_fallback, "tracking.fallback_note")
        self._row_key(f, 2, "tracking.blend",
                  self.ttk.Entry(f, textvariable=self.var_blend), "tracking.blend_note")
        self._row_key(f, 3, "tracking.speed",
                  self.ttk.Entry(f, textvariable=self.var_speed), "tracking.speed_note")
        self._row_key(f, 4, "tracking.cal",
                  self.ttk.Entry(f, textvariable=self.var_cal), "tracking.cal_note")
        self._row_key(f, 5, "tracking.stale",
                  self.ttk.Entry(f, textvariable=self.var_stale), "tracking.stale_note")
        self._row_key(f, 6, "tracking.gap",
                  self.ttk.Entry(f, textvariable=self.var_gap), "tracking.gap_note")
        self._row_key(f, 7, "tracking.config",
                  self.ttk.Entry(f, textvariable=self.var_tracking_path), "tracking.config_note")
        self.chk_two_led = self.ttk.Checkbutton(f, text=self._L("tracking.two_led_check"),
                                               variable=self.var_two_led)
        self._row_key(f, 8, "tracking.two_led", self.chk_two_led)
        two_led_note = self.ttk.Label(f, text=self._L("tracking.two_led_note"),
                                      foreground="#5a6b80", wraplength=850)
        two_led_note.grid(row=9, column=0, columnspan=3, sticky="w", pady=3)
        self._register(two_led_note, "tracking.two_led_note")
        box = self.ttk.Frame(f)
        box.grid(row=10, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self._action_button_key(box, "apply_tracking", "tracking.btn_apply",
                            self.on_apply_tracking).pack(side="left")
        self._action_button_key(box, "reset_relative", "tracking.btn_reset",
                            self.on_reset_relative).pack(side="left", padx=(8, 0))

    def _camera_field(self, parent, row, etichetta, widget, nota=""):
        """Campo Camera impilato: label sopra, controllo sotto, nota sotto.

        Evita il taglio del layout a 3 colonne (label+scale200+nota) nel
        pannello stretto: ogni campo occupa una riga singola espandibile.
        Compat: etichetta/nota letterali. I nuovi caller usano
        _camera_field_key con chiavi catalogue per il live-retranslate.
        """
        parent.columnconfigure(0, weight=1)
        base_row = row * 3
        self.ttk.Label(parent, text=etichetta).grid(
            row=base_row, column=0, sticky="w", pady=(6, 0))
        widget.grid(row=base_row + 1, column=0, sticky="ew", pady=(2, 0))
        if nota:
            self.ttk.Label(parent, text=nota, foreground="#5a6b80",
                           wraplength=340, justify="left").grid(
                row=base_row + 2, column=0, sticky="w")
        return parent

    def _camera_field_key(self, parent, row, key, widget, note_key=None):
        """Variante catalogue di _camera_field (live-retranslate)."""
        parent.columnconfigure(0, weight=1)
        base_row = row * 3
        lbl = self.ttk.Label(parent, text=self._L(key))
        lbl.grid(row=base_row, column=0, sticky="w", pady=(6, 0))
        self._register(lbl, key)
        widget.grid(row=base_row + 1, column=0, sticky="ew", pady=(2, 0))
        if note_key:
            note = self.ttk.Label(parent, text=self._L(note_key), foreground="#5a6b80",
                           wraplength=340, justify="left")
            note.grid(row=base_row + 2, column=0, sticky="w")
            self._register(note, note_key)
        return parent

    def _build_camera_tab(self):
        tk = self.tk
        ps3_controls = module_for_source("pseye").controls
        f = self._tab_key("tab.camera")
        # Anteprima grande a sinistra (espandibile), controlli compatti a destra.
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=0)
        f.rowconfigure(0, weight=1)
        preview_box = self.ttk.Frame(f, padding=(0, 0, 12, 0))
        preview_box.grid(row=0, column=0, sticky="nsew")
        preview_box.columnconfigure(0, weight=1)
        preview_box.rowconfigure(0, weight=1)
        self.preview_frame = self.ttk.Frame(preview_box)
        self.preview_frame.grid(row=0, column=0, sticky="nsew")
        self.preview_frame.columnconfigure(0, weight=1)
        self.preview_frame.rowconfigure(0, weight=1)
        # Anti-oscillazione geometry: il frame non si ridimensiona sul
        # contenuto (immagine), ma resta dettato dalla griglia genitore.
        try:
            self.preview_frame.grid_propagate(False)
        except Exception:
            pass
        # Niente width/height in unità testo: il sizing viene dall'immagine
        # PPM 640x480 con fit continuo smooth 4:3 (bound 1600x1200).
        self.preview_widget = tk.Label(self.preview_frame, text=self._L("camera.preview_placeholder"),
                                       bg="#0b1422", fg="#b8c9dd",
                                       anchor="center", justify="center")
        self.preview_widget.grid(row=0, column=0, sticky="nsew")
        self.ttk.Label(preview_box, textvariable=self.var_preview_info,
                       wraplength=640,
                       justify="left").grid(row=1, column=0, sticky="ew",
                                            pady=(6, 0))
        self.ttk.Label(preview_box, textvariable=self.var_preview_detail,
                       wraplength=640,
                       justify="left", foreground="#5a6b80").grid(
            row=2, column=0, sticky="ew")
        self._preview_base = None
        self._preview_base_size = (0, 0)
        self._preview_ppm_raw = None
        self._preview_resize_after = None
        try:
            self.preview_frame.bind("<Configure>", self._on_preview_configure)
        except Exception:
            pass
        right_outer = self.ttk.Frame(f)
        right_outer.grid(row=0, column=1, sticky="nsew")
        right_outer.columnconfigure(0, weight=1)
        right_outer.rowconfigure(0, weight=1)
        canvas = tk.Canvas(right_outer, highlightthickness=0, width=380)
        scroll = self.ttk.Scrollbar(right_outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        inner = self.ttk.Frame(canvas, padding=6)
        inner.columnconfigure(0, weight=1)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        def _sync(event=None):
            try:
                canvas.configure(scrollregion=canvas.bbox("all"))
                canvas.itemconfigure(win, width=canvas.winfo_width())
            except Exception:
                pass
        inner.bind("<Configure>", _sync)
        canvas.bind("<Configure>", _sync)
        # Tendina scelta videocamera 6DoF: mostra le etichette umane del
        # registro (tools.psvr_suite.cameras), le prefs salvano l'id tecnico;
        # solo etichetta/nota del campo sono tradotte altrove. Il valore del
        # var puo' arrivare come id o etichetta: normalizzazione tollerante.
        # Selection changes DISPLAY only; acquisition still requires an action.
        _src_labels = [camera_source_label(sid)
                       for sid in cameras_module.camera_source_ids()]
        _src_choice = self.var_camera_source.get() if hasattr(
            self.var_camera_source, "get") else cameras_module.DEFAULT_CAMERA_SOURCE
        _src_id = camera_source_from_choice(_src_choice)
        _src_initial = camera_source_label(_src_id) if _src_id else str(_src_choice)
        try:
            self.var_camera_source.set(_src_initial)
        except Exception:
            pass
        _src_menu = self.ttk.Combobox(inner, textvariable=self.var_camera_source,
                                      values=_src_labels, state="readonly")
        _src_menu.bind("<<ComboboxSelected>>", self._on_camera_source_changed)
        self.camera_source_menu = _src_menu
        self._camera_field_key(inner, 0, "camera.source",
                               _src_menu, "camera.source_note")
        # Separate display pane, NEVER bound to var_exposure/gain/format/paths.
        self.camera_source_summary = self.ttk.Label(
            inner, text="", wraplength=340, justify="left", foreground="#5a6b80")
        self.camera_source_summary.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.camera_source_editor = self.ttk.Frame(inner)
        self.camera_source_editor.grid(row=4, column=0, sticky="ew")
        self._source_editors = {}
        legacy = self.ttk.Frame(inner)
        legacy.grid(row=5, column=0, sticky="ew")
        self.camera_legacy_fields = legacy
        self._camera_field_key(legacy, 1, "camera.f_value",
                           tk.Scale(legacy, from_=0, to=255, orient="horizontal",
                                    variable=self.var_value_min, length=300),
                           "camera.f_value_note")
        self._camera_field_key(legacy, 2, "camera.f_area",
                           tk.Scale(legacy, from_=1, to=200, orient="horizontal",
                                    variable=self.var_min_area, length=300),
                           "camera.f_area_note")
        self._camera_field_key(legacy, 3, "camera.f_exposure",
                           tk.Scale(legacy, from_=ps3_controls["exposure"].minimum,
                                    to=ps3_controls["exposure"].maximum, orient="horizontal",
                                    variable=self.var_exposure, length=300),
                           "camera.f_exposure_note")
        self._camera_field_key(legacy, 4, "camera.f_gain",
                           tk.Scale(legacy, from_=ps3_controls["gain"].minimum,
                                    to=ps3_controls["gain"].maximum, orient="horizontal",
                                    variable=self.var_gain, length=300),
                           "camera.f_gain_note")
        self._camera_field_key(legacy, 5, "camera.f_sat",
                           self.ttk.Entry(legacy, textvariable=self.var_saturation),
                           "camera.f_sat_note")
        self._camera_field_key(legacy, 6, "camera.f_opening",
                           self.ttk.Entry(legacy, textvariable=self.var_opening),
                           "camera.f_opening_note")
        self._camera_field_key(legacy, 7, "camera.f_fps",
                           self.ttk.Entry(legacy, textvariable=self.var_fps, state="readonly"),
                           "camera.f_fps_note")
        self._camera_field_key(legacy, 8, "camera.f_width",
                           self.ttk.Entry(legacy, textvariable=self.var_width, state="readonly"),
                           "camera.f_width_note")
        self._camera_field_key(legacy, 9, "camera.f_height",
                           self.ttk.Entry(legacy, textvariable=self.var_height, state="readonly"),
                           "camera.f_height_note")
        self._camera_field_key(legacy, 10, "camera.f_intrinsics",
                           self.ttk.Entry(legacy, textvariable=self.var_intrinsics, state="readonly"),
                           None)
        self._camera_field_key(legacy, 11, "camera.f_rig",
                           self.ttk.Entry(legacy, textvariable=self.var_rig, state="readonly"),
                           None)
        self._camera_field_key(legacy, 12, "camera.f_imu_rig",
                           self.ttk.Entry(legacy, textvariable=self.var_imu_rig, state="readonly"),
                           None)
        btns = self.ttk.Frame(right_outer)
        btns.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        btns.columnconfigure(0, weight=1)
        btns.columnconfigure(1, weight=1)
        self._action_button_key(btns, "attach_camera", "camera.btn_attach",
                            self.on_attach).grid(row=0, column=0, sticky="ew",
                                                 padx=(0, 4))
        self._action_button_key(btns, "apply_camera", "camera.btn_apply",
                            self.on_apply_tracking).grid(row=0, column=1, sticky="ew",
                                                         padx=(4, 0))
        self.lbl_camera_guide = self.ttk.Label(btns, text=self._L("camera.guide"),
                       foreground="#5a6b80", wraplength=340,
                       justify="left")
        self.lbl_camera_guide.grid(row=1, column=0, columnspan=2,
                                            sticky="w", pady=(6, 0))
        self.lbl_camera_warning = self.ttk.Label(inner, text=self._L("camera.warning"),
                       foreground="#8a2b00", wraplength=340,
                       justify="left")
        self.lbl_camera_warning.grid(row=39, column=0, sticky="w", pady=(8, 0))
        self._action_button_key(btns, "ps4_camera", "ps4.open",
                                self.on_ps4_camera).grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))

    def _on_camera_source_changed(self, event=None):
        """Main Tk selection callback: no collection, persistence or hardware."""
        self._refresh_camera_settings()

    def _refresh_camera_settings(self, reload_profile=True):
        # Old headless shells intentionally have no camera tab.
        if not hasattr(self, "camera_source_summary"):
            return
        status = getattr(self, "_last_status", ({}, {}))[1]
        choice = self.var_camera_source.get()
        source = camera_source_from_choice(choice)
        # Read, never normalize/reset, the legacy draft. Its original Tk vars
        # remain the edit buffer across any number of source switches.
        requested = {"camera_profiles": copy.deepcopy(getattr(self, "_camera_profiles_committed", {}))}
        if source == "pseye":
            for key, attr in module_for_source("pseye").legacy_bindings.items():
                try:
                    requested[key] = getattr(self, attr).get()
                except Exception:
                    requested[key] = None
        cached = getattr(self, "_camera_settings_view", None)
        if (not reload_profile and source == "ps4-stereo" and isinstance(cached, dict)
                and cached["source"] == source):
            view = module_for_source(source).refresh_status(cached, status)
        else:
            view = camera_settings_view(self.root_path, choice, status, requested=requested)
        self._camera_settings_view = view
        self.camera_source_summary.configure(text=camera_settings_text(view, self.lang))
        self._render_source_editor(source, view, status)
        if view["legacy"]:
            self.camera_legacy_fields.grid()
        else:
            self.camera_legacy_fields.grid_remove()
        for attr, key in (("lbl_camera_guide", "camera.guide"),
                          ("lbl_camera_warning", "camera.warning")):
            widget = getattr(self, attr, None)
            if widget is not None:
                widget.configure(text=self._L(key if view["legacy"] else "camera.selection_only"))

    def _render_source_editor(self, source, view, status):
        """Data-driven editors; no target defaults, no implicit apply on edits."""
        if not hasattr(self, "camera_source_editor"):
            return
        for state in self._source_editors.values():
            state["frame"].grid_remove()
        if source is None or source == "pseye":
            return
        module = module_for_source(source)
        fields = module.controls or module.configuration_fields
        state = self._source_editors.get(source)
        if state is None:
            frame = self.ttk.Frame(self.camera_source_editor)
            note = self.ttk.Label(frame, text="", wraplength=340, justify="left")
            note.grid(row=0, column=0, columnspan=2, sticky="ew")
            state = {"frame": frame, "note": note, "vars": {}, "entries": {}, "initial": {}}
            for row, name in enumerate(fields, 1):
                value = view["requested"].get(name)
                literal = "" if value is None else str(value)
                var = self.tk.StringVar(value=literal)
                label = self.ttk.Label(frame, text=name)
                label.grid(row=row, column=0, sticky="w")
                entry = self.ttk.Entry(frame, textvariable=var, state="disabled")
                entry.grid(row=row, column=1, sticky="ew")
                state["vars"][name] = var
                state["entries"][name] = entry
                state["initial"][name] = literal
            self._source_editors[source] = state
        state["frame"].grid(row=0, column=0, sticky="ew")
        caps = module.editing_capabilities(status)
        configuration_ready = (getattr(self.tracking, "camera_profile_contract", None) == 1
                               and bool(module.configuration_fields))
        ranges = caps["controls"] if caps else {}
        available = []
        for name, entry in state["entries"].items():
            enabled = configuration_ready
            if name in module.controls and caps and view["error"] is None:
                info = ranges.get(name)
                # Prove range shape/capability/step with a native legal endpoint,
                # not a guessed universal range or a previously saved target.
                try:
                    if caps.get("control_contract") == "opencv-nonstrict-single-ps4":
                        if (source != "ps4-stereo" or not isinstance(info, dict)
                                or info.get("validation") != "profile_policy_not_measured"):
                            raise ValueError("missing legacy policy declaration")
                        module.validate_saved_configuration({"controls": {name: info["minimum"]}})
                    else:
                        native_manual_range(info)
                    enabled = True
                    available.append(f"{name}: {info['minimum']}..{info['maximum']} / {info['step']}"
                                     + (" (policy, NOT measured)" if caps.get("control_contract") ==
                                        "opencv-nonstrict-single-ps4" else ""))
                except (ValueError, TypeError, KeyError):
                    enabled = False
            entry.configure(state="normal" if enabled else "disabled")
        state["note"].configure(text=self._L("camera.source_edit_pending" if
            (available or configuration_ready) else "camera.source_edit_unavailable",
            ranges="; ".join(available), mode=caps["apply_mode"] if caps else "configuration"))

    def _collect_source_profiles(self, source):
        """Only selected-source draft may enter an explicit apply/save payload."""
        profiles = copy.deepcopy(getattr(self, "_camera_profiles_committed", {}))
        state = getattr(self, "_source_editors", {}).get(source)
        if state is None:
            return profiles
        literals = {name: var.get() for name, var in state["vars"].items()}
        changed = {name: value for name, value in literals.items()
                   if value != state["initial"][name]}
        if not changed:
            return profiles
        module = module_for_source(source)
        status = getattr(self, "_last_status", ({}, {}))[1]
        if module.controls:
            caps = module.editing_capabilities(status)
            if caps is None:
                raise ValueError(self._L("camera.source_edit_unavailable"))
            values = {name: AppModel.parse_int(value, name) for name, value in changed.items()
                      if str(value).strip()}
            if caps.get("control_contract") == "opencv-nonstrict-single-ps4":
                module.validate_saved_configuration({"controls": values})
            else:
                module.validate_controls(values, caps["controls"])
            controls = profiles.get(source, {}).get("controls", {}).copy()
            for name in changed:
                if name in values:
                    controls[name] = values[name]
                else:
                    controls.pop(name, None)  # blank means inherit source profile
            if controls:
                profiles[source] = module.validate_saved_configuration({"controls": controls})
            else:
                profiles.pop(source, None)
        else:
            if getattr(self.tracking, "camera_profile_contract", None) != 1:
                raise ValueError(self._L("camera.source_edit_unavailable"))
            config = {name: (AppModel.parse_int(value, name) if kind == "int" else value.strip())
                      for name, kind in module.configuration_fields.items()
                      for value in (literals[name],)}
            profiles[source] = module.validate_saved_configuration(config)
        return validate_camera_profiles(profiles)

    def on_ps4_camera(self):
        # Acquisition-only: do not reuse PS3 Eye calibration or send poses.
        panel = getattr(self, "_ps4_panel", None)
        if panel is not None and panel.winfo_exists():
            panel.lift()
            return
        from .ps4_camera_panel import open_panel
        self._ps4_panel = open_panel(self.root, self.lang)

    def _build_position_tab(self):
        f = self._tab_key("tab.position")
        filt_box = self.ttk.Frame(f)
        filt_box.grid(row=0, column=1, sticky="w")
        # Valori tecnici filtro invarianti; solo etichette tradotte.
        self._filter_radios = []
        for val, tkey in (("one_euro", "position.filter_one_euro"),
                           ("kalman_initial", "position.filter_initial"),
                           ("kalman_optimized", "position.filter_optimized")):
            rb = self.ttk.Radiobutton(filt_box, text=self._L(tkey), value=val,
                                 variable=self.var_filter)
            rb.pack(anchor="w")
            self._filter_radios.append((rb, tkey))
        _filt_lbl = self.ttk.Label(f, text=self._L("position.filter"))
        _filt_lbl.grid(row=0, column=0, sticky="nw")
        self._register(_filt_lbl, "position.filter")
        self._row_key(f, 1, "position.cutoff",
                  self.ttk.Entry(f, textvariable=self.var_cutoff), "position.cutoff_note")
        self._row_key(f, 2, "position.beta",
                  self.ttk.Entry(f, textvariable=self.var_beta), "position.beta_note")
        self._row_key(f, 3, "position.scale",
                  self.ttk.Entry(f, textvariable=self.var_scale), "position.scale_note")
        self._row_key(f, 4, "position.head",
                  self.ttk.Entry(f, textvariable=self.var_head_offset),
                  "position.head_note")
        self._row_key(f, 5, "position.initial",
                  self.ttk.Entry(f, textvariable=self.var_initial_offset),
                  "position.initial_note")
        box = self.ttk.Frame(f)
        box.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self._action_button_key(box, "apply_position", "position.btn_apply",
                            self.on_apply_tracking).pack(side="left")
        self.lbl_position_guide = self.ttk.Label(f, text=self._L("position.guide"),
                       foreground="#5a6b80", wraplength=900)
        self.lbl_position_guide.grid(row=7, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def _build_optics_tab(self):
        f = self._tab_key("tab.optics")
        # Valori profilo ottico invarianti (backend); solo etichetta tradotta.
        opt = self.ttk.OptionMenu(f, self.var_optics, "identity", "current",
                                  "identity", "mild", "medium", "peripheral",
                                  "stronger")
        self._row_key(f, 0, "optics.profile", opt, "optics.profile_note")
        box = self.ttk.Frame(f)
        box.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self._action_button_key(box, "grid", "optics.btn_grid",
                            self.on_show_grid).pack(side="left")
        self._action_button_key(box, "apply_optics", "optics.btn_apply",
                            self.on_apply_optics).pack(side="left", padx=(8, 0))
        self.lbl_optics_guide = self.ttk.Label(f, text=self._L("optics.guide"),
                       foreground="#8a2b00", wraplength=900)
        self.lbl_optics_guide.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def _build_diagnostics_tab(self):
        f = self._tab_key("tab.diagnostics")
        self._row_key(f, 0, "diag.led", self.ttk.Label(f, textvariable=self.var_led))
        self._row_key(f, 1, "diag.rms", self.ttk.Label(f, textvariable=self.var_rms))
        self._row_key(f, 2, "diag.age", self.ttk.Label(f, textvariable=self.var_age))
        self._row_key(f, 3, "diag.imu_session", self.ttk.Label(f, textvariable=self.var_imu_session))
        self._row_key(f, 4, "diag.imu_epoch", self.ttk.Label(f, textvariable=self.var_imu_epoch))
        self._row_key(f, 5, "diag.pos",
                  self.ttk.Label(f, textvariable=self.var_pos))
        self._row_key(f, 6, "diag.pos_mode", self.ttk.Label(f, textvariable=self.var_pos_mode))
        box = self.ttk.Frame(f)
        box.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self._action_button_key(box, "recenter", "diag.btn_recenter",
                            self.on_recenter).pack(side="left")
        self._action_button_key(box, "realign", "diag.btn_realign",
                            self.on_realign).pack(side="left", padx=(8, 0))
        self._action_button_key(box, "logs", "diag.btn_logs",
                            self.on_open_logs).pack(side="left", padx=(8, 0))
        self.lbl_diag_guide = self.ttk.Label(f, text=self._L("diag.guide"),
                       foreground="#5a6b80", wraplength=900)
        self.lbl_diag_guide.grid(row=8, column=0, columnspan=3, sticky="w", pady=(8, 0))

    # -- prefs <-> widget -------------------------------------------------------
    def _remember_ps3_preferences(self, prefs):
        """Commit only a validated load/explicit save, never an apply or switch."""
        validated = AppModel.validate(prefs)
        self._ps3_committed = {key: copy.deepcopy(validated[key])
                               for key in module_for_source("pseye").legacy_keys}

    def _apply_prefs_to_widgets(self, prefs):
        # A load establishes the legacy schema bridge. This is separate from
        # model (which collect_prefs updates) and from the literal Tk edit buffer.
        try:
            self._remember_ps3_preferences(prefs)
            self._camera_profiles_committed = validate_camera_profiles(prefs.get("camera_profiles", {}))
        except Exception:
            self._ps3_committed = None  # no guessed defaults for hidden fields
            self._camera_profiles_committed = {}
        for state in getattr(self, "_source_editors", {}).values():
            state["frame"].grid_remove()
        self._source_editors = {}
        try:
            self.var_tracking_mode.set(prefs.get("tracking_mode", "6dof"))
            self.var_fallback.set(bool(prefs.get("fallback_3dof", False)))
            self.var_two_led.set(bool(prefs.get("two_led_experimental", False)))
            self.var_blend.set(str(prefs.get("reacquire_blend_ms", 150)))
            self.var_speed.set(str(prefs.get("reacquire_speed_m_s", 1.5)))
            self.var_cal.set(str(prefs.get("imu_calibration_ms", 8000)))
            self.var_stale.set(str(prefs.get("imu_stale_ms", 100)))
            self.var_gap.set(str(prefs.get("imu_max_gap_us", 5000)))
            self.var_tracking_path.set(str(prefs.get("tracking_config_path", "config.json")))
            self.var_value_min.set(int(prefs["value_min"]))
            self.var_min_area.set(int(prefs["min_area"]))
            self.var_exposure.set(int(prefs["exposure"]))
            self.var_gain.set(int(prefs["gain"]))
            self.var_saturation.set(str(prefs["saturation_min"]))
            self.var_opening.set(str(prefs["opening_size"]))
            self.var_fps.set(str(prefs["fps"]))
            self.var_width.set(str(prefs["width"]))
            self.var_height.set(str(prefs["height"]))
            self.var_intrinsics.set(str(prefs["intrinsicsPath"]))
            self.var_rig.set(str(prefs["rigPath"]))
            self.var_imu_rig.set(str(prefs["imu_rig"]))
            self.var_filter.set(str(prefs.get("position_filter", "one_euro")))
            self.var_cutoff.set(str(prefs.get("min_cutoff_hz", 2.5)))
            self.var_beta.set(str(prefs.get("beta", 8.0)))
            self.var_scale.set(str(prefs.get("position_scale", 1.0)))
            self.var_head_offset.set(AppModel.format_vector3(
                prefs.get("head_offset_model_m", [0.0, 0.0, 0.0])))
            self.var_initial_offset.set(AppModel.format_vector3(
                prefs.get("initial_offset_m", [0.0, 0.0, 0.0])))
            self.var_optics.set(str(prefs.get("optics_profile", "identity")))
            self.var_camera_source.set(camera_source_label(str(prefs.get(
                "camera_source", cameras_module.DEFAULT_CAMERA_SOURCE))))
        except Exception as exc:
            self.var_msg.set(self._L("common.initial_values_partial", errore=str(exc)))
        self._refresh_camera_settings()

    def collect_prefs(self):
        """Validate selected-source edits plus common prefs, not hidden drafts.

        The unchanged flat backend schema still requires PS3-shaped fields for
        PS4. Supply the last validated load/save snapshot, not the hidden Tk
        draft. PS4's source owner later resolves its own profile independently.
        """
        raw = {}
        errori = []
        # Compatibility with headless shells that bypass __init__/initial load.
        # Capture the initial model BEFORE any collection can replace it.
        if not hasattr(self, "_ps3_committed"):
            try:
                self._remember_ps3_preferences(self.model.to_prefs())
            except Exception:
                self._ps3_committed = None
        if not hasattr(self, "_camera_profiles_committed"):
            try:
                self._camera_profiles_committed = validate_camera_profiles(
                    self.model.to_prefs().get("camera_profiles", {}))
            except ValueError:
                return None, [self._L("camera.legacy_snapshot_missing")]
        try:
            choice = self.var_camera_source.get()
            source = camera_source_from_choice(choice)
            raw_source = source if source is not None else choice
        except Exception:
            source, raw_source = None, None
            errori.append("camera_source: lettura non riuscita")
        ps3_keys = set(module_for_source("pseye").legacy_keys)
        read_ps3_draft = source == "pseye"
        try:
            source_profiles = self._collect_source_profiles(source)
        except ValueError as exc:
            source_profiles = {}
            errori.append(str(exc))
        try:
            raw["tracking_mode"] = self.var_tracking_mode.get().strip()
        except Exception:
            errori.append("tracking_mode: lettura non riuscita")
        try:
            raw["fallback_3dof"] = self.var_fallback.get()
            raw["two_led_experimental"] = self.var_two_led.get()
        except Exception:
            errori.append("fallback_3dof: checkbox non valida")
        typed = {"camera_source": raw_source}
        if source_profiles or "camera_profiles" in self.model.to_prefs():
            typed["camera_profiles"] = source_profiles
        if not read_ps3_draft:
            committed = self._ps3_committed
            if isinstance(committed, dict) and set(committed) == ps3_keys:
                typed.update(copy.deepcopy(committed))
            else:
                errori.append(self._L("camera.legacy_snapshot_missing"))
        if not errori:
            try:
                typed["fallback_3dof"] = AppModel.parse_bool(raw["fallback_3dof"], "fallback_3dof")
                typed["two_led_experimental"] = AppModel.parse_bool(raw["two_led_experimental"], "two_led_experimental")
            except ValueError as exc:
                errori.append(str(exc))
        specs = (
            (self.var_blend, "reacquire_blend_ms", "int"),
            (self.var_speed, "reacquire_speed_m_s", "float"),
            (self.var_cal, "imu_calibration_ms", "int"),
            (self.var_stale, "imu_stale_ms", "int"),
            (self.var_gap, "imu_max_gap_us", "int"),
            (self.var_cutoff, "min_cutoff_hz", "float"),
            (self.var_beta, "beta", "float"),
            (self.var_scale, "position_scale", "float"),
            (self.var_saturation, "saturation_min", "int"),
            (self.var_opening, "opening_size", "int"),
            (self.var_fps, "fps", "int"),
            (self.var_width, "width", "int"),
            (self.var_height, "height", "int"),
        )
        for var, chiave, tipo in specs:
            if not read_ps3_draft and chiave in ps3_keys:
                continue
            try:
                testo = var.get()
            except Exception:
                errori.append(f"{chiave}: lettura non riuscita")
                continue
            try:
                typed[chiave] = (AppModel.parse_int(testo, chiave)
                                 if tipo == "int" else AppModel.parse_float(testo, chiave))
            except ValueError as exc:
                errori.append(str(exc))
        for var, chiave in ((self.var_value_min, "value_min"),
                            (self.var_min_area, "min_area"),
                            (self.var_exposure, "exposure"),
                            (self.var_gain, "gain")):
            if not read_ps3_draft:
                continue
            try:
                typed[chiave] = AppModel.parse_int(var.get(), chiave)
            except ValueError as exc:
                errori.append(str(exc))
            except Exception:
                errori.append(f"{chiave}: slider non valido")
        for var, chiave in ((self.var_tracking_path, "tracking_config_path"),
                            (self.var_intrinsics, "intrinsicsPath"),
                            (self.var_rig, "rigPath"),
                            (self.var_imu_rig, "imu_rig"),
                            (self.var_filter, "position_filter"),
                            (self.var_optics, "optics_profile")):
            if not read_ps3_draft and chiave in ps3_keys:
                continue
            try:
                typed[chiave] = var.get().strip() if isinstance(var.get(), str) else var.get()
            except Exception:
                errori.append(f"{chiave}: lettura non riuscita")
        # La tendina espone etichette umane: la scelta (etichetta o id) va
        # mappata all'id tecnico registrato prima della validazione strict.
        # Ignoto -> resta grezzo, sara' rifiutato senza inventare una camera.
        mapped = camera_source_from_choice(typed.get("camera_source"))
        if mapped is not None:
            typed["camera_source"] = mapped
        if "tracking_mode" not in errori and "tracking_mode" in raw:
            typed["tracking_mode"] = raw["tracking_mode"]
        for var, chiave in ((self.var_head_offset, "head_offset_model_m"),
                            (self.var_initial_offset, "initial_offset_m")):
            try:
                typed[chiave] = AppModel.parse_vector3(var.get(), chiave)
            except ValueError as exc:
                errori.append(str(exc))
            except Exception:
                errori.append(f"{chiave}: lettura non riuscita")
        if errori:
            return None, errori
        if source != "pseye" and hasattr(self, "root_path"):
            source_view = camera_settings_view(self.root_path, raw_source,
                getattr(self, "_last_status", ({}, {}))[1], requested=typed)
            if source_view["error"] is not None:
                return None, [camera_settings_text(source_view, getattr(self, "lang", "it"))]
        try:
            cleaned = self._settings.validate_preferences(typed)
        except ValueError as exc:
            return None, [f"backend strict: {exc}"]
        except Exception as exc:
            return None, [f"Validazione non riuscita: {exc}"]
        try:
            self.model = AppModel(cleaned)
        except ValueError as exc:
            return None, [str(exc)]
        return self.model.to_prefs(), []

    # -- guardie azioni bound ----------------------------------------------------
    def _busy(self):
        with self._pending_lock:
            return self._pending > 0 or self.closing

    def _set_buttons(self, enabled):
        stato = "normal" if enabled else "disabled"
        for btn in self.buttons.values():
            try:
                btn.configure(state=stato)
            except Exception:
                pass

    def _run_serial(self, label, func, task_key=None):
        # task_key: chiave catalogue per ri-etichettare il task nella lingua
        # corrente (solo display). Il worker non tocca mai widget Tk: nel
        # wrapper solo stringhe + coda display (phone/async-safe).
        if task_key is not None:
            try:
                label = self._L(task_key)
            except Exception:
                pass
        with self._pending_lock:
            if self._pending > 0 or self.closing:
                self.var_msg.set(self._L("action.busy"))
                return False
            self._pending += 1
        self._set_buttons(False)
        self.var_msg.set(i18n_module.fmt_in_progress(
            getattr(self, "lang", i18n_module.DEFAULT), label))

        def wrapper():
            # B12-01: mai put bloccante qui. Coda piena (512) + closing
            # (drain fermo) = worker bloccato per sempre, final_task mai
            # eseguita, poll_finish infinito. Evento droppato su Full,
            # come _on_worker_error/_on_backend_event; mai bloccare il worker.
            try:
                result = func()
                if result is not None and not _result_ok(result):
                    raise RuntimeError(f"{label}: backend ha risposto False/insuccesso")
                try:
                    self.display.put_nowait(("task_done", {"label": label, "ok": True}))
                except queue.Full:
                    pass
            except Exception as exc:  # noqa: BLE001
                try:
                    self.display.put_nowait(("task_done", {"label": label, "ok": False,
                                                    "errore": str(exc)}))
                except queue.Full:
                    pass
        if not self.serial.submit(label, wrapper):
            with self._pending_lock:
                self._pending -= 1
            self._set_buttons(True)
            self.var_msg.set(self._L("action.worker_closing"))
            return False
        return True

    def _on_worker_error(self, label, exc):
        try:
            self.display.put_nowait(("task_done", {"label": str(label),
                                                   "ok": False, "errore": str(exc)}))
        except queue.Full:
            pass

    def _on_backend_event(self, kind_or_event, payload=None):
        """Compatibile con Runtime(event-dict) e Tracking(kind, payload)."""
        try:
            if payload is None and isinstance(kind_or_event, dict):
                event = dict(kind_or_event)
                kind = str(event.get("kind", event.get("type", "runtime")))
                payload = event
            else:
                kind = str(kind_or_event)
                if payload is None:
                    payload = {}
            self.display.put_nowait((kind, payload))
        except queue.Full:
            pass

    # -- azioni ------------------------------------------------------------------
    def on_start_vr(self):
        """Ordine: runtime.start_vr POI tracking.start. False -> fallito."""
        if self._busy():
            self.var_msg.set(self._L("action.busy_short"))
            return
        prefs, errori = self.collect_prefs()
        if errori:
            self.var_msg.set(self._L("action.prefs_invalid", dettagli="; ".join(errori)))
            return
        snapshot = copy.deepcopy(prefs)

        def task():
            if hasattr(self.runtime,'needs_driver_apply') and self.runtime.needs_driver_apply(snapshot):
                if not _result_ok(self.runtime.apply_driver(snapshot,restart=False)):
                    raise RuntimeError('Configurazione driver non applicata')
            res = self.runtime.start_vr(snapshot)
            if not _result_ok(res):
                raise RuntimeError("Avvio VR: runtime.start_vr insuccesso")
            running=self.tracking.status().get('running') is True
            res2 = self.tracking.update_settings(snapshot) if running else self.tracking.start(snapshot)
            if not _result_ok(res2):
                raise RuntimeError("Avvio VR: tracking.start insuccesso")
            return True
        self._run_serial(self._L("task.start_vr"), task)

    def on_attach(self):
        """Solo tracking verso VR esistente, nessun cambio modo hardware."""
        if self._busy():
            self.var_msg.set(self._L("action.busy_short"))
            return
        prefs, errori = self.collect_prefs()
        if errori:
            self.var_msg.set(self._L("action.prefs_invalid", dettagli="; ".join(errori)))
            return
        snapshot = copy.deepcopy(prefs)

        def task():
            try:
                running = self.tracking.status().get("running") is True
            except Exception:
                running = False
            if running:
                # Tracking gia' attivo: applica le impostazioni in-place
                # (stesso ramo di on_start_vr), mai restart cieco che
                # riaprirebbe la camera sotto i piedi del worker.
                res = self.tracking.update_settings(snapshot)
                if not _result_ok(res):
                    raise RuntimeError("Collega: tracking.update_settings insuccesso")
            else:
                res = self.tracking.start(snapshot)
                if not _result_ok(res):
                    raise RuntimeError("Collega: tracking.start insuccesso")
            return True
        self._run_serial(self._L("task.attach"), task)

    def on_stop_vr(self):
        if self._busy():
            return

        def task():
            try:
                res = self.tracking.stop()
                if not _result_ok(res):
                    raise RuntimeError("Stop: tracking.stop insuccesso")
            finally:
                res2 = self.runtime.stop_vr()
                if not _result_ok(res2):
                    raise RuntimeError("Stop: runtime.stop_vr insuccesso")
            return True
        self._run_serial(self._L("task.stop"), task)

    def on_cinema(self):
        """Cinema: prima stop tracking posseduto, poi to_cinema."""
        if self._busy():
            return

        prefs, errori = self.collect_prefs()
        if errori:
            self.var_msg.set(self._L("action.prefs_invalid", dettagli="; ".join(errori)))
            return
        snapshot = copy.deepcopy(prefs)

        def task():
            res = self.tracking.stop()
            if not _result_ok(res):
                raise RuntimeError("Cinema: tracking.stop insuccesso")
            res2 = self.runtime.to_cinema(snapshot)
            if not _result_ok(res2):
                raise RuntimeError("Cinema: to_cinema non confermato")
            return True
        self._run_serial(self._L("task.cinema"), task)

    def on_recenter(self):
        if self._busy():
            return

        def task():
            res = self.runtime.recenter()
            if not _result_ok(res):
                raise RuntimeError(f"Ricentra insuccesso: {res}")
            return True
        self._run_serial(self._L("task.recenter"), task)

    def on_realign(self):
        if self._busy():
            return

        def task():
            res = self.tracking.realign()
            if not _result_ok(res):
                raise RuntimeError("Riallinea insuccesso")
            return True
        self._run_serial(self._L("task.realign"), task)

    def on_reset_relative(self):
        if self._busy():
            return

        def task():
            res = self.tracking.reset_relative()
            if not _result_ok(res):
                raise RuntimeError("Azzera relativo insuccesso")
            return True
        self._run_serial(self._L("task.reset_relative"), task)

    def on_show_grid(self):
        if self._busy():
            return

        def task():
            res = self.runtime.show_grid()
            if not _result_ok(res):
                raise RuntimeError(f"Griglia insuccesso: {res}")
            return True
        self._run_serial(self._L("task.grid"), task)

    def _apply_driver_cold(self, snapshot, apply_optics, label):
        """Apply freddo: stop tracking PRIMA, apply, restart SOLO se era attivo.

        - Conserva tracking.status()['running'].
        - Se era attivo: tracking.stop() PRIMA dell'apply e verifica stop ok;
          se lo stop fallisce l'apply e' bloccato (solleva RuntimeError).
        - Snapshot prefs gia' validato; chiama _call_apply_driver una sola
          volta e controlla il result (_result_ok; restart_required -> fail,
          gestito dal primary in runtime.py).
        - Se apply fallisce/solleva: segnala chiaro, tracking NON riavviato
          automaticamente, healthy=False.
        - tracking.start(snapshot) SOLO se era attivo E apply ok; se prima
          non attivo non avvia niente di nuovo.
        """
        try:
            stato = self.tracking.status()
        except Exception as exc:
            raise RuntimeError(
                f"{label}: tracking.status non leggibile ({exc}); "
                "apply bloccato") from exc
        era_attivo = isinstance(stato, dict) and stato.get("running") is True
        if era_attivo:
            try:
                stop_res = self.tracking.stop()
            except Exception as exc:
                raise RuntimeError(
                    f"{label}: tracking.stop fallito ({exc}); apply bloccato, "
                    "tracking NON riavviato automaticamente") from exc
            if not _result_ok(stop_res):
                raise RuntimeError(
                    f"{label}: tracking.stop insuccesso ({stop_res!r}); "
                    "apply bloccato, tracking NON riavviato automaticamente")
        try:
            res = _call_apply_driver(self.runtime, snapshot, apply_optics)
        except Exception as exc:
            raise RuntimeError(
                f"{label}: apply driver eccezione ({exc}); tracking NON "
                "riavviato automaticamente, healthy=False") from exc
        if not _result_ok(res):
            raise RuntimeError(
                f"{label}: apply driver insuccesso ({res!r}); tracking NON "
                "riavviato automaticamente, healthy=False")
        if era_attivo:
            try:
                start_res = self.tracking.start(snapshot)
            except Exception as exc:
                raise RuntimeError(
                    f"{label}: driver applicato ma tracking.start fallito "
                    f"({exc}); riavvio manuale richiesto") from exc
            if not _result_ok(start_res):
                raise RuntimeError(
                    f"{label}: driver applicato ma tracking.start insuccesso "
                    f"({start_res!r}); riavvio manuale richiesto")
        return True

    def on_apply_driver(self):
        """Apply freddo senza ottica; stop tracking PRIMA, restart SOLO se attivo."""
        if self._busy():
            return
        prefs, errori = self.collect_prefs()
        if errori:
            self.var_msg.set(self._L("action.prefs_invalid", dettagli="; ".join(errori)))
            return
        snapshot = copy.deepcopy(prefs)

        def task():
            return self._apply_driver_cold(snapshot, False, self._L("task.apply_driver"))
        ok = self._run_serial(self._L("task.apply_driver"), task)
        if ok:
            try:
                corrente = self.var_msg.get()
            except Exception:
                corrente = ""
            self.var_msg.set(f"{self._L('calibration.notice')} | {corrente}")

    def on_apply_optics(self):
        """Solo pulsante esplicito mappa l'ottica (profilo != 'current')."""
        if self._busy():
            return
        prefs, errori = self.collect_prefs()
        if errori:
            self.var_msg.set(self._L("action.prefs_invalid", dettagli="; ".join(errori)))
            return
        if prefs.get("optics_profile", "current") == "current":
            self.var_msg.set(self._L("action.optics_current_noop"))
            return
        snapshot = copy.deepcopy(prefs)

        def task():
            return self._apply_driver_cold(snapshot, True, self._L("task.apply_optics"))
        ok = self._run_serial(self._L("task.apply_optics"), task)
        if ok:
            try:
                corrente = self.var_msg.get()
            except Exception:
                corrente = ""
            self.var_msg.set(f"{self._L('calibration.notice')} | {corrente}")

    def on_apply_tracking(self):
        if self._busy():
            return
        prefs, errori = self.collect_prefs()
        if errori:
            self.var_msg.set(self._L("action.prefs_invalid", dettagli="; ".join(errori)))
            return
        snapshot = copy.deepcopy(prefs)
        caps = module_for_source("ps4-stereo").editing_capabilities(
            getattr(self, "_last_status", ({}, {}))[1])
        legacy_apply = (snapshot.get("camera_source") == "ps4-stereo" and caps is not None
                        and caps.get("control_contract") == "opencv-nonstrict-single-ps4")
        if legacy_apply:
            # Clicking Apply makes the visible nonblank targets explicit, even
            # if their text still matches the profile. Collection/save alone
            # never opt into writes or synthesize readbacks.
            try:
                editor = self._source_editors["ps4-stereo"]
                values = {name: AppModel.parse_int(var.get(), name)
                          for name, var in editor["vars"].items() if str(var.get()).strip()}
                if not values:
                    raise ValueError("PS4 Apply requires at least one explicit target")
                validated = module_for_source("ps4-stereo").validate_saved_configuration({"controls": values})
                snapshot.setdefault("camera_profiles", {})["ps4-stereo"] = validated
            except (ValueError, KeyError) as exc:
                self.var_msg.set(self._L("action.prefs_invalid", dettagli=str(exc)))
                return

        def task():
            res = (self.tracking.update_settings(snapshot, force_legacy_reopen=True)
                   if legacy_apply else self.tracking.update_settings(snapshot))
            if not _result_ok(res):
                raise RuntimeError(f"Applica tracking insuccesso: {res}")
            return True
        label = "task.request_ps4_controls" if snapshot.get("camera_source") == "ps4-stereo" else "task.apply_tracking"
        self._run_serial(self._L(label), task)

    def on_save(self):
        if self._busy():
            self.var_msg.set(self._L("action.busy_short"))
            return
        prefs, errori = self.collect_prefs()
        if errori:
            self.var_msg.set(self._L("action.prefs_invalid", dettagli="; ".join(errori)))
            return
        try:
            persisted = None
            if Path(self.user_path).is_file():
                # Preserve the destination file's PS3 values even if another
                # explicit save changed them since this GUI loaded. Unreadable
                # existing prefs fail closed; do not overwrite with a fallback.
                persisted = AppModel.validate(self._settings.load_preferences(str(self.user_path)))
                if prefs["camera_source"] != "pseye":
                    prefs.update({key: copy.deepcopy(persisted[key])
                                  for key in module_for_source("pseye").legacy_keys})
            selected = prefs["camera_source"]
            # Preserve every OTHER source from the destination. Only an explicit
            # save of the selected source can commit that source's valid draft.
            # An existing destination is authoritative even when its carrier
            # was removed by another writer. Falling back per-key would revive
            # stale hidden-source profiles from this GUI's loaded snapshot.
            profiles = copy.deepcopy(
                persisted.get("camera_profiles", {}) if persisted is not None
                else getattr(self, "_camera_profiles_committed", {}))
            candidate = prefs.get("camera_profiles", {})
            if selected != "pseye":
                if selected in candidate:
                    profiles[selected] = candidate[selected]
                else:
                    profiles.pop(selected, None)
            # Commit this merge for ALL selections, including PS3. Never leave
            # the candidate's stale carrier in place on the legacy-flat branch.
            carrier_owner = persisted if persisted is not None else prefs
            if profiles or "camera_profiles" in carrier_owner:
                prefs["camera_profiles"] = validate_camera_profiles(profiles)
            else:
                prefs.pop("camera_profiles", None)
            saved_model = AppModel(prefs)
            self._settings.save_preferences(str(self.user_path), saved_model.to_prefs())
            self.model = saved_model  # reflect the final merge, not pre-reread drafts
            self._remember_ps3_preferences(prefs)
            self._camera_profiles_committed = copy.deepcopy(prefs.get("camera_profiles", {}))
            state = getattr(self, "_source_editors", {}).get(selected)
            if state is not None:
                state["initial"] = {name: var.get() for name, var in state["vars"].items()}
            key = ("action.prefs_saved" if prefs["camera_source"] == "pseye"
                   else "camera.saved_preserving_ps3")
            self.var_msg.set(self._L(key, path=str(self.user_path)))
            self._refresh_camera_settings()
        except Exception as exc:
            self.var_msg.set(self._L("action.save_failed", errore=str(exc)))

    def on_open_logs(self):
        try:
            path = str(self.logs_dir)
            if os.name == "nt":
                os.startfile(path)  # noqa: S606
            else:
                subprocess.Popen(
                    ["xdg-open", path],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.var_msg.set(self._L("action.logs_folder", path=str(path)))
        except Exception as exc:
            self.var_msg.set(self._L("action.logs_open_failed", errore=str(exc)))

    def _schedule_poll(self, task):
        """Schedula poll-stato senza overlap; False se saltato, mai flag incastrato."""
        with self._pending_lock:
            if self._status_pending or self.serial.pending_count() != 0:
                return False
            self._status_pending = True
        accepted = self.serial.submit("poll-stato", task, drop_if_pending=True)
        if not accepted:
            with self._pending_lock:
                self._status_pending = False
            return False
        return True

    # -- polling stato ogni 2 s su worker seriale (no overlap, no freeze) ---------
    def _poll_loop(self):
        while not self._poll_stop.is_set():
            if self._poll_stop.wait(STATUS_POLL_S):
                return
            if self.closing:
                return

            def task():
                try:
                    try:
                        status_fn=self.runtime.status
                        rt = status_fn(query_driver=True) if 'query_driver' in inspect.signature(status_fn).parameters else status_fn()
                    except Exception:
                        rt = {}
                    try:
                        tw = self.tracking.status()
                    except Exception:
                        tw = {}
                    # Read-only application diagnostics for unattended checks.
                    # Written on the worker, never carrying raw image bytes.
                    status_path=self.logs_dir/'suite_status.json'
                    tmp_path=self.logs_dir/'suite_status.tmp'
                    try:
                        tmp_path.write_text(json.dumps({'runtime':rt,'tracking':tw},indent=2,allow_nan=False),encoding='utf-8')
                        os.replace(tmp_path,status_path)
                    except (OSError,TypeError,ValueError):
                        pass
                    try:
                        self.display.put(("status", {"runtime": rt, "tracking": tw}),
                                         block=False)
                    except queue.Full:
                        pass
                finally:
                    with self._pending_lock:
                        self._status_pending = False
            self._schedule_poll(task)

    # -- drain display con throttle + coalescing frame -----------------------------
    def _drain_display(self):
        if self.closing:
            return
        try:
            batch = []
            for _ in range(DISPLAY_DRAIN_MAX):
                try:
                    batch.append(self.display.get_nowait())
                except queue.Empty:
                    break
            # B12-05: ogni frame trasporta il PPM (~0,9 MB); decodificare ogni
            # frame in coda spreca CPU su frame stantii. Coalescing: nel batch
            # si decodifica solo l'ultimo frame, gli intermedi sono scartati
            # senza decodifica. Eventi non-frame sempre elaborati in ordine.
            last_frame = None
            for idx, item in enumerate(batch):
                if item[0] == "frame":
                    last_frame = idx
            for idx, item in enumerate(batch):
                kind, payload = item
                if kind == "frame" and idx != last_frame:
                    continue
                try:
                    self._handle_event(kind, payload)
                except Exception as exc:
                    try:
                        self.var_msg.set(self._L("action.event_unhandled", errore=str(exc)))
                    except Exception:
                        pass
        finally:
            try:
                self.root.after(120, self._drain_display)
            except Exception:
                pass

    def _handle_event(self, kind, payload):
        lang = getattr(self, "lang", i18n_module.DEFAULT)
        if kind == "task_done":
            with self._pending_lock:
                self._pending = max(0, self._pending - 1)
                still = self._pending
            if still == 0 and not self.closing:
                self._set_buttons(True)
            _default_label = "azione" if lang == "it" else "action"
            label = payload.get("label", _default_label) if isinstance(payload, dict) else _default_label
            if isinstance(payload, dict) and payload.get("ok"):
                self.var_msg.set(i18n_module.fmt_task_done(lang, label))
            elif isinstance(payload, dict):
                # Reason tecnica backend restata verbatim, solo etichetta tradotta.
                self.var_msg.set(i18n_module.fmt_task_failed(
                    lang, label, payload.get("errore", "?")))
            return
        if kind == "status":
            if not isinstance(payload, dict):
                return
            rt = payload.get("runtime", {})
            tw = payload.get("tracking", {})
            self._last_status = (rt, tw)
            self._refresh_camera_settings(reload_profile=False)
            info = summarize_status(rt, tw)
            self.var_stato.set(_txt_lang(info["stato"], lang))
            self.var_sorgente.set(_txt_lang(info["sorgente"], lang))
            self.var_readiness.set(_txt_lang(info["readiness"], lang))
            self.var_modo_richiesto.set(_txt_lang(info["modo_richiesto"], lang))
            self.var_modo_effettivo.set(_txt_lang(info["modo_effettivo"], lang))
            fb = info["fallback_attivo"]
            self.var_fallback_diag.set(i18n_module.bool_text(fb, lang)
                                       if isinstance(fb, bool)
                                       else i18n_module.unknown(lang))
            self.var_recovery.set(_txt_lang(info.get("recovery"), lang))
            led = info["led"]
            self.var_led.set(_txt_lang(led, lang) if not isinstance(led, (list, tuple)) else (
                ", ".join(_txt_lang(v, lang) for v in led) if led else i18n_module.unknown(lang)))
            rms = info["rms"]
            if isinstance(rms, (int, float)) and not isinstance(rms, bool):
                self.var_rms.set(f"{rms:.3f} px" if math.isfinite(rms) else i18n_module.unknown(lang))
            else:
                self.var_rms.set(_txt_lang(rms, lang))
            age = info["age"]
            if isinstance(age, (int, float)) and not isinstance(age, bool):
                self.var_age.set(f"{age:.2f} s" if math.isfinite(age) else i18n_module.unknown(lang))
            else:
                self.var_age.set(_txt_lang(age, lang))
            self.var_imu_session.set(_txt_lang(info["imu_session"], lang))
            self.var_imu_epoch.set(_txt_lang(info["imu_epoch"], lang))
            pos = info["pos"]
            if isinstance(pos, (list, tuple)) and len(pos) == 3:
                try:
                    self.var_pos.set(AppModel.format_vector3([float(v) for v in pos]) + " m")
                except (TypeError, ValueError):
                    self.var_pos.set(i18n_module.unknown(lang))
            else:
                self.var_pos.set(_txt_lang(pos, lang))
            self.var_pos_mode.set(_txt_lang(info["pos_mode"], lang))
            if isinstance(tw, dict) and tw.get("preview_ppm"):
                self.var_preview_info.set(self._L("camera.preview_named",
                                                  info=str(tw.get("preview_ppm"))))
            return
        if kind == "frame":
            self._handle_frame(payload if isinstance(payload, dict) else {})
            return
        if kind == "calibration_waiting":
            secondi = None
            guida = ""
            if isinstance(payload, dict):
                secondi = payload.get("seconds", payload.get("secondi"))
                guida = payload.get("guidance", payload.get("guida", ""))
            try:
                sec_txt = f"{int(secondi)}s" if secondi is not None else ""
            except (TypeError, ValueError):
                sec_txt = ""
            dettaglio = f" {guida}".rstrip() if guida else ""
            self.var_msg.set(self._L("calibration.waiting", sec=sec_txt, dettaglio=dettaglio))
            try:
                self.var_preview_info.set(self._L("calibration.preview_waiting", sec=sec_txt))
            except Exception:
                pass
            return
        if kind in ("calibration_failed", "calibration-failed"):
            self.var_msg.set(self._L("calibration.failed"))
            return
        if kind == "restart_required":
            motivo = ""
            if isinstance(payload, dict):
                motivo = payload.get("reason", payload.get("motivo", ""))
            motivo_txt = f" ({motivo})" if motivo else ""
            self.var_msg.set(self._L("calibration.restart_required", motivo=motivo_txt))
            return
        if kind == "started":
            self.var_msg.set(self._L("calibration.started"))
            return
        if kind == "not_ready":
            motivo = ""
            if isinstance(payload, dict):
                motivo = payload.get("reason", payload.get("motivo", ""))
            motivo_txt = f" ({motivo})" if motivo else ""
            self.var_msg.set(self._L("calibration.not_ready", motivo=motivo_txt))
            return
        if kind in ("runtime", "tracking", "nota", "stopped", "error"):
            if isinstance(payload, dict):
                testo = payload.get("testo", payload.get("status",
                          payload.get("esito", payload.get("detail",
                          payload.get("azione", payload.get("error", ""))))))
                self.var_msg.set(f"{kind}: {_txt_lang(testo, lang)}")
            else:
                self.var_msg.set(f"{kind}: {_txt_lang(payload, lang)}")
            return
        self.var_msg.set(self._L("action.generic_event", kind=str(kind)))

    def _on_preview_configure(self, event=None):
        """Resize event (main thread): ricalo debounced, mai per-frame worker."""
        try:
            after = getattr(self.root, "after", None)
            if after is None:
                return
            pending = getattr(self, "_preview_resize_after", None)
            if pending is not None:
                try:
                    self.root.after_cancel(pending)
                except Exception:
                    pass
            self._preview_resize_after = self.root.after(
                100, self._refresh_preview)
        except Exception:
            pass

    def _refresh_preview(self):
        """Fit continuo smooth 4:3 al contenitore (bound 1600x1200).

        Chiamato solo dal main thread (frame drain o <Configure> debounced,
        ~8-10Hz: niente CPU da 75fps in GUI). Usa il PPM raw full-source
        640x480 del worker con cv2.resize INTER_LINEAR via lazy import DENTRO
        il metodo (niente import a modulo/headless); fallback a zoom/subsample
        interi se cv2 non disponibile o fake-Tk. Ritorna True se ha mostrato
        un'immagine, False altrimenti.
        """
        self._preview_resize_after = None
        try:
            base = getattr(self, "_preview_base", None)
            if base is None:
                return False
            try:
                bw, bh = self._preview_base_size
                bw, bh = int(bw), int(bh)
            except (TypeError, ValueError):
                try:
                    bw, bh = int(base.width()), int(base.height())
                except Exception:
                    return False
            if bw <= 0 or bh <= 0:
                return False
            frame = getattr(self, "preview_frame", None)
            if frame is None:
                return False
            try:
                aw = int(frame.winfo_width())
                ah = int(frame.winfo_height())
            except (TypeError, ValueError):
                return False
            # Contenitore non ancora mappato: attendi il prossimo evento.
            if aw < 50 or ah < 50:
                return False
            raw = getattr(self, "_preview_ppm_raw", None)
            if raw:
                try:
                    tw, th = _preview_fit_size(bw, bh, aw, ah)
                except Exception:
                    tw, th = (0, 0)
                if tw > 0 and th > 0:
                    try:
                        import numpy as _np  # lazy: niente import a modulo
                        import cv2 as _cv2  # lazy: gia' dipendenza camera env
                    except Exception:
                        _np = None
                        _cv2 = None
                    if _np is not None and _cv2 is not None:
                        try:
                            dw, dh, pix = _decode_ppm_to_rgb(raw)
                            if dw > 0 and dh > 0 and pix:
                                try:
                                    ftw, fth = _preview_fit_size(dw, dh, aw, ah)
                                except Exception:
                                    ftw, fth = (tw, th)
                                if ftw > 0 and fth > 0:
                                    tw, th = ftw, fth
                                    if (dw, dh) == (tw, th):
                                        # Exact-fit gia' nativo: mostra base.
                                        raise _UseBaseFallback()
                                    arr = _np.frombuffer(
                                        pix, dtype=_np.uint8).reshape((dh, dw, 3))
                                    interp = getattr(_cv2, "INTER_LINEAR", 1)
                                    resized = _cv2.resize(arr, (tw, th),
                                                          interpolation=interp)
                                    try:
                                        out = resized.tobytes()
                                    except Exception:
                                        out = bytes(resized.data)
                                    ppm2 = _encode_rgb_to_ppm(tw, th, out)
                                    if ppm2.startswith(b"P6"):
                                        img2 = self.tk.PhotoImage(data=ppm2,
                                                                  format="PPM")
                                        self._preview_img = img2
                                        try:
                                            self.preview_widget.configure(
                                                image=img2, text="")
                                        except Exception:
                                            return False
                                        return True
                        except _UseBaseFallback:
                            pass
                        except Exception:
                            pass
            # Fallback intero (cv2 assente, fake-Tk, o raw mancante).
            try:
                zoom, sub = _preview_zoom_for(bw, bh, aw, ah, max_zoom=3)
            except Exception:
                return False
            try:
                if zoom > 1:
                    scaled = base.zoom(zoom, zoom)
                elif sub > 1:
                    scaled = base.subsample(sub, sub)
                else:
                    scaled = base
            except Exception:
                return False
            self._preview_img = scaled
            try:
                self.preview_widget.configure(image=scaled, text="")
            except Exception:
                return False
            return True
        except Exception:
            return False

    def _handle_frame(self, payload):
        """Frame reale 'ppm' full-source -> PhotoImage con ref, fit 4:3.

        Sorgente worker 640x480 full (punti preservati); qui solo display:
        conserva il PPM raw per lo scaling smooth exact-fit in
        _refresh_preview (lazy cv2 INTER_LINEAR, bound 1600x1200), con base
        PhotoImage come fallback. Downsample di sicurezza se >640x480 per la
        base; drain ~8-10Hz + resize debounced (niente CPU da 75fps in GUI).
        Aspect mai stirato.
        """
        ppm = payload.get("ppm") if isinstance(payload, dict) else None
        status_text = payload.get("status", "") if isinstance(payload, dict) else ""
        detail = payload.get("detail", "") if isinstance(payload, dict) else ""
        if isinstance(status_text, str) and status_text.strip():
            self.var_preview_info.set(status_text.strip()[:300])
        if isinstance(detail, str) and detail.strip():
            self.var_preview_detail.set(detail.strip()[:300])
        if not ppm:
            return
        try:
            if isinstance(ppm, (bytes, bytearray)):
                raw = bytes(ppm)
            elif isinstance(ppm, str):
                raw = ppm.encode("ascii", errors="ignore")
            else:
                return
            if not raw.startswith(b"P6"):
                return
            img = self.tk.PhotoImage(data=raw,format="PPM")
            try:
                w, h = int(img.width()), int(img.height())
            except Exception:
                w, h = 640, 480
            factor = 1
            while (w // max(factor, 1) > 640 or h // max(factor, 1) > 480) and factor < 8:
                factor += 1
            if factor > 1:
                try:
                    img = img.subsample(factor, factor)
                except Exception:
                    pass
            try:
                bw, bh = int(img.width()), int(img.height())
            except Exception:
                bw, bh = w, h
            self._preview_base = img
            self._preview_base_size = (bw, bh)
            self._preview_ppm_raw = raw
            try:
                if self._refresh_preview():
                    return
            except Exception:
                pass
            self._preview_img = img
            try:
                self.preview_widget.configure(image=img, text="")
            except Exception:
                pass
        except Exception as exc:
            self.var_preview_detail.set(self._L("camera.preview_decode_failed", errore=str(exc)))

    # -- chiusura: rilascia lock backend + tracking, mai join sul main ------------
    def on_close(self):
        if self.closing:
            return
        self.closing = True
        # B12-09: cancella il resize-after pendente (best-effort, mai solleva):
        # evita callback orfane dopo destroy (mitigate comunque dal try/except
        # in _refresh_preview). Non tocca drain/preview/settings.
        try:
            pending = getattr(self, "_preview_resize_after", None)
            if pending is not None:
                try:
                    self.root.after_cancel(pending)
                except Exception:
                    pass
                self._preview_resize_after = None
        except Exception:
            pass
        try:
            self.var_msg.set(self._L("action.closing"))
        except Exception:
            pass
        self._set_buttons(False)
        self._poll_stop.set()
        done = threading.Event()
        shutdown_done = threading.Event()
        bg_started = {"started": False}

        def final_task():
            try:
                try:
                    self.tracking.stop()
                except Exception:
                    pass
                try:
                    close = getattr(self.runtime, "close", None)
                    if callable(close):
                        close()
                except Exception:
                    pass
            finally:
                done.set()
        try:
            self.serial.submit("stop-posseduto", final_task)
        except Exception:
            done.set()

        def poll_finish():
            if self.closing and not done.is_set():
                try:
                    self.root.after(100, poll_finish)
                except Exception:
                    pass
                return
            # B12-02: Tk non e' thread-safe. Il bg fa solo shutdown+Event;
            # destroy resta sul main (questo callback gira via root.after).
            if not bg_started["started"]:
                bg_started["started"] = True

                def _shutdown_bg():
                    try:
                        self.serial.shutdown(timeout=5.0)
                    finally:
                        shutdown_done.set()
                threading.Thread(target=_shutdown_bg, daemon=True).start()
            if not shutdown_done.is_set():
                try:
                    self.root.after(100, poll_finish)
                except Exception:
                    pass
                return
            try:
                self.root.destroy()
            except Exception:
                pass
        try:
            self.root.after(100, poll_finish)
        except Exception:
            pass


def main(argv=None):
    parser = build_arg_parser()
    raw = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(normalize_tab_argv(raw))
    root_path = Path(args.root) if args.root else ROOT_DEFAULT
    if args.headless_check:
        return headless_check(root_path, args.preferences)
    if args.attach and args.start_vr:
        print("--attach e --start-vr incompatibili", file=sys.stderr)
        return 2
    lock = SuiteLock(root_path / LOCK_REL)
    try:
        lock.acquire()
    except RuntimeError as exc:
        print(f"Avvio rifiutato: {exc}", file=sys.stderr)
        return 3
    try:
        import tkinter as tk
    except Exception as exc:
        print(f"Tk non disponibile: {exc}", file=sys.stderr)
        lock.close()
        return 4
    try:
        tk_root = tk.Tk()
    except Exception as exc:
        print(f"Display non disponibile: {exc}", file=sys.stderr)
        lock.close()
        return 4
    app = SuiteApp(tk_root, root_path,
                   preferences_path=args.preferences,
                   start_vr=bool(args.start_vr),
                   attach=bool(args.attach))
    tab_names = _TAB_CANONICAL
    app.tabs.select(tab_names.index(args.tab))
    try:
        tk_root.mainloop()
    finally:
        try:
            app._poll_stop.set()
        except Exception:
            pass
        try:
            close = getattr(app.runtime, "close", None)
            if callable(close):
                close()
        except Exception:
            pass
        lock.close()
    return 0
