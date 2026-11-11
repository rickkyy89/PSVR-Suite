"""Offline source-module and actual selection binding tests; no Tk/USB/runtime."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.psvr_suite import app as ui, i18n, settings, tracking
from tools.psvr_suite.camera_settings import (module_for_source, validate_camera_profiles,
                                              resolve_camera_preferences)
from tools.psvr_suite.camera_settings.ps3 import Ps3Module
from tools.psvr_suite.camera_settings.ps4 import Ps4Module


class FakeVar:
    def __init__(self, value=""):
        self.value = value
        self.writes = []

    def get(self):
        return self.value

    def set(self, value):
        self.value = value
        self.writes.append(value)


class FakeWidget:
    def __init__(self, parent=None, *args, **kwargs):
        self.parent = parent
        self.options = dict(kwargs)
        self.bindings = {}
        self.visible = True

    def configure(self, **kwargs):
        self.options.update(kwargs)

    def bind(self, event, callback):
        self.bindings[event] = callback

    def select(self, value):
        # Exercise the real event registration, not a direct app callback.
        self.options["textvariable"].set(value)
        self.bindings["<<ComboboxSelected>>"](None)

    def grid(self, *args, **kwargs):
        self.visible = True

    def grid_remove(self):
        self.visible = False

    def pack(self, *args, **kwargs):
        pass

    def columnconfigure(self, *args, **kwargs):
        pass

    rowconfigure = columnconfigure
    grid_propagate = columnconfigure
    add = columnconfigure
    tab = columnconfigure
    yview = columnconfigure
    set = columnconfigure
    itemconfigure = columnconfigure

    def create_window(self, *args, **kwargs):
        return 1


def make_app(root):
    app = ui.SuiteApp.__new__(ui.SuiteApp)
    app.root_path = root
    app.user_path = root / ui.USER_PREFS_REL
    app.root = Mock()
    app.lang = "it"
    app.tk = types.SimpleNamespace(StringVar=FakeVar, IntVar=FakeVar,
                                   BooleanVar=FakeVar, Scale=FakeWidget, Canvas=FakeWidget,
                                   Label=FakeWidget)
    app.ttk = types.SimpleNamespace(**{key: FakeWidget for key in
        ("Frame", "Label", "Entry", "Button", "Scrollbar", "Combobox",
         "Notebook", "OptionMenu", "Radiobutton", "Checkbutton")})
    app._static, app._tab_order = [], []
    app._last_status = ({}, {})
    app._settings = settings
    app.model = ui.AppModel()
    app.runtime, app.tracking, app.serial = Mock(), Mock(), Mock()
    app.closing = False
    app._pending = 0
    app._pending_lock = threading.Lock()
    app._build_widgets()
    app._apply_prefs_to_widgets(app.model.to_prefs())
    return app


def profile_tree(root, controls=None):
    profile = json.loads((ROOT / "configurations/ps4_suite_source.json").read_text())
    paths = ("configurations/ps4_suite_source.json", profile["calibration_path"],
             profile["rig_path"])
    for relative in paths:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    if controls is not None:
        profile["controls"] = controls
        (root / paths[0]).write_text(json.dumps(profile), encoding="utf-8")
    return paths


def fingerprints(root, paths):
    return {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in paths}


class CameraSettingsUiTests(unittest.TestCase):
    def test_selection_roundtrip_preserves_unsaved_ps3_draft_and_never_applies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = profile_tree(root)
            before_files = fingerprints(root, paths)
            app = make_app(root)
            app.var_exposure.set(29)
            app.var_gain.set(6)
            app.var_saturation.set("091")  # exact unsaved text, not normalized
            bindings = module_for_source("pseye").legacy_bindings
            before_vars = {k: (getattr(app, v).get(), list(getattr(app, v).writes))
                           for k, v in bindings.items()}
            before_model = app.model.to_prefs()
            for _ in range(3):
                app.camera_source_menu.select("PS4 Camera stereo")
                self.assertFalse(app.camera_legacy_fields.visible)
                displayed = app.camera_source_summary.options["text"]
                self.assertIn("TARGET exposure: -7", displayed)
                self.assertIn("TARGET gain: 8", displayed)
                self.assertIn("TARGET brightness: 1", displayed)
                self.assertIn("640×400 @ 60", displayed)
                self.assertIn("NON confermati", displayed)
                app.camera_source_menu.select("PS3 Eye")
                self.assertTrue(app.camera_legacy_fields.visible)
            self.assertEqual(before_vars, {k: (getattr(app, v).get(), list(getattr(app, v).writes))
                                          for k, v in bindings.items()})
            self.assertEqual(before_model, app.model.to_prefs())
            self.assertEqual(before_files, fingerprints(root, paths))
            self.assertFalse((root / ui.USER_PREFS_REL).exists())
            self.assertEqual(app.runtime.mock_calls + app.tracking.mock_calls + app.serial.mock_calls, [])
            # Hidden PS3 edits, even valid ones, cannot silently become PS4's
            # persisted schema bridge. Keep the last validated load/save copy.
            app.camera_source_menu.select("PS4 Camera stereo")
            prefs, errors = app.collect_prefs()
            self.assertEqual(errors, [])
            self.assertEqual((prefs["camera_source"], prefs["exposure"], prefs["gain"]),
                             ("ps4-stereo", 13, 0))

    def test_module_configs_match_worker_projection_not_ps3_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_tree(root)
            requested = tracking.normalize_prefs(dict(settings.default_preferences(),
                                                       camera_source="ps4-stereo"))
            original = copy.deepcopy(requested)
            effective, profile = tracking._source_preferences(root, requested)
            metadata = tracking._camera_metadata(requested, effective, profile)["effective_camera"]
            view = ui.camera_settings_view(root, "PS4 Camera stereo", requested=requested)
            for key, value in view["effective_config"].items():
                self.assertEqual(value, metadata[key], key)
            self.assertEqual(original, requested)
            self.assertIsNone(view["effective_config"]["imu_rig"])
            for lang in ("it", "en"):
                rendered = ui.camera_settings_text(view, lang)
                self.assertIn(profile["calibration_path"], rendered)
                self.assertIn(profile["rig_path"], rendered)
                self.assertIn("Manual", rendered)
                self.assertIn("TARGET", rendered)

    def test_isolation_both_directions_and_distinct_profiles(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            root_a, root_b = Path(first), Path(second)
            profile_tree(root_a, {"exposure": -7, "gain": 8, "brightness": 1})
            profile_tree(root_b, {"exposure": -5, "gain": 3, "brightness": 2})
            legacy = settings.default_preferences()
            legacy_before = copy.deepcopy(legacy)
            ps3 = module_for_source("pseye")
            self.assertIsInstance(ps3, Ps3Module)
            self.assertIsInstance(module_for_source("ps4-stereo"), Ps4Module)
            a = module_for_source("ps4-stereo").view(root_a, legacy, {})
            legacy["exposure"], legacy["gain"] = 77, 11
            again = module_for_source("ps4-stereo").view(root_a, legacy, {})
            self.assertEqual(a, again)  # PS3 edits cannot leak to PS4
            ps3_before = ps3.view(root_a, legacy, {})
            b = module_for_source("ps4-stereo").view(root_b, legacy, {})
            self.assertNotEqual(a["requested"], b["requested"])
            b["requested"]["exposure"] = -2  # detached snapshot, not an edit API
            b["effective_config"]["gain"] = 1
            self.assertEqual(ps3_before, ps3.view(root_b, legacy, {}))
            self.assertEqual(legacy["intrinsicsPath"], legacy_before["intrinsicsPath"])
            self.assertEqual(legacy["rigPath"], legacy_before["rigPath"])
            self.assertEqual(module_for_source("ps4-stereo").view(root_b, legacy, {})
                             ["requested"]["exposure"], -5)
            self.assertFalse(module_for_source("ps4-stereo").editable)

    def test_legacy_projection_is_non_destructive_even_invalid_unsaved_text(self):
        legacy = {"exposure": "not-yet-valid", "gain": "06", "rigPath": "custom.json"}
        original = copy.deepcopy(legacy)
        view = module_for_source("pseye").view(ROOT, legacy, {})
        self.assertEqual(view["requested"], legacy)
        view["requested"]["gain"] = 12
        self.assertEqual(legacy, original)
        with self.assertRaises(ValueError):
            module_for_source("pseye").validate_controls({"exposure": -7})

    def test_ps4_validation_requires_measured_manual_range(self):
        ps4 = module_for_source("ps4-stereo")
        for bad_range in (None, {}, {"exposure": {"minimum": -13, "maximum": -1}}):
            with self.assertRaises(ValueError):
                ps4.validate_controls({"exposure": -7}, bad_range)
        info = {"minimum": -13, "maximum": -1, "step": 1, "capabilities": 2}
        self.assertEqual(ps4.validate_controls({"exposure": -7}, {"exposure": info}),
                         {"exposure": -7})
        for info_override, value in (({"capabilities": 1}, -7), ({"step": 0}, -7),
                                     ({"step": 2}, -6), ({}, 13), ({}, True)):
            with self.assertRaises(ValueError):
                ps4.validate_controls({"exposure": value},
                                      {"exposure": dict(info, **info_override)})
        self.assertEqual(module_for_source("pseye").validate_controls({"exposure": 13}),
                         {"exposure": 13})

    def test_profile_error_unknown_and_generic_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = make_app(root)
            app.camera_source_menu.select("PS4 Camera stereo")
            self.assertFalse(app.camera_legacy_fields.visible)
            self.assertIn("Nessun TARGET", app.camera_source_summary.options["text"])
            paths = profile_tree(root)
            (root / paths[0]).write_text("{}", encoding="utf-8")
            app.camera_source_menu.select("PS4 Camera stereo")
            self.assertIn("Nessun TARGET", app.camera_source_summary.options["text"])
            for source in ("unknown-camera", "OpenCV DirectShow"):
                app.camera_source_menu.select(source)
                self.assertFalse(app.camera_legacy_fields.visible)
                self.assertNotIn("TARGET exposure", app.camera_source_summary.options["text"])
                with self.assertRaises(ValueError):
                    module_for_source(ui.camera_source_from_choice(source)).validate_controls(
                        {"exposure": 13})
            app.camera_source_menu.select("PS3 Eye")
            self.assertTrue(app.camera_legacy_fields.visible)

    def test_readback_only_current_status_not_targets_metadata_or_other_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_tree(root)
            controls = {"readback": {"exposure": -6, "gain": None, "brightness": 1},
                        "readback_confirmed": {"exposure": False, "gain": True,
                                               "brightness": True}}
            status = {"running": True, "state": "running", "camera_source": "ps4-stereo",
                      "prefs": {"camera_source": "pseye"},
                      "camera_controls": controls}
            view = ui.camera_settings_view(root, "ps4-stereo", status)
            self.assertIsNone(view["camera_controls"])
            status["prefs"]["camera_source"] = "ps4-stereo"
            view = ui.camera_settings_view(root, "ps4-stereo", status)
            rendered = ui.camera_settings_text(view, "en")
            self.assertIn("exposure: read -6; backend acquisition confirmation: no", rendered)
            self.assertIn("gain: read unknown; backend acquisition confirmation: unknown", rendered)
            self.assertIn("brightness: read 1; backend acquisition confirmation: yes", rendered)
            self.assertIn("TARGET exposure: -7", rendered)
            controls["readback"]["exposure"] = -2
            self.assertEqual(view["camera_controls"]["readback"]["exposure"], -6)
            for changes in ({"running": False}, {"state": "stopped"},
                            {"camera_source": "pseye"}, {"camera_source": None},
                            {"camera_controls": None}):
                self.assertIsNone(ui.camera_settings_view(root, "ps4-stereo", dict(status, **changes))
                                  ["camera_controls"])
            metadata_only = {"running": True, "state": "running", "prefs": status["prefs"],
                             "effective_camera": controls, "source_profile": controls}
            self.assertIsNone(ui.camera_settings_view(root, "ps4-stereo", metadata_only)
                              ["camera_controls"])

    def test_i18n_refresh_does_not_reset_source_or_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_tree(root)
            app = make_app(root)
            app.var_exposure.set(31)
            app.camera_source_menu.select("PS4 Camera stereo")
            app.set_language("en", save=False)
            self.assertIn("PS4 · read-only", app.camera_source_summary.options["text"])
            app.set_language("it", save=False)
            self.assertIn("PS4 · sola lettura", app.camera_source_summary.options["text"])
            self.assertEqual(app.var_exposure.get(), 31)
            self.assertEqual(app.var_camera_source.get(), "PS4 Camera stereo")
            self.assertEqual(i18n.missing_translations(), {})

    def test_status_event_updates_only_current_observations_and_selection_reloads_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_tree(root)
            app = make_app(root)
            app.camera_source_menu.select("PS4 Camera stereo")
            status = {"running": True, "state": "running", "camera_source": "ps4-stereo",
                      "prefs": {"camera_source": "ps4-stereo"},
                      "camera_controls": {"readback": {"brightness": 1},
                                           "readback_confirmed": {"brightness": True}}}
            app._handle_event("status", {"runtime": {}, "tracking": status})
            self.assertIn("brightness: letto 1", app.camera_source_summary.options["text"])
            status["prefs"]["camera_source"] = "pseye"
            app._handle_event("status", {"runtime": {}, "tracking": status})
            self.assertIn("NON confermati", app.camera_source_summary.options["text"])
            path = root / "configurations/ps4_suite_source.json"
            profile = json.loads(path.read_text())
            profile["controls"]["exposure"] = -5
            path.write_text(json.dumps(profile), encoding="utf-8")
            app.camera_source_menu.select("PS4 Camera stereo")
            self.assertIn("TARGET exposure: -5", app.camera_source_summary.options["text"])

    def test_incomplete_ps3_draft_does_not_block_ps4_attach_apply_or_safe_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_tree(root)
            app = make_app(root)
            saved = dict(settings.default_preferences(), exposure=27, gain=5,
                         saturation_min=102, intrinsicsPath="custom-ps3-calibration.json")
            settings.save_preferences(app.user_path, saved)
            app._apply_prefs_to_widgets(saved)
            app.var_saturation.set("")
            app.camera_source_menu.select("PS4 Camera stereo")
            app._run_serial = Mock(return_value=True)  # capture tasks; no real worker
            app.tracking.status.return_value = {"running": False}
            app.tracking.start.return_value = True
            app.tracking.update_settings.return_value = True
            for action, backend_call in ((app.on_attach, app.tracking.start),
                                         (app.on_apply_tracking, app.tracking.update_settings)):
                action()
                task = app._run_serial.call_args.args[1]
                self.assertTrue(task())  # MOCK backend only; no device/runtime
                submitted = backend_call.call_args.args[0]
                self.assertEqual(submitted["camera_source"], "ps4-stereo")
                for key in Ps3Module.legacy_keys:
                    self.assertEqual(submitted[key], saved[key], key)
            self.assertEqual(app.runtime.mock_calls, [])
            app.on_save()
            persisted = settings.load_preferences(app.user_path)
            self.assertEqual(persisted["camera_source"], "ps4-stereo")
            for key in Ps3Module.legacy_keys:
                self.assertEqual(persisted[key], saved[key], key)
            self.assertIn("NON è stato salvato", app.var_msg.get())
            app.camera_source_menu.select("PS3 Eye")
            self.assertEqual(app.var_saturation.get(), "")
            prefs, errors = app.collect_prefs()
            self.assertIsNone(prefs)
            self.assertTrue(any("saturation_min" in error for error in errors))
            before = app.user_path.read_bytes()
            app.on_save()
            self.assertEqual(app.user_path.read_bytes(), before)

    def test_invalid_hidden_controls_format_and_paths_use_loaded_snapshot_not_defaults(self):
        cases = (("var_exposure", -7, "exposure"), ("var_gain", 64, "gain"),
                 ("var_fps", "bad", "fps"), ("var_width", "", "width"),
                 ("var_height", "-1", "height"), ("var_intrinsics", "", "intrinsicsPath"),
                 ("var_rig", "", "rigPath"), ("var_opening", "2", "opening_size"))
        for attr, invalid, key in cases:
            with self.subTest(field=key), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                profile_tree(root)
                app = make_app(root)
                loaded = dict(settings.default_preferences(), exposure=31, gain=7,
                              saturation_min=113, rigPath="loaded-ps3-rig.json")
                app._apply_prefs_to_widgets(loaded)
                getattr(app, attr).set(invalid)
                for selected in ("PS4 Camera stereo", "OpenCV DirectShow"):
                    app.camera_source_menu.select(selected)
                    prefs, errors = app.collect_prefs()
                    if selected == "OpenCV DirectShow":
                        self.assertIsNone(prefs)
                        self.assertTrue(any("Configurazione dedicata" in error for error in errors))
                        continue
                    self.assertEqual(errors, [])
                    for legacy_key in Ps3Module.legacy_keys:
                        self.assertEqual(prefs[legacy_key], loaded[legacy_key], legacy_key)
                    self.assertFalse(app.camera_legacy_fields.visible)
                self.assertFalse(app.user_path.exists())
                app.camera_source_menu.select("PS3 Eye")
                self.assertEqual(getattr(app, attr).get(), invalid)
                _, errors = app.collect_prefs()
                self.assertTrue(any(key in error for error in errors), errors)

    def test_apply_is_not_commit_only_explicit_valid_ps3_save_updates_bridge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_tree(root)
            app = make_app(root)
            app.var_exposure.set(29)
            app.var_gain.set(6)
            _, errors = app.collect_prefs()  # model changes, not loaded/saved snapshot
            self.assertEqual(errors, [])
            app.camera_source_menu.select("PS4 Camera stereo")
            prefs, errors = app.collect_prefs()
            self.assertEqual(errors, [])
            self.assertEqual((prefs["exposure"], prefs["gain"]), (13, 0))
            app.on_save()
            self.assertEqual(settings.load_preferences(app.user_path)["exposure"], 13)
            app.camera_source_menu.select("PS3 Eye")
            self.assertEqual((app.var_exposure.get(), app.var_gain.get()), (29, 6))
            app.on_save()  # explicit PS3 valid save commits this camera's draft
            self.assertEqual(settings.load_preferences(app.user_path)["exposure"], 29)
            app.var_exposure.set(-7)
            app.camera_source_menu.select("PS4 Camera stereo")
            prefs, errors = app.collect_prefs()
            self.assertEqual(errors, [])
            self.assertEqual((prefs["exposure"], prefs["gain"]), (29, 6))

    def test_non_ps3_save_preserves_existing_destination_and_fails_closed_if_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_tree(root)
            app = make_app(root)
            app.var_saturation.set("")
            app.camera_source_menu.select("PS4 Camera stereo")
            # A separate explicit writer updated destination after the GUI load.
            destination = dict(settings.default_preferences(), exposure=42, gain=9,
                               saturation_min=114, rigPath="already-saved-ps3-rig.json")
            settings.save_preferences(app.user_path, destination)
            app.on_save()
            persisted = settings.load_preferences(app.user_path)
            for key in Ps3Module.legacy_keys:
                self.assertEqual(persisted[key], destination[key], key)
            app.user_path.write_text("{invalid", encoding="utf-8")
            before = app.user_path.read_bytes()
            app.on_save()
            self.assertEqual(before, app.user_path.read_bytes())
            self.assertIn("Salvataggio non riuscito", app.var_msg.get())
            self.assertEqual(app.var_saturation.get(), "")

    def test_ps3_save_merges_external_profiles_without_committing_hidden_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_tree(root)
            app = make_app(root)
            loaded = dict(settings.default_preferences(), camera_profiles={
                "ps4-stereo": {"controls": {"exposure": -7, "gain": 8, "brightness": 1}}})
            settings.save_preferences(app.user_path, loaded)
            app._apply_prefs_to_widgets(loaded)
            initial_snapshot = copy.deepcopy(app._ps3_committed)
            app.var_exposure.set(29)
            app.var_gain.set(6)
            app.var_saturation.set("091")
            before_switch = app.user_path.read_bytes()
            app.camera_source_menu.select("PS4 Camera stereo")
            hidden = app._source_editors["ps4-stereo"]
            hidden["vars"]["exposure"].set("unfinished PS4 draft")
            app.camera_source_menu.select("PS3 Eye")
            self.assertEqual(app.user_path.read_bytes(), before_switch)
            self.assertEqual(app._ps3_committed, initial_snapshot)
            self.assertEqual(app.runtime.mock_calls + app.tracking.mock_calls + app.serial.mock_calls, [])

            # Deterministic separate writer, completed AFTER GUI load/switch and
            # BEFORE explicit Save's reread. No threads, devices or real runtime.
            external = copy.deepcopy(loaded)
            external["camera_profiles"] = {
                "ps4-stereo": {"controls": {"exposure": -6, "gain": 3, "brightness": 2}},
                "opencv-dshow": {"width": 800, "height": 600, "fps": 30,
                    "intrinsicsPath": "writer-calibration.json", "rigPath": "writer-rig.json",
                    "imu_rig": "writer-imu.json"}}
            settings.save_preferences(app.user_path, external)
            ps3_literals = {key: (getattr(app, attr).get(), list(getattr(app, attr).writes))
                            for key, attr in Ps3Module.legacy_bindings.items()}
            app.on_save()
            saved = settings.load_preferences(app.user_path)
            self.assertEqual(saved["camera_profiles"], external["camera_profiles"])
            self.assertEqual(app.model.to_prefs(), saved)
            self.assertEqual(app._camera_profiles_committed, external["camera_profiles"])
            self.assertEqual((saved["exposure"], saved["gain"], saved["saturation_min"]), (29, 6, 91))
            self.assertEqual(app._ps3_committed,
                             {key: saved[key] for key in Ps3Module.legacy_keys})
            self.assertEqual(ps3_literals, {key: (getattr(app, attr).get(), list(getattr(app, attr).writes))
                                           for key, attr in Ps3Module.legacy_bindings.items()})
            self.assertEqual(hidden["vars"]["exposure"].get(), "unfinished PS4 draft")
            self.assertIn("Preferenze salvate", app.var_msg.get())

    def test_ps3_save_does_not_resurrect_profiles_removed_by_external_writer(self):
        for keep_empty_carrier in (False, True):
            with self.subTest(empty_carrier=keep_empty_carrier), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                profile_tree(root)
                app = make_app(root)
                loaded = dict(settings.default_preferences(), camera_profiles={
                    "ps4-stereo": {"controls": {"exposure": -7, "gain": 8, "brightness": 1}}})
                settings.save_preferences(app.user_path, loaded)
                app._apply_prefs_to_widgets(loaded)
                app.var_saturation.set("091")
                external = settings.default_preferences()
                if keep_empty_carrier:
                    external["camera_profiles"] = {}
                settings.save_preferences(app.user_path, external)
                before_switch = app.user_path.read_bytes()
                before_snapshot = copy.deepcopy(app._ps3_committed)
                app.camera_source_menu.select("PS4 Camera stereo")
                app.camera_source_menu.select("PS3 Eye")
                self.assertEqual(before_switch, app.user_path.read_bytes())
                self.assertEqual(before_snapshot, app._ps3_committed)
                app.on_save()
                saved = settings.load_preferences(app.user_path)
                self.assertEqual(saved.get("camera_profiles", {}), {})
                self.assertEqual("camera_profiles" in saved, keep_empty_carrier)
                self.assertEqual(app.model.to_prefs(), saved)
                self.assertEqual(app._camera_profiles_committed, {})
                self.assertEqual(app.var_saturation.get(), "091")
                self.assertEqual(app._ps3_committed["saturation_min"], 91)

    def test_missing_validated_legacy_snapshot_fails_explicitly_without_guessing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_tree(root)
            app = make_app(root)
            app._ps3_committed = None
            app.camera_source_menu.select("PS4 Camera stereo")
            prefs, errors = app.collect_prefs()
            self.assertIsNone(prefs)
            self.assertIn("Copia PS3 validata", errors[0])
            app._run_serial = Mock()
            app.on_attach()
            app._run_serial.assert_not_called()
            app.on_save()
            self.assertFalse(app.user_path.exists())

    def test_hidden_tk_intvar_read_error_is_never_consulted_for_ps4(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_tree(root)
            app = make_app(root)
            raw_gain = FakeVar("unfinished-int")
            # Tk IntVar.get itself can raise before AppModel sees the draft.
            gain = Mock(wraps=raw_gain)
            gain.get.side_effect = ValueError("gain: invalid literal")
            app.var_gain = gain
            app.camera_source_menu.select("PS4 Camera stereo")
            prefs, errors = app.collect_prefs()
            self.assertEqual(errors, [])
            self.assertEqual(prefs["gain"], 0)
            gain.get.assert_not_called()
            gain.set.assert_not_called()
            app.camera_source_menu.select("PS3 Eye")
            _, errors = app.collect_prefs()
            self.assertTrue(any("gain" in error for error in errors))
            self.assertEqual(raw_gain.value, "unfinished-int")

    def test_optional_profiles_legacy_roundtrip_and_strict_source_validation(self):
        legacy = settings.default_preferences()
        self.assertNotIn("camera_profiles", settings.validate_preferences(legacy))
        profiles = {"ps4-stereo": {"controls": {"exposure": -7, "gain": 8, "brightness": 1}}}
        prefs = dict(legacy, camera_profiles=profiles)
        checked = ui.AppModel.validate(prefs)
        self.assertEqual(checked["camera_profiles"], profiles)
        self.assertEqual(settings.driver_keys_for(prefs), settings.driver_keys_for(legacy))
        checked["camera_profiles"]["ps4-stereo"]["controls"]["gain"] = 0
        self.assertEqual(profiles["ps4-stereo"]["controls"]["gain"], 8)
        for invalid in ({"pseye": {"controls": {"gain": 2}}},
                        {"ps4-stereo": {"controls": {"exposure": 13}}},
                        {"ps4-stereo": {"intrinsicsPath": "ps3.json"}},
                        {"unknown": {}}, {"opencv-dshow": {}}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_camera_profiles(invalid)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prefs.json"
            settings.save_preferences(path, prefs)
            self.assertEqual(settings.load_preferences(path), prefs)

    @staticmethod
    def measured_status():
        ranges = {
            "exposure": {"minimum": -13, "maximum": -1, "step": 1, "capabilities": 2},
            "gain": {"minimum": 0, "maximum": 8, "step": 1, "capabilities": 2},
            "brightness": {"minimum": 0, "maximum": 8, "step": 1, "capabilities": 2}}
        return {"running": True, "state": "running", "camera_source": "ps4-stereo",
                "prefs": {"camera_source": "ps4-stereo"},
                "camera_settings_capabilities": {"source": "ps4-stereo",
                    "preference_key": "camera_profiles", "apply_mode": "reopen", "controls": ranges}}

    def test_measured_capability_edit_apply_and_independent_dirty_drafts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = profile_tree(root)
            accepted_before = fingerprints(root, paths)
            app = make_app(root)
            app._last_status = ({}, self.measured_status())
            app.var_saturation.set("")
            app.camera_source_menu.select("PS4 Camera stereo")
            ps4 = app._source_editors["ps4-stereo"]
            for entry in ps4["entries"].values():
                self.assertEqual(entry.options["state"], "normal")
            for name, value in {"exposure": "-6", "gain": "4", "brightness": "2"}.items():
                ps4["vars"][name].set(value)
            app._run_serial = Mock(return_value=True)
            app.on_apply_tracking()
            app.tracking.update_settings.return_value = True
            self.assertTrue(app._run_serial.call_args.args[1]())
            payload = app.tracking.update_settings.call_args.args[0]
            self.assertEqual(payload["camera_profiles"]["ps4-stereo"]["controls"],
                             {"exposure": -6, "gain": 4, "brightness": 2})
            self.assertEqual(payload["exposure"], 13)  # legacy schema bridge, NOT PS4 units
            effective, profile = resolve_camera_preferences(root, payload)
            self.assertEqual(effective["exposure"], -6)
            self.assertEqual(profile["controls"]["brightness"], 2)
            app.camera_source_menu.select("PS3 Eye")
            self.assertEqual(app.var_saturation.get(), "")
            app.var_saturation.set("91")
            app.on_save()  # must not save the hidden, merely-applied PS4 draft
            self.assertNotIn("ps4-stereo", settings.load_preferences(app.user_path)
                             .get("camera_profiles", {}))
            app.camera_source_menu.select("PS4 Camera stereo")
            self.assertEqual(ps4["vars"]["exposure"].get(), "-6")
            app.on_save()
            persisted = settings.load_preferences(app.user_path)
            self.assertEqual(persisted["camera_profiles"]["ps4-stereo"]["controls"]["gain"], 4)
            self.assertEqual(persisted["saturation_min"], 91)
            ps4["vars"]["exposure"].set("unfinished")
            app.camera_source_menu.select("PS3 Eye")
            _, errors = app.collect_prefs()
            self.assertEqual(errors, [])
            app.camera_source_menu.select("PS4 Camera stereo")
            self.assertEqual(ps4["vars"]["exposure"].get(), "unfinished")
            _, errors = app.collect_prefs()
            self.assertTrue(any("exposure" in error for error in errors))
            self.assertEqual(accepted_before, fingerprints(root, paths))

    def test_no_edit_contract_stale_wrong_device_and_invalid_steps_fail_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_tree(root)
            app = make_app(root)
            app.camera_source_menu.select("PS4 Camera stereo")
            state = app._source_editors["ps4-stereo"]
            self.assertTrue(all(e.options["state"] == "disabled" for e in state["entries"].values()))
            state["vars"]["gain"].set("3")  # a retained draft cannot bypass disabled capability
            _, errors = app.collect_prefs()
            self.assertTrue(errors)
            status = self.measured_status()
            status["camera_settings_capabilities"]["controls"]["gain"]["step"] = 2
            app._last_status = ({}, status)
            app._refresh_camera_settings(reload_profile=False)
            _, errors = app.collect_prefs()
            self.assertTrue(errors)  # 3 not on native 0,2,4,... step
            for changes in ({"camera_source": "pseye"}, {"running": False}):
                app._last_status = ({}, dict(status, **changes))
                app._refresh_camera_settings(reload_profile=False)
                self.assertEqual(state["entries"]["gain"].options["state"], "disabled")
                _, errors = app.collect_prefs()
                self.assertTrue(errors)

    def test_generic_configuration_is_explicit_source_owned_and_backend_acknowledged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_tree(root)
            app = make_app(root)
            app.tracking.camera_profile_contract = 1
            app.camera_source_menu.select("OpenCV DirectShow")
            state = app._source_editors["opencv-dshow"]
            config = {"width": 800, "height": 600, "fps": 30,
                      "intrinsicsPath": "generic-calibration.json", "rigPath": "generic-rig.json",
                      "imu_rig": "generic-imu-rig.json"}
            for name, value in config.items():
                state["vars"][name].set(str(value))
                self.assertEqual(state["entries"][name].options["state"], "normal")
            prefs, errors = app.collect_prefs()
            self.assertEqual(errors, [])
            effective, profile = resolve_camera_preferences(root, prefs)
            self.assertIsNone(profile)
            for name, value in config.items():
                self.assertEqual(effective[name], value)
            self.assertEqual(app.var_intrinsics.get(), settings.default_preferences()["intrinsicsPath"])


if __name__ == "__main__":
    unittest.main()
