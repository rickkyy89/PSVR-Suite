"""Registro sorgenti videocamera 6DoF per la suite (puro, niente Tk/hardware).

Ogni voce descrive un'implementazione di acquisizione realmente disponibile
oggi nel repo, mai opzioni fittizie:

- ``pseye``: backend nativo PS3 Eye (``tools.psvr_camera.pseye_backend`` via
  ``camera_source.open_camera_source(..., backend="pseye")``). Richiede
  ``psvr_pseye.dll`` e binding libusb-win32 esistente. E' il default.
- ``opencv-dshow`` / ``opencv-msmf`` / ``opencv-any``: stack OpenCV generico
  (``tools.psvr_camera.camera_backend.open_camera``) con i backend Windows
  ``dshow`` / ``msmf`` / ``any`` (auto). Richiede ``cv2`` nell'ambiente.
- ``ps4-stereo``: adapter OV580 UVC con profilo stereo Suite dedicato;
  usa DirectShow internamente, senza aliasare ``opencv-dshow``.

Altri alias OpenCV (v4l2/gstreamer/ffmpeg/vfw/mjpg) esistono nel codice ma non
sono percorsi 6DoF Windows: non vengono elencati. Ogni voce ha anche una
``label`` umana per la tendina ("PS3 Eye", "OpenCV DirectShow"): le prefs
memorizzano l'``id`` tecnico stabile, la UI mostra l'etichetta. Estensione
futura: una sola chiamata a :func:`register_camera_source` con
``(id, backend, index)`` reale (etichetta opzionale); la tendina UI e la
validazione prefs seguono il registro live senza modifiche.

Regole:
- Solo stdlib, nessun import Tk/cv2/numpy/hardware (test headless sicuri).
- ``id`` stabile per le prefs canoniche (``settings.camera_source``);
  ``backend``/``index`` sono i parametri di ``open_camera_source``.
- Nessuna enumerazione hardware implicita: l'indice e' esplicito (0 oggi).
- B12-09: il confronto degli ``id`` e delle ``label`` e' case-sensitive
  per scelta (voluto, nessun aliasing/normalizzazione): gli id sono chiavi
  tecniche stabili salvate nelle prefs, le label sono testi UI esatti della
  tendina. Solo ``find_camera_source`` normalizza il backend
  (case-insensitive, come ``camera_backend.resolve_backend``).
"""

from __future__ import annotations


DEFAULT_CAMERA_SOURCE = "pseye"

# Voci disponibili oggi: (id, backend per open_camera_source, index, kind,
# label umana per la tendina). ``kind`` e' solo documentazione;
# il worker usa backend/index. Nessuna voce fittizia: ogni backend
# e' risolto per nome da camera_backend.resolve_backend o dal dispatch pseye
# di camera_source.
_SOURCES: list = [
    {"id": "pseye", "backend": "pseye", "index": 0, "kind": "native",
     "label": "PS3 Eye",
     "requires": "psvr_pseye.dll + binding libusb-win32 esistente"},
    {"id": "opencv-dshow", "backend": "dshow", "index": 0, "kind": "opencv",
     "label": "OpenCV DirectShow",
     "requires": "cv2 con backend DSHOW"},
    {"id": "opencv-msmf", "backend": "msmf", "index": 0, "kind": "opencv",
     "label": "OpenCV MSMF",
     "requires": "cv2 con backend MSMF"},
    {"id": "opencv-any", "backend": "any", "index": 0, "kind": "opencv",
      "label": "OpenCV (auto)",
      "requires": "cv2 (selezione automatica)"},
    {"id": "ps4-stereo", "backend": "ps4-stereo", "index": 0, "kind": "stereo",
     "label": "PS4 Camera stereo",
     "requires": "OV580 UVC + cv2 DirectShow + source-owned stereo profile"},
]


