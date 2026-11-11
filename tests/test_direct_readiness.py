# SPDX-License-Identifier: MIT
"""Tests for the pure Direct Mode readiness classifier and static contracts of
scripts/collect_direct_readiness.ps1. Fully offline: fixtures, real-classifier
CLI subprocess over temp bundles, and PowerShell harnesses that exercise the
collector's extracted resolve function plus a parameter-binding stub with the
inspector's real parameter shape. The collector itself is never executed and
nothing touches hardware."""
import ast
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "readiness", ROOT / "tools/psvr_native_analysis/readiness.py")
readiness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(readiness)

SONY_PATH = "\\\\?\\DISPLAY#SNY6A04#1&2&3#{a}"
LG_PATH = "\\\\?\\DISPLAY#GSM1321#4&5&6#{b}"
VENDOR_CANDIDATE = readiness.VENDOR_CANDIDATE_STATE


def source(data, status="ok"):
    return {"status": status, "begin_utc": "2026-10-05T00:00:00Z",
            "end_utc": "2026-10-05T00:00:01Z", "data": data, "error": None}


def inventory(displays):
    return source({"schema_version": 1, "read_only": True, "displays": displays})


def nvidia(displays, direct):
    return source({"read_only": True, "displays": displays, "direct_enumeration": direct})


def core(targets):
    return source({"read_only": True, "targets": targets})


def LG_DISPLAY():
    return {"monitor_device_path": LG_PATH,
            "edid_ids": {"manufacturer": 0x1E6D, "product": 0x1321, "valid": True}}


def SONY_DISPLAY():
    return {"monitor_device_path": SONY_PATH,
            "edid_ids": {"manufacturer": 0xD94D, "product": 0x6A04, "valid": True}}


def NV_SONY(os_visible=True):
    return {"display_id": 1, "active": False, "os_visible": os_visible, "connected": True,
            "physically_connected": True, "edid_status": 0,
            "edid_vendor": 0xD94D, "edid_product": 0x6A04}


def NV_LG():
    return {"display_id": 2, "active": True, "os_visible": True, "connected": True,
            "physically_connected": True, "edid_status": 0,
            "edid_vendor": 0x1E6D, "edid_product": 0x1321}


def CORE_TARGET(path=SONY_PATH, connected=True, stale=False, low=76797, high=0,
                usage="HeadMounted"):
    return {"adapter_relative_id": "pci#ven_10de...", "device_interface_path": path,
            "connected": connected, "stale": stale, "usage_kind": usage,
            "adapter_id_low": low, "adapter_id_high": high}


def EMPTY_DIRECT():
    return [{"vendor": 0xD94D, "mode": 0, "status": 0, "count": 0}]


def positive_bundle():
    """Everything observed and satisfied: off-desktop, direct vendor candidate,
    exact-path connected non-stale HMD-usage core target, plan applied, runtime
    not probed. NV visibility happens to be true but must not be load-bearing."""
    return {
        "schema_version": 1, "kind": "direct_readiness_bundle",
        "created_utc": "2026-10-05T00:00:00Z", "atomic": False,
        "sources": {
            "windows_active_inventory": inventory([LG_DISPLAY()]),
            "nvidia_public_displays": nvidia(
                [NV_LG(), NV_SONY()],
                [{"vendor": 0xD94D, "mode": 1, "status": 0, "count": 1},
                 {"vendor": 0xD94D, "mode": 0, "status": -3, "count": 1024}]),
            "windows_display_core": core([CORE_TARGET(), CORE_TARGET(path=LG_PATH)]),
            "edid_plan_inspection": source({"read_only": True, "plan_applied": True,
                                            "restore_gate_accepted": True}),
        },
        "runtime_summary": {"status": "absent", "data": None},
        "hmd": {"device_interface_path": SONY_PATH,
                "adapter_luid": {"low": 76797, "high": 0}},
    }


