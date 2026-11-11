"""Test i18n IT/EN PSVRSuite: parity, persistenza, live-switch. Niente Tk/HW."""

import copy
import inspect
import json
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.psvr_suite import i18n
from tools.psvr_suite import app as suite_app
from tools.psvr_suite.app import AppModel, CANONICAL_KEYS, WINDOW_TITLE
from tools.psvr_suite import settings as backend_settings


class FakeVar:
    def __init__(self, value=""):
        self._v = value

    def get(self):
        return self._v

    def set(self, value):
        self._v = value


class FakeWidget:
    """Surrogato label/button: registra configure(text=...) senza Tk."""

    def __init__(self, text=""):
        self.text = text
        self.calls = []

    def configure(self, **kwargs):
        self.calls.append(dict(kwargs))
        if "text" in kwargs:
            self.text = kwargs["text"]

    def grid(self, *args, **kwargs):
        pass

    def pack(self, *args, **kwargs):
        pass


def make_i18n_shell(root, lang="it"):
    """SuiteApp senza Tk: solo stato lingua + widget finti per live-switch."""
    app = suite_app.SuiteApp.__new__(suite_app.SuiteApp)
    app.root_path = Path(root)
    app.lang = i18n.normalize_language(lang)
    app._static = []
    app._tab_order = []
    app._filter_radios = []
    app._lang_updating = False
    app.buttons = {}
    app.var_language = FakeVar("Italiano" if app.lang == "it" else "English")
    app.var_msg = FakeVar("")
    app.var_preview_info = FakeVar("")
    app.preview_widget = FakeWidget("placeholder")
    app._preview_base = None
    app.tabs = Mock()
    app.rb_3dof = None
    app.rb_6dof = None
    app.chk_fallback = None
    app.runtime = Mock()
    app.tracking = Mock()
    app.closing = False
    app._pending = 0
    app._pending_lock = threading.Lock()
    return app


class ParityTests(unittest.TestCase):
    def test_nessuna_traduzione_mancante(self):
        self.assertEqual(i18n.missing_translations(), {})

    def test_ogni_chiave_ha_it_e_en_non_vuoti(self):
        for chiave, entry in i18n.STRINGS.items():
            for code in ("it", "en"):
                val = entry.get(code)
                self.assertIsInstance(val, str, f"{chiave}[{code}]")
                self.assertTrue(val.strip(), f"{chiave}[{code}] vuota")

    def test_copertura_aree_gui(self):
        chiavi = set(i18n.STRINGS)
        for prefisso in ("header.", "tab.", "session.", "tracking.", "camera.",
                         "position.", "optics.", "diag.", "footer.",
                         "action.", "calibration.", "task.", "common."):
            self.assertTrue([k for k in chiavi if k.startswith(prefisso)],
                            f"area {prefisso} senza chiavi")
        # Voci esplicite richieste: selettore, tab, pulsanti, guide, errori.
        for chiave in ("header.language", "header.title", "header.ready",
                       "tab.session", "tab.tracking", "tab.camera",
                       "tab.position", "tab.optics", "tab.diagnostics",
                       "session.btn_start", "session.btn_apply_driver",
                        "tracking.btn_apply", "camera.source",
                        "camera.source_note", "camera.btn_attach",
                        "camera.btn_apply", "position.btn_apply",
                       "optics.btn_apply", "optics.btn_grid",
                       "diag.btn_recenter", "diag.btn_realign",
                       "diag.btn_logs", "footer.save",
                       "session.guide", "camera.guide", "camera.warning",
                       "position.guide", "optics.guide", "diag.guide",
                       "action.prefs_invalid", "action.task_done",
                       "action.task_failed", "calibration.notice",
                       "calibration.failed", "calibration.restart_required"):
            self.assertIn(chiave, chiavi)

    def test_selettore_bilingue_e_opzioni(self):
        self.assertEqual(i18n.t("it", "header.language"), "Lingua / Language")
        self.assertEqual(i18n.t("en", "header.language"), "Lingua / Language")
        self.assertEqual(i18n.t("it", "header.lang_italiano"), "Italiano")
        self.assertEqual(i18n.t("en", "header.lang_english"), "English")

    def test_formatter_tipati_trattengono_reason_backend(self):
        # Etichetta tradotta, reason tecnico verbatim (anche inglese backend).
        self.assertEqual(i18n.fmt_task_failed("en", "Apply driver", "boom driver"),
                         "Failed Apply driver: boom driver")
        self.assertEqual(i18n.fmt_task_failed("it", "Applica driver", "boom driver"),
                         "Fallito Applica driver: boom driver")
        self.assertEqual(i18n.fmt_task_done("en", "Start VR"), "Completed: Start VR.")
        self.assertEqual(i18n.fmt_in_progress("it", "Avvio VR"), "In corso: Avvio VR…")


