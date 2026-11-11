# SPDX-License-Identifier: MIT
"""Offline CLI checks ONLY. Never pass a real DLL or initialize OpenVR."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: check_runtime_probe_cli.py <psvr_runtime_probe.exe>")
    exe = Path(sys.argv[1]).resolve(strict=True)

    def run(expected, *args):
        result = subprocess.run([str(exe), *map(str, args)], capture_output=True,
                                text=True, encoding="utf-8", timeout=10, check=False)
        assert result.returncode == expected, (args, result.returncode, result.stdout, result.stderr)
        if expected:
            events = [json.loads(line) for line in result.stdout.splitlines()]
            assert len(events) == 1, events
            assert events[0]["event"] == "error" and events[0]["exit_code"] == expected, events
            if expected == 3:
                assert "LoadLibraryExW failed" in events[0]["message"], events
        return result

    help_text = run(0, "--help").stdout
    for text in ("PSVRPC-HMD-0", "1000", "100..10000", "Utility", "--openvr-dll",
                 "--request recenter|recalibrate", "read-only by default", "NOT applied",
                 "--compositor-status", "--bring-to-front implies status", "focus request dispatched once",
                 "no guaranteed fullscreen", "observed_before_after, NOT success",
                 "after 100 ms", "no Scene fallback", "Missing compositor API: exit 3"):
        assert text in help_text, help_text

    # The only syntactically valid invocations use a guaranteed nonexistent path.
    # Do not accept a DLL path argument/environment override: tests must remain offline.
    with tempfile.TemporaryDirectory(prefix="psvr_runtime_probe_cli_") as temp:
        missing = Path(temp) / "missing-é-測試" / "openvr_api.dll"
        assert not missing.exists() and not missing.parent.exists()
        run(2)
        run(2, "--unknown")
        run(2, "--openvr-dll")
        run(2, "--duration-ms", "100")
        for path in ("", "openvr_api.dll", r"relative\openvr_api.dll", r"C:openvr_api.dll", r"\openvr_api.dll"):
            run(2, "--openvr-dll", path)
        for value in ("", "0", "99", "10001", "-100", "+100", "1.0", "100ms", " 100", "100 ", "999999999999999999999"):
            run(2, "--openvr-dll", missing, "--duration-ms", value)
        run(2, "--openvr-dll", missing, "--duration-ms")
        run(2, "--openvr-dll", missing, "--openvr-dll", missing)
        run(2, "--openvr-dll", missing, "--duration-ms", "100", "--duration-ms", "100")
        run(2, "--openvr-dll", missing, "--help")
        run(2, "--help", "--openvr-dll", missing)
        run(2, "--request", "recenter")
        run(2, "--openvr-dll", missing, "--request")
        for command in ("", "quit", "unknown", "Recenter", "recenter ", "recalibration", "--help"):
            run(2, "--openvr-dll", missing, "--request", command)
        for first in ("recenter", "recalibrate"):
            for second in ("recenter", "recalibrate"):
                run(2, "--openvr-dll", missing, "--request", first, "--request", second)
        run(2, "--openvr-dll", missing, "--unknown")
        for flag in ("--compositor-status", "--bring-to-front", "--disable-standby-pause"):
            run(2, flag)
            run(2, "--openvr-dll", missing, flag, flag)
            run(2, flag, "--openvr-dll", missing, flag)
            run(2, "--openvr-dll", missing, flag, "true")
            run(2, "--openvr-dll", missing, flag, "--duration-ms", "99")
            run(2, "--openvr-dll", missing, flag, "--unknown")
            for command in ("recenter", "recalibrate"):
                run(2, "--openvr-dll", missing, flag, "--request", command)
                run(2, "--request", command, "--openvr-dll", missing, flag)
            run(3, "--openvr-dll", missing, flag)
            run(3, flag, "--duration-ms", "100", "--openvr-dll", missing)
        for flags in (("--compositor-status", "--bring-to-front"),
                      ("--bring-to-front", "--compositor-status")):
            # Implication is not a duplicate of the explicit status flag.
            run(3, "--openvr-dll", missing, *flags)
            for flag in flags:
                run(2, "--openvr-dll", missing, *flags, flag)
            run(2, "--openvr-dll", missing, *flags, "--request", "recenter")
        run(2, "--openvr-dll", missing, "--disable-standby-pause", "--bring-to-front")
        run(2, "--openvr-dll", missing, "--bring-to-front", "--disable-standby-pause")
        run(3, "--openvr-dll", missing)
        run(2, "--openvr-dll", missing, "--optics-grid", "relative.png")
        run(2, "--openvr-dll", missing, "--optics-grid", missing, "--optics-grid", missing)
        run(2, "--openvr-dll", missing, "--optics-grid", missing, "--bring-to-front")
        run(2, "--openvr-dll", missing, "--optics-grid", missing, "--request", "recenter")
        run(3, "--openvr-dll", missing, "--optics-grid", missing, "--duration-ms", "60000")
        run(2, "--openvr-dll", missing, "--grid-near")
        run(3, "--openvr-dll", missing, "--optics-grid", missing, "--grid-near", "--duration-ms", "60000")
        run(2, "--openvr-dll", missing, "--dashboard-curvature", "unknown")
        run(2, "--openvr-dll", missing, "--dashboard-curvature", "off", "--request", "recenter")
        run(2, "--openvr-dll", missing, "--dashboard-curvature", "off", "--dashboard-curvature", "on")
        for value in ("off", "on", "default"):
            run(3, "--openvr-dll", missing, "--dashboard-curvature", value)
        for bound in ("100", "1000", "10000"):
            run(3, "--openvr-dll", missing, "--duration-ms", bound)
        run(3, "--duration-ms", "100", "--openvr-dll", missing)
        for command in ("recenter", "recalibrate"):
            run(3, "--openvr-dll", missing, "--request", command)
            run(3, "--request", command, "--duration-ms", "100", "--openvr-dll", missing)
            run(2, "--openvr-dll", missing, "--request", command, "--duration-ms", "99")
            run(2, "--openvr-dll", missing, "--request", command, "--unknown")
    print("PASS offline runtime probe CLI: help, invalid arguments/requests, compositor flags/duplicates/conflicts, bounds, missing Unicode DLL; no OpenVR initialization or action dispatch")


if __name__ == "__main__":
    main()