class ClassifierTests(unittest.TestCase):
    def test_output_is_json_serializable_with_schema_version(self):
        result = readiness.classify(positive_bundle())
        json.dumps(result)
        self.assertEqual(result["schema_version"], 1)

    def test_mixed_positive_claims_never_claim_working_or_acquisition(self):
        # Even with every positive observation, the outcome must stay a
        # prerequisite verdict, not a Direct Mode working or acquisition claim.
        bundle = positive_bundle()
        bundle["runtime_summary"] = {"status": "provided",
                                     "data": {"event": "summary",
                                              "compositor": {"fullscreen": True},
                                              "reason": "valid_target_pose_collected"}}
        result = readiness.classify(bundle)
        self.assertEqual(result["outcome"], "next_probe_possible")
        self.assertFalse(result["claims"]["gpu_acquisition_claimed"])
        self.assertFalse(result["claims"]["direct_mode_working_claimed"])
        self.assertNotIn("working", result["outcome"])

    def test_direct_route_requires_explicit_product_identity_caveat(self):
        # next_probe_possible reached through the vendor-scoped list must say
        # the product identity is not confirmed and no target was acquired.
        bundle = positive_bundle()
        result = readiness.classify(bundle)
        self.assertEqual(result["outcome"], "next_probe_possible")
        self.assertTrue(any("vendor-scoped" in row and "identity" in row
                            for row in result["reasons"]))
        self.assertTrue(any("not a claim" in row and "acquired" in row
                            for row in result["reasons"]))

    def test_desktop_active_is_blocked(self):
        bundle = positive_bundle()
        bundle["sources"]["windows_active_inventory"] = inventory([LG_DISPLAY(), SONY_DISPLAY()])
        result = readiness.classify(bundle)
        self.assertEqual(result["checks"]["windows_desktop"]["state"], "active")
        self.assertEqual(result["outcome"], "blocked")

    def test_os_visible_is_not_desktop_active(self):
        # NVAPI sees the HMD as os_visible while the desktop inventory does not
        # contain it: the two checks must stay independent.
        bundle = positive_bundle()
        result = readiness.classify(bundle)
        self.assertEqual(result["checks"]["windows_desktop"]["state"], "removed")
        self.assertEqual(result["checks"]["nvidia_os_visible"]["state"], "visible")

    def test_os_visible_not_visible_is_observational_not_a_gate(self):
        # A successful direct acquisition legitimately hides the HMD from the
        # OS: not_visible must never block when a valid route is observed.
        bundle = positive_bundle()
        bundle["sources"]["nvidia_public_displays"] = nvidia([NV_LG()], [
            {"vendor": 0xD94D, "mode": 1, "status": 0, "count": 1}])
        result = readiness.classify(bundle)
        self.assertEqual(result["checks"]["nvidia_os_visible"]["state"], "not_visible")
        self.assertEqual(result["outcome"], "next_probe_possible")
        self.assertEqual(result["blocking_evidence"], [])
        self.assertTrue(any("observational" in row for row in result["reasons"]))

    def test_failed_direct_status_count_is_ignored(self):
        # The observed SteamVR ABI returns count=1024 with a failed status;
        # that must never be a candidate and never an affirmative empty list.
        bundle = positive_bundle()
        bundle["sources"]["nvidia_public_displays"] = nvidia(
            [NV_SONY()], [{"vendor": 0xD94D, "mode": 0, "status": -3, "count": 1024}])
        bundle["sources"]["windows_display_core"] = core([])
        result = readiness.classify(bundle)
        check = result["checks"]["nvidia_direct_enumeration"]
        self.assertEqual(check["state"], "unknown")
        self.assertEqual(check["ignored_failed_rows"][0]["count"], 1024)
        self.assertEqual(result["outcome"], "unknown")

    def test_malformed_status_zero_count_is_unknown_not_type_error(self):
        bundle = positive_bundle()
        bundle["sources"]["nvidia_public_displays"] = nvidia(
            [NV_SONY()], [{"vendor": 0xD94D, "mode": 0, "status": 0, "count": None},
                          {"vendor": 0xD94D, "mode": 1, "status": 0, "count": "5"}])
        result = readiness.classify(bundle)
        check = result["checks"]["nvidia_direct_enumeration"]
        self.assertEqual(check["state"], "unknown")
        self.assertEqual(len(check["malformed_count_rows"]), 2)

    def test_direct_empty_plus_core_unavailable_blocks(self):
        bundle = positive_bundle()
        bundle["sources"]["nvidia_public_displays"] = nvidia([NV_SONY()], EMPTY_DIRECT())
        bundle["sources"]["windows_display_core"] = core([CORE_TARGET(connected=False)])
        result = readiness.classify(bundle)
        self.assertEqual(result["outcome"], "blocked")

    def test_core_hint_luid_is_never_selected(self):
        # Same adapter LUID but a different device interface path: hint only.
        bundle = positive_bundle()
        bundle["sources"]["nvidia_public_displays"] = nvidia([NV_SONY()], EMPTY_DIRECT())
        bundle["sources"]["windows_display_core"] = core(
            [CORE_TARGET(path="\\\\?\\PCI#OTHER#7#{c}", low=76797, high=0)])
        result = readiness.classify(bundle)
        check = result["checks"]["display_core_target"]
        self.assertEqual(check["state"], "no_match")
        self.assertEqual(check["hints"][0]["selection"], "hint_only")
        self.assertFalse(check["hints"][0]["selected"])

    def test_core_exact_path_available_requires_connected_not_stale(self):
        bundle = positive_bundle()
        # Drop the direct candidate so availability alone drives the outcome.
        bundle["sources"]["nvidia_public_displays"] = nvidia([NV_SONY()], EMPTY_DIRECT())
        result = readiness.classify(bundle)
        self.assertEqual(result["checks"]["display_core_target"]["state"], "available")
        self.assertEqual(result["outcome"], "next_probe_possible")
        bundle["sources"]["windows_display_core"] = core([CORE_TARGET(stale=True)])
        self.assertEqual(readiness.classify(bundle)["checks"]["display_core_target"]["state"],
                         "unavailable")

    def test_core_standard_usage_exact_match_is_rejected(self):
        # A path match reporting Standard desktop usage must never be selected,
        # even connected and non-stale.
        bundle = positive_bundle()
        bundle["sources"]["nvidia_public_displays"] = nvidia([NV_SONY()], EMPTY_DIRECT())
        bundle["sources"]["windows_display_core"] = core([CORE_TARGET(usage="Standard")])
        result = readiness.classify(bundle)
        self.assertEqual(result["checks"]["display_core_target"]["state"], "unavailable")
        self.assertEqual(result["outcome"], "blocked")

    def test_core_unknown_usage_kind_is_unknown(self):
        bundle = positive_bundle()
        bundle["sources"]["nvidia_public_displays"] = nvidia([NV_SONY()], EMPTY_DIRECT())
        bundle["sources"]["windows_display_core"] = core([CORE_TARGET(usage="Mystery")])
        result = readiness.classify(bundle)
        self.assertEqual(result["checks"]["display_core_target"]["state"], "unknown")
        self.assertEqual(result["outcome"], "unknown")

    def test_core_duplicate_exact_matches_are_ambiguous(self):
        bundle = positive_bundle()
        bundle["sources"]["nvidia_public_displays"] = nvidia([NV_SONY()], EMPTY_DIRECT())
        bundle["sources"]["windows_display_core"] = core(
            [CORE_TARGET(), CORE_TARGET(usage="SpecialPurpose")])
        result = readiness.classify(bundle)
        check = result["checks"]["display_core_target"]
        self.assertEqual(check["state"], "ambiguous")
        self.assertEqual(check["matched_count"], 2)
        self.assertEqual(result["outcome"], "unknown")
        self.assertTrue(any("ambiguous" in row for row in result["missing_evidence"]))

    def test_core_inspector_ok_false_is_not_trusted(self):
        # A healthy transport status must not launder an inspector verdict of
        # ok:false into evidence.
        bundle = positive_bundle()
        bundle["sources"]["windows_display_core"] = source(
            {"read_only": True, "ok": False, "targets": [CORE_TARGET()]})
        result = readiness.classify(bundle)
        check = result["checks"]["display_core_target"]
        self.assertEqual(check["state"], "unknown")
        self.assertEqual(check["source_status"], "inspector_not_ok")

    def test_availability_unknown_is_not_false_when_source_failed(self):
        bundle = positive_bundle()
        bundle["sources"]["windows_display_core"] = source(None, status="failed")
        result = readiness.classify(bundle)
        self.assertEqual(result["checks"]["display_core_target"]["state"], "unknown")
        # The direct vendor candidate still holds; the failed core source is
        # listed as missing evidence, not as an unmet prerequisite.
        self.assertEqual(result["outcome"], "next_probe_possible")
        self.assertTrue(any("display_core_target" in row
                            for row in result["missing_evidence"]))

    def test_ipc_failed_fullscreen_is_unknown_not_false(self):
        bundle = positive_bundle()
        bundle["runtime_summary"] = {"status": "provided", "data": {"reason": "probe_failed"}}
        check = readiness.classify(bundle)["checks"]["runtime_fullscreen"]
        self.assertEqual(check["state"], "unknown")

    def test_runtime_probe_exit_code_failure_is_unknown_despite_bool(self):
        # exit code 3 (IPC failure) invalidates the measurement itself.
        bundle = positive_bundle()
        bundle["runtime_summary"] = {"status": "provided",
                                     "data": {"exit_code": 3, "compositor": {"fullscreen": True}}}
        check = readiness.classify(bundle)["checks"]["runtime_fullscreen"]
        self.assertEqual(check["state"], "unknown")
        self.assertIn("exit_code=3", check["evidence"]["failures"])

    def test_runtime_non_summary_event_is_unknown(self):
        bundle = positive_bundle()
        bundle["runtime_summary"] = {"status": "provided",
                                     "data": {"event": "sample", "fullscreen": True}}
        self.assertEqual(readiness.classify(bundle)["checks"]["runtime_fullscreen"]["state"],
                         "unknown")

    def test_runtime_success_reason_does_not_invalidate_measurement(self):
        # Real healthy summaries carry reason=valid_target_pose_collected; the
        # measured bool must survive it.
        bundle = positive_bundle()
        bundle["runtime_summary"] = {"status": "provided",
                                     "data": {"event": "summary", "compositor": {"fullscreen": False},
                                              "reason": "valid_target_pose_collected"}}
        check = readiness.classify(bundle)["checks"]["runtime_fullscreen"]
        self.assertEqual(check["state"], "false")
        self.assertEqual(check["evidence"]["reason"], "valid_target_pose_collected")

    def test_multiple_sony_paths_fallback_is_ambiguous_never_first(self):
        # No explicit reference: two Sony monitor paths on the desktop must
        # produce 'ambiguous' instead of silently choosing the first.
        bundle = positive_bundle()
        bundle["hmd"].pop("device_interface_path")
        bundle["sources"]["windows_active_inventory"] = inventory(
            [SONY_DISPLAY(), dict(SONY_DISPLAY(), monitor_device_path=SONY_PATH + "2")])
        result = readiness.classify(bundle)
        check = result["checks"]["display_core_target"]
        self.assertEqual(check["state"], "ambiguous")
        self.assertIn("never chooses the first", check["note"])
        self.assertEqual(result["checks"]["windows_desktop"]["state"], "active")
        self.assertEqual(result["outcome"], "blocked")

    def test_single_sony_path_fallback_is_used_as_reference(self):
        bundle = positive_bundle()
        bundle["hmd"].pop("device_interface_path")
        bundle["sources"]["windows_active_inventory"] = inventory([LG_DISPLAY(), SONY_DISPLAY()])
        check = readiness.classify(bundle)["checks"]["display_core_target"]
        self.assertEqual(check["expected_device_interface_path"], SONY_PATH)
        self.assertEqual(check["state"], "available")

    def test_failed_source_keeps_error_context(self):
        bundle = positive_bundle()
        entry = source(None, status="failed")
        entry["error"] = "WinRT type unavailable"
        bundle["sources"]["windows_display_core"] = entry
        check = readiness.classify(bundle)["checks"]["display_core_target"]
        self.assertEqual(check["state"], "unknown")
        self.assertEqual(check["source_error"], "WinRT type unavailable")

    def test_edid_applied_is_not_rendering(self):
        bundle = positive_bundle()
        # EDID applied, but the rest is unobserved: no positive outcome.
        bundle["sources"]["windows_active_inventory"] = source(None, status="failed")
        result = readiness.classify(bundle)
        self.assertEqual(result["checks"]["edid_plan"]["state"], "applied")
        self.assertEqual(result["outcome"], "unknown")
        self.assertTrue(any("rendering" in row.lower()
                            for row in result["reasons"]))

    def test_all_sources_missing_is_unknown(self):
        result = readiness.classify({"schema_version": 1})
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(result["blocking_evidence"], [])
        self.assertEqual(len(result["missing_evidence"]), 6)

    def test_source_status_and_timestamps_are_echoed(self):
        result = readiness.classify(positive_bundle())
        entry = result["source_status"]["windows_active_inventory"]
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["begin_utc"], "2026-10-05T00:00:00Z")
        self.assertEqual(entry["end_utc"], "2026-10-05T00:00:01Z")

    def test_vendor_byte_order_variants_are_vendor_scoped_only(self):
        bundle = positive_bundle()
        bundle["sources"]["nvidia_public_displays"] = nvidia(
            [NV_SONY()], [{"vendor": 0x4DD9, "mode": 1, "status": 0, "count": 2}])
        result = readiness.classify(bundle)
        check = result["checks"]["nvidia_direct_enumeration"]
        self.assertEqual(check["state"], VENDOR_CANDIDATE)
        self.assertIn("0x6A04", check["note"])
        self.assertNotIn("PSVR match", check["note"])


