# SPDX-License-Identifier: MIT
"""Summarize the dedicated transition capture, without dumping unrelated logs."""
import argparse
import json
from pathlib import Path

def _new_report():
    return {"records": 0, "samples": [], "log_evidence": [], "skipped": 0}


def _summarize_row(row):
    """Build (item, log_evidence) for one record; raise on malformed rows."""
    nvidia = row.get("nvidia") or []
    lists = row.get("direct_enumeration") or []
    windows = row.get("windows") or []
    if not isinstance(nvidia, (list, tuple)):
        raise TypeError("nvidia is not a list")
    if not isinstance(lists, (list, tuple)):
        raise TypeError("direct_enumeration is not a list")
    if not isinstance(windows, (list, tuple)):
        raise TypeError("windows is not a list")
    sony = [d for d in nvidia if isinstance(d, dict)
            and d.get("edid_vendor") == 0xD94D and d.get("edid_product") == 0x6A04]
    summary = row.get("runtime_summary") or []
    if isinstance(summary, dict):
        summary = [summary]
    elapsed = row.get("elapsed_s")
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
        raise TypeError("elapsed_s is not a number")
    evidence = row.get("new_log_evidence") or []
    if not isinstance(evidence, (list, tuple)):
        raise TypeError("new_log_evidence is not a list")
    successful = [d for d in lists if isinstance(d, dict)
                  and d.get("status") == 0 and d.get("vendor") == 0xD94D]
    exit_code = row.get("runtime_probe_exit")
    item = {
        "elapsed_s": round(elapsed, 2), "phase": row.get("phase"),
        "sony_windows_active": any(isinstance(d, dict)
                                   and (d.get("edid_ids") or {}).get("manufacturer") == 0xD94D
                                   and (d.get("edid_ids") or {}).get("product") == 0x6A04
                                   for d in windows),
        "sony_os_visible": [d.get("os_visible") for d in sony],
        "direct_lists": [[d.get("vendor"), d.get("mode"), d.get("status"), d.get("count")]
                         for d in lists if isinstance(d, dict)],
        "direct_candidate_observed": (any(d.get("count", 0) > 0 for d in successful) if successful else None),
        "runtime_probe_exit": exit_code,
        "runtime_observation": "summary" if summary else ("probe_failed" if exit_code not in (None, 0) else "not_observed"),
        "fullscreen": None,
    }
    if summary:
        s = summary[0]
        item.update(valid=s.get("valid_count"), samples=s.get("sample_count"),
                    fullscreen=(s.get("compositor") or {}).get("fullscreen"), reason=s.get("reason"))
    return item, evidence


def _append(result, row):
    """Append one parsed record; malformed rows are skipped and counted."""
    try:
        if not isinstance(row, dict):
            raise TypeError("record is not an object")
        item, evidence = _summarize_row(row)
    except (KeyError, TypeError, ValueError, AttributeError):
        result["skipped"] += 1
        return
    result["samples"].append(item)
    result["log_evidence"].extend(evidence)


def summarize(records):
    """Summarize parsed records; malformed rows are skipped and counted."""
    result = _new_report()
    for row in records:
        result["records"] += 1
        _append(result, row)
    return result


def summarize_file(path):
    """Stream a JSONL capture line by line; malformed lines are skipped and counted."""
    result = _new_report()
    with open(path, encoding="utf-8-sig") as handle:
        for line in handle:
            if not line.strip():
                continue
            result["records"] += 1
            try:
                row = json.loads(line)
            except ValueError:
                result["skipped"] += 1
                continue
            _append(result, row)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("capture", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize_file(args.capture), indent=2))
