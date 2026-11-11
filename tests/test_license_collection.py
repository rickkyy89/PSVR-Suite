"""Self-contained license-collection tests: no network, no live repo writes."""
import ast
import hashlib
import importlib.util
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "collect_licenses", ROOT / "tools" / "collect_licenses.py")
collect_licenses = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collect_licenses)

LIVE_DEST = ROOT / "third_party" / "licenses"
LIVE_MANIFEST = LIVE_DEST / "manifest.json"


def _write(dest, name, data):
    target = dest / name
    target.write_bytes(data)
    return target


def _manifest_for(entries, manifest_path):
    payload = {"licenses": entries}
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n",
                             encoding="utf-8")
    return payload


def _entry(dest, name, data):
    return {"path": f"third_party/licenses/{name}",
            "category": "included",
            "version": "test",
            "source_file": None,
            "source_url": "test",
            "revision": "test",
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "retrieval": "test",
            "verification": "test"}


_PIN_RE = re.compile(
    r"^([A-Za-z0-9_.\-]+(?:\[[^\]]+\])?)\s*==\s*([^\s;]+)")


def _requirements_pins(filenames=("requirements-suite.txt",
                                  "requirements-dev.txt")):
    """Pin esatti (==) dai requirements, solo lettura (B14-05)."""
    pins = {}

    def _parse(path):
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if line.startswith("-r "):
                inc = (ROOT / line[2:].strip().split()[0])
                if inc.is_file():
                    _parse(inc)
                continue
            if line.startswith("-"):
                continue
            match = _PIN_RE.match(line.split(";", 1)[0].strip())
            if match:
                name = match.group(1).split("[", 1)[0].strip().lower()
                pins[name] = match.group(2).strip()

    for filename in filenames:
        _parse(ROOT / filename)
    return pins


def _runtime_versions_dict():
    """Dict versioni runtime da tools/collect_licenses.py, solo lettura."""
    src = (ROOT / "tools" / "collect_licenses.py").read_text(
        encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Dict):
            continue
        keys = [k.value for k in node.keys
                if isinstance(k, ast.Constant)]
        if "numpy" in keys and "pytest" in keys:
            return {k.value.lower(): v.value
                    for k, v in zip(node.keys, node.values)
                    if isinstance(k, ast.Constant)
                    and isinstance(v, ast.Constant)}
    raise AssertionError(
        "versions dict non trovato in tools/collect_licenses.py")


