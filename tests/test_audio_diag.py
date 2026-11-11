# SPDX-License-Identifier: MIT
"""Audio-diag contracts: fake backend + mocked subprocess, never real USB."""
import io
import json
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from tools.psvr_audio_diag import cli
from tools.psvr_audio_diag import scan as A


class Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeBackend:
    """Same surface as DefaultBackend; records every call."""

    def __init__(self, pu_run=None, ps_by_script=None, raise_on=None):
        self.pu_run = pu_run
        self.ps_by_script = ps_by_script or {}
        self.raise_on = raise_on or set()
        self.calls = []

    def pu_status(self, config=None, timeout=None):
        self.calls.append(("pu_status", config, timeout))
        if "pu" in self.raise_on:
            raise RuntimeError("pu boom")
        return self.pu_run

    def powershell(self, script, timeout=None):
        self.calls.append(("powershell", script, timeout))
        if "ps" in self.raise_on:
            raise RuntimeError("ps boom")
        result = self.ps_by_script.get(script, "")
        return result if isinstance(result, Proc) else Proc(0, result)


class ParseTests(unittest.TestCase):
    def test_control_status_bare_json(self):
        out = json.dumps({"schema_version": 1, "event": "control_status",
                          "power_on": True, "worn": False, "mode": "vr",
                          "vr_active": 1, "status_mask": 7})
        parsed = A.parse_control_status(out)
        self.assertEqual(parsed["pu_mode"], "vr")
        self.assertEqual(parsed["source"], "F0")

    def test_control_status_prefixed_and_last_wins(self):
        lines = [
            'DATA {"schema_version":1,"event":"control_status","mode":"cinema","vr_active":0,"power_on":true}',
            'INFO unrelated {"event":"usb_device"}',
            'DATA {"schema_version":1,"event":"control_status","mode":"transition","vr_active":1,"power_on":true}',
        ]
        parsed = A.parse_control_status("\n".join(lines))
        self.assertEqual(parsed["pu_mode"], "transition")

    def test_control_status_unknown_never_guessed(self):
        for stdout in ("", "no braces here", "DATA {not json"):
            parsed = A.parse_control_status(stdout)
            self.assertEqual(parsed["pu_mode"], "unknown")
            self.assertIsNone(parsed["power_on"])

    def test_control_status_mode_contract(self):
        # main.cpp control_status_event emits these explicit modes. Do not
        # infer a mode from power_on/vr_active when the field is absent.
        for mode in ("off", "cinema", "vr", "transition", None, "bogus", 1):
            event = {"schema_version": 1, "event": "control_status",
                     "power_on": True, "vr_active": 1, "status_mask": 5}
            if mode is not None:
                event["mode"] = mode
            parsed = A.parse_control_status(json.dumps(event))
            self.assertEqual(parsed["pu_mode"], mode if isinstance(mode, str)
                             and mode != "bogus" else "unknown")
            self.assertEqual(parsed["source"], "F0")

    def test_parse_rows_preserves_empty_columns(self):
        rows = A.parse_rows("\tOK\r\nUnnamed\t\r\n\t\n",
                            ("name", "status"))
        self.assertEqual(rows, [{"name": "", "status": "OK"},
                                {"name": "Unnamed", "status": ""}])
        self.assertEqual(A.parse_rows("A\tB\textra", ("a", "b")), [])

    def test_parse_rows_splits_and_drops_malformed(self):
        text = "A\tB\tC\tD\nshort\tline\n\nX\tY\tZ\tW-extra-tabs\n"
        rows = A.parse_rows(text, ("a", "b", "c", "d"))
        self.assertEqual(rows[0], {"a": "A", "b": "B", "c": "C", "d": "D"})
        # Ordinary last-field text remains intact.
        self.assertEqual(rows[1]["d"], "W-extra-tabs")
        self.assertEqual(len(rows), 2)


