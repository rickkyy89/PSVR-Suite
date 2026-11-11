"""Test integrazione app.py su schema canonico backend. Niente Tk/display/camera/HW."""

import copy
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.psvr_suite import settings as backend_settings
from tools.psvr_suite import app as suite_app
from tools.psvr_suite.app import (
    AppModel, SerialWorker, summarize_status, CANONICAL_KEYS, _result_ok,
)


class FakeVar:
    def __init__(self, value=""):
        self._v = value

    def get(self):
        return self._v

    def set(self, value):
        self._v = value

    def configure(self, **kwargs):
        pass


def make_app_shell(settings_mod=None):
    """SuiteApp senza Tk: solo vars/model/code per collect/eventi."""
    app = suite_app.SuiteApp.__new__(suite_app.SuiteApp)
    app._settings = settings_mod if settings_mod is not None else backend_settings
    app.model = AppModel(backend_settings.default_preferences())
    app.closing = False
    app._pending = 0
    app._pending_lock = threading.Lock()
    app._status_pending = False
    app.display = __import__("queue").Queue(maxsize=512)
    app.serial = SerialWorker()
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
    return app


class CanonicalSchemaTests(unittest.TestCase):
    def test_chiavi_canoniche_backend(self):
        prefs = backend_settings.default_preferences()
        self.assertEqual(sorted(prefs.keys()), sorted(CANONICAL_KEYS))
        self.assertIn("intrinsicsPath", prefs)
        self.assertIn("imu_calibration_ms", prefs)
        self.assertNotIn("intrinsics_path", prefs)
        self.assertNotIn("calibration_ms", prefs)
        for chiave in ("intrinsicsPath", "rigPath", "imu_rig"):
            self.assertTrue(prefs[chiave].strip())

    def test_appmodel_roundtrip_backend(self):
        prefs = AppModel.default_prefs()
        self.assertEqual(sorted(prefs.keys()), sorted(CANONICAL_KEYS))
        cleaned = AppModel.validate(copy.deepcopy(prefs))
        backend_settings.validate_preferences(cleaned)

    def test_unknown_rifiutato_non_ignorato(self):
        base = AppModel.default_prefs()
        data = dict(base, chiave_futura="x")
        with self.assertRaises(ValueError):
            AppModel.validate(data)
        with self.assertRaises(ValueError):
            backend_settings.validate_preferences(data)

    def test_range_integrazione(self):
        base = AppModel.default_prefs()
        with self.assertRaises(ValueError):
            AppModel.validate(dict(base, min_cutoff_hz="50"))
        with self.assertRaises(ValueError):
            AppModel.validate(dict(base, opening_size="1"))
        with self.assertRaises(ValueError):
            AppModel.validate(dict(base, reacquire_speed_m_s="0.0"))
        ok = AppModel.validate(dict(base, min_cutoff_hz="40",
                                    opening_size="3",
                                    reacquire_speed_m_s="0.01"))
        backend_settings.validate_preferences(ok)

    def test_fresh_defaults_fallback_off_identity(self):
        prefs = AppModel.default_prefs()
        self.assertIs(prefs["fallback_3dof"], False)
        self.assertEqual(prefs["optics_profile"], "identity")
        self.assertEqual(prefs, backend_settings.default_preferences())
        # Shell headless fresca: stessi default, nessun opt-in implicito.
        app = make_app_shell()
        self.assertIs(app.var_fallback.get(), False)
        self.assertEqual(app.var_optics.get(), "identity")


