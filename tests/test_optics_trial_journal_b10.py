"""B10-06 regressions: missing section diagnostics and atomic journal writes."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "tools")]

from psvr_optics import trial


class RequireSectionTests(unittest.TestCase):
    def test_missing_section_value_error(self):
        with self.assertRaises(ValueError):
            trial.update({}, {"distortionMode": "identity", "distortionProfilePath": "p"})

    def test_non_dict_section_value_error(self):
        settings = {"driver_psvrpc": [1, 2]}
        with self.assertRaises(ValueError):
            trial.preserved(settings)

    def test_main_baseline_without_section(self):
        tmp = Path(tempfile.mkdtemp())
        settings = tmp / "settings.json"
        settings.write_text(json.dumps({"other": {}}), encoding="utf-8")
        with self.assertRaises(ValueError):
            trial.main(["--apply", __file__, "--settings", str(settings),
                        "--backup-dir", str(tmp / "backup"), "--validator", __file__])
        self.assertFalse((tmp / "backup").exists())


class JournalAtomicityTests(unittest.TestCase):
    def test_write_journal_round_trip(self):
        tmp = Path(tempfile.mkdtemp())
        journal_path = tmp / "journal.json"
        trial.write_journal(journal_path, {"applied": False})
        self.assertEqual(json.loads(journal_path.read_text(encoding="utf-8")),
                         {"applied": False})
        trial.write_journal(journal_path, {"applied": True})
        self.assertEqual(json.loads(journal_path.read_text(encoding="utf-8")),
                         {"applied": True})

    def test_write_journal_never_partial(self):
        tmp = Path(tempfile.mkdtemp())
        journal_path = tmp / "journal.json"
        journal_path.write_text('{"applied": false}', encoding="utf-8")
        before = journal_path.read_bytes()
        try:
            trial.write_journal(tmp / "no-such-dir" / "journal.json", {"applied": True})
        except OSError:
            pass
        self.assertEqual(journal_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
