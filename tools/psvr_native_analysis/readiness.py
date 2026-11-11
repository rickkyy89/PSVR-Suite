# SPDX-License-Identifier: MIT
"""Pure Direct Mode readiness classifier over an observation bundle.

Consumes a fixture or the collector bundle produced by
scripts/collect_direct_readiness.ps1 and decides prerequisites only. It never
touches hardware, never claims a GPU target was acquired and never claims
Direct Mode works; the outcome is "blocked", "next_probe_possible" or
"unknown". Deliberate epistemic rules:

- Windows desktop membership gates the outcome. The NVAPI os_visible flag is
  observational only: a successful direct route legitimately hides the HMD
  from the OS and the public inventory can omit direct displays.
- Counts from private direct-enumeration rows with a failed status are
  ignored; only status==0 rows are meaningful. Those rows are vendor-scoped,
  so a positive count is a vendor candidate awaiting product (0x6A04)
  confirmation, never a PSVR match claim.
- A Display Core target is selected only with an exact device-interface-path
  match (case-insensitive), connected==True, stale==False and a usage_kind of
  HeadMounted or SpecialPurpose. Duplicate exact matches are ambiguous and
  nothing is selected. Unknown usage or flags stay unknown. Standard desktop
  usage is rejected even when the path matches. An inspector object marked
  ok:false is not trusted even when the transport reported status "ok".
- A missing runtime summary, a non-zero probe exit code, a failure status or
  a non-summary event leaves fullscreen unknown, not false. A healthy summary
  carries a reason string (for example valid_target_pose_collected); the
  reason alone never invalidates the measured fullscreen bool.
- An applied EDID plan is a plan state, not proof of rendering.

Outcome decision table:
- blocked: at least one prerequisite is affirmatively unmet
  (HMD on the desktop, or empty direct lists combined with an affirmatively
  unavailable/no-match Display Core route).
- next_probe_possible: the HMD is off the desktop and at least one route is
  affirmatively open (vendor candidate or Display Core available target).
  Reaching this may still require a product identity probe; it is not a
  claim that the target was acquired.
- unknown: any prerequisite is unobserved; otherwise the default.
"""
import argparse
import json
from pathlib import Path

SCHEMA_VERSION = 1
SONY_VENDOR = 0xD94D
SONY_PRODUCT = 0x6A04
# The private enumeration tests both vendor byte orders; accept either.
SONY_VENDORS = {SONY_VENDOR, 0x4DD9}
VENDOR_CANDIDATE_STATE = "vendor_candidate_needs_product_confirmation"
# WinRT DisplayTargetUsageKind values that can represent a specialized HMD
# target; anything else is not eligible for selection.
ELIGIBLE_USAGE_KINDS = ("HeadMounted", "SpecialPurpose")
# Known desktop usage kinds: an exact path match with one of these is an
# affirmative rejection, not missing evidence.
DESKTOP_USAGE_KINDS = ("Standard", "Primary", "Hidden")

SOURCE_NAMES = (
    "windows_active_inventory",
    "nvidia_public_displays",
    "windows_display_core",
    "edid_plan_inspection",
)

DISCLAIMERS = (
    "Read-only classification; no GPU target acquisition was attempted or claimed.",
    "Outcome reports prerequisites for a supervised probe, not a working Direct Mode.",
    "NVAPI os_visible is observational and never a gate: a working direct route "
    "legitimately hides the display and public inventories can omit direct displays.",
    "A failed or missing runtime probe leaves the fullscreen state unknown, not false.",
    "An applied EDID plan state does not establish rendering.",
)


def get_source(bundle, name):
    """Return (data_or_None, status_string, source_error). Data counts only when
    the transport status is ok and the inspector itself did not report ok:false.
    Failed paths keep the source error text as context for the assessment."""
    entry = bundle.get("sources", {}).get(name)
    if not isinstance(entry, dict):
        return None, "missing", None
    status = entry.get("status", "failed")
    error = entry.get("error")
    data = entry.get("data")
    if status != "ok" or not isinstance(data, dict):
        return None, status, error
    if data.get("ok") is False:
        return None, "inspector_not_ok", data.get("error") or error
    return data, status, None


def is_sony_edid_ids(ids):
    return bool(ids) and ids.get("manufacturer") == SONY_VENDOR and ids.get("product") == SONY_PRODUCT


