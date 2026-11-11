# SPDX-License-Identifier: MIT
"""Read-only summary of capture_presentation_trial.ps1 output (stdlib only)."""
import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("capture timestamp must include timezone")
    return result


def summarize(directory):
    directory = Path(directory)
    metadata = load(directory / "metadata.json")
    completion_path = directory / "completion.json"
    completion = load(completion_path) if completion_path.exists() else None
    warnings = []
    exits, fullscreen, rendering = Counter(), Counter(), Counter()
    valid = total = parsed = 0
    first_failure = None
    start = end = None
    sony_states = set()
    files = sorted(directory.glob("sample-*.json"))
    for path in files:
        sample = load(path)
        started = timestamp(sample["start_utc"])
        ended = timestamp(sample["end_utc"])
        start = min(start, started) if start else started
        end = max(end, ended) if end else ended
        exits[str(sample["runtime_probe_exit"])] += 1
        summaries = []
        for line in sample["runtime_probe_lines"]:
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                warnings.append(f"{path.name}: malformed probe JSON")
                continue
            if event.get("event") == "summary":
                summaries.append(event)
        if len(summaries) != 1:
            warnings.append(f"{path.name}: expected one probe summary, got {len(summaries)}")
            summary = {}
        else:
            summary = summaries[0]
            parsed += 1
            valid += summary["valid_count"]
            total += summary["sample_count"]
        compositor = summary.get("compositor") or {}
        full, render = compositor.get("fullscreen"), compositor.get("can_render_scene")
        fullscreen[str(full).lower()] += 1
        rendering[str(render).lower()] += 1
        reasons = []
        if sample["runtime_probe_exit"] != 0:
            reasons.append("probe_failed")
        if not summary:
            reasons.append("missing_probe_summary")
        elif summary["valid_count"] != summary["sample_count"]:
            reasons.append("invalid_pose_samples")
        if full is not True:
            reasons.append("fullscreen_false_or_unavailable")
        if render is not True:
            reasons.append("render_false_or_unavailable")
        if reasons and first_failure is None:
            first_failure = {"file": path.name, "timestamp_utc": sample["start_utc"], "reasons": reasons}
        for display in sample["displays"]["displays"]:
            if "SNY6A04" not in (display.get("monitor_device_path") or ""):
                continue
            source = display["source"]
            refresh = display["refresh"]
            signal = display["target"]["signal_refresh"]
            sony_states.add((display["monitor_device_path"], source["x"], source["y"],
                             source["width"], source["height"], refresh["numerator"],
                             refresh["denominator"], signal["numerator"], signal["denominator"]))
    requested = metadata["duration_seconds"]
    elapsed = completion["elapsed_seconds"] if completion else None
    duration_reached = elapsed is not None and elapsed >= requested
    count_matches = completion is not None and completion["samples"] == len(files)
    if not duration_reached:
        warnings.append("requested capture duration was not reached or completion record is missing")
    if not count_matches:
        warnings.append("completion sample count does not match capture files or completion is missing")
    if len(sony_states) != 1:
        warnings.append("Sony SNY6A04 state missing or changed; inspect live display inventory")
    synthetic = metadata.get("synthetic_presence_requested")
    if synthetic is None:
        warnings.append("older capture metadata: synthetic presence not specified; inspect driver log")
    passed = (bool(files) and duration_reached and count_matches and parsed == len(files)
              and first_failure is None and len(sony_states) == 1 and not warnings)
    return {
        "schema_version": 1, "capture_directory": str(directory.resolve()), "read_only": True,
        "unattended_sampled_checks_passed": passed,
        "physical_lens_acceptance_verified": False,
        "requested_duration_seconds": requested, "elapsed_seconds": elapsed,
        "requested_duration_reached": duration_reached,
        "samples": len(files), "summaries_parsed": parsed,
        "sampled_start_utc": start.isoformat() if start else None,
        "sampled_end_utc": end.isoformat() if end else None,
        "probe_exit_counts": dict(exits), "fullscreen_counts": dict(fullscreen),
        "can_render_scene_counts": dict(rendering),
        "valid_pose_samples": valid, "total_pose_samples": total,
        "first_failed_sample": first_failure,
        "sony_display_states": [dict(zip(
            ("monitor_path", "x", "y", "width", "height", "path_refresh_numerator",
             "path_refresh_denominator", "signal_refresh_numerator", "signal_refresh_denominator"), state))
            for state in sorted(sony_states)],
        "synthetic_presence_requested": synthetic,
        "driver_sha256": metadata.get("sha256"), "warnings": warnings,
        "limitations": "Sampled observations, not continuous frame delivery or lens acceptance. "
                       "Does not infer hardware failure cause or diagnose -202 from invalid poses. "
                       "Inspect retained compositor/driver logs for events between samples.",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_directory", type=Path)
    args = parser.parse_args(argv)
    try:
        report = summarize(args.capture_directory)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(2, f"Invalid/incomplete capture: {error}\n")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["unattended_sampled_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
