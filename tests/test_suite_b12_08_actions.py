"""Regressione B12-08 (coverage): azioni UI mai esercitate + matrice collect_prefs.

Stile headless test_suite_app_model.py: SuiteApp.__new__ senza Tk,
FakeVar/Mock, runtime/tracking mockati. Nessun HW, nessun display,
nessun processo reale. ZERO modifiche al codice di produzione.

Copre:
1. on_stop_vr chiama runtime.stop_vr + tracking.stop.
2. on_save invalido -> prefs_invalid senza write; valido -> save chiamato.
3. on_apply_tracking passthrough; on_close idempotente (coerente B12-02).
4. on_recenter/realign/reset_relative/show_grid smoke (ok + failure-helper).
5. Matrice numerica collect_prefs (fps, exposure, gain, min_area, cutoff,
   beta, scale, offset): valido ok, non-numerico/fuori-range rifiutato.
"""

import copy
import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.psvr_suite import settings as backend_settings
from tools.psvr_suite import app as suite_app


class FakeVar:
    def __init__(self, value=""):
        self._v = value

    def get(self):
        return self._v

    def set(self, value):
        self._v = value

    def configure(self, **kwargs):
        pass


def make_shell():
    """SuiteApp senza Tk: solo vars/model/code per collect/eventi + save."""
    app = suite_app.SuiteApp.__new__(suite_app.SuiteApp)
    app._settings = backend_settings
    app.lang = "it"
    # _L deterministico: la chiave resta leggibile negli assert
    # senza dipendere dai letterali localizzati del catalogue.
    app._L = lambda key, **kw: key if not kw else "%s|%s" % (key, kw)
    app.model = suite_app.AppModel(backend_settings.default_preferences())
    app.closing = False
    app._pending = 0
    app._pending_lock = threading.Lock()
    app._status_pending = False
    app.display = queue.Queue(maxsize=512)
    app.serial = suite_app.SerialWorker()
    app.buttons = {}
    app._preview_img = None
    app.preview_widget = Mock()
    app.tk = Mock()
    prefs = backend_settings.default_preferences()
    app.var_tracking_mode = FakeVar(prefs["tracking_mode"])
    app.var_fallback = FakeVar(prefs["fallback_3dof"])
    app.var_two_led = FakeVar(prefs["two_led_experimental"])
    app.var_blend = FakeVar(str(prefs["reacquire_blend_ms"]))
    app.var_speed = FakeVar(str(prefs["reacquire_speed_m_s"]))
    app.var_cal = FakeVar(str(prefs["imu_calibration_ms"]))
    app.var_stale = FakeVar(str(prefs["imu_stale_ms"]))
    app.var_gap = FakeVar(str(prefs["imu_max_gap_us"]))
    app.var_tracking_path = FakeVar(prefs["tracking_config_path"])
    app.var_value_min = FakeVar(prefs["value_min"])
    app.var_min_area = FakeVar(prefs["min_area"])
    app.var_exposure = FakeVar(prefs["exposure"])
    app.var_gain = FakeVar(prefs["gain"])
    app.var_saturation = FakeVar(str(prefs["saturation_min"]))
    app.var_opening = FakeVar(str(prefs["opening_size"]))
    app.var_fps = FakeVar(str(prefs["fps"]))
    app.var_width = FakeVar(str(prefs["width"]))
    app.var_height = FakeVar(str(prefs["height"]))
    app.var_intrinsics = FakeVar(prefs["intrinsicsPath"])
    app.var_rig = FakeVar(prefs["rigPath"])
    app.var_imu_rig = FakeVar(prefs["imu_rig"])
    app.var_preview_info = FakeVar("")
    app.var_preview_detail = FakeVar("")
    app.var_filter = FakeVar(prefs["position_filter"])
    app.var_cutoff = FakeVar(str(prefs["min_cutoff_hz"]))
    app.var_beta = FakeVar(str(prefs["beta"]))
    app.var_scale = FakeVar(str(prefs["position_scale"]))
    app.var_head_offset = FakeVar("0.000, 0.000, 0.000")
    app.var_initial_offset = FakeVar("0.000, 0.000, 0.000")
    app.var_optics = FakeVar(prefs["optics_profile"])
    app.var_camera_source = FakeVar(prefs["camera_source"])
    for name in ("var_stato", "var_sorgente", "var_readiness",
                 "var_modo_richiesto", "var_modo_effettivo",
                 "var_fallback_diag", "var_recovery", "var_led", "var_rms",
                 "var_age", "var_imu_session", "var_imu_epoch", "var_pos",
                 "var_pos_mode", "var_msg"):
        setattr(app, name, FakeVar("sconosciuto"))
    app.runtime = Mock()
    app.tracking = Mock()
    app.root = Mock()
    app._poll_stop = threading.Event()
    app.logs_dir = Path("logs")
    app.user_path = Path("user_tmp.json")
    return app


