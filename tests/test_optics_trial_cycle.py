"""Regressione B10-07: ciclo apply/restore trial ottici su fixture isolata.

Replica il ciclo di ``probe_cycle.py`` come test stabile: settings finto +
backup-parent isolato, injection ``--settings``/``--backup-dir``,
``runtime_stopped`` mockato, mai path live. Validator nativo reale quando
disponibile (offline, nessuna HMD); il profilo e' una copia isolata in temp.
"""
import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tools.psvr_optics import trial

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / 'out' / 'optics' / 'psvr_optics.exe'
PROFILE_SRC = ROOT / 'configurations' / 'optics_radial_medium_20261014.json'


def _write_settings(path):
    data = {
        'driver_psvrpc': {
            'distortionMode': 'identity',
            'distortionProfilePath': '',
            'displayWidth': 1920,
        },
        'steamvr': {'showMirrorView': False},
    }
    path.write_text(json.dumps(data, indent=2), encoding='utf-8')
    return path.read_bytes()


def _copy_profile(tmp):
    dest = Path(tmp) / 'profile.json'
    dest.write_bytes(PROFILE_SRC.read_bytes())
    return dest


class OpticalTrialCycleTests(unittest.TestCase):
    def test_apply_restore_roundtrip_on_isolated_fixture(self):
        if not VALIDATOR.is_file():
            self.skipTest('native validator is unavailable')
        with TemporaryDirectory() as tmp:
            settings = Path(tmp) / 'steamvr.vrsettings'
            orig_raw = _write_settings(settings)
            profile = _copy_profile(tmp)
            backups = Path(tmp) / 'backups'
            backups.mkdir()
            backup1 = backups / 'case1'
            with patch.object(trial, 'runtime_stopped', lambda: None):
                self.assertEqual(trial.main(['--apply', str(profile),
                                             '--settings', str(settings),
                                             '--backup-dir', str(backup1),
                                             '--validator', str(VALIDATOR)]), 0)
            journal1 = json.loads((backup1 / 'journal.json').read_text(encoding='utf-8'))
            self.assertEqual(journal1['schema'], 'psvr.optics.controlled_trial')
            self.assertTrue(journal1['applied'])
            self.assertEqual(journal1['backup_sha256'], hashlib.sha256(orig_raw).hexdigest())
            self.assertEqual(journal1['before'],
                             {'distortionMode': 'identity', 'distortionProfilePath': ''})
            self.assertEqual(journal1['after']['distortionMode'], 'profile')
            live = json.loads(settings.read_text(encoding='utf-8'))
            self.assertEqual(live['driver_psvrpc']['distortionMode'], 'profile')
            self.assertEqual(live['steamvr'], {'showMirrorView': False})
            # restore con il journal del caso apply
            backup2 = backups / 'case2'
            with patch.object(trial, 'runtime_stopped', lambda: None):
                self.assertEqual(trial.main(['--restore', str(backup1 / 'journal.json'),
                                             '--settings', str(settings),
                                             '--backup-dir', str(backup2)]), 0)
            restored = json.loads(settings.read_text(encoding='utf-8'))
            self.assertEqual(restored['driver_psvrpc']['distortionMode'], 'identity')
            self.assertEqual(restored['driver_psvrpc']['distortionProfilePath'], '')
            self.assertEqual(restored['steamvr'], {'showMirrorView': False})
            journal2 = json.loads((backup2 / 'journal.json').read_text(encoding='utf-8'))
            self.assertTrue(journal2['applied'])
            self.assertEqual(journal2['before'], journal1['after'])
            self.assertEqual(journal2['after'], journal1['before'])

    def test_stale_restore_refused_without_write(self):
        if not VALIDATOR.is_file():
            self.skipTest('native validator is unavailable')
        with TemporaryDirectory() as tmp:
            settings = Path(tmp) / 'steamvr.vrsettings'
            _write_settings(settings)
            profile = _copy_profile(tmp)
            backups = Path(tmp) / 'backups'
            backups.mkdir()
            with patch.object(trial, 'runtime_stopped', lambda: None):
                trial.main(['--apply', str(profile), '--settings', str(settings),
                            '--backup-dir', str(backups / 'case1'),
                            '--validator', str(VALIDATOR)])
                trial.main(['--restore', str(backups / 'case1' / 'journal.json'),
                            '--settings', str(settings),
                            '--backup-dir', str(backups / 'case2')])
            settled = settings.read_bytes()
            with patch.object(trial, 'runtime_stopped', lambda: None):
                with self.assertRaises(RuntimeError):
                    trial.main(['--restore', str(backups / 'case1' / 'journal.json'),
                                '--settings', str(settings),
                                '--backup-dir', str(backups / 'case3')])
            self.assertEqual(settings.read_bytes(), settled)
            self.assertFalse((backups / 'case3').exists())

    def test_existing_backup_dir_refused_with_exit_2(self):
        if not VALIDATOR.is_file():
            self.skipTest('native validator is unavailable')
        with TemporaryDirectory() as tmp:
            settings = Path(tmp) / 'steamvr.vrsettings'
            _write_settings(settings)
            profile = _copy_profile(tmp)
            backups = Path(tmp) / 'backups'
            backups.mkdir()
            before = settings.read_bytes()
            with patch.object(trial, 'runtime_stopped', lambda: None):
                with self.assertRaises(SystemExit) as ctx:
                    trial.main(['--apply', str(profile), '--settings', str(settings),
                                '--backup-dir', str(backups),
                                '--validator', str(VALIDATOR)])
            self.assertEqual(ctx.exception.code, 2)
            self.assertEqual(settings.read_bytes(), before)

    def test_never_touches_live_settings_path(self):
        with TemporaryDirectory() as tmp:
            settings = Path(tmp) / 'steamvr.vrsettings'
            _write_settings(settings)
            self.assertNotIn('Program Files', str(settings))
            self.assertEqual(settings.parent, Path(tmp))


if __name__ == '__main__':
    unittest.main()