class FactsTests(unittest.TestCase):
    def test_pnp_facts_detects_mi01_only_for_psvr(self):
        rows = A.parse_rows(
            "USB\\VID_054C&PID_09AF&MI_01\\6&1\tPS VR Audio\tMEDIA\tOK\n"
            "USB\\VID_054C&PID_09AF&MI_04\\6&2\tPS VR Sensor\tHIDCLASS\tOK\n"
            "USB\\VID_1415&PID_2000&MI_01\\6&3\tOther device\tMEDIA\tOK\n",
            ("instance_id", "name", "class", "status"))
        facts = A.pnp_facts(rows)
        self.assertTrue(facts["psvr_composite_present"])
        self.assertEqual(facts["psvr_interface_count"], 2)
        self.assertEqual(facts["mi01_rows"], [{"name": "PS VR Audio",
                                               "class": "MEDIA", "status": "OK"}])

    def test_endpoint_facts_never_claims_default(self):
        rows = A.parse_rows(
            "MMDEVAPI\\{0.0.0.00000000}.{a}\tSIE HMD (NVIDIA High Definition Audio)\tOK\t0\n"
            "MMDEVAPI\\{0.0.0.00000000}.{b}\tSpeakers (Realtek)\tOK\t0\n",
            ("device_id", "name", "status", "problem_code"))
        facts = A.endpoint_facts(rows)
        self.assertNotIn("default", facts)
        self.assertNotIn("default", facts["render_endpoints"][0])
        self.assertEqual(facts["headset_like_endpoints"],
                         ["SIE HMD (NVIDIA High Definition Audio)"])


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend(
            pu_run=Proc(0, json.dumps({"schema_version": 1,
                                       "event": "control_status",
                                       "power_on": True, "mode": "vr",
                                       "vr_active": 1, "worn": True,
                                       "status_mask": 5})),
            ps_by_script={
                A.PS_PNP: "USB\\VID_054C&PID_09AF&MI_01\\6&1\tPS VR Audio\tMEDIA\tOK\n",
                A.PS_ENDPOINTS: "MMDEVAPI\\{0.0.0.x}\tSIE HMD\tOK\t0\n",
                A.PS_SOUND: "NVIDIA High Definition Audio\tOK\n",
            })

    def test_collect_ok_and_readonly_contract(self):
        report = A.collect(backend=self.backend, root="C:/fake")
        self.assertTrue(report["read_only"])
        self.assertEqual(report["pu"]["pu_mode"], "vr")
        self.assertTrue(report["usb"]["psvr_composite_present"])
        self.assertEqual(report["endpoints"]["headset_like_endpoints"], ["SIE HMD"])
        self.assertEqual(report["sound_devices"]["devices"],
                         [{"name": "NVIDIA High Definition Audio", "status": "OK"}])
        # Only ever the read-only surface: two backend entry points, and the
        # diag observation is requested through pu_status (no argv exposure
        # to write actions from collect).
        self.assertIn(("pu_status", None, A.DEFAULT_TIMEOUT_S),
                      [c[:3] for c in self.backend.calls])

    def test_collect_degrades_to_unavailable_never_raises(self):
        backend = FakeBackend(pu_run=None, raise_on={"ps"})
        report = A.collect(backend=backend)
        self.assertEqual(report["pu"]["availability"], "unavailable")
        self.assertEqual(report["pu"]["pu_mode"], "unknown")
        for key in ("usb", "endpoints", "sound_devices"):
            self.assertEqual(report[key]["availability"], "unavailable")

    def test_failed_queries_discard_partial_stdout(self):
        self.backend.pu_run.returncode = 6
        self.backend.pu_run.stderr = "status observation failed"
        for script, output in list(self.backend.ps_by_script.items()):
            self.backend.ps_by_script[script] = Proc(1, output, "inventory failed")
        report = A.collect(backend=self.backend)
        for key in ("pu", "usb", "endpoints", "sound_devices"):
            self.assertEqual(report[key]["availability"], "unavailable")
            self.assertNotEqual(report[key]["returncode"], 0)
            self.assertIn("failed", report[key]["reason"])
        self.assertEqual(report["pu"]["pu_mode"], "unknown")
        self.assertNotIn("psvr_composite_present", report["usb"])
        self.assertNotIn("render_endpoints", report["endpoints"])
        self.assertNotIn("devices", report["sound_devices"])

    def test_failed_query_without_stderr_has_reason(self):
        self.backend.pu_run = Proc(3)
        report = A.collect(backend=self.backend)
        self.assertIn("code 3", report["pu"]["reason"])
        self.assertEqual(report["usb"]["availability"], "ok")

    def test_success_without_status_is_not_available(self):
        for stdout in ("", 'DATA {"event":"usb_device"}', 'DATA {broken'):
            self.backend.pu_run = Proc(0, stdout)
            report = A.collect(backend=self.backend)
            self.assertEqual(report["pu"]["availability"], "unavailable")
            self.assertEqual(report["pu"]["pu_mode"], "unknown")

    def test_empty_successful_inventory_is_available(self):
        report = A.collect(backend=FakeBackend())
        self.assertEqual(report["usb"]["availability"], "ok")
        self.assertFalse(report["usb"]["psvr_composite_present"])
        self.assertEqual(report["endpoints"]["render_endpoints"], [])

    def test_timeout_isolated_to_failed_section(self):
        with mock.patch.object(self.backend, "pu_status",
                               side_effect=A.subprocess.TimeoutExpired("fake", 1)):
            report = A.collect(backend=self.backend, timeout=1)
        self.assertEqual(report["pu"]["availability"], "unavailable")
        self.assertEqual(report["sound_devices"]["availability"], "ok")

    def test_offline_report_has_no_live_sections(self):
        report = A.offline_report()
        self.assertTrue(report["offline"])
        self.assertEqual(report["pu"]["pu_mode"], "unknown")
        for key in ("usb", "endpoints", "sound_devices"):
            self.assertEqual(report[key]["availability"], "skipped-offline")
        self.assertIn("vr_silence", report["guidance"])
        self.assertTrue(report["validation_pending"])