def capture_task(app, method):
    """Intercetta _run_serial e restituisce il task schedulato."""
    funcs = []
    app._run_serial = lambda label, func: funcs.append((label, func)) or True
    getattr(app, method)()
    assert len(funcs) == 1, "%s deve schedulare un solo task" % method
    return funcs[0][1]


class StopVrTests(unittest.TestCase):
    def test_stop_chiama_tracking_e_runtime(self):
        app = make_shell()
        app.tracking.stop.return_value = True
        app.runtime.stop_vr.return_value = True
        task = capture_task(app, "on_stop_vr")
        self.assertTrue(task())
        app.tracking.stop.assert_called_once_with()
        app.runtime.stop_vr.assert_called_once_with()

    def test_stop_failure_helper_sollevato(self):
        # tracking.stop falso -> RuntimeError; runtime.stop_vr comunque
        # tentato (finally), e suo fallimento -> RuntimeError.
        app = make_shell()
        app.tracking.stop.return_value = {"ok": False}
        app.runtime.stop_vr.return_value = True
        with self.assertRaises(RuntimeError):
            capture_task(app, "on_stop_vr")()
        app.runtime.stop_vr.assert_called_once_with()
        app2 = make_shell()
        app2.tracking.stop.return_value = True
        app2.runtime.stop_vr.return_value = {"ok": False}
        with self.assertRaises(RuntimeError):
            capture_task(app2, "on_stop_vr")()


