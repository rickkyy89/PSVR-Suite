"""Tendina videocamera 6DoF: registro, settings, tracking, modello UI, i18n.

Niente Tk/display/camera/HW: solo prefs pure, mock worker e sorgenti.
Contratto integrazione: UI (app.CANONICAL_KEYS/AppModel) -> settings
-> tracking.normalize (backend/index per open_camera_source). La camera non
tocca mai steamvr.vrsettings (nessuna chiave driver) ne' la pipeline
camera condivisa (tools/psvr_camera invariata).
"""

import contextlib
import copy
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.psvr_suite import cameras as cameras_module
from tools.psvr_suite import settings as backend_settings
from tools.psvr_suite import tracking as tracking_module
from tools.psvr_suite import i18n as i18n_module
from tools.psvr_suite import app as suite_app
from tools.psvr_suite.app import AppModel, CANONICAL_KEYS


EXPECTED_IDS = ("pseye", "opencv-dshow", "opencv-msmf", "opencv-any", "ps4-stereo")
EXPECTED_BACKENDS = {
    "pseye": ("pseye", 0),
    "opencv-dshow": ("dshow", 0),
    "opencv-msmf": ("msmf", 0),
    "opencv-any": ("any", 0),
    "ps4-stereo": ("ps4-stereo", 0),
}
# Etichette umane della tendina (registro cameras): prefs salvano l'id.
EXPECTED_LABELS = {
    "pseye": "PS3 Eye",
    "opencv-dshow": "OpenCV DirectShow",
    "opencv-msmf": "OpenCV MSMF",
    "opencv-any": "OpenCV (auto)",
    "ps4-stereo": "PS4 Camera stereo",
}


def _wait_for(predicate, timeout=5.0, step=0.02):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


class FakeCap:
    """Cap stile cv2.VideoCapture: solo read/release, nessun controllo PSeye."""

    def __init__(self):
        self.released = False

    def read(self):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        return True, frame

    def release(self):
        self.released = True


class FakePseyeCap(FakeCap):
    """Cap di backend nativo (PS3 Eye o futuro equivalente) con controlli."""

    def __init__(self):
        super().__init__()
        self.controls_calls = []

    def set_manual_controls(self, exposure, gain):
        self.controls_calls.append((exposure, gain))


class FakeImu:
    def at(self, captured_at, wait_s=0.025):
        return {"session_id": "s", "recenter_epoch": 1, "seq": 1,
                "monotonic_s": captured_at - 0.005,
                "valid": True, "connected": True}

    def close(self):
        pass


class FakeDetector:
    def __init__(self, *args, **kwargs):
        self.config = kwargs.get("config")

    def detect(self, frame):
        return []

    def annotate(self, frame, candidates):
        return frame


class FakeTracker:
    def __init__(self, *args, **kwargs):
        self.alignment = np.eye(3)

    def reset(self):
        pass

    def set_imu(self, sample):
        pass

    def search_region(self, width, height, now):
        return None, 1.0

    def update(self, candidates, now):
        return FakePose()


class FakePipeline:
    def __init__(self, *args, **kwargs):
        pass

    def reset(self, reason="manual"):
        pass

    def invalid(self, now):
        pass

    def filter_packet(self, packet, alignment, now, **kwargs):
        return dict(packet) if packet else None

    def diagnostics(self):
        return {}


class FakeSender:
    def __init__(self, *args, **kwargs):
        pass

    def send(self, packet):
        return True

    def invalidate(self):
        return True

    def close(self):
        return True


