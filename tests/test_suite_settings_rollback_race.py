"""B1-01 rollback race: file only, no hardware."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.psvr_suite import settings as S


def _seed_vrsettings(section_extra=None, other_apps=None):
    section = {
        "positionEnabled": True,
        "imuExportEnabled": True,
        "displayEnabled": False,
        "displayWidth": 1920,
        "distortionMode": "identity",
        "projectionLeftLeft": -1.0,
    }
    section.update(section_extra or {})
    data = {S.DRIVER_SECTION: section}
    data.update(other_apps or {})
    return data


def _make_store(tmp, section_extra=None, other_apps=None):
    tmp = Path(tmp)
    path = tmp / "steamvr.vrsettings"
    path.write_text(json.dumps(_seed_vrsettings(section_extra, other_apps)), encoding="utf-8")
    backup = tmp / "backup-race"
    store = S.DriverSettingStore(
        path,
        backup,
        running_checker=lambda: set(),
    )
    return store, path


class RollbackRaceTests(unittest.TestCase):
    def test_rollback_race_concurrent_other_section_refuses_and_preserves(self):
        """Concurrent write between snapshot parse and replace: RuntimeError."""
        with tempfile.TemporaryDirectory() as tmp:
            store, path = _make_store(tmp, other_apps={"steamvr": {"x": 1}})
            prefs = S.default_preferences()
            journal = store.apply(prefs)

            # Applied owned values for later check.
            applied_owned = {
                k: copy.deepcopy(journal["after"][k])
                for k in journal["owned_keys"]
                if k not in set(journal.get("after_absent", []))
            }

            real_loads = S.json.loads
            armed = {"active": True}

            def racing_loads(payload, *args, **kwargs):
                data = real_loads(payload, *args, **kwargs)
                if armed["active"] and isinstance(data, dict) and S.DRIVER_SECTION in data:
                    armed["active"] = False
                    # Concurrent writer.
                    current_data = real_loads(store.path.read_bytes().decode("utf-8"))
                    current_data["concurrent_app"] = {"injected": 7}
                    store.path.write_text(
                        json.dumps(current_data, indent=3) + "\n",
                        encoding="utf-8",
                    )
                return data

            with mock.patch.object(S.json, "loads", side_effect=racing_loads):
                with self.assertRaisesRegex(RuntimeError, "concurr"):
                    store.rollback(journal)

            # Concurrent value kept, applied owned kept.
            final = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(final.get("concurrent_app"), {"injected": 7})
            # Owned still applied.
            section = final[S.DRIVER_SECTION]
            for key, value in applied_owned.items():
                self.assertIn(key, section)
                self.assertEqual(section[key], value)
            # Other app intact.
            self.assertEqual(final.get("steamvr"), {"x": 1})

    def test_rollback_preserves_preexisting_other_app_edit_and_restores_only_owned(self):
        """Pre-existing other-app edit: successful rollback preserves it."""
        with tempfile.TemporaryDirectory() as tmp:
            store, path = _make_store(tmp, other_apps={"steamvr": {"x": 1}})
            prefs = S.default_preferences()
            journal = store.apply(prefs)

            # Pre-existing edit.
            pre = json.loads(path.read_text(encoding="utf-8"))
            pre["steamvr"]["kept"] = 2
            pre["extra_app"] = {"a": 1}
            # Non-owned key.
            self.assertEqual(pre[S.DRIVER_SECTION]["displayWidth"], 1920)
            path.write_text(json.dumps(pre, indent=3) + "\n", encoding="utf-8")

            report = store.rollback(journal)

            final = json.loads(path.read_text(encoding="utf-8"))
            # Other apps kept.
            self.assertEqual(final.get("steamvr"), {"x": 1, "kept": 2})
            self.assertEqual(final.get("extra_app"), {"a": 1})
            # Non-owned kept.
            self.assertEqual(final[S.DRIVER_SECTION]["displayWidth"], 1920)
            self.assertEqual(final[S.DRIVER_SECTION]["distortionMode"], "identity")
            # Owned restored.
            before = dict(journal.get("before", {}))
            before_absent = set(journal.get("before_absent", []))
            section = final[S.DRIVER_SECTION]
            for key in journal["owned_keys"]:
                if key in before_absent:
                    self.assertNotIn(key, section)
                elif key in before:
                    self.assertEqual(section[key], before[key])
            self.assertIn("restored_keys", report)


if __name__ == "__main__":
    unittest.main()