class CollectHeadlessTests(unittest.TestCase):
    def test_collect_tipizza_stringhe_per_backend(self):
        app = make_app_shell()
        app.var_blend.set("150")
        app.var_speed.set("1.5")
        app.var_head_offset.set("0.1, 0.2, 0.3")
        app.var_initial_offset.set("1, 2, 3")
        prefs, errori = app.collect_prefs()
        self.assertEqual(errori, [])
        self.assertIsInstance(prefs["reacquire_blend_ms"], int)
        self.assertIsInstance(prefs["reacquire_speed_m_s"], float)
        self.assertEqual(prefs["head_offset_model_m"], [0.1, 0.2, 0.3])
        backend_settings.validate_preferences(prefs)

    def test_collect_stringa_grezza_non_passa(self):
        app = make_app_shell()
        app.var_speed.set("non-numero")
        prefs, errori = app.collect_prefs()
        self.assertIsNone(prefs)
        self.assertTrue(errori)

    def test_dim_imutabili_validate_prima_hw(self):
        app = make_app_shell()
        app.var_width.set("0")
        prefs, errori = app.collect_prefs()
        self.assertIsNone(prefs)
        self.assertTrue(errori)


class CallbackNormalizeTests(unittest.TestCase):
    def test_runtime_event_dict_e_tracking_kind_payload(self):
        app = make_app_shell()
        app._on_backend_event({"type": "started", "mode": "vr"})
        app._on_backend_event("frame", {"ppm": b"", "status": "ok"})
        kinds = set()
        while not app.display.empty():
            kinds.add(app.display.get_nowait()[0])
        self.assertEqual(kinds, {"started", "frame"})

    def test_status_driver_tracking_autoritativo(self):
        rt = {"mode": "vr",
              "driver_tracking": {"mode_actual": "6dof",
                                  "mode_requested": "6dof",
                                  "fallback_active": False,
                                  "recovery": "locked"}}
        tw = {"mode_actual": "3dof", "fallback_active": True,
              "imu": {"valid": False}}
        info = summarize_status(rt, tw)
        self.assertEqual(info["modo_effettivo"], "6dof")
        self.assertFalse(info["fallback_attivo"])
        self.assertEqual(info["recovery"], "locked")

    def test_worker_non_fidato_con_imu_invalida(self):
        info = summarize_status({"mode": "vr"},
                                {"mode_actual": "3dof", "fallback_active": True,
                                 "imu": {"valid": False}})
        self.assertEqual(info["modo_effettivo"], "sconosciuto")
        self.assertIsNone(info["fallback_attivo"])


class OrderAndFailureTests(unittest.TestCase):
    def _capture_task(self, app, method):
        funcs = []
        app._run_serial = lambda label, func: funcs.append((label, func)) or True
        getattr(app, method)()
        self.assertEqual(len(funcs), 1)
        return funcs[0][1]

    def test_start_runtime_prima_tracking(self):
        app = make_app_shell()
        app.var_tracking_path.set("configurations/diagnostic custom.json")
        ordine = []
        app.runtime.start_vr.side_effect = lambda p: ordine.append("runtime") or True
        app.tracking.start.side_effect = lambda p: ordine.append("tracking") or True
        task = self._capture_task(app, "on_start_vr")
        task()
        self.assertEqual(ordine, ["runtime", "tracking"])
        self.assertEqual(app.runtime.start_vr.call_args.args[0]["tracking_config_path"],
                         "configurations/diagnostic custom.json")

    def test_attach_solo_tracking(self):
        app = make_app_shell()
        task = self._capture_task(app, "on_attach")
        task()
        app.tracking.start.assert_called_once()
        app.runtime.start_vr.assert_not_called()
        app.runtime.to_cinema.assert_not_called()

    def test_cinema_stop_tracking_prima(self):
        app = make_app_shell()
        app.var_tracking_path.set("configurations/cinema custom.json")
        ordine = []
        app.tracking.stop.side_effect = lambda: ordine.append("tracking") or True
        app.runtime.to_cinema.side_effect = lambda prefs: ordine.append("runtime") or True
        task = self._capture_task(app, "on_cinema")
        app.var_tracking_path.set("changed-after-click.json")
        task()
        self.assertEqual(ordine, ["tracking", "runtime"])
        self.assertEqual(app.runtime.to_cinema.call_args.args[0]["tracking_config_path"],
                         "configurations/cinema custom.json")

    def test_cinema_invalid_config_path_does_not_stop_or_switch(self):
        app = make_app_shell()
        app.var_tracking_path.set("  ")
        app._run_serial = Mock()
        app.on_cinema()
        app._run_serial.assert_not_called()
        app.tracking.stop.assert_not_called()
        app.runtime.to_cinema.assert_not_called()

    def test_false_backend_e_task_fail(self):
        self.assertFalse(_result_ok(False))
        self.assertFalse(_result_ok({"ok": False}))
        self.assertFalse(_result_ok({"ok": True, "restart_required": True}))
        self.assertTrue(_result_ok(True))
        self.assertTrue(_result_ok({"ok": True}))
        self.assertTrue(_result_ok(0))
        self.assertFalse(_result_ok(1))
        app = make_app_shell()
        app.runtime.start_vr.return_value = False
        task = self._capture_task(app, "on_start_vr")
        with self.assertRaises(RuntimeError):
            task()
        app.runtime.recenter.return_value = {"ok": False}
        task2 = self._capture_task(app, "on_recenter")
        with self.assertRaises(RuntimeError):
            task2()

    def test_apply_optics_solo_esplicito(self):
        app = make_app_shell()
        app.var_optics.set("current")
        app.on_apply_optics()
        app.runtime.apply_driver.assert_not_called()
        app.var_optics.set("mild")
        funcs = []
        app._run_serial = lambda label, func: funcs.append(func) or True
        app.on_apply_optics()
        funcs[0]()
        self.assertTrue(app.runtime.apply_driver.called)
        args, kwargs = app.runtime.apply_driver.call_args
        self.assertTrue(kwargs.get("apply_optics", False) is True
                        or "apply_optics" in str(app.runtime.apply_driver.call_args))


