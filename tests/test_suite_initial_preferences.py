"""Preferenze iniziali headless senza Tk/hardware/processi.

Copre _load_initial_prefs con SuiteApp.__new__ + attributi reali:
_settings reale, root_path tmp, user_path tmp, preferences_arg,
lang/display queue per le note. Usa JSON driver vero in TEMP con
mock.patch.object(settings, 'DEFAULT_VRSETTINGS', tempPath) e funzioni
settings reali. Nessuno SteamVR reale, nessun costruttore Tk.
"""

import json
import queue
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.psvr_suite import settings as settings_module
from tools.psvr_suite import app as suite_app


def make_app(tmpdir, user_name="psvr_suite_user.json", preferences_arg=None):
    """SuiteApp headless: nessun Tk, nessun hardware, nessun processo."""
    app = suite_app.SuiteApp.__new__(suite_app.SuiteApp)
    app._settings = settings_module
    app.root_path = Path(tmpdir)
    app.user_path = Path(tmpdir) / user_name
    app.preferences_arg = preferences_arg
    app.lang = "it"
    app.display = queue.Queue(maxsize=512)
    return app


def drain_notes(app):
    """Testi delle note accodate via _note (coda display)."""
    testi = []
    while not app.display.empty():
        try:
            kind, payload = app.display.get_nowait()
        except queue.Empty:
            break
        if kind == "nota":
            testi.append(str(payload.get("testo", "")))
        else:
            testi.append(str(payload))
    return testi


def write_json(path, obj, raw_bytes=None):
    path = Path(path)
    if raw_bytes is not None:
        path.write_bytes(raw_bytes)
    else:
        path.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    return path


def write_driver(path, section, other_apps=None):
    payload = {"driver_psvrpc": dict(section)}
    if other_apps:
        payload.update(other_apps)
    return write_json(path, payload)


def write_prefs_file(path, blend=None, imu_cal=None, extra=None):
    prefs = settings_module.default_preferences()
    if blend is not None:
        prefs["reacquire_blend_ms"] = blend
    if imu_cal is not None:
        prefs["imu_calibration_ms"] = imu_cal
    if extra:
        prefs.update(extra)
    # Valida con funzione reale prima di scrivere.
    settings_module.validate_preferences(prefs)
    return write_json(path, prefs)


NONDEFAULT_SECTION = {
    "positionEnabled": False,
    "positionReacquireBlendMs": 300,
    "positionReacquireMaxSpeedMps": 2.0,
    "positionInitialOffsetX": 0.5,
    "positionInitialOffsetY": -0.25,
    "positionInitialOffsetZ": 0.75,
    "imuCalibrationMs": 4000,
}


