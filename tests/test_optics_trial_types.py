"""Regressione B10-01: restore rifiuta tipi non validati senza scrittura."""
import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tools.psvr_optics import trial
from tools.psvr_optics.trial import update


def _settings(mode='profile', path='C:/profiles/medium.json'):
    return {'driver_psvrpc': {'distortionMode': mode, 'distortionProfilePath': path,
                             'positionEnabled': True}}


class OpticalTrialTypeTests(unittest.TestCase):
    def test_update_rejects_non_string_values(self):
        settings = _settings('identity', '')
        snapshot = copy.deepcopy(settings)
        bad_values = [123, None, True, 4.5, ['profile'], {'v': 'profile'}]
        for bad in bad_values:
            with self.assertRaises(TypeError, msg='distortionMode=%r' % (bad,)):
                update(settings, {'distortionMode': bad, 'distortionProfilePath': ''})
            with self.assertRaises(TypeError, msg='profilePath=%r' % (bad,)):
                update(settings, {'distortionMode': 'identity', 'distortionProfilePath': bad})
        self.assertEqual(settings, snapshot)

    def test_update_rejects_wrong_keys_and_mode(self):
        settings = _settings('identity', '')
        with self.assertRaises(ValueError):
            update(settings, {'distortionMode': 'identity'})
        with self.assertRaises(ValueError):
            update(settings, {'distortionMode': 'identity', 'distortionProfilePath': '',
                              'extra': 'x'})
        with self.assertRaises(ValueError):
            update(settings, {'distortionMode': 'raw', 'distortionProfilePath': ''})
        with self.assertRaises(TypeError):
            update(settings, ['identity', ''])
        self.assertEqual(settings, _settings('identity', ''))

    def _run_restore(self, tmp, settings_data, previous_data):
        settings_path = Path(tmp) / 'steamvr.vrsettings'
        settings_path.write_text(json.dumps(settings_data), encoding='utf-8')
        before_bytes = settings_path.read_bytes()
        restore_path = Path(tmp) / 'journal.json'
        restore_path.write_text(json.dumps(previous_data), encoding='utf-8')
        backup_dir = Path(tmp) / 'backup-new'
        with patch.object(trial, 'runtime_stopped', lambda: None):
            trial.main(['--restore', str(restore_path), '--settings', str(settings_path),
                        '--backup-dir', str(backup_dir)])
        return settings_path, backup_dir, before_bytes

    def test_restore_rejects_type_injection_without_write(self):
        injections = [
            {'distortionMode': 123, 'distortionProfilePath': ''},
            {'distortionMode': 'identity', 'distortionProfilePath': None},
            {'distortionMode': True, 'distortionProfilePath': ''},
            {'distortionMode': ['profile'], 'distortionProfilePath': ''},
        ]
        for injected in injections:
            with TemporaryDirectory() as tmp:
                live = _settings('profile', 'C:/profiles/medium.json')
                previous = {'before': injected,
                            'after': {'distortionMode': 'profile',
                                      'distortionProfilePath': 'C:/profiles/medium.json'}}
                settings_path = Path(tmp) / 'steamvr.vrsettings'
                settings_path.write_text(json.dumps(live), encoding='utf-8')
                before_bytes = settings_path.read_bytes()
                restore_path = Path(tmp) / 'journal.json'
                restore_path.write_text(json.dumps(previous), encoding='utf-8')
                backup_dir = Path(tmp) / 'backup-new'
                with patch.object(trial, 'runtime_stopped', lambda: None):
                    with self.assertRaises((TypeError, ValueError)):
                        trial.main(['--restore', str(restore_path),
                                    '--settings', str(settings_path),
                                    '--backup-dir', str(backup_dir)])
                self.assertEqual(settings_path.read_bytes(), before_bytes)
                self.assertFalse(backup_dir.exists())

    def test_restore_rejects_bad_after_and_bad_mode_without_write(self):
        live = _settings('profile', 'C:/profiles/medium.json')
        cases = [
            {'before': {'distortionMode': 'identity', 'distortionProfilePath': ''},
             'after': {'distortionMode': 123, 'distortionProfilePath': 'C:/x'}},
            {'before': {'distortionMode': 'identity', 'distortionProfilePath': ''},
             'after': {'distortionMode': 'profile', 'distortionProfilePath': 'C:/profiles/medium.json',
                       'extra': 1}},
            {'before': {'distortionMode': 'raw', 'distortionProfilePath': ''},
             'after': {'distortionMode': 'profile', 'distortionProfilePath': 'C:/profiles/medium.json'}},
            {'before': {'distortionMode': 'identity', 'distortionProfilePath': ''}},
        ]
        for previous in cases:
            with TemporaryDirectory() as tmp:
                settings_path = Path(tmp) / 'steamvr.vrsettings'
                settings_path.write_text(json.dumps(live), encoding='utf-8')
                before_bytes = settings_path.read_bytes()
                restore_path = Path(tmp) / 'journal.json'
                restore_path.write_text(json.dumps(previous), encoding='utf-8')
                backup_dir = Path(tmp) / 'backup-new'
                with patch.object(trial, 'runtime_stopped', lambda: None):
                    with self.assertRaises((TypeError, ValueError, RuntimeError)):
                        trial.main(['--restore', str(restore_path),
                                    '--settings', str(settings_path),
                                    '--backup-dir', str(backup_dir)])
                self.assertEqual(settings_path.read_bytes(), before_bytes)
                self.assertFalse(backup_dir.exists())

    def test_restore_valid_unchanged(self):
        with TemporaryDirectory() as tmp:
            live = _settings('profile', 'C:/profiles/medium.json')
            previous = {'before': {'distortionMode': 'identity', 'distortionProfilePath': ''},
                        'after': {'distortionMode': 'profile',
                                  'distortionProfilePath': 'C:/profiles/medium.json'}}
            settings_path, backup_dir, before_bytes = self._run_restore(tmp, live, previous)
            loaded = json.loads(settings_path.read_text(encoding='utf-8'))
            self.assertEqual(loaded['driver_psvrpc']['distortionMode'], 'identity')
            self.assertEqual(loaded['driver_psvrpc']['distortionProfilePath'], '')
            self.assertEqual(loaded['driver_psvrpc']['positionEnabled'], True)
            self.assertTrue((backup_dir / 'settings_before.json').is_file())
            journal = json.loads((backup_dir / 'journal.json').read_text(encoding='utf-8'))
            self.assertTrue(journal['applied'])
            self.assertEqual(journal['before'], previous['after'])
            self.assertEqual(journal['after'], previous['before'])
            self.assertEqual(journal['changed_keys'], ['distortionMode', 'distortionProfilePath'])


if __name__ == '__main__':
    unittest.main()