class LicenseCheckTests(unittest.TestCase):
    def test_check_pass_two_files(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            a, b = b"alpha-license\n", b"beta-license\n"
            _write(dest, "a.txt", a)
            _write(dest, "b.txt", b)
            man = dest / "manifest.json"
            _manifest_for([_entry(dest, "a.txt", a),
                           _entry(dest, "b.txt", b)], man)
            self.assertEqual(
                collect_licenses.verify_manifest(man, dest), [])

    def test_check_missing_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            a = b"alpha-license\n"
            _write(dest, "a.txt", a)
            man = dest / "manifest.json"
            _manifest_for([_entry(dest, "a.txt", a),
                           _entry(dest, "gone.txt", b"x")], man)
            errors = collect_licenses.verify_manifest(man, dest)
            self.assertTrue(any("mancante" in e and "gone.txt" in e
                                for e in errors), errors)

    def test_check_tampered_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            _write(dest, "a.txt", b"alpha-license\n")
            man = dest / "manifest.json"
            _manifest_for([_entry(dest, "a.txt", b"alpha-license TAMPERED\n")],
                          man)
            errors = collect_licenses.verify_manifest(man, dest)
            self.assertTrue(any("sha256 mismatch" in e for e in errors),
                            errors)

    def test_check_byte_count_mismatch(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            data = b"alpha-license\n"
            _write(dest, "a.txt", data)
            man = dest / "manifest.json"
            item = _entry(dest, "a.txt", data)
            item["bytes"] = len(data) + 5
            _manifest_for([item], man)
            errors = collect_licenses.verify_manifest(man, dest)
            self.assertTrue(any("byte mismatch" in e for e in errors), errors)

    def test_check_extra_file_flagged(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            a = b"alpha-license\n"
            _write(dest, "a.txt", a)
            _write(dest, "stray.txt", b"stray\n")
            man = dest / "manifest.json"
            _manifest_for([_entry(dest, "a.txt", a)], man)
            errors = collect_licenses.verify_manifest(man, dest)
            self.assertTrue(any("extra" in e and "stray.txt" in e
                                for e in errors), errors)

    def test_check_main_subcommand_exit_codes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            a = b"alpha-license\n"
            _write(dest, "a.txt", a)
            man = dest / "manifest.json"
            _manifest_for([_entry(dest, "a.txt", a)], man)
            ok = collect_licenses.main(
                ["check", "--dest", str(dest), "--manifest", str(man)])
            self.assertEqual(ok, 0)
            _write(dest, "a.txt", b"tampered\n")
            bad = collect_licenses.main(
                ["check", "--dest", str(dest), "--manifest", str(man)])
            self.assertEqual(bad, 1)

    def test_collect_adopts_dest_when_source_absent(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            data = b"adopted-bytes\n"
            _write(dest, "keep.txt", data)
            man = dest / "manifest.json"
            old_entries = list(collect_licenses.ENTRIES)
            try:
                collect_licenses.ENTRIES = (
                    ("keep.txt", "included", "t", (), "url", "rev",
                     "fetch", "ver"),)
                result = collect_licenses.run_collect(dest, man)
            finally:
                collect_licenses.ENTRIES = old_entries
            self.assertEqual(dest.joinpath("keep.txt").read_bytes(), data)
            self.assertEqual(result["licenses"][0]["sha256"],
                             hashlib.sha256(data).hexdigest())
            self.assertIsNone(result["licenses"][0]["source_file"])

    def test_collect_copies_source_byte_identical(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            src = work / "src.txt"
            src.write_bytes(b"source-bytes\n")
            dest = work / "dest"
            dest.mkdir()
            man = work / "manifest.json"
            old_entries = list(collect_licenses.ENTRIES)
            try:
                collect_licenses.ENTRIES = (
                    ("out.txt", "included", "t", (str(src),), "url", "rev",
                     "copia", "ver"),)
                collect_licenses.run_collect(dest, man)
            finally:
                collect_licenses.ENTRIES = old_entries
            self.assertEqual(dest.joinpath("out.txt").read_bytes(),
                             b"source-bytes\n")

    def test_no_live_repo_writes(self):
        before = {p.name: (p.stat().st_mtime_ns, p.stat().st_size)
                  for p in LIVE_DEST.iterdir() if p.is_file()}
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            a = b"alpha-license\n"
            _write(dest, "a.txt", a)
            man = dest / "manifest.json"
            _manifest_for([_entry(dest, "a.txt", a)], man)
            collect_licenses.run_check(man, dest)
        after = {p.name: (p.stat().st_mtime_ns, p.stat().st_size)
                 for p in LIVE_DEST.iterdir() if p.is_file()}
        self.assertEqual(before, after)

    def test_live_manifest_selfcontained(self):
        errors = collect_licenses.verify_manifest(LIVE_MANIFEST, LIVE_DEST)
        self.assertEqual(errors, [])

    def test_runtime_versions_match_requirements_pins(self):
        # B14-05: il dict versioni runtime in collect_licenses.py deve
        # restare coerente coi pin == dei requirements; solo lettura,
        # fallisce se un bump non aggiorna entrambe le sedi.
        pins = _requirements_pins()
        versions = _runtime_versions_dict()
        self.assertTrue(pins, "nessun pin == trovato nei requirements")
        self.assertEqual(versions, pins,
                         f"dict runtime vs requirements: {versions} vs {pins}")


if __name__ == "__main__":
    unittest.main()