class SaveTests(unittest.TestCase):
    def test_save_invalido_prefs_invalid_senza_write(self):
        app = make_shell()
        app.var_speed.set("non-numero")
        with patch.object(backend_settings, "save_preferences") as spy:
            app.on_save()
            spy.assert_not_called()
        self.assertIn("action.prefs_invalid", app.var_msg.get())

    def test_save_valido_chiama_save_preferences(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = make_shell()
            app.user_path = Path(tmp) / "user.json"
            prefs, errori = app.collect_prefs()
            self.assertEqual(errori, [])
            with patch.object(backend_settings, "save_preferences",
                              wraps=backend_settings.save_preferences) as spy:
                app.on_save()
                spy.assert_called_once()
                args, _ = spy.call_args
                self.assertEqual(args[0], str(app.user_path))
                self.assertEqual(args[1], prefs)
            self.assertIn("action.prefs_saved", app.var_msg.get())
            # Wrap reale: il file e' stato davvero scritto in tmp.
            self.assertTrue(app.user_path.is_file())


class ApplyTrackingAndCloseTests(unittest.TestCase):
    def test_apply_tracking_passthrough(self):
        app = make_shell()
        app.tracking.update_settings.return_value = True
        task = capture_task(app, "on_apply_tracking")
        self.assertTrue(task())
        app.tracking.update_settings.assert_called_once()
        snapshot = app.tracking.update_settings.call_args.args[0]
        self.assertEqual(sorted(snapshot.keys()),
                         sorted(backend_settings.default_preferences().keys()))

    def test_apply_tracking_failure_helper(self):
        app = make_shell()
        app.tracking.update_settings.return_value = {"ok": False}
        with self.assertRaises(RuntimeError):
            capture_task(app, "on_apply_tracking")()

    def test_apply_tracking_invalido_non_chiama_backend(self):
        app = make_shell()
        app.var_speed.set("non-numero")
        app._run_serial = Mock()
        app.on_apply_tracking()
        app._run_serial.assert_not_called()
        app.tracking.update_settings.assert_not_called()
        self.assertIn("action.prefs_invalid", app.var_msg.get())

    def test_on_close_idempotente_coerente_b12_02(self):
        # Coerente con B12-02: stop tracking posseduto + runtime.close(),
        # mai runtime.stop_vr (SteamVR esterno resta su).
        app = make_shell()
        submitted = []
        app.serial = Mock()
        app.serial.submit.side_effect = lambda label, func: (
            submitted.append(label), func(), True)[2]
        app.tracking = Mock()
        app.tracking.stop.return_value = True
        app.runtime = Mock()
        app.root = Mock()
        app.on_close()
        self.assertTrue(app.closing)
        self.assertEqual(submitted, ["stop-posseduto"])
        app.tracking.stop.assert_called_once_with()
        app.runtime.close.assert_called_once_with()
        app.runtime.stop_vr.assert_not_called()
        # Seconda chiamata: no-op idempotente, nessun nuovo submit/stop.
        app.on_close()
        self.assertEqual(submitted, ["stop-posseduto"])
        app.tracking.stop.assert_called_once()
        app.runtime.close.assert_called_once()


class SmallActionsSmokeTests(unittest.TestCase):
    """Smoke on_recenter/realign/reset_relative/show_grid: ok + failure."""

    MATRIX = (
        ("on_recenter", "runtime", "recenter", ()),
        ("on_realign", "tracking", "realign", ()),
        ("on_reset_relative", "tracking", "reset_relative", ()),
        ("on_show_grid", "runtime", "show_grid", ()),
    )

    def test_path_ok_chiama_backend(self):
        for method, side, attr, _ in self.MATRIX:
            with self.subTest(azione=method):
                app = make_shell()
                getattr(getattr(app, side), attr).return_value = True
                task = capture_task(app, method)
                self.assertTrue(task())
                getattr(getattr(app, side), attr).assert_called_once_with()

    def test_failure_helper_sollevato(self):
        for method, side, attr, _ in self.MATRIX:
            with self.subTest(azione=method):
                app = make_shell()
                getattr(getattr(app, side), attr).return_value = {"ok": False}
                with self.assertRaises(RuntimeError):
                    capture_task(app, method)()


class CollectPrefsNumericMatrixTests(unittest.TestCase):
    """Matrice per campo numerico: valido ok, non-numerico/fuori-range ko."""

    # (nome_campo, attr_var, valido, non_numerico, fuori_range[])
    MATRIX = (
        ("fps", "var_fps", "75", "non-numero", ["0", "-1", "74.5"]),
        ("exposure", "var_exposure", "13", "non-numero", ["-1", "256"]),
        ("gain", "var_gain", "0", "non-numero", ["-1", "64"]),
        ("min_area", "var_min_area", "6", "non-numero", ["0", "-3"]),
        ("min_cutoff_hz", "var_cutoff", "2.5", "non-numero", ["0", "-1", "50"]),
        ("beta", "var_beta", "8.0", "non-numero", ["-1"]),
        ("position_scale", "var_scale", "1.0", "non-numero", ["0", "-2"]),
        ("head_offset_model_m", "var_head_offset",
         "0.000, 0.000, 0.000", "non-numerico-xyz", ["11, 0, 0"]),
    )

    def test_valido_ok(self):
        for campo, attr, valido, _, _ in self.MATRIX:
            with self.subTest(campo=campo, caso="valido"):
                app = make_shell()
                getattr(app, attr).set(valido)
                prefs, errori = app.collect_prefs()
                self.assertEqual(errori, [], "%s=%r rifiutato" % (campo, valido))
                self.assertIsNotNone(prefs)

    def test_non_numerico_rifiutato(self):
        for campo, attr, _, bad, _ in self.MATRIX:
            with self.subTest(campo=campo, caso="non-numerico"):
                app = make_shell()
                getattr(app, attr).set(bad)
                prefs, errori = app.collect_prefs()
                self.assertIsNone(prefs)
                self.assertTrue(errori, "%s=%r accettato" % (campo, bad))

    def test_fuori_range_rifiutato(self):
        for campo, attr, _, _, out_list in self.MATRIX:
            for fuori in out_list:
                with self.subTest(campo=campo, caso="fuori-range",
                                  valore=fuori):
                    app = make_shell()
                    getattr(app, attr).set(fuori)
                    prefs, errori = app.collect_prefs()
                    self.assertIsNone(
                        prefs, "%s=%r accettato" % (campo, fuori))
                    self.assertTrue(errori)


class AttachBranchTests(unittest.TestCase):
    """on_attach: update_settings se tracking gia' attivo, start altrimenti.

    Regressione cambio-camera PS3 Eye: il restart cieco riapriva la camera
    sotto i piedi del worker (access violation nativa). Headless, mock only.
    """

    def test_attach_running_uses_update_settings(self):
        app = make_shell()
        app.tracking.status.return_value = {"running": True}
        app.tracking.update_settings.return_value = True
        task = capture_task(app, "on_attach")
        self.assertTrue(task())
        app.tracking.update_settings.assert_called_once()
        app.tracking.start.assert_not_called()
        app.runtime.start_vr.assert_not_called()

    def test_attach_stopped_uses_start(self):
        app = make_shell()
        app.tracking.status.return_value = {"running": False}
        app.tracking.start.return_value = True
        task = capture_task(app, "on_attach")
        self.assertTrue(task())
        app.tracking.start.assert_called_once()
        app.tracking.update_settings.assert_not_called()

    def test_attach_update_failure_raises_clear_message(self):
        app = make_shell()
        app.tracking.status.return_value = {"running": True}
        app.tracking.update_settings.return_value = {"ok": False}
        with self.assertRaises(RuntimeError) as ctx:
            capture_task(app, "on_attach")()
        self.assertIn("update_settings", str(ctx.exception))

    def test_attach_start_failure_raises(self):
        app = make_shell()
        app.tracking.status.return_value = {"running": False}
        app.tracking.start.return_value = False
        with self.assertRaises(RuntimeError):
            capture_task(app, "on_attach")()


if __name__ == "__main__":
    unittest.main()
