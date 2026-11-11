# SPDX-License-Identifier: MIT
"""CLI for the read-only audio diagnostics (``python -m tools.psvr_audio_diag``)."""
from __future__ import annotations

import argparse
import json
import math
import sys

from .scan import DEFAULT_TIMEOUT_S, ROOT, collect, offline_report


def positive_timeout(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("timeout must be finite and greater than zero")
    return seconds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="psvr_audio_diag",
        description=(
            "Read-only audio state report for PSVR1 processor-unit modes. "
            "It never sends power/mode/LED commands, never changes SteamVR "
            "settings and never touches driver bindings. VR-mode headphone "
             "silence is not diagnosed by this inventory: no fix is promised here."
        ),
    )
    parser.add_argument("--root", default=str(ROOT),
                        help="repo root (default: %(default)s)")
    parser.add_argument("--config", default=None,
                        help="psvr_diag config.json path (default: <root>/config.json)")
    parser.add_argument("--timeout", type=positive_timeout, default=DEFAULT_TIMEOUT_S,
                        help="per-query timeout in seconds (default: %(default)s)")
    parser.add_argument("--offline", action="store_true",
                        help="print facts/guidance only: no USB, no PowerShell")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable JSON output")
    return parser


def format_text(report):
    """Short human summary; every value comes straight from the report dict."""
    mode = "offline" if report.get("offline") else "live"
    lines = ["[audio_diag] read-only report (%s)" % mode]
    pu = report.get("pu", {})
    lines.append("PU mode: %s (availability: %s, source: %s)"
                 % (pu.get("pu_mode", "unknown"),
                    pu.get("availability", "-"), pu.get("source", "-")))
    usb = report.get("usb", {})
    if usb.get("availability") == "ok":
        lines.append("PSVR1 USB: present=%s interfaces=%d MI_01 rows=%d"
                     % (usb.get("psvr_composite_present"),
                        usb.get("psvr_interface_count", 0),
                        len(usb.get("mi01_rows", []))))
    else:
        lines.append("PSVR1 USB: %s" % usb.get("availability", "-"))
    eps = report.get("endpoints", {})
    if eps.get("availability") == "ok":
        lines.append("Render endpoints listed: %d (headset-like: %s)"
                     % (len(eps.get("render_endpoints", [])),
                        ", ".join(eps.get("headset_like_endpoints", [])) or "-"))
    else:
        lines.append("Render endpoints: %s" % eps.get("availability", "-"))
    sound = report.get("sound_devices", {})
    lines.append("Sound devices: %s" % (
        len(sound.get("devices", [])) if sound.get("availability") == "ok"
        else sound.get("availability", "-")))
    for key in ("pu", "usb", "endpoints", "sound_devices"):
        section = report.get(key, {})
        if section.get("reason"):
            lines.append("%s: %s" % (key, section["reason"]))
    default_guidance = report.get("guidance", {}).get("default_endpoint")
    if default_guidance:
        lines.append(default_guidance)
    for text in report.get("validation_pending", []):
        lines.append("pending: %s" % text)
    return "\n".join(lines)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.offline:
            report = offline_report()
        else:
            report = collect(root=args.root, config=args.config,
                             timeout=args.timeout)
    except Exception as exc:  # defensive: the collectors already degrade
        print("audio_diag failed: %s" % exc, file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(format_text(report))
    return 0