def _check_entry(entry):
    if not isinstance(entry, dict):
        raise ValueError("camera source must be an object")
    for key in ("id", "backend", "index"):
        if key not in entry:
            raise ValueError("camera source missing %r" % key)
    sid = entry["id"]
    backend = entry["backend"]
    index = entry["index"]
    if not isinstance(sid, str) or not sid.strip():
        raise ValueError("camera source id must be a non-empty string")
    if not isinstance(backend, str) or not backend.strip():
        raise ValueError("camera source backend must be a non-empty string")
    if type(index) is not int or index < 0:
        raise ValueError("camera source index must be a non-negative integer")
    label = entry.get("label", sid)
    if not isinstance(label, str) or not label.strip():
        raise ValueError("camera source label must be a non-empty string")
    return {"id": sid, "backend": backend, "index": index,
            "kind": entry.get("kind", "custom"),
            "label": label,
            "requires": entry.get("requires", "")}


def camera_source_ids():
    """Id stabili nell'ordine di presentazione UI (tupla nuova)."""
    return tuple(entry["id"] for entry in _SOURCES)


def is_valid_camera_source(value):
    """True se ``value`` e' un id registrato (stringa esatta).

    B12-09: confronto case-sensitive voluto (nessun aliasing).
    """
    return isinstance(value, str) and any(
        entry["id"] == value for entry in _SOURCES)


def get_camera_source(value):
    """Voce registrata per ``value`` (dict nuovo) o ValueError."""
    for entry in _SOURCES:
        if entry["id"] == value:
            return dict(entry)
    raise ValueError(
        "invalid camera_source %r; known: %s" % (value, ", ".join(camera_source_ids())))


def resolve_camera_source(value):
    """Parametri ``open_camera_source`` per ``value``: (backend, index).

    Ritorna ``{"backend": str, "index": int}`` nuovo. Solleva ValueError per
    id sconosciuti (mai fallback silenziosi verso un'altra camera).
    """
    entry = get_camera_source(value)
    return {"backend": entry["backend"], "index": entry["index"]}


def find_camera_source(backend, index):
    """Id registrato per ``(backend, index)`` o None (mai inventato).

    ``None``/``"auto"`` equivalgono a ``"any"`` (stessa regola di
    ``camera_backend.resolve_backend``); il confronto backend e'
    case-insensitive.
    """
    try:
        idx = int(index)
    except (TypeError, ValueError):
        return None
    if isinstance(backend, str):
        norm = backend.strip().lower()
    elif backend is None:
        norm = "any"
    else:
        return None
    if norm in ("auto",):
        norm = "any"
    for entry in _SOURCES:
        if entry["backend"].lower() == norm and entry["index"] == idx:
            return entry["id"]
    return None


def find_camera_source_by_label(label):
    """Id registrato per l'etichetta umana della tendina, o None.

    Confronto esatto (case-sensitive). Le etichette delle voci built-in sono
    distinte dagli id; una voce custom senza etichetta usa l'id stesso.
    B12-09: case-sensitive voluto (nessun aliasing).
    """
    if not isinstance(label, str):
        return None
    for entry in _SOURCES:
        if entry["label"] == label:
            return entry["id"]
    return None


def register_camera_source(entry):
    """Registra una futura implementazione reale (estensione senza UI nuova).

    Valida forma, rifiuta id duplicati ed etichette già in uso (la tendina
    le userebbe come scelta ambigua); ritorna l'id registrato.
    """
    cleaned = _check_entry(entry)
    if any(item["id"] == cleaned["id"] for item in _SOURCES):
        raise ValueError("camera source already registered: %r" % cleaned["id"])
    if any(item["label"] == cleaned["label"] for item in _SOURCES):
        raise ValueError("camera source label already in use: %r"
                         % cleaned["label"])
    _SOURCES.append(cleaned)
    return cleaned["id"]


def unregister_camera_source(source_id):
    """Rimuove una voce registrata via :func:`register_camera_source`.

    Serve solo ai test (le voci built-in non si rimuovono mai in produzione).
    """
    for pos, entry in enumerate(_SOURCES):
        if entry["id"] == source_id:
            del _SOURCES[pos]
            return True
    return False


__all__ = [
    "DEFAULT_CAMERA_SOURCE",
    "camera_source_ids",
    "is_valid_camera_source",
    "get_camera_source",
    "resolve_camera_source",
    "find_camera_source",
    "find_camera_source_by_label",
    "register_camera_source",
    "unregister_camera_source",
]