class ClassifierPurityTests(unittest.TestCase):
    """The classifier must stay independent of hardware and OS acquisition."""

    def test_module_has_no_hardware_or_os_imports(self):
        tree = ast.parse((ROOT / "tools/psvr_native_analysis/readiness.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertLessEqual(imported, {"argparse", "json", "pathlib"})
        for forbidden in ("ctypes", "subprocess", "socket", "winreg"):
            self.assertNotIn(forbidden, imported)


class CollectorStaticContractTests(unittest.TestCase):
    """scripts/collect_direct_readiness.ps1 is never executed here; its text is
    checked for the read-only promises the task requires."""

    @classmethod
    def setUpClass(cls):
        path = ROOT / "scripts/collect_direct_readiness.ps1"
        cls.text = path.read_text(encoding="utf-8")

    def test_forbidden_side_effects_are_absent(self):
        forbidden = [
            r"\bStart-Process\b", r"\bVR_Init\b", r"\brunas\b", r"-Verb",
            r"\bnative_direct_mode\b", r"runtime_probe", r"\bStartSteamVR\b",
            r"\bvrmonitor\b", r"\bSet-ItemProperty\b", r"\bNew-ItemProperty\b",
            r"\bRemove-ItemProperty\b", r"\bSetValue(Name|Kind)?\b",
            r"\bRegistry\b", r"\bHKLM:", r"\bHKCU:", r"\bpowercfg\b",
            r"\bRestart-Computer\b", r"\bStop-Computer\b", r"\bTryAcquireTarget\b",
        ]
        for pattern in forbidden:
            self.assertIsNone(
                re.search(pattern, self.text, re.IGNORECASE),
                f"collector must not contain {pattern}")

    def test_readonly_existing_tools_are_invoked(self):
        for name in ("inspect_nvidia_displays.ps1", "inspect_windows_display_core.ps1",
                     "inspect_hmd_edid_plan.ps1", "psvr_display.exe"):
            self.assertIn(name, self.text)

    def test_hmd_interface_path_maps_to_real_inspector_parameters(self):
        # The external -HmdInterfacePath must be forwarded as the inspector's
        # real -RequestedMonitorInterfacePath via hashtable splatting, never
        # as positional-style string arrays.
        core_block = self.text.split("Source 3", 1)[1].split("Source 4", 1)[0]
        self.assertIn("RequestedMonitorInterfacePath", core_block)
        self.assertIn("$params=@{}", core_block)
        self.assertIn("@params", core_block)
        self.assertNotIn("+=@('-", core_block)

    def test_native_command_resolution_requires_application_type(self):
        self.assertIn("function Resolve-ReadOnlyApplication", self.text)
        self.assertIn("-CommandType Application", self.text)
        self.assertIn("Resolve-ReadOnlyApplication -Name $Exe", self.text)

    def test_bundle_outputs_use_utf8_no_bom_writers(self):
        self.assertGreaterEqual(self.text.count("[IO.File]::WriteAllText"), 2)
        self.assertNotIn("Set-Content", self.text)

    def test_bundle_contract_elements(self):
        for fragment in ("Mandatory=$true", "OutputDirectory", "IsPathRooted",
                        "Do not overwrite", "begin_utc", "end_utc", "status",
                        "schema_version", "created_utc"):
            self.assertIn(fragment, self.text)

    def test_per_source_exceptions_are_preserved(self):
        # Each source must run inside try/catch so one failure does not stop the rest.
        self.assertGreaterEqual(self.text.count("try {"), 4)
        self.assertGreaterEqual(self.text.count("catch"), 4)

    def test_runtime_probe_replaced_by_consumer_summary(self):
        self.assertIn("RuntimeSummaryPath", self.text)


class ClassifierCliTests(unittest.TestCase):
    """Real classifier CLI over a temp fixture bundle: pure Python, no hardware."""

    def test_cli_writes_utf8_without_bom_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = Path(td) / "bundle.json"
            result = Path(td) / "result.json"
            fixture.write_text(json.dumps(positive_bundle()), encoding="utf-8")
            script = str(ROOT / "tools/psvr_native_analysis/readiness.py")
            run = subprocess.run([sys.executable, script, str(fixture), "--out", str(result)],
                                 capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            raw = result.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "result must not carry a BOM")
            self.assertEqual(json.loads(raw.decode("utf-8"))["outcome"], "next_probe_possible")
            again = subprocess.run([sys.executable, script, str(fixture), "--out", str(result)],
                                   capture_output=True, text=True)
            self.assertNotEqual(again.returncode, 0, "refusing to overwrite must fail nonzero")


HAS_POWERSHELL = bool(shutil.which("powershell"))


def run_powershell(script):
    return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                           "-Command", script],
                          capture_output=True, text=True)


