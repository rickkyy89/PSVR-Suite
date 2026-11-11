"""Summarize LED failure stage versus apparent size, without modifying logs."""

import argparse
from collections import Counter
import json


def summarize(path):
    groups = {}
    examples = {}
    last_pose = None
    last_imu = None
    with open(path,encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # a live log can have an incomplete final line
            if row.get("event") != "pose":
                continue
            last_pose = {key:row.get(key) for key in
                ("monotonic_s","valid","reason","candidate_count","relative_camera_m","led_labels")}
            if row.get("imu"):
                last_imu = {key:row["imu"].get(key) for key in
                    ("valid","state","session_id","recenter_epoch","sample_time_s")}
            candidates = row.get("candidates",[])
            count = row.get("candidate_count",len(candidates))
            if count != 5:
                bucket = "not_five"
            else:
                coords = [c["center_px"] for c in candidates]
                span = max(max(p[a] for p in coords)-min(p[a] for p in coords) for a in (0,1))
                bucket = "span_lt80" if span < 80 else "span_80_120" if span < 120 else "span_120_200" if span < 200 else "span_ge200"
            reasons = groups.setdefault(bucket,Counter())
            reason = "valid" if row.get("valid") else row.get("reason","unknown").split(":")[0]
            reasons[reason] += 1
            if not row.get("valid") and count == 5 and "modello non compatibile" in reason:
                examples[bucket] = {key:row.get(key) for key in
                    ("monotonic_s","rms_px","rms_limit_px","image_span_px","rms_fraction","reason")}
    return {"groups":{key:dict(value) for key,value in groups.items()},"model_failure_examples":examples,
            "last_pose":last_pose,"last_imu":last_imu}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("log")
    args = parser.parse_args()
    print(json.dumps(summarize(args.log),indent=2))
