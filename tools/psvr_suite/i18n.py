"""Dizionario i18n IT/EN per PSVRSuite (solo UI, mai backend/driver).

Regole:
- Catalogue statico: nessuna replace di sottostringhe casuali o debug.
- La lingua NON vive in settings.py / schema canonico (28 pref) / driver:
  sta nel file UI separato configurations/psvr_suite_ui.json.
- Default italiano per compat con le preferenze attuali; lingua
  sconosciuta -> fallback grazioso a italiano, mai eccezioni in lettura.
- Messaggi dinamici solo via formatter tipati (t/format_*), mai
  concatenazioni sparse nella GUI. Dettaglio tecnico backend (reason)
  restato verbatim, solo l'etichetta e' tradotta.
- Window title Tk resta invariante "PSVR Suite" per compat con
  launcher (FindWindowW) e ui_snapshot: solo l'header interno e' tradotto.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

LANG_IT = "it"
LANG_EN = "en"
SUPPORTED = (LANG_IT, LANG_EN)
DEFAULT = LANG_IT

UI_PREFS_REL = Path("configurations") / "psvr_suite_ui.json"

# Invariante di compat: titolo finestra OS Cercato da launcher/snapshot.
WINDOW_TITLE = "PSVR Suite"


def normalize_language(value) -> str:
    """Normalizza un codice lingua a 'it'/'en'; unknown -> 'it' (grazioso)."""
    try:
        if value is None:
            return DEFAULT
        if not isinstance(value, str):
            return DEFAULT
        s = value.strip().lower().replace("_", "-")
        if s in ("it", "it-it", "italiano", "italian"):
            return LANG_IT
        if s in ("en", "en-us", "en-gb", "english", "inglese"):
            return LANG_EN
        return DEFAULT
    except Exception:
        return DEFAULT


STRINGS = {
    "ps4.open": {"it": "PS4 Camera · acquisizione stereo", "en": "PS4 Camera · stereo capture"},
    "ps4.inventory": {"it": "Rileva USB", "en": "Detect USB"},
    "ps4.preview": {"it": "Anteprima L/R (60 s)", "en": "L/R preview (60 s)"},
    "ps4.stop": {"it": "Ferma", "en": "Stop"},
    "ps4.led_check": {"it": "Verifica LED (10 s)", "en": "Check LEDs (10 s)"},
    "ps4.points": {"it": "Punti 3D (30 s)", "en": "3D points (30 s)"},
    "ps4.calibrate": {"it": "Calibrazione guidata stereo", "en": "Guided stereo calibration"},
    "ps4.calibration_dshow": {"it": "Per la procedura PS4 guidata seleziona DirectShow (dshow).", "en": "Select DirectShow (dshow) for the PS4 guided workflow."},
    "camera.ps4_legacy_warning": {
        "it": "LEGACY OpenCV NON-STRICT: identità del filtro di controllo NON verificabile. Edit solo con un unico OV580 runtime all'indice scelto. Limiti dei campi = policy, NON range misurati. Conferma = sola osservazione numerica get(), non identità/flag Manual né verifica indipendente. Apply richiede riapertura; Stop tenta restore. Nessun salvataggio implicito.",
        "en": "NON-STRICT OpenCV LEGACY: control-filter identity CANNOT be verified. Edits require a single OV580 runtime at the selected index. Field limits are policy, NOT measured ranges. Confirmation means numeric get() observation only, not identity/Manual flags or independent verification. Apply reopens; Stop attempts restore. No implicit save."},
    "camera.ps4_legacy_result": {
        "it": "Esito Apply OpenCV: {state} (confirmed = readback numerici confermati; unconfirmed = NON confermato, nessun successo dichiarato)",
        "en": "OpenCV Apply result: {state} (confirmed = numeric readbacks confirmed; unconfirmed = NOT confirmed, no success claimed)"},
    "camera.ps4_legacy_restore": {
        "it": "Ultimo restore OpenCV: {state}; cleanup pendente: {pending}. readback_confirmed = ripristino numerico confermato, NON verifica indipendente del sensore.",
        "en": "Last OpenCV restore: {state}; cleanup pending: {pending}. readback_confirmed = numeric restoration confirmed, NOT independent sensor verification."},
    "task.request_ps4_controls": {
        "it": "Richiesta riapertura PS4 (controllare esito/readback, NON conferma applicazione)",
        "en": "PS4 reopen request (check result/readback, NOT application confirmation)"},
    "ps4.restoring": {"it": "Chiusura guidata richiesta: attendo rilascio camera e ripristino controlli LED...", "en": "Guided close requested: waiting for camera release and LED control restoration..."},
    "ps4.index_error": {"it": "Indice UVC: inserisci un intero >= 0.", "en": "UVC index: enter an integer >= 0."},
    "ps4.running": {"it": "Operazione in corso. ESC chiude l'anteprima; Ferma interrompe il processo.", "en": "Running. ESC closes preview; Stop terminates the process."},
    "ps4.guide": {
        "it": "PS4 Camera: 6DoF statico sperimentale verificato, con dropout presenti. Richiede firmware UVC esterno e calibrazione PS4 dedicata. Il pannello separato serve per acquisizione e calibrazione; il tracking si avvia dalla Suite. Il pannello non installa driver o firmware. Vedi docs/PS4_CAMERA.md.",
        "en": "PS4 Camera: 6DoF static experimental verification confirmed; dropouts present. Requires external UVC firmware and dedicated PS4 calibration. The separate panel is for acquisition/calibration only; tracking starts from the Suite. No driver or firmware is installed by the panel.",
    },
    # -- header / lingua -----------------------------------------------------
    "header.title": {
        "it": "PSVR · Suite utente persistente",
        "en": "PSVR · Persistent user suite",
    },
    "header.language": {
        # Bilingue invariante cosi' e' trovabile in entrambe le lingue.
        "it": "Lingua / Language",
        "en": "Lingua / Language",
    },
    "header.lang_italiano": {"it": "Italiano", "en": "Italiano"},
    "header.lang_english": {"it": "English", "en": "English"},
    "header.ready": {
        "it": "Pronta. Nessuna azione all'avvio.",
        "en": "Ready. No action at startup.",
    },
    "header.attach_incompatible": {
        "it": "--attach e --start-vr incompatibili: uso --attach.",
        "en": "--attach and --start-vr are incompatible: using --attach.",
    },
    "header.language_switched": {
        "it": "Lingua: Italiano.",
        "en": "Language: English.",
    },
    "header.language_save_failed": {
        "it": "Lingua non salvata: {errore}",
        "en": "Language not saved: {errore}",
    },
    "header.prefs_note_bad_file": {
        "it": "Preferenze {path} non caricate ({errore}); provo le impostazioni del driver.",
        "en": "Preferences {path} not loaded ({errore}); trying the driver settings.",
    },
    "header.prefs_note_live_failed": {
        "it": "Live driver prefs non lette ({errore}); uso default.",
        "en": "Live driver prefs not read ({errore}); using defaults.",
    },
    "common.unknown": {"it": "sconosciuto", "en": "unknown"},
    "common.yes": {"it": "sì", "en": "yes"},
    "common.no": {"it": "no", "en": "no"},
    "common.initial_values_partial": {
        "it": "Valori iniziali parziali: {errore}",
        "en": "Partial initial values: {errore}",
    },
    # -- tabs ----------------------------------------------------------------
    "tab.session": {"it": "Sessione", "en": "Session"},
    "tab.tracking": {"it": "Tracking", "en": "Tracking"},
    "tab.camera": {"it": "Camera", "en": "Camera"},
    "tab.position": {"it": "Posizione", "en": "Position"},
    "tab.optics": {"it": "Ottica", "en": "Optics"},
    "tab.diagnostics": {"it": "Diagnostica", "en": "Diagnostics"},
    # -- sessione --------------------------------------------------------------
    "session.state": {"it": "Stato:", "en": "State:"},
    "session.source": {"it": "Sorgente:", "en": "Source:"},
    "session.readiness": {"it": "Prontezza:", "en": "Readiness:"},
    "session.mode_requested": {"it": "Modo richiesto:", "en": "Requested mode:"},
    "session.mode_actual": {"it": "Modo effettivo:", "en": "Actual mode:"},
    "session.fallback": {"it": "Fallback 3DoF (diagnostica):", "en": "3DoF fallback (diagnostics):"},
    "session.recovery": {"it": "Recovery/riaggancio:", "en": "Recovery/reacquire:"},
    "session.btn_start": {"it": "Avvia VR", "en": "Start VR"},
    "session.btn_attach": {"it": "Collega tracking (VR esistente)", "en": "Attach tracking (existing VR)"},
    "session.btn_stop": {"it": "Ferma VR (ferma SteamVR)", "en": "Stop VR (stops SteamVR)"},
    "session.btn_cinema": {"it": "Vai a cinema", "en": "Go to cinema"},
    "session.btn_apply_driver": {"it": "Applica driver", "en": "Apply driver"},
    "session.guide": {
        "it": "Avvio VR: prima runtime.start_vr poi tracking.start, prefs effettive solo al click. La finestra resta aperta durante l'uso VR.",
        "en": "Start VR: runtime.start_vr first, then tracking.start; effective prefs only on click. The window stays open while using VR.",
    },
    # -- tracking --------------------------------------------------------------
    "tracking.mode": {"it": "Modo tracking:", "en": "Tracking mode:"},
    "tracking.fallback_check": {"it": "Fallback 3DoF attivo", "en": "3DoF fallback enabled"},
    "tracking.two_led": {"it": "Opzioni 6DoF", "en": "6DoF options"},
    "tracking.two_led_check": {"it": "Sperimentale: posizione anche con 2 LED", "en": "Experimental: position with 2 LEDs"},
    "tracking.two_led_note": {"it": "Disattivato: raccordo al centro tra 90° e 120°. Attivo: 6DoF con 2 LED e angolo IMU; sensibile alla deriva. Richiede riavvio VR.", "en": "Off: blend to center from 90° to 120°. On: 6DoF with 2 LEDs and IMU angle; drift-sensitive. Requires VR restart."},
    "tracking.fallback": {"it": "Fallback:", "en": "Fallback:"},
    "tracking.fallback_note": {"it": "se 6DoF perso, tiene 3DoF", "en": "if 6DoF is lost, keeps 3DoF"},
    "tracking.blend": {"it": "Blend riaggancio (ms):", "en": "Reacquire blend (ms):"},
    "tracking.blend_note": {"it": "default 150", "en": "default 150"},
    "tracking.speed": {"it": "Velocità riaggancio (m/s):", "en": "Reacquire speed (m/s):"},
    "tracking.speed_note": {"it": "default 1.5, min 0.01", "en": "default 1.5, min 0.01"},
    "tracking.cal": {"it": "Calibrazione IMU (ms):", "en": "IMU calibration (ms):"},
    "tracking.cal_note": {"it": "baseline 8000", "en": "baseline 8000"},
    "tracking.stale": {"it": "Stale IMU (ms):", "en": "IMU stale (ms):"},
    "tracking.stale_note": {"it": "baseline 100", "en": "baseline 100"},
    "tracking.gap": {"it": "Gap max campioni (us):", "en": "Max sample gap (us):"},
    "tracking.gap_note": {
        "it": "driver 1000..50000; derivato diagnostico max 20000 us",
        "en": "driver 1000..50000; diagnostic derivative max 20000 us",
    },
    "tracking.config": {"it": "Config diagnostico:", "en": "Diagnostic config:"},
    "tracking.config_note": {
        "it": "psvr_diag per avvio VR/cinema; relativo alla root. IMU driver: campi sopra",
        "en": "psvr_diag for VR start/cinema; relative to root. Driver IMU: fields above",
    },
    "tracking.btn_apply": {"it": "Applica impostazioni tracking", "en": "Apply tracking settings"},
    "tracking.btn_reset": {"it": "Azzera relativo camera", "en": "Reset camera relative"},
    # -- camera ------------------------------------------------------------------
    "camera.preview_placeholder": {"it": "Anteprima…", "en": "Preview…"},
    "camera.preview_waiting": {
        "it": "Anteprima camera (PPM): in attesa del backend…",
        "en": "Camera preview (PPM): waiting for backend…",
    },
    "camera.preview_unavailable": {"it": "Anteprima non disponibile", "en": "Preview unavailable"},
    "camera.preview_decode_failed": {
        "it": "Anteprima non decodificata: {errore}",
        "en": "Preview not decoded: {errore}",
    },
    "camera.preview_named": {
        "it": "Anteprima: {info}",
        "en": "Preview: {info}",
    },
    "camera.source": {"it": "Videocamera 6DoF:", "en": "6DoF camera:"},
    "camera.source_note": {
        "it": "La selezione aggiorna solo il display. PS4 usa il proprio profilo; i valori PS3 non salvati restano separati e conservati.",
        "en": "Selection updates the display only. PS4 uses its own profile; unsaved PS3 values remain separate and preserved.",
    },
    "camera.legacy_requested": {
        "it": "Preferenze richieste (non readback del dispositivo). Esposizione {exposure_min}..{exposure_max} e gain {gain_min}..{gain_max} sono controlli PS3 Eye, non unità PS4.",
        "en": "Requested preferences (not device readback). Exposure {exposure_min}..{exposure_max} and gain {gain_min}..{gain_max} are PS3 Eye controls, not PS4 units.",
    },
    "camera.legacy_snapshot_missing": {
        "it": "Copia PS3 validata caricata/salvata non disponibile: operazione bloccata senza inventare valori. Ricarica preferenze valide; il draft PS3 resta intatto.",
        "en": "No validated loaded/saved PS3 snapshot is available: operation blocked without inventing values. Reload valid preferences; the PS3 draft remains intact.",
    },
    "camera.saved_preserving_ps3": {
        "it": "Preferenze salvate in {path}. Valori PS3 già salvati/caricati conservati; il draft PS3 nascosto NON è stato salvato. Per salvarlo, seleziona PS3 Eye e salva valori validi.",
        "en": "Preferences saved to {path}. Previously saved/loaded PS3 values preserved; the hidden PS3 draft was NOT saved. To save it, select PS3 Eye and save valid values.",
    },
    "camera.source_unknown": {
        "it": "Sorgente sconosciuta: controlli nascosti, nessun fallback PS3/PS4.",
        "en": "Unknown source: controls hidden, no PS3/PS4 fallback.",
    },
    "camera.generic_readonly": {
        "it": "Sorgente generica: nessun controllo nativo dichiarato; range e readback sconosciuti. Usa solo la configurazione dedicata esplicita, validata dal backend prima dell'apertura. Nessun controllo o calibrazione PS3 viene assegnato implicitamente.",
        "en": "Generic source: no native controls declared; ranges and readback unknown. Use only explicit dedicated configuration, validated by the backend before open. No PS3 controls or calibration are assigned implicitly.",
    },
    "camera.generic_configuration_required": {
        "it": "Configurazione dedicata richiesta: specifica formato, calibrazione e rig per questa sorgente in camera_profiles. Avvio/applicazione bloccati senza fallback PS3.",
        "en": "Dedicated configuration required: specify this source's format, calibration and rig in camera_profiles. Start/apply blocked without PS3 fallback.",
    },
    "camera.generic_configuration": {
        "it": "Configurazione dedicata: {width}×{height} @ {fps}\nCalibrazione: {intrinsicsPath}\nRig LED: {rigPath}\nRig IMU: {imu_rig}",
        "en": "Dedicated configuration: {width}×{height} @ {fps}\nCalibration: {intrinsicsPath}\nLED rig: {rigPath}\nIMU rig: {imu_rig}",
    },
    "camera.source_edit_pending": {
        "it": "Richiesto (non ancora applicato). Applica/Collega espliciti; modalità backend: {mode}. Limiti dichiarati (range nativi o policy etichettata): {ranges}. Campo controllo vuoto = eredita il profilo sorgente. Salva persiste solo il draft valido della sorgente selezionata, non applica hardware.",
        "en": "Requested (not yet applied). Explicit Apply/Attach; backend mode: {mode}. Declared limits (native ranges or labelled policy): {ranges}. Blank control = inherit source profile. Save persists only this source's valid draft, never applies hardware.",
    },
    "camera.source_edit_unavailable": {
        "it": "Sola lettura: editing/applicazione non dichiarati dal backend o range/capacità dispositivo non disponibili. I draft restano conservati; nessun controllo fittizio è applicato.",
        "en": "Read-only: backend editing/apply contract or device ranges/capabilities unavailable. Drafts remain preserved; no pretend controls are applied.",
    },
    "camera.profile_error": {
        "it": "Profilo PS4 non disponibile/non valido: {errore}\nNessun TARGET disponibile e nessun fallback PS3.",
        "en": "PS4 profile unavailable/invalid: {errore}\nNo TARGET available and no PS3 fallback.",
    },
    "camera.ps4_readonly": {
        "it": "PS4 · sola lettura senza capability backend e range reali. I controlli sono gestiti dalla sorgente: nessuna modifica hardware alla selezione. Nessun controllo PS3 viene trasferito.",
        "en": "PS4 · read-only without backend capabilities and measured ranges. Controls are source-owned: selection never changes hardware. No PS3 controls are transferred.",
    },
    "camera.ps4_controls_ready": {
        "it": "PS4 · controlli propri. Il backend dichiara applicazione {mode}; editor secondo contratto esplicito: range nativi misurati oppure policy OpenCV NON-STRICT etichettata. Conferma solo dal readback corrente.",
        "en": "PS4 · source-owned controls. Backend declares {mode} apply; editors follow an explicit contract: measured native ranges OR labelled NON-STRICT OpenCV policy. Confirmation only from current readback.",
    },
    "camera.profile_target": {
        "it": "TARGET configurato: profilo {path} + eventuali override espliciti camera_profiles.\nLetto alla selezione (riseleziona per rileggere). Configurazione risolta, NON conferma hardware; gli editor mostrano i draft separati.",
        "en": "Configured TARGET: profile {path} + any explicit camera_profiles overrides.\nRead on selection (reselect to reload). Resolved configuration, NOT hardware confirmation; editors show separate drafts.",
    },
    "camera.profile_format": {
        "it": "Formato del profilo per occhio: {width}×{height} @ {fps} FPS",
        "en": "Profile per-eye format: {width}×{height} @ {fps} FPS",
    },
    "camera.profile_controls": {
        "it": "TARGET exposure: {exposure} · Manual richiesto\nTARGET gain: {gain} · Manual richiesto\nTARGET brightness: {brightness}\nUnità PS4/DirectShow (non PS3). Il backend nativo misura range/capacità; OpenCV legacy espone solo policy e NON conferma i flag Manual.",
        "en": "TARGET exposure: {exposure} · Manual requested\nTARGET gain: {gain} · Manual requested\nTARGET brightness: {brightness}\nPS4/DirectShow units (not PS3). The native backend measures ranges/capabilities; OpenCV legacy exposes policy only and does NOT confirm Manual flags.",
    },
    "camera.owner_selected": {
        "it": "non specificato; risolto dalla sorgente all'apertura",
        "en": "unspecified; resolved by the source at open",
    },
    "camera.profile_artifacts": {
        "it": "Calibrazione del profilo: {calibration}\nRig LED del profilo: {rig}\nOrdine occhi: {eyes}",
        "en": "Profile calibration: {calibration}\nProfile LED rig: {rig}\nEye order: {eyes}",
    },
    "camera.profile_imu_rig": {
        "it": "Rig IMU PS3: non usato dal percorso stereo (frame camera sinistra calibrata).",
        "en": "PS3 IMU rig: not used by the stereo path (calibrated left-camera frame).",
    },
    "camera.profile_detector": {
        "it": "Detector del profilo (sola lettura): V {value_min}, saturazione {saturation_min}, area {min_area}, apertura {opening_size}.",
        "en": "Profile detector (read-only): V {value_min}, saturation {saturation_min}, area {min_area}, opening {opening_size}.",
    },
    "camera.readback_unavailable": {
        "it": "Readback PS4: non disponibile nello stato della sorgente corrente. Valori e modo Manual NON confermati.",
        "en": "PS4 readback: unavailable in the current source status. Values and Manual mode are NOT confirmed.",
    },
    "camera.readback_heading": {
        "it": "Readback da camera_controls nello stato PS4 corrente (ultimo poll). La conferma acquisizione riguarda il target all'apertura, NON il TARGET del profilo mostrato sopra.",
        "en": "Readback from camera_controls in current PS4 status (last poll). Acquisition confirmation concerns the target at open, NOT the profile TARGET above.",
    },
    "camera.readback_value": {
        "it": "{control}: letto {value}; conferma acquisizione backend: {confirmed}",
        "en": "{control}: read {value}; backend acquisition confirmation: {confirmed}",
    },
    "camera.device_exposure_range": {
        "it": "Range esposizione letto dal dispositivo: {minimum}..{maximum}, passo {step}",
        "en": "Exposure range read from device: {minimum}..{maximum}, step {step}",
    },
    "camera.selection_only": {
        "it": "Questa vista non applica modifiche. La sorgente selezionata può differire da quella in acquisizione; Collega/Applica restano azioni esplicite. Nessuna riapertura automatica.",
        "en": "This view applies no changes. The selected source may differ from the acquiring source; Attach/Apply remain explicit actions. No automatic reopen.",
    },
    "camera.f_value": {"it": "Soglia V (value_min):", "en": "V threshold (value_min):"},
    "camera.f_value_note": {"it": "dalle preferenze PS3", "en": "from PS3 preferences"},
    "camera.f_area": {"it": "Area minima (min_area):", "en": "Minimum area (min_area):"},
    "camera.f_area_note": {"it": "dalle preferenze PS3", "en": "from PS3 preferences"},
    "camera.f_exposure": {"it": "Esposizione (exposure):", "en": "Exposure (exposure):"},
    "camera.f_exposure_note": {"it": "unità sensore PS3; valore dalle preferenze", "en": "PS3 sensor units; target from preferences"},
    "camera.f_gain": {"it": "Guadagno (gain):", "en": "Gain (gain):"},
    "camera.f_gain_note": {"it": "unità sensore PS3; valore dalle preferenze", "en": "PS3 sensor units; target from preferences"},
    "camera.f_sat": {"it": "Saturazione min:", "en": "Min saturation:"},
    "camera.f_sat_note": {"it": "dalle preferenze PS3", "en": "from PS3 preferences"},
    "camera.f_opening": {"it": "Apertura (opening):", "en": "Opening:"},
    "camera.f_opening_note": {"it": "solo 0/3/5", "en": "only 0/3/5"},
    "camera.f_fps": {"it": "FPS (readonly):", "en": "FPS (readonly):"},
    "camera.f_fps_note": {"it": "dalla configurazione sorgente", "en": "from source configuration"},
    "camera.f_width": {"it": "Larghezza (readonly):", "en": "Width (readonly):"},
    "camera.f_width_note": {"it": "dalla configurazione sorgente", "en": "from source configuration"},
    "camera.f_height": {"it": "Altezza (readonly):", "en": "Height (readonly):"},
    "camera.f_height_note": {"it": "dalla configurazione sorgente", "en": "from source configuration"},
    "camera.f_intrinsics": {"it": "Intrinseci (readonly):", "en": "Intrinsics (readonly):"},
    "camera.f_rig": {"it": "Rig LED (readonly):", "en": "LED rig (readonly):"},
    "camera.f_imu_rig": {"it": "Rig IMU (readonly):", "en": "IMU rig (readonly):"},
    "camera.btn_attach": {"it": "Collega camera / tracking", "en": "Attach camera / tracking"},
    "camera.btn_apply": {"it": "Applica camera", "en": "Apply camera"},
    "camera.guide": {
        "it": "Collega avvia solo il tracking (preview senza VR); Applica invia le impostazioni camera al tracking.",
        "en": "Attach starts only tracking (preview without VR); Apply sends the camera settings to tracking.",
    },
    "camera.warning": {
        "it": "Formato camera immutabile: risoluzioni diverse richiedono ricalibrazione; validato prima dell'hardware.",
        "en": "Immutable camera format: different resolutions require recalibration; validated before hardware.",
    },
    # -- posizione ---------------------------------------------------------------
    "position.filter": {"it": "Filtro (esclusivo):", "en": "Filter (exclusive):"},
    "position.filter_one_euro": {"it": "One Euro (default)", "en": "One Euro (default)"},
    "position.filter_initial": {"it": "Kalman iniziale", "en": "Initial Kalman"},
    "position.filter_optimized": {"it": "Kalman ottimizzato", "en": "Optimized Kalman"},
    "position.cutoff": {"it": "Min cutoff (Hz):", "en": "Min cutoff (Hz):"},
    "position.cutoff_note": {"it": "default 2.5, max 40", "en": "default 2.5, max 40"},
    "position.beta": {"it": "Beta:", "en": "Beta:"},
    "position.beta_note": {"it": "default 8", "en": "default 8"},
    "position.scale": {"it": "Scala posizione:", "en": "Position scale:"},
    "position.scale_note": {"it": "default 1", "en": "default 1"},
    "position.head": {"it": "Offset modello testa XYZ (m):", "en": "Head model offset XYZ (m):"},
    "position.head_note": {"it": "modello testa, NON mondo", "en": "head model, NOT world"},
    "position.initial": {"it": "Offset mondo INIZIALE XYZ (m):", "en": "INITIAL world offset XYZ (m):"},
    "position.initial_note": {"it": "mondo iniziale, NON modello", "en": "initial world, NOT model"},
    "position.btn_apply": {"it": "Applica posizione", "en": "Apply position"},
    "position.guide": {
        "it": "Formato XYZ: 'x, y, z' in metri, finiti. I due offset hanno base diversa e restano separati.",
        "en": "XYZ format: 'x, y, z' in metres, finite. The two offsets have different bases and stay separate.",
    },
    # -- ottica ------------------------------------------------------------------
    "optics.profile": {"it": "Profilo ottico:", "en": "Optics profile:"},
    "optics.profile_note": {"it": "'current' = non toccare live", "en": "'current' = do not touch live"},
    "optics.btn_grid": {"it": "Mostra griglia piatta", "en": "Show flat grid"},
    "optics.btn_apply": {"it": "Applica ottica (esplicita)", "en": "Apply optics (explicit)"},
    "optics.guide": {
        "it": "L'applicazione ottica richiede il riavvio del runtime, non e' live. Solo il pulsante esplicito mappa l'ottica quando il profilo e' diverso da 'current'.",
        "en": "Optical guide requires a runtime restart, not live. Only the explicit button maps optics when the profile differs from 'current'.",
    },
    # -- diagnostica ---------------------------------------------------------------
    "diag.led": {"it": "LED visti:", "en": "LEDs seen:"},
    "diag.rms": {"it": "RMS (px):", "en": "RMS (px):"},
    "diag.age": {"it": "Età misura (s):", "en": "Measurement age (s):"},
    "diag.imu_session": {"it": "IMU sessione:", "en": "IMU session:"},
    "diag.imu_epoch": {"it": "IMU epoca:", "en": "IMU epoch:"},
    "diag.pos": {"it": "Posizione stimata (m):", "en": "Estimated position (m):"},
    "diag.pos_mode": {"it": "Modo posizione:", "en": "Position mode:"},
    "diag.btn_recenter": {"it": "Ricentra (IMU)", "en": "Recenter (IMU)"},
    "diag.btn_realign": {"it": "Riallinea camera", "en": "Realign camera"},
    "diag.btn_logs": {"it": "Apri cartella log", "en": "Open log folder"},
    "diag.guide": {
        "it": "Ricentra (IMU) e Riallinea camera sono azioni separate. Campi mancanti dal backend mostrano 'sconosciuto'.",
        "en": "Recenter (IMU) and Realign camera are separate actions. Fields missing from the backend show 'unknown'.",
    },
    # -- footer ----------------------------------------------------------------------
    "footer.save": {"it": "Salva preferenze utente", "en": "Save user preferences"},
    # -- azioni / messaggi dinamici ----------------------------------------------------
    "action.busy": {
        "it": "Azione già in corso: attendere il completamento.",
        "en": "Action already running: please wait for completion.",
    },
    "action.busy_short": {
        "it": "Azione già in corso: attendere.",
        "en": "Action already running: please wait.",
    },
    "action.in_progress": {"it": "In corso: {label}…", "en": "In progress: {label}…"},
    "action.worker_closing": {
        "it": "Worker in chiusura: azione rifiutata.",
        "en": "Worker closing: action rejected.",
    },
    "action.prefs_invalid": {
        "it": "Preferenze non valide: {dettagli}",
        "en": "Invalid preferences: {dettagli}",
    },
    "action.optics_current_noop": {
        "it": "Ottica 'current': niente di esplicito da applicare.",
        "en": "Optics 'current': nothing explicit to apply.",
    },
    "action.prefs_saved": {
        "it": "Preferenze salvate in {path}.",
        "en": "Preferences saved to {path}.",
    },
    "action.save_failed": {
        "it": "Salvataggio non riuscito: {errore}",
        "en": "Save failed: {errore}",
    },
    "action.logs_folder": {"it": "Cartella log: {path}", "en": "Log folder: {path}"},
    "action.logs_open_failed": {
        "it": "Apertura log non riuscita: {errore}",
        "en": "Could not open log folder: {errore}",
    },
    "action.event_unhandled": {
        "it": "Evento non gestito: {errore}",
        "en": "Unhandled event: {errore}",
    },
    "action.task_done": {"it": "Completato: {label}.", "en": "Completed: {label}."},
    "action.task_failed": {"it": "Fallito {label}: {errore}", "en": "Failed {label}: {errore}"},
    "action.generic_event": {"it": "Evento {kind}: ricevuto.", "en": "Event {kind}: received."},
    "action.closing": {
        "it": "Chiusura: arresto risorse possedute (la VR esterna resta)…",
        "en": "Closing: stopping owned resources (external VR stays)…",
    },
    "calibration.notice": {
        "it": "Avviso: l'applicazione ferma il tracking e riavvia SteamVR; appoggia il visore e tienilo FERMO fino al termine della calibrazione IMU.",
        "en": "Warning: applying stops tracking and restarts SteamVR; put the headset down and hold it STILL until IMU calibration ends.",
    },
    "calibration.waiting": {
        "it": "Calibrazione guidata in attesa: tieni l'headset FERMO {sec} per la calibrazione.{dettaglio}",
        "en": "Guided calibration waiting: hold the headset STILL {sec} for calibration.{dettaglio}",
    },
    "calibration.preview_waiting": {
        "it": "Attendi calibrazione {sec}: tieni FERMO l'headset",
        "en": "Wait for calibration {sec}: hold the headset STILL",
    },
    "calibration.failed": {
        "it": "Calibrazione FALLITA per variazione giroscopica (log 19:16:32 calibration_failed): fermare la camera NON lo cura fisicamente; healthy=False, riavvio manuale richiesto.",
        "en": "Calibration FAILED due to gyroscope variation (log 19:16:32 calibration_failed): stopping the camera does NOT fix it physically; healthy=False, manual restart required.",
    },
    "calibration.restart_required": {
        "it": "Riavvio richiesto{motivo}: tracking NON riavviato automaticamente; healthy=False.",
        "en": "Restart required{motivo}: tracking NOT restarted automatically; healthy=False.",
    },
    "calibration.started": {
        "it": "Avviato: calibrazione osservata dal backend, nessuna posa valida inventata.",
        "en": "Started: calibration observed by the backend, no valid pose invented.",
    },
    "calibration.not_ready": {
        "it": "Non pronto{motivo}: nessuna posa valida inventata, attendi calibrazione guidata.",
        "en": "Not ready{motivo}: no valid pose invented, wait for guided calibration.",
    },
    # -- label task (mostrate in In corso/Completato; solo display) --------------------
    "task.start_vr": {"it": "Avvio VR", "en": "Start VR"},
    "task.attach": {"it": "Collega tracking", "en": "Attach tracking"},
    "task.stop": {"it": "Stop VR", "en": "Stop VR"},
    "task.cinema": {"it": "Modo cinema", "en": "Cinema mode"},
    "task.recenter": {"it": "Ricentra IMU", "en": "Recenter IMU"},
    "task.realign": {"it": "Riallinea camera", "en": "Realign camera"},
    "task.reset_relative": {"it": "Azzera relativo", "en": "Reset relative"},
    "task.grid": {"it": "Griglia piatta", "en": "Flat grid"},
    "task.apply_driver": {"it": "Applica driver", "en": "Apply driver"},
    "task.apply_optics": {"it": "Applica ottica", "en": "Apply optics"},
    "task.apply_tracking": {"it": "Applica tracking", "en": "Apply tracking"},
}


def t(lang, key: str, **kwargs: str) -> str:
    """Ritorna la stringa per (lang, key) formattata con kwargs.

    Fallback: lingua normalizzata -> italiano -> chiave stessa.
    Errori di formato graziosi: ritorna il template non formattato.
    """
    code = normalize_language(lang)
    entry = STRINGS.get(key)
    if not isinstance(entry, dict):
        return str(key)
    template = entry.get(code, entry.get(DEFAULT, str(key)))
    if not kwargs:
        return str(template)
    try:
        return str(template).format(**kwargs)
    except Exception:
        return str(template)


def unknown(lang) -> str:
    """Valore 'sconosciuto/unknown' per campi backend mancanti."""
    return t(lang, "common.unknown")


MARKERS_UNKNOWN = frozenset({"sconosciuto", "unknown", ""})


def bool_text(value: bool, lang) -> str:
    """Si/no localizzati per booleani di diagnostica (mai inventare dati)."""
    if value is True:
        return t(lang, "common.yes")
    if value is False:
        return t(lang, "common.no")
    return unknown(lang)


# ---------------------------------------------------------------------------
# Formatter tipati per messaggi dinamici (lifecycle esplicito, no concat sparse)
# ---------------------------------------------------------------------------

def fmt_prefs_invalid(lang, dettagli: str) -> str:
    return t(lang, "action.prefs_invalid", dettagli=str(dettagli))


def fmt_in_progress(lang, label: str) -> str:
    return t(lang, "action.in_progress", label=str(label))


def fmt_task_done(lang, label: str) -> str:
    return t(lang, "action.task_done", label=str(label))


def fmt_task_failed(lang, label: str, errore: str) -> str:
    # Reason tecnica backend restata verbatim, solo etichetta tradotta.
    return t(lang, "action.task_failed", label=str(label), errore=str(errore))


def fmt_prefs_saved(lang, path: str) -> str:
    return t(lang, "action.prefs_saved", path=str(path))


def fmt_calibration_waiting(lang, sec: str, dettaglio: str) -> str:
    return t(lang, "calibration.waiting", sec=str(sec), dettaglio=str(dettaglio))


# ---------------------------------------------------------------------------
# Persistenza UI separata (mai dentro settings.py / schema canonico / driver)
# ---------------------------------------------------------------------------

def ui_prefs_path(root) -> Path:
    return Path(root) / UI_PREFS_REL


def load_language(root) -> str:
    """Legge la lingua dal file UI; file assente/corroto/unknown -> 'it'."""
    try:
        path = ui_prefs_path(root)
        if not path.is_file():
            return DEFAULT
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return DEFAULT
        if not isinstance(data, dict):
            return DEFAULT
        return normalize_language(data.get("language"))
    except Exception:
        return DEFAULT


def save_language(root, lang: str) -> str:
    """Salva atomicamente {"language": lang} (tmp + os.replace, stessa root).

    Ritorna la lingua normalizzata salvata. Solleva OSError su errore IO:
    il chiamante mostra il messaggio tradotto (mai crash silenziosi).
    """
    code = normalize_language(lang)
    path = ui_prefs_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"language": code}
    text = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    fd, tmp = tempfile.mkstemp(prefix="psvr-suite-ui-", suffix=".tmp",
                               dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except Exception:
            pass
    return code


def missing_translations() -> dict:
    """Chiavi senza entrambe le lingue o con stringa vuota (per test parity)."""
    mancanti: dict = {}
    for chiave, entry in STRINGS.items():
        problems = []
        for code in SUPPORTED:
            val = entry.get(code) if isinstance(entry, dict) else None
            if not isinstance(val, str) or not val.strip():
                problems.append(code)
        if problems:
            mancanti[chiave] = problems
    return mancanti
