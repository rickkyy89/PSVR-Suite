"""The two-LED experiment is explicit, persisted, and reaches both consumers."""
import tempfile
from pathlib import Path
import unittest

from tools.psvr_suite import settings, tracking
from tools.psvr_suite.app import AppModel


class AngularOptionsTests(unittest.TestCase):
    def test_default_selects_driver_centering_and_at_least_three_leds(self):
        prefs = settings.default_preferences()
        self.assertFalse(prefs['two_led_experimental'])
        self.assertTrue(settings.driver_keys_for(prefs)['positionAngularCenterEnabled'])
        self.assertFalse(tracking.normalize_prefs(prefs)['two_led_experimental'])

    def test_experiment_explicitly_disables_angular_centering(self):
        prefs = settings.default_preferences()
        prefs['two_led_experimental'] = True
        self.assertFalse(settings.driver_keys_for(prefs)['positionAngularCenterEnabled'])
        self.assertTrue(tracking.normalize_prefs(prefs)['two_led_experimental'])
        imported = settings.preferences_from_driver(source=settings.driver_keys_for(prefs))
        self.assertTrue(imported['two_led_experimental'])

    def test_native_three_dof_never_enables_camera_center_policy(self):
        prefs = settings.default_preferences()
        prefs['tracking_mode'] = '3dof'
        self.assertFalse(settings.driver_keys_for(prefs)['positionAngularCenterEnabled'])

    def test_load_old_preferences_and_persist_new_option(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'prefs.json'
            path.write_text('{"tracking_mode":"6dof"}', encoding='utf-8')
            prefs = settings.load_preferences(path)
            self.assertFalse(prefs['two_led_experimental'])
            prefs['two_led_experimental'] = True
            settings.save_preferences(path, prefs)
            self.assertTrue(settings.load_preferences(path)['two_led_experimental'])

    def test_backend_and_worker_reject_non_boolean_experiment(self):
        for value in (1, 0, None, 'yes'):
            prefs = settings.default_preferences()
            prefs['two_led_experimental'] = value
            with self.assertRaises(ValueError):
                settings.validate_preferences(prefs)
            with self.assertRaises(ValueError):
                tracking.normalize_prefs(prefs)

    def test_app_types_checkbox_before_strict_backend(self):
        prefs = settings.default_preferences()
        prefs['two_led_experimental'] = 'true'
        self.assertTrue(AppModel.validate(prefs)['two_led_experimental'])


if __name__ == '__main__':
    unittest.main()