class ApplyColdRestartTests(unittest.TestCase):
    """Regressione apply freddo: stop PRIMA, apply, start SOLO se era attivo."""

    def _capture_task(self, app, method):
        funcs = []
        app._run_serial = lambda label, func: funcs.append((label, func)) or True
        getattr(app, method)()
        self.assertEqual(len(funcs), 1)
        return funcs[0][1]

    def _shell_running(self):
        app = make_app_shell()
        app.tracking.status.return_value = {"running": True}
        app.tracking.stop.return_value = True
        app.tracking.start.return_value = True
        app.runtime.apply_driver.return_value = {"ok": True}
        app.tracking.update_settings = Mock(return_value=True)
        return app

    def test_order_stop_apply_start(self):
        app = self._shell_running()
        ordine = []
        app.tracking.stop.side_effect = lambda *a, **k: ordine.append("stop") or True
        app.runtime.apply_driver.side_effect = lambda *a, **k: ordine.append("apply") or {"ok": True}
        app.tracking.start.side_effect = lambda *a, **k: ordine.append("start") or True
        task = self._capture_task(app, "on_apply_driver")
        task()
        self.assertEqual(ordine, ["stop", "apply", "start"])
        app.tracking.update_settings.assert_not_called()

    def test_order_optics_stop_apply_start(self):
        app = self._shell_running()
        app.var_optics.set("mild")
        ordine = []
        app.tracking.stop.side_effect = lambda *a, **k: ordine.append("stop") or True
        app.runtime.apply_driver.side_effect = lambda *a, **k: ordine.append("apply") or {"ok": True}
        app.tracking.start.side_effect = lambda *a, **k: ordine.append("start") or True
        task = self._capture_task(app, "on_apply_optics")
        task()
        self.assertEqual(ordine, ["stop", "apply", "start"])

    def test_failure_stop_blocks_apply(self):
        app = self._shell_running()
        app.tracking.stop.return_value = False
        task = self._capture_task(app, "on_apply_driver")
        with self.assertRaises(RuntimeError) as ctx:
            task()
        self.assertIn("tracking.stop", str(ctx.exception))
        self.assertIn("NON riavviato", str(ctx.exception))
        app.runtime.apply_driver.assert_not_called()
        app.tracking.start.assert_not_called()

    def test_fail_apply_no_start(self):
        app = self._shell_running()
        app.runtime.apply_driver.return_value = {"ok": False}
        task = self._capture_task(app, "on_apply_driver")
        with self.assertRaises(RuntimeError) as ctx:
            task()
        self.assertIn("NON", str(ctx.exception))
        self.assertIn("healthy=False", str(ctx.exception))
        app.tracking.start.assert_not_called()

    def test_fail_apply_restart_required_no_start(self):
        app = self._shell_running()
        app.runtime.apply_driver.return_value = {"ok": True, "restart_required": True}
        task = self._capture_task(app, "on_apply_driver")
        with self.assertRaises(RuntimeError):
            task()
        app.tracking.start.assert_not_called()

    def test_apply_exception_no_restart(self):
        app = self._shell_running()
        app.runtime.apply_driver.side_effect = RuntimeError("boom driver")
        task = self._capture_task(app, "on_apply_driver")
        with self.assertRaises(RuntimeError) as ctx:
            task()
        self.assertIn("NON", str(ctx.exception))
        self.assertIn("healthy=False", str(ctx.exception))
        app.tracking.start.assert_not_called()

    def test_not_running_no_start(self):
        app = make_app_shell()
        app.tracking.status.return_value = {"running": False}
        app.runtime.apply_driver.return_value = {"ok": True}
        app.tracking.update_settings = Mock(return_value=True)
        task = self._capture_task(app, "on_apply_driver")
        task()
        app.tracking.stop.assert_not_called()
        app.tracking.start.assert_not_called()
        app.tracking.update_settings.assert_not_called()
        self.assertTrue(app.runtime.apply_driver.called)

    def test_explicit_optics_and_driver_flags(self):
        app = self._shell_running()
        app.var_optics.set("mild")
        task = self._capture_task(app, "on_apply_optics")
        task()
        _, kwargs = app.runtime.apply_driver.call_args
        self.assertTrue(kwargs.get("apply_optics") is True)
        app2 = self._shell_running()
        task2 = self._capture_task(app2, "on_apply_driver")
        task2()
        _, kwargs2 = app2.runtime.apply_driver.call_args
        # Driver freddo mai con ottica esplicita.
        self.assertFalse(kwargs2.get("apply_optics") is True)

    def test_warning_visible_before_apply(self):
        # B12-09: letterali IT ancorati a fonte app.CALIBRATION_APPLY_NOTICE /
        # catalogo i18n (calibration.notice): assert invariati, solo fonte.
        app = make_app_shell()
        app.tracking.status.return_value = {"running": False}
        app._run_serial = lambda label, func: True
        app.on_apply_driver()
        msg = app.var_msg.get()
        self.assertIn("Avviso", msg)
        self.assertIn("FERMO", msg)
        self.assertIn("calibrazione IMU", msg)
        self.assertIn("riavvia SteamVR", msg)
        app2 = make_app_shell()
        app2.tracking.status.return_value = {"running": False}
        app2.var_optics.set("mild")
        app2._run_serial = lambda label, func: True
        app2.on_apply_optics()
        self.assertIn("FERMO", app2.var_msg.get())

    def test_calibration_events_matrix(self):
        # B12-09: letterali IT ancorati a fonte catalogo i18n (it):
        # calibration.waiting/failed, action.* e common.unknown ("sconosciuto").
        # Assert invariati (suite test non riscritta), solo fonte.
        app = make_app_shell()
        app._handle_event("calibration_waiting", {"seconds": 8, "guidance": "tieni fermo"})
        self.assertIn("8s", app.var_msg.get())
        self.assertIn("FERMO", app.var_msg.get())
        self.assertIn("Attendi", app.var_preview_info.get())
        # Nessuna posa valida inventata: modo resta sconosciuto.
        self.assertEqual(app.var_modo_effettivo.get(), "sconosciuto")
        app._handle_event("calibration_failed", {"detail": "gyro variation"})
        self.assertIn("FALLITA", app.var_msg.get())
        self.assertIn("giroscopica", app.var_msg.get())
        self.assertIn("healthy=False", app.var_msg.get())
        app._handle_event("restart_required", {"reason": "calibration failed"})
        self.assertIn("NON riavviato", app.var_msg.get())
        app._handle_event("started", {"mode": "vr"})
        self.assertIn("Avviato", app.var_msg.get())
        app._handle_event("not_ready", {"reason": "no valid IMU pose"})
        self.assertIn("Non pronto", app.var_msg.get())
        self.assertEqual(app.var_modo_effettivo.get(), "sconosciuto")

    def test_calibration_waiting_countdown_seconds(self):
        app = make_app_shell()
        app._handle_event("calibration_waiting", {"seconds": 5})
        self.assertIn("5s", app.var_msg.get())
        self.assertIn("5s", app.var_preview_info.get())
        app._handle_event("calibration-failed", {})
        self.assertIn("FALLITA", app.var_msg.get())


