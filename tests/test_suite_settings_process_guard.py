"""B1-02 process guard: query errors must not look like stopped runtime.

Solo file, no inventario reale / no hardware SteamVR: il fake base per
``S.subprocess.run`` restituisce SimpleNamespace oppure solleva
TimeoutExpired/OSError; le 3 prove live usano il vero interprete
powershell solo con Get-Process ombreggiato da mock avanzato.
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.psvr_suite import settings as S


def _ok(stdout=""):
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def _fail(returncode=1, stdout="", stderr="deny"):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _check_useful(testcase, exc):
    msg = str(exc)
    testcase.assertTrue(msg.strip())
    low = msg.lower()
    testcase.assertTrue(
        any(h in low for h in ("process", "query", "steamvr", "timeout", "vrserver", "vrcompositor")),
        "got: %r" % msg,
    )


def _seed_vrsettings():
    return {
        S.DRIVER_SECTION: {
            "positionEnabled": True,
            "imuExportEnabled": True,
            "displayEnabled": False,
            "displayWidth": 1920,
            "distortionMode": "identity",
        },
        "steamvr": {"x": 1},
    }


def _make_default_store(tmp, backup_name="backup"):
    tmp = Path(tmp)
    path = tmp / "steamvr.vrsettings"
    path.write_text(json.dumps(_seed_vrsettings()), encoding="utf-8")
    return S.DriverSettingStore(path, tmp / backup_name), path, path.read_bytes()


class DefaultRunningProcessesContractTests(unittest.TestCase):
    def test_valid_empty_returns_empty_set(self):
        with mock.patch.object(S.subprocess, "run", lambda *a, **k: _ok("")):
            self.assertEqual(S.default_running_processes(), set())
        with mock.patch.object(S.subprocess, "run", lambda *a, **k: _ok("  \n \n")):
            self.assertEqual(S.default_running_processes(), set())

    def test_valid_names_normalized(self):
        fake = lambda *a, **k: _ok("vrserver\n VrCompositor \n\n")
        with mock.patch.object(S.subprocess, "run", fake):
            self.assertEqual(S.default_running_processes(), {"vrserver", "vrcompositor"})

    def test_nonzero_raises_even_with_partial_stdout(self):
        fake = lambda *a, **k: _fail(returncode=1, stdout="vrserver\n", stderr="deny")
        with mock.patch.object(S.subprocess, "run", fake):
            with self.assertRaises(RuntimeError) as ctx:
                S.default_running_processes()
        _check_useful(self, ctx.exception)

    def test_nonzero_empty_raises(self):
        fake = lambda *a, **k: _fail(returncode=1, stdout="", stderr="deny")
        with mock.patch.object(S.subprocess, "run", fake):
            with self.assertRaises(RuntimeError) as ctx:
                S.default_running_processes()
        _check_useful(self, ctx.exception)

    def test_stderr_with_rc0_raises(self):
        fake = lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr="warn")
        with mock.patch.object(S.subprocess, "run", fake):
            with self.assertRaises(RuntimeError) as ctx:
                S.default_running_processes()
        _check_useful(self, ctx.exception)

    def test_unexpected_name_raises(self):
        fake = lambda *a, **k: _ok("vrserver\nexplorer\n")
        with mock.patch.object(S.subprocess, "run", fake):
            with self.assertRaises(RuntimeError) as ctx:
                S.default_running_processes()
        _check_useful(self, ctx.exception)

    def test_unexpected_exe_suffix_raises(self):
        fake = lambda *a, **k: _ok("vrserver.exe\n")
        with mock.patch.object(S.subprocess, "run", fake):
            with self.assertRaises(RuntimeError) as ctx:
                S.default_running_processes()
        _check_useful(self, ctx.exception)

    def test_timeout_raises_runtime_error(self):
        def fake(*a, **k):
            raise subprocess.TimeoutExpired(cmd="powershell", timeout=15)

        with mock.patch.object(S.subprocess, "run", fake):
            with self.assertRaises(RuntimeError) as ctx:
                S.default_running_processes()
        _check_useful(self, ctx.exception)

    def test_oserror_raises_runtime_error(self):
        def fake(*a, **k):
            raise OSError("no shell")

        with mock.patch.object(S.subprocess, "run", fake):
            with self.assertRaises(RuntimeError) as ctx:
                S.default_running_processes()
        _check_useful(self, ctx.exception)


def _run_live_with_mock(mock_body):
    """Esegue la vera query con Get-Process ombreggiato, nessun inventario reale."""
    real_run = S.subprocess.run
    seen = {}

    def wrapper(cmd, *args, **kwargs):
        new_cmd = list(cmd)
        idx = new_cmd.index("-Command") + 1 if "-Command" in new_cmd else len(new_cmd) - 1
        new_cmd[idx] = mock_body + "\n" + str(new_cmd[idx])
        seen["timeout"] = kwargs.get("timeout")
        return real_run(new_cmd, *args, **kwargs)

    with mock.patch.object(S.subprocess, "run", wrapper):
        result = S.default_running_processes()
    return result, seen


@unittest.skipUnless(shutil.which("powershell"), "powershell unavailable")
class LiveQueryWithMockedGetProcessTests(unittest.TestCase):
    """Comportamento reale dello script, inventario sempre finto."""

    def test_live_nontargets_means_empty(self):
        mock_body = (
            "function Get-Process { [CmdletBinding()] param("
            "[Parameter(ValueFromRemainingArguments=$true)] $Remaining) "
            "@([pscustomobject]@{ ProcessName = 'explorer' }, "
            "[pscustomobject]@{ ProcessName = 'notepad' }) }"
        )
        result, seen = _run_live_with_mock(mock_body)
        self.assertEqual(result, set())
        self.assertEqual(seen.get("timeout"), 15)

    def test_live_targets_filtered(self):
        mock_body = (
            "function Get-Process { [CmdletBinding()] param("
            "[Parameter(ValueFromRemainingArguments=$true)] $Remaining) "
            "@([pscustomobject]@{ ProcessName = 'vrserver' }, "
            "[pscustomobject]@{ ProcessName = 'explorer' }, "
            "[pscustomobject]@{ ProcessName = 'VRCompositor' }) }"
        )
        result, seen = _run_live_with_mock(mock_body)
        self.assertEqual(result, {"vrserver", "vrcompositor"})
        self.assertEqual(seen.get("timeout"), 15)

    def test_live_write_error_is_failure(self):
        mock_body = (
            "function Get-Process { [CmdletBinding()] param("
            "[Parameter(ValueFromRemainingArguments=$true)] $Remaining) "
            "Write-Error 'deny' -ErrorAction Stop }"
        )
        real_run = S.subprocess.run

        def wrapper(cmd, *args, **kwargs):
            new_cmd = list(cmd)
            idx = new_cmd.index("-Command") + 1 if "-Command" in new_cmd else len(new_cmd) - 1
            new_cmd[idx] = mock_body + "\n" + str(new_cmd[idx])
            return real_run(new_cmd, *args, **kwargs)

        with mock.patch.object(S.subprocess, "run", wrapper):
            with self.assertRaises(RuntimeError) as ctx:
                S.default_running_processes()
        _check_useful(self, ctx.exception)


class DriverStoreProcessGuardTests(unittest.TestCase):
    def test_default_checker_calls_subprocess_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _, _ = _make_default_store(tmp)
            self.assertIs(store.running_checker, S.default_running_processes)
            with mock.patch.object(
                S.subprocess, "run", return_value=_ok("")
            ) as fake:
                self.assertEqual(store.running_checker(), set())
                self.assertTrue(fake.called)

    def test_apply_success_when_no_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, path, _ = _make_default_store(tmp)
            with mock.patch.object(S.subprocess, "run", lambda *a, **k: _ok("")):
                journal = store.apply(S.default_preferences())
            self.assertTrue(journal["applied"])
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("positionReacquireBlendMs", data[S.DRIVER_SECTION])

    def test_apply_refuses_when_initial_query_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, path, original = _make_default_store(tmp)
            with mock.patch.object(
                S.subprocess, "run", lambda *a, **k: _fail(1, "", "deny")
            ):
                with self.assertRaises(RuntimeError):
                    store.apply(S.default_preferences())
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse((Path(tmp) / "backup").exists())

    def test_apply_refuses_when_checker_returns_none_before_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "steamvr.vrsettings"
            path.write_text(json.dumps(_seed_vrsettings()), encoding="utf-8")
            original = path.read_bytes()
            store = S.DriverSettingStore(
                path, Path(tmp) / "backup", running_checker=lambda: None
            )
            with self.assertRaises(RuntimeError):
                store.apply(S.default_preferences())
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse((Path(tmp) / "backup").exists())

    def test_apply_refuses_when_second_query_fails_journal_applied_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, path, original = _make_default_store(tmp)
            calls = {"n": 0}

            def fake(*a, **k):
                calls["n"] += 1
                if calls["n"] == 1:
                    return _ok("")
                return _fail(1, "", "deny")

            with mock.patch.object(S.subprocess, "run", fake):
                with self.assertRaises(RuntimeError):
                    store.apply(S.default_preferences())
            self.assertEqual(path.read_bytes(), original)
            disk = json.loads((Path(tmp) / "backup" / "journal.json").read_text(encoding="utf-8"))
            self.assertFalse(disk["applied"])
            self.assertEqual(
                (Path(tmp) / "backup" / "settings_before.json").read_bytes(), original
            )

    def test_apply_refuses_when_second_query_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "steamvr.vrsettings"
            path.write_text(json.dumps(_seed_vrsettings()), encoding="utf-8")
            original = path.read_bytes()
            calls = {"n": 0}

            def checker():
                calls["n"] += 1
                return set() if calls["n"] == 1 else None

            store = S.DriverSettingStore(path, Path(tmp) / "backup", running_checker=checker)
            with self.assertRaises(RuntimeError):
                store.apply(S.default_preferences())
            self.assertEqual(path.read_bytes(), original)
            disk = json.loads((Path(tmp) / "backup" / "journal.json").read_text(encoding="utf-8"))
            self.assertFalse(disk["applied"])

    def test_rollback_refuses_when_query_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, path, _ = _make_default_store(tmp)
            with mock.patch.object(S.subprocess, "run", lambda *a, **k: _ok("")):
                journal = store.apply(S.default_preferences())
            applied = path.read_bytes()
            with mock.patch.object(
                S.subprocess, "run", lambda *a, **k: _fail(1, "", "deny")
            ):
                with self.assertRaises(RuntimeError):
                    store.rollback(journal)
            self.assertEqual(path.read_bytes(), applied)


if __name__ == "__main__":
    unittest.main()