class DefaultBackendReadOnlyGuard(unittest.TestCase):
    """The real backend must only ever assemble read-only commands."""

    FORBIDDEN_DIAG = {"--vr-mode", "--cinema-mode", "--power-on",
                      "--power-off", "--leds-on"}
    FORBIDDEN_PS = ("Set-", "New-", "Remove-", "Start-", "Stop-", "Enable-",
                    "Disable-", "Restart-", "Install-", "Uninstall-")

    def test_pu_status_argv_is_passive(self):
        import pathlib
        root = pathlib.Path("C:/fake-root")
        backend = A.DefaultBackend(root=root)
        with mock.patch.object(A.Path, "is_file", return_value=True):
            with mock.patch("tools.psvr_audio_diag.scan.subprocess.run",
                            return_value=Proc(0, "")) as run:
                backend.pu_status(timeout=1.0)
        argv = [str(a) for a in run.call_args.args[0]]
        self.assertIn("--status", argv)
        self.assertIn("--json-stream", argv)
        self.assertFalse(self.FORBIDDEN_DIAG & set(argv))
        self.assertEqual(argv, [str(root / A.DIAG_REL), "--config",
                                str(root / "config.json"), "--status", "--json-stream"])
        self.assertEqual(run.call_args.kwargs["timeout"], 1.0)

    def test_missing_executable_never_spawns(self):
        with mock.patch.object(A.Path, "is_file", return_value=False), \
                mock.patch.object(A.subprocess, "run") as run:
            self.assertIsNone(A.DefaultBackend().pu_status())
        run.assert_not_called()

    def test_powershell_scripts_are_read_only(self):
        backend = A.DefaultBackend()
        for script in (A.PS_PNP, A.PS_ENDPOINTS, A.PS_SOUND):
            with mock.patch("tools.psvr_audio_diag.scan.subprocess.run",
                            return_value=Proc(0, "")) as run:
                backend.powershell(script, timeout=1.0)
            argv = run.call_args.args[0]
            self.assertEqual(argv[:3], ["powershell", "-NoProfile", "-Command"])
            self.assertEqual(argv[3], "$ErrorActionPreference = 'Stop'; " + script)
            for bad in self.FORBIDDEN_PS:
                self.assertNotIn(bad, argv[3])


class CliTests(unittest.TestCase):
    def test_offline_cli_json_exits_zero(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer), mock.patch.object(A.subprocess, "run") as run, \
                mock.patch.object(cli, "collect") as collect:
            code = cli.main(["--offline", "--json"])
        run.assert_not_called()
        collect.assert_not_called()
        self.assertEqual(code, 0)
        report = json.loads(buffer.getvalue())
        self.assertEqual(report["schema"], "psvr.audio_diag")
        self.assertTrue(report["offline"])

    def test_offline_cli_text_exits_zero(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.main(["--offline"])
        self.assertEqual(code, 0)
        self.assertIn("PU mode: unknown", buffer.getvalue())

    def test_text_includes_failures_and_inventory_limit(self):
        report = A.collect(backend=FakeBackend(raise_on={"ps"}))
        text = cli.format_text(report)
        self.assertIn("endpoints: ps boom", text)
        self.assertIn("PnP inventory does not determine", text)
        self.assertIn("MMDevice", text)

    def test_invalid_timeout_rejected_before_collect(self):
        for value in ("0", "-1", "nan", "inf", "-inf"):
            with mock.patch.object(cli, "collect") as collect, \
                    redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                cli.main(["--timeout=" + value])
            self.assertEqual(exc.exception.code, 2)
            collect.assert_not_called()

    def test_live_cli_forwards_arguments_without_hardware(self):
        with mock.patch.object(cli, "collect", return_value=A.offline_report()) as collect, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--root", "fake", "--config", "cfg",
                                       "--timeout", "2", "--json"]), 0)
        collect.assert_called_once_with(root="fake", config="cfg", timeout=2.0)


if __name__ == "__main__":
    unittest.main()