@contextlib.contextmanager
def patched_camera_worker(caps, **overrides):
    """TrackingWorker senza hardware: cede (worker, opens) nel contesto.

    ``opens`` registra (backend, index) di ogni open_camera_source; ``caps``
    e' la coda di cap restituite nell'ordine (riaperture incluse). Fixtures
    di calibrazione/IMU/detector mockate: nessun device toccato.
    """
    from tools.psvr_suite.tracking import TrackingWorker

    opens = []

    def _open(index, backend=None, width=None, height=None, fps=None,
               dll_path=None, read_timeout_s=0.5,
               max_consecutive_failures=10, **kwargs):
        if opens and not caps[len(opens) - 1].released:
            raise AssertionError("previous source must release before reopening")
        opens.append({"backend": backend, "index": index})
        return caps[len(opens) - 1], None

    intrinsics = Mock()
    intrinsics.image_size = (640, 480)
    intrinsics.camera_matrix = np.eye(3)
    intrinsics.distortion_coeffs = np.zeros(5)
    intrinsics.calibrated = True

    prefs = tracking_module.default_prefs()
    prefs.update(overrides)
    # Generic sources deliberately no longer borrow the PS3 flat calibration.
    # These paths are explicit per-source fixture artifacts; loaders below are
    # mocked and the format matches the synthetic frame, never a real device.
    prefs.setdefault("camera_profiles", {
        sid: {"width": 640, "height": 480, "fps": 75,
              "intrinsicsPath": "fixtures/%s_intrinsics.json" % sid,
              "rigPath": "fixtures/%s_rig.json" % sid,
              "imu_rig": "fixtures/%s_imu_rig.json" % sid}
        for sid in ("opencv-dshow", "opencv-msmf", "opencv-any")})
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "logs").mkdir(parents=True, exist_ok=True)
        with patch("tools.psvr_suite.tracking.load_intrinsics",
                   return_value=intrinsics), \
             patch("tools.psvr_suite.tracking.load_led_rig",
                   return_value=({"labels": ["a"]}, np.zeros((1, 3)))), \
             patch("tools.psvr_suite.tracking.load_rotation_rig",
                   return_value=({"labels": ["a"]}, np.zeros((1, 3)),
                                 np.zeros((1, 3)))), \
             patch("tools.psvr_suite.tracking.load_position_config",
                   return_value={}), \
             patch("tools.psvr_suite.tracking.open_camera_source",
                   side_effect=_open), \
             patch("tools.psvr_suite.tracking.DriverImuSource",
                   return_value=FakeImu()), \
             patch("tools.psvr_suite.tracking.BlueLedDetector",
                   FakeDetector), \
             patch("tools.psvr_suite.tracking.ImuLedTracker",
                   FakeTracker), \
             patch("tools.psvr_suite.tracking.PositionPipeline",
                   FakePipeline), \
             patch("tools.psvr_suite.tracking.DriverPositionSender",
                   FakeSender), \
             patch("tools.psvr_suite.tracking.build_driver_position_packet",
                   return_value={"session_id": "s", "recenter_epoch": 1,
                                 "valid": False, "state": "lost",
                                 "position_m": None, "seq": 0}), \
             patch("tools.psvr_suite.tracking.estimate_pose_covariance",
                   return_value=(None, {"reliable": False})):
            worker = TrackingWorker(root, lambda e: None)
            worker.start(prefs)
            try:
                yield worker, opens
            finally:
                if not worker.stop(timeout=5.0):
                    raise AssertionError("mock camera worker did not stop")
                if not all(cap.released for cap in caps[:len(opens)]):
                    raise AssertionError("owned fixture source was not released")