@unittest.skipUnless(HAS_POWERSHELL, "Windows PowerShell not available")
class CollectorResolveHarnessTests(unittest.TestCase):
    """Run the collector's EXTRACTED Resolve-ReadOnlyApplication function in a
    separate PowerShell process against real command names. The collector
    itself is never executed and no tool other than name resolution runs."""

    @classmethod
    def setUpClass(cls):
        text = (ROOT / "scripts/collect_direct_readiness.ps1").read_text(encoding="utf-8")
        match = re.search(r"(?ms)^function Resolve-ReadOnlyApplication \{.*?^\}", text)
        if not match:
            raise AssertionError("Resolve-ReadOnlyApplication function not extractable")
        cls.function = match.group(0)

    def test_command_names_resolve_to_executables_only(self):
        script = (
            "$ErrorActionPreference='Stop'\n" + self.function + "\n"
            "$r=Resolve-ReadOnlyApplication -Name 'python'\n"
            "if ($r -notmatch '\\.exe$') { throw 'python did not resolve to an executable' }\n"
            "foreach ($name in @('cd','psvr-definitely-not-a-real-exe-zz')) {\n"
            "  $rejected=$false\n"
            "  try { Resolve-ReadOnlyApplication -Name $name | Out-Null } catch { $rejected=$true }\n"
            "  if (-not $rejected) { throw \"$name must not resolve as a native application\" }\n"
            "}\n"
            "if ((Resolve-ReadOnlyApplication -Name $r) -ne $r) { throw 'absolute round-trip failed' }\n"
            "'RESOLVE-PASS'\n"
        )
        run = run_powershell(script)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("RESOLVE-PASS", run.stdout)