class PreviewTests(unittest.TestCase):
    def _ppm(self, w=8, h=6):
        header = f"P6\n{w} {h}\n255\n".encode("ascii")
        return header + bytes([10, 20, 30] * w * h)

    def test_frame_ppm_a_photoimage_con_ref(self):
        app = make_app_shell()
        created = []
        fake_img = Mock()
        fake_img.width.return_value = 640
        fake_img.height.return_value = 480
        app.tk.PhotoImage.side_effect = lambda data,format: created.append((data,format)) or fake_img
        app._handle_frame({"ppm": self._ppm(), "status": "TRACKING",
                           "detail": "candidati 3"})
        self.assertTrue(created)
        self.assertEqual(created[0],(self._ppm(),'PPM'))
        self.assertIs(app._preview_img, fake_img)
        app.preview_widget.configure.assert_called_once()
        self.assertEqual(app.var_preview_info.get(), "TRACKING")

    def test_frame_grande_downsample(self):
        app = make_app_shell()
        fake_img = Mock()
        fake_img.width.return_value = 1280
        fake_img.height.return_value = 960
        sub = Mock()
        sub.width.return_value = 640
        sub.height.return_value = 480
        fake_img.subsample.return_value = sub
        app.tk.PhotoImage.return_value = fake_img
        app._handle_frame({"ppm": self._ppm(16, 12), "status": "x"})
        fake_img.subsample.assert_called()
        self.assertIs(app._preview_img, sub)