def check_windows_desktop(bundle):
    """HMD is on a desktop active path ('active') or absent from all of them ('removed')."""
    data, status, error = get_source(bundle, "windows_active_inventory")
    if data is None or not isinstance(data.get("displays"), list):
        return {"state": "unknown", "source_status": status, "source_error": error}
    for display in data["displays"]:
        if is_sony_edid_ids(display.get("edid_ids")):
            return {"state": "active", "source_status": status,
                    "evidence": {"monitor_device_path": display.get("monitor_device_path")}}
    return {"state": "removed", "source_status": status,
            "note": "Absence from active desktop paths; gates the outcome."}


def check_nvidia_os_visible(bundle):
    """Observational only: public NVAPI inventory exposure. Never blocks or enables
    an outcome because a legitimate direct route hides the HMD from the OS."""
    data, status, error = get_source(bundle, "nvidia_public_displays")
    if data is None or not isinstance(data.get("displays"), list):
        return {"state": "unknown", "source_status": status, "source_error": error}
    sony = [d for d in data["displays"]
            if d.get("edid_vendor") == SONY_VENDOR and d.get("edid_product") == SONY_PRODUCT]
    state = "visible" if any(d.get("os_visible") is True for d in sony) else "not_visible"
    return {"state": state, "source_status": status,
            "note": "Observational; not a prerequisite and not a substitute for desktop membership."}


def check_direct_enumeration(bundle):
    """Private direct lists, vendor-scoped: candidate rows only prove a Sony-vendor
    list became non-empty; product 0x6A04 identity still needs a separate probe.
    Counts from failed-status rows are ignored; malformed counts keep the state
    unknown instead of crashing."""
    data, status, error = get_source(bundle, "nvidia_public_displays")
    if data is None:
        return {"state": "unknown", "source_status": status, "source_error": error}
    rows = data.get("direct_enumeration")
    if not isinstance(rows, list):
        return {"state": "no_data", "source_status": status,
                "note": "Source collected without -IncludeDirectEnumeration."}
    sony = [r for r in rows if r.get("vendor") in SONY_VENDORS]
    ignored = [{"vendor": r.get("vendor"), "mode": r.get("mode"),
                "status": r.get("status"), "count": r.get("count")}
               for r in sony if r.get("status") != 0]
    successful = [r for r in sony if r.get("status") == 0]
    base = {"source_status": status, "ignored_failed_rows": ignored,
            "note": "Rows expose a vendor only; product 0x6A04 is never identified here. "
                    "Counts from failed-status rows are ignored, not evidence."}
    if not successful:
        return {"state": "unknown", **base}
    malformed = [r for r in successful if not (isinstance(r.get("count"), int)
                                               and not isinstance(r.get("count"), bool))]
    if malformed:
        return {"state": "unknown", **base,
                "malformed_count_rows": [{"vendor": r.get("vendor"), "mode": r.get("mode"),
                                          "count": r.get("count")} for r in malformed],
                "note": base["note"] + " Non-integer counts are unknown, not candidates."}
    state = VENDOR_CANDIDATE_STATE if any(r["count"] > 0 for r in successful) else "empty"
    return {"state": state, **base}


def sony_inventory_path(bundle):
    """Fallback HMD path reference from the desktop inventory.

    Returns (kind, path) where kind is 'single' (one Sony monitor path),
    'ambiguous' (two or more; the first is NEVER chosen), 'none' (no Sony
    display on the desktop) or 'unknown' (source failed/absent)."""
    data, _, _ = get_source(bundle, "windows_active_inventory")
    if data is None or not isinstance(data.get("displays"), list):
        return "unknown", None
    paths = [display.get("monitor_device_path") for display in data["displays"]
             if is_sony_edid_ids(display.get("edid_ids")) and display.get("monitor_device_path")]
    if len(paths) == 1:
        return "single", paths[0]
    if len(paths) > 1:
        return "ambiguous", None
    return "none", None