class RegistryTests(unittest.TestCase):
    def test_ids_stabili_solo_implementazioni_disponibili(self):
        self.assertEqual(cameras_module.camera_source_ids(), EXPECTED_IDS)
        self.assertEqual(cameras_module.DEFAULT_CAMERA_SOURCE, "pseye")

    def test_resolve_backend_index_reali(self):
        for sid, (backend, index) in EXPECTED_BACKENDS.items():
            with self.subTest(source=sid):
                resolved = cameras_module.resolve_camera_source(sid)
                self.assertEqual(resolved, {"backend": backend, "index": index})
                entry = cameras_module.get_camera_source(sid)
                self.assertEqual(entry["id"], sid)
                self.assertTrue(cameras_module.is_valid_camera_source(sid))

    def test_ogni_backend_registrato_esiste_davvero(self):
        # Niente opzioni fittizie: pseye via dispatch camera_source, gli altri
        # via alias OpenCV noti a camera_backend.resolve_backend.
        from tools.psvr_camera import camera_backend
        from tools.psvr_camera.camera_source import is_pseye_backend
        for sid in cameras_module.camera_source_ids():
            with self.subTest(source=sid):
                entry = cameras_module.get_camera_source(sid)
                backend = entry["backend"]
                if is_pseye_backend(backend):
                    continue
                if backend == "ps4-stereo":
                    from tools.psvr_camera.ps4_suite_source import Ps4SuiteSource
                    self.assertTrue(callable(Ps4SuiteSource.open))
                    continue
                self.assertIn(str(backend).lower(), camera_backend._BACKEND_ALIASES)

    def test_sconosciuti_rifiutati_mai_fallback_silenzioso(self):
        for bad in ("bogus", "", "  ", "PSEYE", "pseye ", None, 0, ["pseye"]):
            with self.subTest(value=bad):
                self.assertFalse(cameras_module.is_valid_camera_source(bad))
                with self.assertRaises(ValueError):
                    cameras_module.get_camera_source(bad)
                with self.assertRaises(ValueError):
                    cameras_module.resolve_camera_source(bad)

    def test_find_roundtrip_ed_equivalenze_any(self):
        for sid, (backend, index) in EXPECTED_BACKENDS.items():
            with self.subTest(source=sid):
                self.assertEqual(
                    cameras_module.find_camera_source(backend, index), sid)
        self.assertEqual(cameras_module.find_camera_source(None, 0), "opencv-any")
        self.assertEqual(cameras_module.find_camera_source("auto", 0), "opencv-any")
        self.assertEqual(cameras_module.find_camera_source("DSHOW", 0), "opencv-dshow")
        self.assertIsNone(cameras_module.find_camera_source("bogus", 0))
        self.assertIsNone(cameras_module.find_camera_source("pseye", 3))
        self.assertIsNone(cameras_module.find_camera_source("pseye", "x"))

    def test_etichette_umane_per_la_tendina(self):
        labels = []
        for sid, label in EXPECTED_LABELS.items():
            with self.subTest(source=sid):
                entry = cameras_module.get_camera_source(sid)
                self.assertEqual(entry["label"], label)
                # Etichetta umana, non l'id grezzo; reverse-map stabile.
                self.assertNotEqual(entry["label"], sid)
                self.assertEqual(
                    cameras_module.find_camera_source_by_label(label), sid)
                labels.append(label)
        self.assertEqual(len(set(labels)), len(labels))
        # L'id grezzo non e' un'etichetta (la UI prova prima l'id, poi questa).
        self.assertIsNone(cameras_module.find_camera_source_by_label("pseye"))
        self.assertIsNone(cameras_module.find_camera_source_by_label("Bogus"))
        self.assertIsNone(cameras_module.find_camera_source_by_label(None))

    def test_estensione_futura_senza_opzioni_fittizie(self):
        sid = cameras_module.register_camera_source(
            {"id": "future-test-cam", "backend": "dshow", "index": 1,
             "kind": "opencv", "requires": "fixture"})
        try:
            self.assertIn("future-test-cam", cameras_module.camera_source_ids())
            self.assertEqual(
                cameras_module.resolve_camera_source("future-test-cam"),
                {"backend": "dshow", "index": 1})
            # Senza etichetta: default = id (tendina comunque popolata).
            self.assertEqual(
                cameras_module.get_camera_source("future-test-cam")["label"],
                "future-test-cam")
            self.assertEqual(
                cameras_module.find_camera_source_by_label("future-test-cam"),
                "future-test-cam")
            labeled = cameras_module.register_camera_source(
                {"id": "future-test-cam-2", "backend": "msmf", "index": 1,
                 "kind": "opencv", "label": "Future Test Cam Due"})
            try:
                self.assertEqual(
                    cameras_module.find_camera_source_by_label(
                        "Future Test Cam Due"), labeled)
                with self.assertRaises(ValueError):
                    # Etichetta duplicata: scelta ambigua nella tendina.
                    cameras_module.register_camera_source(
                        {"id": "future-test-cam-3", "backend": "msmf",
                         "index": 1, "label": "Future Test Cam Due"})
            finally:
                self.assertTrue(
                    cameras_module.unregister_camera_source(labeled))
            with self.assertRaises(ValueError):
                cameras_module.register_camera_source(
                    {"id": "future-test-cam", "backend": "msmf", "index": 0})
            for bad in ({"id": "", "backend": "dshow", "index": 0},
                        {"id": "x", "backend": "", "index": 0},
                        {"id": "y", "backend": "dshow", "index": -1},
                        {"id": "z", "backend": "dshow"},
                        {"id": "w", "backend": "dshow", "index": 0,
                         "label": ""}):
                with self.assertRaises(ValueError):
                    cameras_module.register_camera_source(bad)
        finally:
            self.assertTrue(cameras_module.unregister_camera_source("future-test-cam"))
        self.assertNotIn("future-test-cam", cameras_module.camera_source_ids())

    def test_modulo_puro_senza_tk_hw(self):
        src = (ROOT / "tools" / "psvr_suite" / "cameras.py").read_text(
            encoding="utf-8")
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                lowered = stripped.lower()
                for banned in ("tkinter", "cv2", "numpy", "ctypes",
                               "psvr_camera", "psvr_suite"):
                    self.assertNotIn(banned, lowered)


