# SPDX-License-Identifier: MIT
"""Read-only audio state for PSVR1 PU modes (cinema/VR).

Facts (full context: ``docs/AUDIO_TRACKING_CAMERA_20261015.md``):

* Cinema mode: the processor unit carries host HDMI audio to the headset
  headphone jack. Validated by the user in the cinema SIE-HMD session.
* VR mode (SteamVR): observed silent in that same session. This inventory
  does not establish the cause. No documented command in this repo
  controls PU headphone routing, so NO host-side fix is promised here
  and none is attempted.
* USB MI_01 "PS VR Audio" is a plain usbaudio interface on the composite
  device. It is NOT the proprietary 3D-audio pipeline; selecting it as a
  Windows playback device does not enable 3D audio.

Guarantees of this module: strictly read-only. It runs only the existing
``psvr_diag --status --json-stream`` passive observation (USB IN reads,
no query/write commands) and read-only PowerShell inventory queries. It
never sends power/mode/LED actions, never touches SteamVR settings and
never changes a driver binding. Unknown stays ``unknown``. Tests inject a
fake backend (same pattern as ``tools/psvr_suite/runtime.py``).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PSVR_MATCH = "VID_054C&PID_09AF"
DIAG_REL = Path("out/diag/psvr_diag.exe")  # same location the suite runtime uses
DEFAULT_TIMEOUT_S = 30.0

# Tokens marking endpoints whose name suggests the headset/HDMI path.
# Presence here is only an inventory fact, never a "default device" proof.
HEADSET_TOKENS = ("SIE", "HDMI", "PS VR", "PSVR", "PLAYSTATION VR")

# Fixed, honest guidance strings shipped with every report.
GUIDANCE = {
    "cinema": (
        "Cinema mode: headset-jack audio comes from the host HDMI stream "
        "carried by the processor unit. Working in the user's cinema "
        "session; treated as hardware-validated for that session only."),
    "vr_silence": (
         "SteamVR/VR-mode silence was observed in the user's session; this "
         "inventory does not establish its cause. No documented host-side "
         "command controls PU headphone routing in this repo; this tool "
         "reports state only and promises no fix."),
    "usb_audio": (
        "MI_01 'PS VR Audio' (usbaudio) is plain USB audio, not the "
        "proprietary 3D-audio pipeline. Selecting it does NOT enable 3D "
        "audio; it is only a possible stereo path, still unvalidated."),
    "default_endpoint": (
         "This PnP inventory does not determine the default render endpoint "
         "or per-application routing. Read-only MMDevice queries can identify "
         "defaults by role, but are not implemented here; verify Windows "
         "playback and application routing settings separately."),
}

VALIDATION_PENDING = (
    "SteamVR/VR-mode headphone silence needs a formal test matrix (same "
    "PU, cable, endpoint, application) before any claim.",
    "MI_01 USB-audio interface capability (mic? stereo playback?) is not "
    "validated in this repo.",
    "HDMI endpoint behaviour across cinema/VR transitions is not validated.",
)


def parse_control_status(stdout):
    """Latest F0 ``control_status`` JSON line; missing/invalid stays unknown.

    Accepts bare JSON lines and lines with a log prefix (e.g. ``DATA {..}``),
    like ``runtime.py`` tolerates probe framing. Never guesses a mode.
    """
    latest = None
    for line in (stdout or "").splitlines():
        start = line.find("{")
        if start < 0:
            continue
        try:
            event = json.loads(line[start:])
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("event") == "control_status":
            latest = event
    if latest is None:
        return {"pu_mode": "unknown", "power_on": None, "worn": None,
                "vr_active": None, "status_mask": None,
                "source": "no F0 control_status line in output"}
    return {
        "pu_mode": (latest.get("mode") if latest.get("mode") in
                    ("off", "cinema", "vr", "transition") else "unknown"),
        "power_on": latest.get("power_on"),
        "worn": latest.get("worn"),
        "vr_active": latest.get("vr_active"),
        "status_mask": latest.get("status_mask"),
        "source": "F0",
    }


def parse_rows(text, fields):
    """TAB-separated PowerShell output -> list of dicts; malformed lines dropped."""
    rows = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != len(fields):
            continue
        rows.append({key: value.strip() for key, value in zip(fields, parts)})
    return rows


def pnp_facts(rows):
    """PSVR1 composite inventory (presence only; MI_01 stays untouched)."""
    psvr = [row for row in rows if PSVR_MATCH.lower() in row["instance_id"].lower()]
    mi01 = [row for row in psvr if "&MI_01" in row["instance_id"].upper()]
    return {
        "psvr_composite_present": bool(psvr),
        "psvr_interface_count": len(psvr),
        "mi01_rows": [{"name": row["name"], "class": row["class"],
                       "status": row["status"]} for row in mi01],
    }


def endpoint_facts(rows):
    """PnP render inventory, not MMDevice defaults or application routing."""
    endpoints = []
    for row in rows:
        upper = row["name"].upper()
        endpoints.append({
            "name": row["name"],
            "status": row["status"],
            "headset_tokens": [token for token in HEADSET_TOKENS if token in upper],
        })
    return {
        "render_endpoints": endpoints,
        "headset_like_endpoints": [e["name"] for e in endpoints if e["headset_tokens"]],
    }


class DefaultBackend:
    """Real read-only backend (tests inject a fake). Never writes anywhere."""

    def __init__(self, root=ROOT):
        self.root = Path(root)

    def diag_exe(self):
        return self.root / DIAG_REL

    def _run(self, argv, timeout):
        return subprocess.run(
            [str(a) for a in argv], capture_output=True, text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def powershell(self, script, timeout=DEFAULT_TIMEOUT_S):
        # Cmdlet errors are otherwise non-terminating and may exit with code 0.
        return self._run(["powershell", "-NoProfile", "-Command",
                          "$ErrorActionPreference = 'Stop'; " + script], timeout)

    def pu_status(self, config=None, timeout=DEFAULT_TIMEOUT_S):
        """Passive PU observation via the existing read-only flags only."""
        exe = self.diag_exe()
        if not exe.is_file():
            return None  # absent -> unknown, never a crash
        cfg = Path(config) if config is not None else self.root / "config.json"
        return self._run([exe, "--config", cfg, "--status", "--json-stream"], timeout)


PS_PNP = (
    "Get-PnpDevice -PresentOnly"
    " | Where-Object { $_.InstanceId -match 'VID_054C&PID_09AF' }"
    " | ForEach-Object { \"$($_.InstanceId)`t$($_.FriendlyName)"
    "`t$($_.Class)`t$($_.Status)\" }")

PS_ENDPOINTS = (
    "Get-CimInstance Win32_PnPEntity"
    " | Where-Object { $_.DeviceID -like 'MMDEVAPI\\{0.0.0.*' }"
    " | ForEach-Object { \"$($_.DeviceID)`t$($_.Name)`t$($_.Status)"
    "`t$($_.ConfigManagerErrorCode)\" }")

PS_SOUND = (
    "Get-CimInstance Win32_SoundDevice"
    " | ForEach-Object { \"$($_.Name)`t$($_.Status)\" }")


def _base_report():
    return {
        "schema": "psvr.audio_diag", "version": 1, "read_only": True,
        "guidance": dict(GUIDANCE),
        "validation_pending": list(VALIDATION_PENDING),
    }


def _process_failure(proc):
    """Do not promote partial stdout from a failed query to inventory facts."""
    if proc.returncode == 0:
        return None
    return {"availability": "unavailable", "returncode": proc.returncode,
            "reason": (proc.stderr or "").strip() or
                      "query exited with code %s" % proc.returncode}


def collect(backend=None, root=None, config=None, timeout=DEFAULT_TIMEOUT_S):
    """Build the live report; every failing section degrades to 'unavailable'
    with the raw reason, never a fake value and never an exception here."""
    root = Path(root) if root is not None else ROOT
    backend = backend if backend is not None else DefaultBackend(root)
    report = _base_report()
    try:
        run = backend.pu_status(config=config, timeout=timeout)
        if run is None:
            report["pu"] = {"availability": "unavailable",
                            "reason": "psvr_diag.exe not built (see BUILD.md); "
                                      "no USB was touched",
                            **parse_control_status("")}
        else:
            failure = _process_failure(run)
            status = parse_control_status("" if failure else run.stdout)
            report["pu"] = {"availability": "ok", "returncode": run.returncode,
                            **status}
            if failure:
                report["pu"].update(failure)
            elif status["source"] != "F0":
                report["pu"].update(availability="unavailable",
                                    reason="no F0 control_status line in output")
    except Exception as exc:
        report["pu"] = {"availability": "unavailable", "reason": str(exc),
                        **parse_control_status("")}
    try:
        proc = backend.powershell(PS_PNP, timeout=timeout)
        failure = _process_failure(proc)
        rows = parse_rows(proc.stdout, ("instance_id", "name", "class", "status"))
        report["usb"] = failure or {"availability": "ok", **pnp_facts(rows)}
    except Exception as exc:
        report["usb"] = {"availability": "unavailable", "reason": str(exc)}
    try:
        proc = backend.powershell(PS_ENDPOINTS, timeout=timeout)
        rows = parse_rows(proc.stdout,
                          ("device_id", "name", "status", "problem_code"))
        report["endpoints"] = _process_failure(proc) or {
            "availability": "ok", **endpoint_facts(rows)}
    except Exception as exc:
        report["endpoints"] = {"availability": "unavailable", "reason": str(exc)}
    try:
        proc = backend.powershell(PS_SOUND, timeout=timeout)
        rows = parse_rows(proc.stdout, ("name", "status"))
        report["sound_devices"] = _process_failure(proc) or {
            "availability": "ok", "devices": rows}
    except Exception as exc:
        report["sound_devices"] = {"availability": "unavailable", "reason": str(exc)}
    return report


def offline_report():
    """Fact/guidance-only report: no USB, no PowerShell, safe for CI/tests."""
    report = _base_report()
    report["offline"] = True
    report["pu"] = {"availability": "skipped-offline", **parse_control_status("")}
    for key in ("usb", "endpoints", "sound_devices"):
        report[key] = {"availability": "skipped-offline"}
    return report