def check_display_core(bundle):
    """Windows Display Core target availability.

    Selection requires exactly one target: exact case-insensitive path match,
    connected==True, stale==False, usage_kind in ELIGIBLE_USAGE_KINDS.
    The path reference is the explicit hmd.device_interface_path; when absent,
    the fallback accepts a desktop inventory with exactly one Sony monitor path
    and reports 'ambiguous' instead of picking the first of several.
    - duplicate exact matches: 'ambiguous' (nothing selected)
    - eligible usage but disconnected or stale: 'unavailable'
    - known desktop usage (Standard/Primary/Hidden) on a match: 'unavailable'
    - unrecognized/missing usage or non-boolean flags: 'unknown'
    - successful query with no matched target: 'no_match'
    Connector and LUID values alone remain hint_only and are never selected.
    """
    data, status, error = get_source(bundle, "windows_display_core")
    if data is None or not isinstance(data.get("targets"), list):
        return {"state": "unknown", "source_status": status, "source_error": error,
                "note": "Availability unknown, not false: no successful query to judge."}
    hmd = bundle.get("hmd") or {}
    expected = hmd.get("device_interface_path")
    if not expected:
        fallback_kind, fallback_path = sony_inventory_path(bundle)
        expected = fallback_path
        if not expected:
            if fallback_kind == "ambiguous":
                return {"state": "ambiguous", "source_status": status, "hints": [],
                        "source_error": error,
                        "note": "The desktop exposes multiple Sony monitor paths; the fallback "
                                "never chooses the first, so there is no exact reference."}
            return {"state": "unknown", "source_status": status, "hints": [],
                    "source_error": error,
                    "note": "No HMD device-interface-path reference was provided, so nothing "
                            "can be matched; this is unknown, not false."}
    luid = hmd.get("adapter_luid") or {}
    hints = []
    matched = []
    for target in data["targets"]:
        path = (target.get("device_interface_path") or "").lower()
        exact = path == str(expected).lower()
        luid_match = (luid and target.get("adapter_id_low") == luid.get("low")
                      and target.get("adapter_id_high") == luid.get("high"))
        if luid_match and not exact:
            hints.append({"device_interface_path": target.get("device_interface_path"),
                          "adapter_id_low": target.get("adapter_id_low"),
                          "adapter_id_high": target.get("adapter_id_high"),
                          "connected": target.get("connected"), "stale": target.get("stale"),
                          "usage_kind": target.get("usage_kind"),
                          "selection": "hint_only", "selected": False})
        if exact:
            matched.append(target)
    base = {"source_status": status, "hints": hints,
            "expected_device_interface_path": expected,
            "note": "Selection requires one exact path match, connected==True, stale==False "
                    "and a HeadMounted/SpecialPurpose usage; LUID/connector values are hints."}
    if len(matched) > 1:
        return {"state": "ambiguous", **base,
                "matched_count": len(matched),
                "note": base["note"] + " Multiple exact matches are ambiguous; nothing selected."}
    if not matched:
        return {"state": "no_match", **base}
    target = matched[0]
    connected = target.get("connected")
    stale = target.get("stale")
    usage = target.get("usage_kind")
    if not isinstance(connected, bool) or not isinstance(stale, bool) or not isinstance(usage, str):
        return {"state": "unknown", **base,
                "note": base["note"] + " Unknown usage or flags keep availability unknown, not false."}
    if connected and not stale and usage in ELIGIBLE_USAGE_KINDS:
        return {"state": "available", **base}
    if usage in ELIGIBLE_USAGE_KINDS:
        return {"state": "unavailable", **base,
                "reason": "disconnected or stale"}
    if usage in DESKTOP_USAGE_KINDS:
        return {"state": "unavailable", **base,
                "reason": f"desktop usage kind {usage} is not an HMD target"}
    return {"state": "unknown", **base,
            "note": base["note"] + f" Unrecognized usage_kind {usage!r} is unknown."}


def check_runtime_fullscreen(bundle):
    """Fullscreen only from a consumer-provided summary; the collector never probes.
    A non-zero exit code, a failure status or a non-summary event is unknown.
    A measured bool from a healthy summary is a real measurement even when the
    summary carries a success reason such as valid_target_pose_collected."""
    block = bundle.get("runtime_summary")
    if not isinstance(block, dict):
        block = {"status": "absent"}
    if block.get("status") != "provided" or not isinstance(block.get("data"), dict):
        return {"state": "unknown", "source_status": block.get("status", "absent"),
                "note": "No valid runtime summary; the collector avoids the runtime probe by design."}
    summary = block["data"]
    failures = []
    for key in ("exit_code", "probe_exit", "runtime_probe_exit"):
        value = summary.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value != 0:
            failures.append(f"{key}={value}")
    event = summary.get("event")
    if event is not None and event != "summary":
        failures.append(f"event={event!r}")
    status_value = summary.get("status")
    if isinstance(status_value, str) and status_value.lower() in ("failed", "error"):
        failures.append(f"status={status_value!r}")
    if failures:
        return {"state": "unknown", "source_status": "provided",
                "evidence": {"failures": failures, "reason": summary.get("reason")},
                "note": "A failed probe or IPC error is unknown, not a false fullscreen measurement."}
    compositor = summary.get("compositor") if isinstance(summary.get("compositor"), dict) else {}
    fullscreen = compositor.get("fullscreen", summary.get("fullscreen"))
    if isinstance(fullscreen, bool):
        return {"state": "true" if fullscreen else "false", "source_status": "provided",
                "evidence": {"reason": summary.get("reason")}}
    return {"state": "unknown", "source_status": "provided",
            "note": "No measured fullscreen in the summary; unknown, not false."}