class SettingsCameraTests(unittest.TestCase):
    def test_default_pseye_e_schema_29_chiavi(self):
        prefs = backend_settings.default_preferences()
        self.assertEqual(prefs["camera_source"], "pseye")
        self.assertIn("camera_source", list(CANONICAL_KEYS))
        self.assertEqual(len(list(CANONICAL_KEYS)), 29)
        self.assertEqual(sorted(prefs.keys()), sorted(CANONICAL_KEYS))

    def test_validate_accetta_ogni_sorgente_registrata(self):
        for sid in cameras_module.camera_source_ids():
            with self.subTest(source=sid):
                prefs = backend_settings.default_preferences()
                prefs["camera_source"] = sid
                cleaned = backend_settings.validate_preferences(prefs)
                self.assertEqual(cleaned["camera_source"], sid)

    def test_validate_rifiuta_senza_autocorrezioni(self):
        for bad in ("bogus", "", "  ", None, 0, "PSEYE"):
            with self.subTest(value=bad):
                prefs = backend_settings.default_preferences()
                prefs["camera_source"] = bad
                with self.assertRaises(ValueError):
                    backend_settings.validate_preferences(prefs)
                self.assertEqual(prefs["camera_source"], bad)

    def test_load_preserva_sorgente_e_missing_da_default(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prefs.json"
            path.write_text(json.dumps({"camera_source": "opencv-msmf"}),
                            encoding="utf-8")
            prefs = backend_settings.load_preferences(path)
            self.assertEqual(prefs["camera_source"], "opencv-msmf")
            fresh = backend_settings.load_preferences(Path(tmp) / "absent.json")
            self.assertEqual(fresh["camera_source"], "pseye")

    def test_camera_mai_scritta_nel_driver(self):
        # Contratto: scelta host-side, nessuna chiave driver_psvrpc la descrive.
        for sid in cameras_module.camera_source_ids():
            with self.subTest(source=sid):
                prefs = backend_settings.default_preferences()
                prefs["camera_source"] = sid
                mapped = backend_settings.driver_keys_for(prefs)
                for key, value in mapped.items():
                    self.assertNotIn("camera", key.lower())
                    self.assertNotIn(sid, str(value))
                self.assertNotIn("camera_source", mapped)

    def test_preferences_from_driver_resta_default_host_side(self):
        prefs = backend_settings.preferences_from_driver(
            source={"positionEnabled": True})
        self.assertEqual(prefs["camera_source"], "pseye")


class TrackingCameraTests(unittest.TestCase):
    def test_default_pseye_backend_derivato(self):
        prefs = tracking_module.default_prefs()
        self.assertEqual(prefs["camera_source"], "pseye")
        cleaned = tracking_module.normalize_prefs({"log": None})
        self.assertEqual(cleaned["camera_source"], "pseye")
        self.assertEqual(cleaned["backend"], "pseye")
        self.assertEqual(cleaned["index"], 0)

    def test_ogni_sorgente_mappa_backend_index(self):
        for sid, (backend, index) in EXPECTED_BACKENDS.items():
            with self.subTest(source=sid):
                cleaned = tracking_module.normalize_prefs(
                    {"camera_source": sid, "log": None})
                self.assertEqual(cleaned["camera_source"], sid)
                self.assertEqual(cleaned["backend"], backend)
                self.assertEqual(cleaned["index"], index)

    def test_legacy_coerente_accettato_conflitto_rifiutato(self):
        ok = tracking_module.normalize_prefs(
            {"backend": "dshow", "index": 0, "log": None})
        self.assertEqual(ok["camera_source"], "opencv-dshow")
        ok2 = tracking_module.normalize_prefs(
            {"camera_source": "opencv-msmf", "backend": "msmf",
             "index": 0, "log": None})
        self.assertEqual(ok2["backend"], "msmf")
        with self.assertRaises(ValueError):
            tracking_module.normalize_prefs(
                {"camera_source": "pseye", "backend": "dshow",
                 "index": 0, "log": None})
        with self.assertRaises(ValueError):
            tracking_module.normalize_prefs(
                {"camera_source": "bogus", "log": None})
        with self.assertRaises(ValueError):
            tracking_module.normalize_prefs(
                {"backend": "bogus", "index": 0, "log": None})

    def test_immutabile_richiede_reopen(self):
        self.assertIn("camera_source", tracking_module.IMMUTABLE_CAM_KEYS)
        self.assertIn("backend", tracking_module.IMMUTABLE_CAM_KEYS)
        self.assertIn("index", tracking_module.IMMUTABLE_CAM_KEYS)

    def test_worker_apre_backend_scelto(self):
        cap = FakeCap()
        with patched_camera_worker([cap], camera_source="opencv-msmf",
                                   log="logs/cam_src.jsonl") as (worker, opens):
            ok = _wait_for(lambda: worker.status()["captured_frames"] >= 1)
            self.assertTrue(ok, "worker non arrivato al primo frame")
            self.assertEqual(opens[0], {"backend": "msmf", "index": 0})

    def test_worker_reapre_al_cambio_sorgente(self):
        # camera_source e' immutabile (IMMUTABLE_CAM_KEYS): l'update live
        # riapre la camera col nuovo backend/index del registro, stesso log,
        # senza restart del worker.
        caps = [FakeCap(), FakeCap()]
        with patched_camera_worker(caps, camera_source="opencv-msmf",
                                   log="logs/reopen.jsonl") as (worker, opens):
            ok = _wait_for(lambda: len(opens) >= 1
                           and worker.status()["captured_frames"] >= 1)
            self.assertTrue(ok, "primo open/captures non raggiunto")
            first_log = worker.status()["log"]
            updated = dict(worker.status()["prefs"],
                           camera_source="opencv-dshow", backend="dshow",
                           index=0)
            worker.update_settings(updated)
            ok = _wait_for(lambda: len(opens) >= 2)
            self.assertTrue(ok, "worker non ha riaperto la camera")
            self.assertEqual(opens[0], {"backend": "msmf", "index": 0})
            self.assertEqual(opens[1], {"backend": "dshow", "index": 0})
            self.assertTrue(caps[0].released)
            self.assertEqual(worker.status()["log"], first_log)
            ok = _wait_for(
                lambda: worker.status()["prefs"]["camera_source"] == "opencv-dshow")
            self.assertTrue(ok, "prefs live non aggiornate alla sorgente")

    def test_controlli_proprietari_solo_al_backend_nativo(self):
        # Nativo (PS3 Eye o futuro con set_manual_controls): il worker chiama
        # i controlli manuali su cambio exposure/gain. OpenCV (cap nudo, come
        # cv2.VideoCapture): nessun controllo proprietario PSeye chiamato,
        # worker sano e continua a catturare.
        native_cap = FakePseyeCap()
        with patched_camera_worker([native_cap], camera_source="pseye",
                                   log="logs/controls_native.jsonl") as (worker, opens):
            ok = _wait_for(lambda: worker.status()["captured_frames"] >= 1)
            self.assertTrue(ok)
            self.assertEqual(opens[0], {"backend": "pseye", "index": 0})
            self.assertEqual(native_cap.controls_calls[0], (13, 0))
            updated = dict(worker.status()["prefs"], exposure=20, gain=1)
            worker.update_settings(updated)
            ok = _wait_for(lambda: (20, 1) in native_cap.controls_calls)
            self.assertTrue(ok, "backend nativo senza controlli manuali applicati")
        plain_cap = FakeCap()
        self.assertFalse(hasattr(plain_cap, "set_manual_controls"))
        with patched_camera_worker([plain_cap], camera_source="opencv-dshow",
                                   log="logs/controls_opencv.jsonl") as (worker, opens):
            ok = _wait_for(lambda: worker.status()["captured_frames"] >= 1)
            self.assertTrue(ok)
            self.assertEqual(opens[0], {"backend": "dshow", "index": 0})
            self.assertIsNone(worker.status()["effective_camera"]["exposure"])
            self.assertIsNone(worker.status()["effective_camera"]["gain"])
            before = worker.status()["captured_frames"]
            updated = dict(worker.status()["prefs"], exposure=20, gain=1)
            worker.update_settings(updated)
            ok = _wait_for(lambda: worker.status()["prefs"]["exposure"] == 20)
            self.assertTrue(ok, "prefs live non aggiornate")
            ok = _wait_for(lambda: worker.status()["captured_frames"] > before + 2)
            self.assertTrue(ok, "cattura OpenCV interrotta")
            self.assertIsNone(worker.status()["error"])


class FakeVar:
    def __init__(self, value=""):
        self._v = value

    def get(self):
        return self._v

    def set(self, value):
        self._v = value


class FakePose:
    valid = False
    reason = "lost: test"
    rms_px = 0.5
    rms_limit_px = 2.5
    image_span_px = 0.0
    led_labels = []
    predicted_labels = []
    imu_age_s = 0.005

    def to_dict(self):
        return {"valid": False, "reason": self.reason}


def make_camera_shell():
    app = suite_app.SuiteApp.__new__(suite_app.SuiteApp)
    app._settings = backend_settings
    app.model = AppModel(backend_settings.default_preferences())
    app.closing = False
    app._pending = 0
    app._pending_lock = threading.Lock()
    prefs = backend_settings.default_preferences()
    for key, var in (
            ("tracking_mode", "var_tracking_mode"),
            ("tracking_config_path", "var_tracking_path"),
            ("intrinsicsPath", "var_intrinsics"),
            ("rigPath", "var_rig"),
            ("imu_rig", "var_imu_rig"),
            ("position_filter", "var_filter"),
            ("optics_profile", "var_optics"),
            ("camera_source", "var_camera_source")):
        setattr(app, var, FakeVar(prefs[key]))
    app.var_fallback = FakeVar(prefs["fallback_3dof"])
    app.var_two_led = FakeVar(prefs["two_led_experimental"])
    app.var_blend = FakeVar(str(prefs["reacquire_blend_ms"]))
    app.var_speed = FakeVar(str(prefs["reacquire_speed_m_s"]))
    app.var_cal = FakeVar(str(prefs["imu_calibration_ms"]))
    app.var_stale = FakeVar(str(prefs["imu_stale_ms"]))
    app.var_gap = FakeVar(str(prefs["imu_max_gap_us"]))
    app.var_value_min = FakeVar(prefs["value_min"])
    app.var_min_area = FakeVar(prefs["min_area"])
    app.var_exposure = FakeVar(prefs["exposure"])
    app.var_gain = FakeVar(prefs["gain"])
    app.var_saturation = FakeVar(str(prefs["saturation_min"]))
    app.var_opening = FakeVar(str(prefs["opening_size"]))
    app.var_fps = FakeVar(str(prefs["fps"]))
    app.var_width = FakeVar(str(prefs["width"]))
    app.var_height = FakeVar(str(prefs["height"]))
    app.var_cutoff = FakeVar(str(prefs["min_cutoff_hz"]))
    app.var_beta = FakeVar(str(prefs["beta"]))
    app.var_scale = FakeVar(str(prefs["position_scale"]))
    app.var_head_offset = FakeVar("0.000, 0.000, 0.000")
    app.var_initial_offset = FakeVar("0.000, 0.000, 0.000")
    return app


class AppModelCameraTests(unittest.TestCase):
    def test_modello_allineato_al_registro(self):
        # Nessuna lista frozen in AppModel: la validazione segue il registro
        # (test_validate_segue_il_registro_live copre le nuove registrazioni).
        prefs = AppModel.default_prefs()
        self.assertEqual(prefs["camera_source"], "pseye")
        cleaned = AppModel.validate(copy.deepcopy(prefs))
        backend_settings.validate_preferences(cleaned)

    def test_validate_segue_il_registro_live(self):
        # Regressione: la vecchia CAMERA_SOURCES congelata all'import
        # rifiutava una sorgente registrata dopo l'import, a differenza di
        # settings/tracking che seguono il registro live.
        base = AppModel.default_prefs()
        sid = cameras_module.register_camera_source(
            {"id": "future-live-cam", "backend": "msmf", "index": 2,
             "kind": "opencv", "requires": "fixture"})
        try:
            ok = AppModel.validate(dict(base, camera_source=sid))
            self.assertEqual(ok["camera_source"], sid)
            # Catena UI completa: etichetta tendina -> collect_prefs -> strict.
            app = make_camera_shell()
            app.var_msg = FakeVar("")
            app.var_camera_source.set(
                cameras_module.get_camera_source(sid)["label"])
            prefs, errori = app.collect_prefs()
            self.assertEqual(errori, [])
            self.assertEqual(prefs["camera_source"], sid)
        finally:
            self.assertTrue(cameras_module.unregister_camera_source(sid))
        # Rimossa dal registro: rifiutata di nuovo, mai accept-then-forget.
        with self.assertRaises(ValueError):
            AppModel.validate(dict(base, camera_source=sid))

    def test_validate_tutte_le_sorgenti_e_rifiuti(self):
        base = AppModel.default_prefs()
        for sid in cameras_module.camera_source_ids():
            with self.subTest(source=sid):
                ok = AppModel.validate(dict(base, camera_source=sid))
                self.assertEqual(ok["camera_source"], sid)
        for bad in ("bogus", "", "  "):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    AppModel.validate(dict(base, camera_source=bad))

    def test_collect_prefs_roundtrip_tendina(self):
        app = make_camera_shell()
        app.var_msg = FakeVar("")
        for sid in cameras_module.camera_source_ids():
            with self.subTest(source=sid):
                # La tendina reale scrive l'etichetta umana nel var.
                app.var_camera_source.set(EXPECTED_LABELS[sid])
                prefs, errori = app.collect_prefs()
                self.assertEqual(errori, [])
                self.assertEqual(prefs["camera_source"], sid)
                backend_settings.validate_preferences(prefs)
                # Reopen: dai prefs salvati (id) il widget torna all'etichetta.
                app._apply_prefs_to_widgets(prefs)
                self.assertEqual(app.var_camera_source.get(),
                                 EXPECTED_LABELS[sid])
        # Tolleranza shell legacy: l'id grezzo nel var resta una scelta valida.
        app.var_camera_source.set("opencv-dshow")
        prefs, errori = app.collect_prefs()
        self.assertEqual(errori, [])
        self.assertEqual(prefs["camera_source"], "opencv-dshow")
        app.var_camera_source.set("bogus")
        prefs, errori = app.collect_prefs()
        self.assertIsNone(prefs)
        self.assertTrue(errori)

    def test_apply_widgets_ripristina_sorgente(self):
        app = make_camera_shell()
        app.var_msg = FakeVar("")
        app._apply_prefs_to_widgets(
            dict(backend_settings.default_preferences(),
                 camera_source="opencv-dshow"))
        # Il var mostra l'etichetta umana, non l'id grezzo.
        self.assertEqual(app.var_camera_source.get(),
                         EXPECTED_LABELS["opencv-dshow"])

    def test_tab_camera_contiene_tendina_registro(self):
        import inspect as _inspect
        src = _inspect.getsource(suite_app.SuiteApp._build_camera_tab)
        self.assertIn("camera.source", src)
        self.assertIn("camera.source_note", src)
        self.assertIn("Combobox", src)
        self.assertIn("<<ComboboxSelected>>", src)
        self.assertIn("camera_source_ids", src)
        # Etichette umane dal registro (non id grezzi) nella tendina.
        self.assertIn("camera_source_label", src)

    def test_headless_check_schema_29(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(suite_app.headless_check(Path(d), None), 0)


class I18nCameraTests(unittest.TestCase):
    def test_chiavi_tendina_bilingui(self):
        self.assertEqual(i18n_module.missing_translations(), {})
        for key in ("camera.source", "camera.source_note"):
            it = i18n_module.t("it", key)
            en = i18n_module.t("en", key)
            self.assertTrue(it.strip() and en.strip())
        self.assertEqual(i18n_module.t("it", "camera.source"), "Videocamera 6DoF:")
        self.assertEqual(i18n_module.t("en", "camera.source"), "6DoF camera:")


if __name__ == "__main__":
    unittest.main()