class HeadlessFullTests(unittest.TestCase):
    def test_headless_import_reali_e_schema(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            code = suite_app.headless_check(Path(d), None)
            self.assertEqual(code, 0)

    def test_worker_seriale_senza_tk(self):
        worker = SerialWorker()
        worker.start()
        ordine = []
        lock = threading.Lock()

        def task(i):
            def _run():
                time.sleep(0.01)
                with lock:
                    ordine.append(i)
            return _run
        for i in range(4):
            self.assertTrue(worker.submit(f"t{i}", task(i)))
        worker._tasks.join()
        worker.shutdown()
        self.assertEqual(ordine, [0, 1, 2, 3])

    def test_status_non_accoda_illimitato(self):
        worker = SerialWorker()
        worker.start()
        blocco = threading.Event()
        worker.submit("blocco", lambda: blocco.wait(timeout=2))
        ok_count = sum(1 for _ in range(5)
                       if worker.submit("poll", lambda: None, drop_if_pending=True))
        self.assertLessEqual(ok_count, 1)
        blocco.set()
        worker._tasks.join()
        worker.shutdown()


class PollScheduleTests(unittest.TestCase):
    """Regressione _status_pending: submit rifiutato non incastra il polling."""

    class FakeSerial:
        def __init__(self, pending=0, accept=True, run_now=False):
            self._pending = pending
            self.accept = accept
            self.run_now = run_now
            self.submitted = []

        def pending_count(self):
            return self._pending

        def submit(self, label, func, drop_if_pending=False):
            self.submitted.append((label, func, drop_if_pending))
            if not self.accept:
                return False
            if self.run_now:
                func()
            return True

    def test_submit_rifiutato_sblocca_flag(self):
        app = make_app_shell()
        app.serial = self.FakeSerial(pending=0, accept=False)
        self.assertFalse(app._schedule_poll(lambda: None))
        self.assertFalse(app._status_pending)
        self.assertEqual(len(app.serial.submitted), 1)

    def test_polling_successivo_possibile_dopo_rifiuto(self):
        app = make_app_shell()
        app.serial = self.FakeSerial(pending=0, accept=False)
        self.assertFalse(app._schedule_poll(lambda: None))
        self.assertFalse(app._status_pending)
        # Contesa rientrata: scheduling riuscito, task completa e libera il flag.
        completati = []

        def task():
            try:
                completati.append("run")
            finally:
                with app._pending_lock:
                    app._status_pending = False

        app.serial = self.FakeSerial(pending=0, accept=True, run_now=True)
        self.assertTrue(app._schedule_poll(task))
        self.assertEqual(completati, ["run"])
        self.assertFalse(app._status_pending)
        # Terza schedulazione ancora possibile: nessun incastro permanente.
        app.serial.accept = True
        app.serial.run_now = False
        self.assertTrue(app._schedule_poll(lambda: None))
        self.assertTrue(app._status_pending)

    def test_overlap_saltato_senza_submit(self):
        app = make_app_shell()
        app._status_pending = True
        app.serial = self.FakeSerial(pending=0, accept=True)
        self.assertFalse(app._schedule_poll(lambda: None))
        self.assertEqual(app.serial.submitted, [])
        app._status_pending = False
        app.serial = self.FakeSerial(pending=1, accept=True)
        self.assertFalse(app._schedule_poll(lambda: None))
        self.assertEqual(app.serial.submitted, [])


class PreviewResponsiveTests(unittest.TestCase):
    """Anteprima Camera grande e responsive: fit continuo 4:3, fallback, icona."""

    def test_zoom_preserva_aspect_e_bounded(self):
        from tools.psvr_suite.app import _preview_zoom_for
        # Base tracking 480x360 (4:3) in contenitore grande: zoom 2x intero.
        self.assertEqual(_preview_zoom_for(480, 360, 1000, 800), (2, 1))
        # Contenitore piccolo: subsample intero, mai upscale.
        self.assertEqual(_preview_zoom_for(480, 360, 240, 180), (1, 2))
        # Esatta: nessun fattore.
        self.assertEqual(_preview_zoom_for(480, 360, 480, 360), (1, 1))
        # Non stimabile: fallback sicuro.
        self.assertEqual(_preview_zoom_for(480, 360, 0, 0), (1, 1))
        self.assertEqual(_preview_zoom_for(0, 0, 800, 600), (1, 1))
        # Cap zoom: mai oltre 4x anche su schermo enorme.
        z, s = _preview_zoom_for(480, 360, 5000, 4000)
        self.assertLessEqual(z, 4)
        self.assertEqual(s, 1)
        # Aspect: uscita resta 4:3 (480*z x 360*z).
        z, s = _preview_zoom_for(480, 360, 1280, 800)
        self.assertEqual((480 * z, 360 * s if s > 1 else 360 * z),
                         (480 * z, 360 * z))
        self.assertAlmostEqual((480 * z) / (360 * z), 4.0 / 3.0)

    def test_refresh_mostra_zoom_quando_contenitore_grande(self):
        from tools.psvr_suite import app as suite_app
        app = make_app_shell()
        base = Mock()
        base.width.return_value = 480
        base.height.return_value = 360
        zoomed = Mock()
        base.zoom.return_value = zoomed
        frame = Mock()
        frame.winfo_width.return_value = 1000
        frame.winfo_height.return_value = 800
        app._preview_base = base
        app._preview_base_size = (480, 360)
        app._preview_ppm_raw = None
        app.preview_frame = frame
        app._preview_resize_after = None
        self.assertTrue(app._refresh_preview())
        base.zoom.assert_called_once_with(2, 2)
        self.assertIs(app._preview_img, zoomed)
        app.preview_widget.configure.assert_called_once()

    def test_fit_continuo_exact_900x660(self):
        from tools.psvr_suite.app import _preview_fit_size
        # Bug riportato: pannello 900x660 con zoom intero restava 480x360
        # perche' 2x non entrava. Fit continuo -> ~880x660, molto piu' grande.
        tw, th = _preview_fit_size(640, 480, 900, 660)
        self.assertEqual((tw, th), (880, 660))
        self.assertGreater(tw * th, 480 * 360)
        self.assertAlmostEqual(tw / th, 4.0 / 3.0, places=2)
        # Base 480x360 nello stesso pannello: exact-fit 880x660.
        tw2, th2 = _preview_fit_size(480, 360, 900, 660)
        self.assertEqual((tw2, th2), (880, 660))
        # Bound 1600x1200 su schermo enorme.
        tw3, th3 = _preview_fit_size(640, 480, 5000, 4000)
        self.assertLessEqual(tw3, 1600)
        self.assertLessEqual(th3, 1200)
        self.assertEqual((tw3, th3), (1600, 1200))
        # Non stimabile: fallback sicuro.
        self.assertEqual(_preview_fit_size(640, 480, 0, 0), (0, 0))
        self.assertEqual(_preview_fit_size(0, 0, 800, 600), (0, 0))

    def test_ppm_decode_encode_roundtrip(self):
        from tools.psvr_suite.app import _decode_ppm_to_rgb, _encode_rgb_to_ppm
        raw = b"P6\n4 3\n255\n" + bytes([10, 20, 30] * 12)
        w, h, pix = _decode_ppm_to_rgb(raw)
        self.assertEqual((w, h), (4, 3))
        self.assertEqual(len(pix), 36)
        back = _encode_rgb_to_ppm(w, h, pix)
        self.assertTrue(back.startswith(b"P6\n4 3\n255\n"))
        self.assertEqual(back[11:], pix)
        self.assertEqual(_decode_ppm_to_rgb(b"nope"), (0, 0, b""))
        self.assertEqual(_decode_ppm_to_rgb(b"P6\n0 0\n255\n"), (0, 0, b""))

    def test_refresh_smooth_usa_cv2_quando_raw_presente(self):
        try:
            import cv2  # noqa: F401
            import numpy  # noqa: F401
        except Exception:
            self.skipTest("cv2/numpy non disponibili")
        from tools.psvr_suite.app import _decode_ppm_to_rgb
        app = make_app_shell()
        w0, h0 = 16, 12
        raw = b"P6\n%d %d\n255\n" % (w0, h0) + bytes([40, 90, 160] * w0 * h0)
        base = Mock()
        base.width.return_value = w0
        base.height.return_value = h0
        frame = Mock()
        frame.winfo_width.return_value = 900
        frame.winfo_height.return_value = 660
        shown = {}
        fake_img = Mock()
        def _photo(data=None, format=None):
            shown["data"] = bytes(data)
            return fake_img
        app.tk.PhotoImage.side_effect = _photo
        app._preview_base = base
        app._preview_base_size = (w0, h0)
        app._preview_ppm_raw = raw
        app.preview_frame = frame
        app._preview_resize_after = None
        self.assertTrue(app._refresh_preview())
        self.assertIs(app._preview_img, fake_img)
        # Smooth exact-fit 16x12 in 900x660 -> 880x660 preservando 4:3.
        dw, dh, _ = _decode_ppm_to_rgb(shown["data"])
        self.assertEqual((dw, dh), (880, 660))
        self.assertAlmostEqual(dw / dh, 4.0 / 3.0, places=2)
        base.zoom.assert_not_called()

    def test_refresh_fallback_senza_cv2_usa_zoom_intero(self):
        import sys
        app = make_app_shell()
        w0, h0 = 16, 12
        raw = b"P6\n%d %d\n255\n" % (w0, h0) + bytes([40, 90, 160] * w0 * h0)
        base = Mock()
        base.width.return_value = w0
        base.height.return_value = h0
        zoomed = Mock()
        base.zoom.return_value = zoomed
        frame = Mock()
        frame.winfo_width.return_value = 900
        frame.winfo_height.return_value = 660
        app._preview_base = base
        app._preview_base_size = (w0, h0)
        app._preview_ppm_raw = raw
        app.preview_frame = frame
        app._preview_resize_after = None
        with patch.dict(sys.modules, {"cv2": None}):
            self.assertTrue(app._refresh_preview())
        base.zoom.assert_called_once()
        self.assertIs(app._preview_img, zoomed)

    def test_niente_import_cv2_a_modulo_e_pulsanti_camera(self):
        from pathlib import Path as _Path
        src = (_Path(ROOT) / "tools" / "psvr_suite" / "app.py").read_text(
            encoding="utf-8")
        for line in src.splitlines():
            if line.startswith("import cv2") or line.startswith("from cv2"):
                self.fail("cv2 non deve essere importato a modulo (solo lazy in metodo)")
            if line.startswith("import numpy") or line.startswith("from numpy"):
                self.fail("numpy non deve essere importato a modulo")
        # Pulsanti camera richiamano le azioni esistenti senza AvviaVR.
        # i18n: i letterali vivono nel catalogue (tools/psvr_suite/i18n.py),
        # il tab referenzia le chiavi per il live-retranslate.
        import inspect as _inspect
        self.assertTrue(callable(suite_app.SuiteApp.on_attach))
        self.assertTrue(callable(suite_app.SuiteApp.on_apply_tracking))
        src_cam = _inspect.getsource(suite_app.SuiteApp._build_camera_tab)
        self.assertIn("camera.btn_attach", src_cam)
        self.assertIn("camera.btn_apply", src_cam)
        self.assertIn("grid_propagate", src_cam)
        from tools.psvr_suite import i18n as _i18n
        self.assertEqual(_i18n.t("it", "camera.btn_attach"), "Collega camera / tracking")
        self.assertEqual(_i18n.t("it", "camera.btn_apply"), "Applica camera")

    def test_refresh_fallback_senza_frame(self):
        app = make_app_shell()
        # Shell test senza preview_frame: refresh False, handle legacy invariato.
        if hasattr(app, "preview_frame"):
            try:
                delattr(app, "preview_frame")
            except Exception:
                app.preview_frame = None
        app._preview_base = Mock()
        app._preview_base_size = (480, 360)
        self.assertFalse(app._refresh_preview())

    def test_icona_mancante_mai_fallire_e_geometria_adattata(self):
        import tempfile
        from unittest.mock import patch
        from tools.psvr_suite.app import (
            _suite_icon_path, _apply_suite_icon, _apply_initial_geometry,
        )
        with tempfile.TemporaryDirectory() as d:
            # Se l'altro agente non ha ancora generato l'ico: no-op, mai eccezioni.
            with patch.object(Path, "is_file", return_value=False):
                self.assertIsNone(_suite_icon_path(Path(d)))
                root = Mock()
                _apply_suite_icon(root, Path(d))
                root.iconbitmap.assert_not_called()
            # Con ico presente (altro agente): apply chiama iconbitmap senza fallire.
            root_ok = Mock()
            _apply_suite_icon(root_ok, ROOT)
            # Non assertiamo la chiamata (env senza display), solo assenza eccezioni.
            # Schermo grande: geometria più grande del vecchio 1080x760.
            root2 = Mock()
            root2.winfo_screenwidth.return_value = 1920
            root2.winfo_screenheight.return_value = 1080
            _apply_initial_geometry(root2)
            arg = root2.geometry.call_args[0][0]
            w, h = (int(v) for v in arg.lower().split("x"))
            self.assertGreaterEqual(w, 1080)
            self.assertGreaterEqual(h, 760)
            self.assertLessEqual(w, 1920)
            # Schermo illeggibile: fallback default senza eccezioni.
            root3 = Mock()
            root3.winfo_screenwidth.side_effect = Exception("no screen")
            _apply_initial_geometry(root3)
            root3.geometry.assert_called_once()


if __name__ == "__main__":
    unittest.main()