class NormalizeTests(unittest.TestCase):
    def test_unknown_grazioso_default_italiano(self):
        for valore in (None, "", "  ", "xx", "fr", "de", 123, ["it"], object()):
            self.assertEqual(i18n.normalize_language(valore), "it")

    def test_alias_accettati(self):
        for valore in ("it", "IT", " Italiano ", "italiano", "it-IT"):
            self.assertEqual(i18n.normalize_language(valore), "it")
        for valore in ("en", "EN", " English ", "english", "inglese", "en-US"):
            self.assertEqual(i18n.normalize_language(valore), "en")


class PersistenceTests(unittest.TestCase):
    def test_default_italiano_senza_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(i18n.load_language(Path(d)), "it")

    def test_file_corrotto_o_unknown_grazioso(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            ui = root / "configurations" / "psvr_suite_ui.json"
            ui.parent.mkdir(parents=True, exist_ok=True)
            ui.write_text("{non json", encoding="utf-8")
            self.assertEqual(i18n.load_language(root), "it")
            ui.write_text(json.dumps({"language": "xx"}), encoding="utf-8")
            self.assertEqual(i18n.load_language(root), "it")
            ui.write_text(json.dumps(["non", "dict"]), encoding="utf-8")
            self.assertEqual(i18n.load_language(root), "it")

    def test_roundtrip_atomico_file_separato(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertEqual(i18n.save_language(root, "en"), "en")
            ui = root / "configurations" / "psvr_suite_ui.json"
            self.assertTrue(ui.is_file())
            data = json.loads(ui.read_text(encoding="utf-8"))
            self.assertEqual(data, {"language": "en"})
            self.assertEqual(i18n.load_language(root), "en")
            # Nessun tmp residuo nella stessa root (atomicita').
            self.assertEqual(list((root / "configurations").glob("*.tmp")), [])
            self.assertEqual(i18n.save_language(root, "xx"), "it")
            self.assertEqual(i18n.load_language(root), "it")

    def test_lingua_fuori_dal_canonico_e_driver(self):
        # Mai dentro settings.py / schema canonico / driver prefs.
        self.assertNotIn("language", list(CANONICAL_KEYS))
        self.assertNotIn("language", backend_settings.default_preferences())
        base = AppModel.default_prefs()
        with self.assertRaises(ValueError):
            AppModel.validate(dict(base, language="en"))
        with self.assertRaises(ValueError):
            backend_settings.validate_preferences(dict(base, language="en"))
        # Il file UI non e' il file prefs utente e non contiene pref canoniche.
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            i18n.save_language(root, "en")
            data = json.loads((root / "configurations" / "psvr_suite_ui.json")
                              .read_text(encoding="utf-8"))
            self.assertEqual(set(data), {"language"})
            for chiave in CANONICAL_KEYS:
                self.assertNotIn(chiave, data)


class LiveSwitchTests(unittest.TestCase):
    def test_switch_cambia_testi_non_prefs_ne_hardware(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            app = make_i18n_shell(Path(d), lang="it")
            lbl = FakeWidget(app._L("header.title"))
            btn = FakeWidget(app._L("footer.save"))
            app._register(lbl, "header.title")
            app._register(btn, "footer.save")
            app.buttons["save"] = btn
            app._tab_order = ["tab.session", "tab.camera"]
            # Prefs non salvate dell'utente: devono restare intatte.
            app.var_blend = FakeVar("123")
            app.var_speed = FakeVar("9.9")
            before = (app.var_blend.get(), app.var_speed.get())
            app.set_language("en", save=False)
            self.assertEqual(app.lang, "en")
            self.assertEqual(lbl.text, i18n.t("en", "header.title"))
            self.assertEqual(btn.text, i18n.t("en", "footer.save"))
            self.assertEqual(app.var_language.get(), "English")
            self.assertEqual((app.var_blend.get(), app.var_speed.get()), before)
            # Nessuna chiamata hardware/tracking/SteamVR, nessuna prefs scritta.
            app.runtime.assert_not_called()
            app.tracking.assert_not_called()
            app.tabs.tab.assert_any_call(0, text=i18n.t("en", "tab.session"))
            app.tabs.tab.assert_any_call(1, text=i18n.t("en", "tab.camera"))
            # Ritorno a italiano: testi di nuovo IT, prefs ancora intatte.
            app.set_language("it", save=False)
            self.assertEqual(lbl.text, i18n.t("it", "header.title"))
            self.assertEqual((app.var_blend.get(), app.var_speed.get()), before)

    def test_set_language_persiste_su_file_separato(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            app = make_i18n_shell(root, lang="it")
            app.set_language("en", save=True)
            self.assertEqual(i18n.load_language(root), "en")
            # Il file prefs utente canonico non e' stato creato/toccato.
            self.assertFalse((root / "configurations" / "psvr_suite_user.json").exists())

    def test_stato_sconosciuto_localizzato_senza_inventare(self):
        app = make_i18n_shell(Path("."), lang="en")
        app.var_modo_effettivo = FakeVar("")
        app.var_fallback_diag = FakeVar("")
        app.var_stato = FakeVar("")
        app.var_sorgente = FakeVar("")
        app.var_readiness = FakeVar("")
        app.var_modo_richiesto = FakeVar("")
        app.var_recovery = FakeVar("")
        app.var_led = FakeVar("")
        app.var_rms = FakeVar("")
        app.var_age = FakeVar("")
        app.var_imu_session = FakeVar("")
        app.var_imu_epoch = FakeVar("")
        app.var_pos = FakeVar("")
        app.var_pos_mode = FakeVar("")
        app._last_status = ({}, {})
        app._handle_event("status", {"runtime": {}, "tracking": {}})
        self.assertEqual(app.var_modo_effettivo.get(), "unknown")
        self.assertEqual(app.var_fallback_diag.get(), "unknown")
        # Testo backend reale passa verbatim (mai tradotto/inventato).
        app._handle_event("status", {"runtime": {"state": "vr-attivo"},
                                     "tracking": {}})
        self.assertEqual(app.var_stato.get(), "vr-attivo")

    def test_calibrazione_en_trattiene_dettaglio_tecnico(self):
        app = make_i18n_shell(Path("."), lang="en")
        app._handle_event("calibration_failed", {})
        msg = app.var_msg.get()
        self.assertIn("FAILED", msg)
        self.assertIn("calibration_failed", msg)
        self.assertIn("healthy=False", msg)
        app._handle_event("restart_required", {"reason": "gyro"})
        self.assertIn("gyro", app.var_msg.get())
        self.assertIn("NOT restarted", app.var_msg.get())


class TitleInvariantTests(unittest.TestCase):
    def test_window_title_stabile_launcher_snapshot(self):
        # Launcher/snapshot cercano il titolo esatto: la GUI non lo traduce.
        from tools.psvr_suite import psvr_suite_launcher as launcher
        self.assertEqual(WINDOW_TITLE, "PSVR Suite")
        self.assertEqual(launcher.WINDOW_TITLE, "PSVR Suite")
        src = (ROOT / "tools" / "psvr_suite" / "ui_snapshot.py").read_text(
            encoding="utf-8")
        self.assertIn("PSVR Suite", src)
        app_src = (ROOT / "tools" / "psvr_suite" / "app.py").read_text(
            encoding="utf-8")
        self.assertIn('self.root.title(WINDOW_TITLE)', app_src)

    def test_header_tradotto_titolo_os_no(self):
        self.assertNotEqual(i18n.t("it", "header.title"),
                            i18n.t("en", "header.title"))


class WorkerSafetyTests(unittest.TestCase):
    def test_wrapper_worker_solo_coda_mai_tk(self):
        # Il wrapper SerialWorker non deve toccare widget Tk (solo display).
        # Il ramo submit-rifiutato gira sul main thread (var_msg lecito):
        # si ispeziona solo il corpo worker fino al submit.
        src = inspect.getsource(suite_app.SuiteApp._run_serial)
        worker_part = src.split("def wrapper", 1)[1].split(
            "self.serial.submit", 1)[0]
        for vietato in (".configure(", "var_msg.set", "var_stato",
                        "_retranslate", "set_language", ".grid(", ".pack("):
            self.assertNotIn(vietato, worker_part)
        self.assertIn("self.display.put_nowait(", worker_part)
        # B12-01: nessun put bloccante nel worker (coda piena + closing
        # = deadlock in chiusura); solo put_nowait con drop su Full.
        self.assertNotIn("self.display.put(", worker_part)

    def test_task_eseguito_su_worker_senza_toccare_gui(self):
        app = make_i18n_shell(Path("."), lang="it")
        app.var_msg = FakeVar("prima")
        toccati = []

        class GuardWidget(FakeWidget):
            def configure(self, **kwargs):
                toccati.append(kwargs)
                super().configure(**kwargs)

        app.preview_widget = GuardWidget("x")
        done = threading.Event()
        import queue as _q
        app.display = _q.Queue()
        app.serial = suite_app.SerialWorker()
        app.serial.start()
        try:
            app._set_buttons = lambda enabled: None
            ok = app._run_serial("etichetta", lambda: True)
            self.assertTrue(ok)
            app.serial._tasks.join()
            self.assertEqual(toccati, [])
            kind, payload = app.display.get_nowait()
            self.assertEqual(kind, "task_done")
            self.assertTrue(payload.get("ok"))
        finally:
            app.serial.shutdown()
            done.set()


if __name__ == "__main__":
    unittest.main()
