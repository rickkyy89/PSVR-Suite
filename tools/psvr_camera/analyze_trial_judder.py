"""Read-only position cadence and discontinuity report for a camera trial."""
import argparse
from collections import Counter
import json
import numpy as np


def analyze(path, seconds):
    rows = []
    with open(path, encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # A live writer may have an incomplete final line.
            if row.get("event") == "pose":
                rows.append(row)
    if not rows:
        return {"frames": 0}
    rows = [r for r in rows if r["monotonic_s"] >= rows[-1]["monotonic_s"]-seconds]
    def quantiles(values):
        return dict(zip(("p50", "p95", "p99", "max"),
                        np.percentile(values, [50, 95, 99, 100]).tolist())) if values else None
    dt = np.diff([r["monotonic_s"] for r in rows])
    steps, speeds, jumps = [], [], []
    for a, b in zip(rows, rows[1:]):
        pa = a.get("position_filter", {}).get("filtered_world_position_m")
        pb = b.get("position_filter", {}).get("filtered_world_position_m")
        if not a.get("valid") or not b.get("valid") or pa is None or pb is None:
            continue
        elapsed = b["monotonic_s"]-a["monotonic_s"]
        if elapsed <= 0 or (a.get("imu") or {}).get("recenter_epoch") != (b.get("imu") or {}).get("recenter_epoch"):
            continue
        step = float(np.linalg.norm(np.array(pb)-pa))
        steps.append(step*1000)
        speeds.append(step/elapsed)
        if step > .015:
            jumps.append({"time_s": b["monotonic_s"], "step_mm": step*1000,
                          "dt_ms": elapsed*1000, "labels_before": a.get("led_labels"),
                          "labels_after": b.get("led_labels"), "rms_px": b.get("rms_px")})
    gaps, gap_start = [], None
    for row in rows:
        if not row.get("valid") and gap_start is None:
            gap_start = row["monotonic_s"]
        elif row.get("valid") and gap_start is not None:
            gaps.append((row["monotonic_s"]-gap_start)*1000)
            gap_start = None
    return {"frames": len(rows), "duration_s": rows[-1]["monotonic_s"]-rows[0]["monotonic_s"],
            "valid_frames": sum(bool(r.get("valid")) for r in rows),
            "reasons": dict(Counter(r.get("reason") for r in rows)),
            "frame_dt_ms": quantiles((dt*1000).tolist()),
            "position_step_mm": quantiles(steps), "position_speed_m_s": quantiles(speeds),
            "completed_invalid_gaps_ms": quantiles(gaps),
            "steps_over_15mm": len(jumps), "largest_steps": sorted(jumps, key=lambda j:j["step_mm"], reverse=True)[:12]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log")
    parser.add_argument("--seconds", type=float, default=180.)
    args = parser.parse_args()
    print(json.dumps(analyze(args.log, args.seconds), indent=2))
