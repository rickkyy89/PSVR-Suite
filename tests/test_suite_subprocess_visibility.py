"""Suite subprocess console visibility; mocked subprocess, never real hardware.

Windows pythonw has no console: every powershell/validator probe started by
the suite (runtime DefaultBackend.processes, settings default_running_processes
-- real name behind the "runtime_running" guard --, settings
validate_optics_profile and the deploy runtime check) must pass
``creationflags=CREATE_NO_WINDOW`` so no console flashes. Backend run/popen
already hide the console; pinned here as regression.
"""
import subprocess
import unittest
from pathlib import Path
from unittest import mock
import tempfile

from tools.psvr_suite import deploy as D
from tools.psvr_suite import runtime as R
from tools.psvr_suite import settings as S

EXPECTED = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def assert_hidden(test, kwargs):
    test.assertIn("creationflags", kwargs)
    test.assertEqual(kwargs["creationflags"], EXPECTED)
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        test.assertNotEqual(kwargs["creationflags"], 0)


class Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class RuntimeProcessesTests(unittest.TestCase):
    def test_processes_hides_console(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = R.DefaultBackend(tmp)
            with mock.patch.object(R.subprocess, "run") as run:
                run.return_value = Proc(0, "vrserver\nvrcompositor\n")
                names = backend.processes()
            self.assertEqual(names, {"vrserver", "vrcompositor"})
            argv, kwargs = run.call_args[0][0], run.call_args[1]
            self.assertIn("powershell", argv)
            assert_hidden(self, kwargs)


class RuntimeRunPopenTests(unittest.TestCase):
    def test_run_hides_console(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = R.DefaultBackend(tmp)
            with mock.patch.object(R.subprocess, "run") as run:
                run.return_value = Proc(0, "ok")
                backend.run(["dummy", "--flag"], timeout=5)
            _, kwargs = run.call_args
            assert_hidden(self, kwargs)

    def test_popen_hides_console(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = R.DefaultBackend(tmp)
            with mock.patch.object(R.subprocess, "Popen") as popen:
                popen.return_value = Proc(0, "")
                backend.popen(["dummy"])
            _, kwargs = popen.call_args
            assert_hidden(self, kwargs)


class SettingsRunningTests(unittest.TestCase):
    def test_default_running_processes_hides_console(self):
        # Real name of the "runtime_running" guard in settings.py.
        self.assertTrue(hasattr(S, "default_running_processes"))
        with mock.patch.object(S.subprocess, "run") as run:
            run.return_value = Proc(0, "vrserver\n")
            names = S.default_running_processes()
        self.assertEqual(names, {"vrserver"})
        argv, kwargs = run.call_args[0][0], run.call_args[1]
        self.assertIn("powershell", argv)
        assert_hidden(self, kwargs)


class SettingsOpticsValidatorTests(unittest.TestCase):
    def test_validate_optics_profile_hides_console(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "psvr_optics.exe"
            exe.write_bytes(b"x")
            profile = Path(tmp) / "profile.json"
            profile.write_text("{}", encoding="utf-8")
            with mock.patch.object(S.subprocess, "run") as run:
                run.return_value = Proc(0, "{}")
                result = S.validate_optics_profile(profile, validator=exe)
            self.assertEqual(result, {})
            argv, kwargs = run.call_args[0][0], run.call_args[1]
            self.assertIn(str(exe), argv)
            assert_hidden(self, kwargs)


class DeployRuntimeCheckTests(unittest.TestCase):
    def test_deploy_runtime_check_hides_console(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = Path(tmp) / "driver_psvrpc.dll"
            probe = Path(tmp) / "psvr_runtime_probe.exe"
            driver.write_bytes(b"d")
            probe.write_bytes(b"p")
            backup = Path(tmp) / "backup"
            with mock.patch.object(D.subprocess, "run") as run:
                run.return_value = Proc(0, "")
                D.main(["--backup-dir", str(backup),
                        "--driver", str(driver), "--probe", str(probe)])
            self.assertTrue(run.called)
            argv, kwargs = run.call_args[0][0], run.call_args[1]
            self.assertIn("powershell", argv)
            assert_hidden(self, kwargs)
            self.assertTrue((backup / "manifest.json").is_file())


if __name__ == "__main__":
    unittest.main()