@unittest.skipUnless(HAS_POWERSHELL, "Windows PowerShell not available")
class InspectorParameterBindingFixtureTests(unittest.TestCase):
    """Hashtable-splat binding against a stub with the real inspector's exact
    parameter shape (RequestedMonitorInterfacePath + QueryModes). Fixture tool
    only: the actual inspector, collector and WinRT are not invoked."""

    def test_named_parameters_bind_through_splat(self):
        script = (
            "$ErrorActionPreference='Stop'\n"
            "$file=Join-Path ([IO.Path]::GetTempPath()) ('corebind_'+[Guid]::NewGuid().ToString('N')+'.ps1')\n"
            "$stub='[CmdletBinding()] param([string]$RequestedMonitorInterfacePath,[switch]$QueryModes)'"
            " + ' ([pscustomobject]@{bound_path=$RequestedMonitorInterfacePath;bound_modes=[bool]$QueryModes} "
            "| ConvertTo-Json -Compress)'\n"
            "[IO.File]::WriteAllText($file,$stub)\n"
            "try {\n"
            "  $params=@{}\n"
            "  $params['RequestedMonitorInterfacePath']='\\\\?\\DISPLAY#SNY6A04#1&2&3#{a}'\n"
            "  $params['QueryModes']=$true\n"
            "  $r=(& $file @params | ConvertFrom-Json)\n"
            "  if ($r.bound_path -ne '\\\\?\\DISPLAY#SNY6A04#1&2&3#{a}') { throw 'path did not bind' }\n"
            "  if (-not $r.bound_modes) { throw 'switch did not bind' }\n"
            "  $empty=(& $file @{} | ConvertFrom-Json)\n"
            "  if ($empty.bound_modes) { throw 'switch defaulted on without splat entry' }\n"
            "  'BIND-PASS'\n"
            "} finally { Remove-Item -LiteralPath $file }\n"
        )
        run = run_powershell(script)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("BIND-PASS", run.stdout)


if __name__ == "__main__":
    unittest.main()