def check_edid_plan(bundle):
    """Plan/restore-gate state read from the read-only inspector; applied != rendering."""
    data, status, error = get_source(bundle, "edid_plan_inspection")
    if data is None:
        return {"state": "unknown", "source_status": status, "source_error": error,
                "note": "Plan state unknown; no inference either way."}
    applied = data.get("plan_applied")
    state = "applied" if applied is True else ("not_applied" if applied is False else "unknown")
    return {"state": state, "source_status": status,
            "restore_gate_accepted": data.get("restore_gate_accepted"),
            "note": "A plan-applied state is not proof of rendering; rendering needs a separate observation."}


def classify(bundle):
    if not isinstance(bundle, dict):
        bundle = {}
    checks = {
        "windows_desktop": check_windows_desktop(bundle),
        "nvidia_os_visible": check_nvidia_os_visible(bundle),
        "nvidia_direct_enumeration": check_direct_enumeration(bundle),
        "display_core_target": check_display_core(bundle),
        "runtime_fullscreen": check_runtime_fullscreen(bundle),
        "edid_plan": check_edid_plan(bundle),
    }
    desktop = checks["windows_desktop"]["state"]
    visible = checks["nvidia_os_visible"]["state"]
    direct = checks["nvidia_direct_enumeration"]["state"]
    core = checks["display_core_target"]["state"]

    blocking = []
    if desktop == "active":
        blocking.append("HMD is present on a Windows desktop active path; the Direct Mode "
                        "prerequisite requires it to be off the desktop.")
    if direct == "empty" and core in ("unavailable", "no_match"):
        blocking.append("Neither an NVIDIA direct-enumeration vendor candidate nor a Windows Display "
                        "Core target with exact path match, connected, not stale and HMD usage is observed.")

    missing = []
    for name, check in checks.items():
        if check["state"] in ("unknown", "no_data", "ambiguous"):
            missing.append(f"{name} state is '{check['state']}' (source status "
                           f"'{check.get('source_status')}'); no false inference is made from it.")

    route_via_direct = direct == VENDOR_CANDIDATE_STATE
    route_via_core = core == "available"
    if blocking:
        outcome = "blocked"
    elif desktop == "removed" and (route_via_direct or route_via_core):
        outcome = "next_probe_possible"
    else:
        outcome = "unknown"

    extra = []
    if visible == "not_visible":
        extra.append("NVAPI public inventory shows no visible HMD; observational only, because a "
                     "successful direct route legitimately hides the display from the OS.")
    if outcome == "next_probe_possible" and route_via_direct:
        extra.append("The direct-enumeration route is vendor-scoped: product 0x6A04 identity is not "
                     "confirmed here, so the next step may be a product identity probe; this outcome "
                     "is not a claim that an HMD target was acquired.")

    source_status = {}
    for name in SOURCE_NAMES:
        entry = bundle.get("sources", {}).get(name) or {}
        source_status[name] = {"status": entry.get("status", "missing"),
                               "begin_utc": entry.get("begin_utc"), "end_utc": entry.get("end_utc")}
    runtime = bundle.get("runtime_summary") or {}
    source_status["runtime_summary"] = {"status": runtime.get("status", "absent")}

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "direct_readiness_assessment",
        "bundle_created_utc": bundle.get("created_utc"),
        "source_status": source_status,
        "checks": checks,
        "outcome": outcome,
        "blocking_evidence": blocking,
        "missing_evidence": missing,
        "reasons": blocking + missing + extra + list(DISCLAIMERS),
        "claims": {"gpu_acquisition_claimed": False, "direct_mode_working_claimed": False},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bundle", type=Path, help="Readiness bundle JSON (fixture or collector output)")
    parser.add_argument("--out", type=Path, help="Write the assessment here instead of stdout")
    args = parser.parse_args()
    bundle = json.loads(args.bundle.read_text(encoding="utf-8-sig"))
    assessment = classify(bundle)
    if args.out:
        try:
            with args.out.open('x', encoding='utf-8') as stream:
                stream.write(json.dumps(assessment, indent=2))
        except FileExistsError:
            raise SystemExit(f"Refusing to overwrite existing file: {args.out}")
    else:
        print(json.dumps(assessment, indent=2))


if __name__ == "__main__":
    main()