class InitialPreferencesTests(unittest.TestCase):
    def test_importa_driver_quando_manca_tutto(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = Path(tmp) / "steamvr.vrsettings"
            write_driver(driver, NONDEFAULT_SECTION)
            driver_bytes_before = driver.read_bytes()
            prima = sorted(p.name for p in Path(tmp).iterdir())
            app = make_app(tmp, user_name="assente.json", preferences_arg=None)
            self.assertFalse(app.user_path.is_file())
            with patch.object(settings_module, "DEFAULT_VRSETTINGS", driver):
                prefs = app._load_initial_prefs()
            self.assertEqual(prefs["tracking_mode"], "3dof")
            self.assertEqual(prefs["reacquire_blend_ms"], 300)
            self.assertEqual(prefs["reacquire_speed_m_s"], 2.0)
            self.assertEqual(prefs["initial_offset_m"], [0.5, -0.25, 0.75])
            self.assertEqual(prefs["imu_calibration_ms"], 4000)
            self.assertEqual(prefs["optics_profile"], "current")
            self.assertIs(prefs["fallback_3dof"], False)
            # Mai scrivere: bytes driver intatti e nessun nuovo file in tmp.
            self.assertEqual(driver.read_bytes(), driver_bytes_before)
            dopo = sorted(p.name for p in Path(tmp).iterdir())
            self.assertEqual(prima, dopo)
            self.assertFalse(app.user_path.exists())
            self.assertEqual(drain_notes(app), [])

    def test_explicit_batte_user_e_driver(self):
        with tempfile.TemporaryDirectory() as tmp:
            explicit = Path(tmp) / "explicit.json"
            user = Path(tmp) / "user.json"
            driver = Path(tmp) / "steamvr.vrsettings"
            write_prefs_file(explicit, blend=111)
            write_prefs_file(user, blend=222)
            write_driver(driver, NONDEFAULT_SECTION)
            app = make_app(tmp, user_name="user.json",
                           preferences_arg=str(explicit))
            with patch.object(settings_module, "DEFAULT_VRSETTINGS", driver):
                prefs = app._load_initial_prefs()
            self.assertEqual(prefs["reacquire_blend_ms"], 111)
            self.assertEqual(drain_notes(app), [])

    def test_user_batte_driver(self):
        with tempfile.TemporaryDirectory() as tmp:
            user = Path(tmp) / "user.json"
            driver = Path(tmp) / "steamvr.vrsettings"
            write_prefs_file(user, blend=222)
            write_driver(driver, NONDEFAULT_SECTION)
            app = make_app(tmp, user_name="user.json", preferences_arg=None)
            with patch.object(settings_module, "DEFAULT_VRSETTINGS", driver):
                prefs = app._load_initial_prefs()
            self.assertEqual(prefs["reacquire_blend_ms"], 222)
            self.assertEqual(prefs["optics_profile"], "identity")
            self.assertEqual(drain_notes(app), [])

    def test_explicit_mancante_usa_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            user = Path(tmp) / "user.json"
            driver = Path(tmp) / "steamvr.vrsettings"
            write_prefs_file(user, blend=222)
            write_driver(driver, NONDEFAULT_SECTION)
            mancante = str(Path(tmp) / "non_esiste.json")
            app = make_app(tmp, user_name="user.json",
                           preferences_arg=mancante)
            with patch.object(settings_module, "DEFAULT_VRSETTINGS", driver):
                prefs = app._load_initial_prefs()
            self.assertEqual(prefs["reacquire_blend_ms"], 222)
            self.assertEqual(drain_notes(app), [])

    def test_driver_assente_default_con_nota_senza_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            assente = Path(tmp) / "assente.vrsettings"
            self.assertFalse(assente.exists())
            app = make_app(tmp, user_name="assente.json", preferences_arg=None)
            prima = sorted(p.name for p in Path(tmp).iterdir())
            with patch.object(settings_module, "DEFAULT_VRSETTINGS", assente):
                prefs = app._load_initial_prefs()
            self.assertEqual(prefs, settings_module.default_preferences())
            note = drain_notes(app)
            self.assertEqual(len(note), 1)
            self.assertIn("unreadable", note[0])
            dopo = sorted(p.name for p in Path(tmp).iterdir())
            self.assertEqual(prima, dopo)
            self.assertFalse(app.user_path.exists())

    def test_driver_malformato_default_con_nota_senza_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = Path(tmp) / "steamvr.vrsettings"
            raw = b"{malformato:,,,"
            driver.write_bytes(raw)
            app = make_app(tmp, user_name="assente.json", preferences_arg=None)
            with patch.object(settings_module, "DEFAULT_VRSETTINGS", driver):
                prefs = app._load_initial_prefs()
            self.assertEqual(prefs, settings_module.default_preferences())
            note = drain_notes(app)
            self.assertEqual(len(note), 1)
            self.assertIn("malformed", note[0])
            # Nessun write/correttore: bytes intatti, nessun backup.
            self.assertEqual(driver.read_bytes(), raw)
            self.assertFalse(app.user_path.exists())
            self.assertFalse((Path(tmp) / "steamvr.vrsettings.bak").exists())

    def test_driver_json_non_oggetto_default_con_nota(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = Path(tmp) / "steamvr.vrsettings"
            raw = json.dumps([1, 2, 3]).encode("utf-8")
            driver.write_bytes(raw)
            app = make_app(tmp, user_name="assente.json", preferences_arg=None)
            with patch.object(settings_module, "DEFAULT_VRSETTINGS", driver):
                prefs = app._load_initial_prefs()
            self.assertEqual(prefs, settings_module.default_preferences())
            note = drain_notes(app)
            self.assertEqual(len(note), 1)
            self.assertEqual(driver.read_bytes(), raw)

    def test_user_malformato_conserva_bytes_nota_e_prova_driver(self):
        with tempfile.TemporaryDirectory() as tmp:
            user = Path(tmp) / "user.json"
            driver = Path(tmp) / "steamvr.vrsettings"
            raw_bad = b"{user malformato,,,"
            user.write_bytes(raw_bad)
            write_driver(driver, NONDEFAULT_SECTION)
            app = make_app(tmp, user_name="user.json", preferences_arg=None)
            with patch.object(settings_module, "DEFAULT_VRSETTINGS", driver):
                prefs = app._load_initial_prefs()
            # Comportamento esistente: dopo user illeggibile prova il driver.
            self.assertEqual(prefs["reacquire_blend_ms"], 300)
            self.assertEqual(prefs["optics_profile"], "current")
            note = drain_notes(app)
            # Solo percorso in nota: non dipende dal testo localizzato
            # (il suffisso italiano puo' dire "provo le impostazioni del driver").
            self.assertTrue(any("user.json" in n for n in note))
            # Nessun rewrite/backup dello user malformato.
            self.assertEqual(user.read_bytes(), raw_bad)
            self.assertFalse(user.with_name("user.json.bak").exists())

    def test_user_malformato_senza_driver_default_con_nota(self):
        with tempfile.TemporaryDirectory() as tmp:
            user = Path(tmp) / "user.json"
            raw_bad = b"non json {{"
            user.write_bytes(raw_bad)
            assente = Path(tmp) / "assente.vrsettings"
            app = make_app(tmp, user_name="user.json", preferences_arg=None)
            with patch.object(settings_module, "DEFAULT_VRSETTINGS", assente):
                prefs = app._load_initial_prefs()
            self.assertEqual(prefs, settings_module.default_preferences())
            note = drain_notes(app)
            self.assertGreaterEqual(len(note), 1)
            # Solo percorso in nota: non dipende dal testo localizzato.
            self.assertTrue(any("user.json" in n for n in note))
            self.assertEqual(user.read_bytes(), raw_bad)

    def test_prefs_valido_evita_lettura_driver(self):
        with tempfile.TemporaryDirectory() as tmp:
            user = Path(tmp) / "user.json"
            driver = Path(tmp) / "steamvr.vrsettings"
            write_prefs_file(user, blend=222)
            write_driver(driver, NONDEFAULT_SECTION)
            app = make_app(tmp, user_name="user.json", preferences_arg=None)
            with patch.object(settings_module, "DEFAULT_VRSETTINGS", driver):
                with patch.object(
                    settings_module, "preferences_from_driver",
                    side_effect=AssertionError("driver non deve essere letto"),
                ) as spy:
                    prefs = app._load_initial_prefs()
            self.assertEqual(prefs["reacquire_blend_ms"], 222)
            spy.assert_not_called()
            self.assertEqual(drain_notes(app), [])


if __name__ == "__main__":
    unittest.main()
