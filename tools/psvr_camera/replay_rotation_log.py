"""Replay recorded candidates/IMU for matcher regression, without hardware."""

import argparse
from collections import Counter,deque
import json
import time
import numpy as np
from .calibration import load_intrinsics
from .imu_led_pose import ImuLedTracker,MODEL_TO_IMU,load_rotation_rig,rotation_from_quaternion


def replay(log,intrinsics_path,rig_path,max_frames=4000,end_time=None):
    rows=deque(maxlen=max_frames)
    with open(log,encoding="utf-8") as stream:
        for line in stream:
            if '"quaternion_sensor_wxyz": [' not in line:
                continue
            try:
                row=json.loads(line)
            except json.JSONDecodeError:
                continue
            if end_time is not None and row.get("monotonic_s",0) > end_time:
                continue
            if (row.get("event") == "pose" and (row.get("imu") or {}).get("valid")
                    and len(row.get("candidates",[])) >= 2):
                rows.append(row)
    intrinsics=load_intrinsics(intrinsics_path)
    rig,points,normals=load_rotation_rig(rig_path)
    tracker=ImuLedTracker(intrinsics,points,normals,rig["labels"])
    seeded=False
    recorded_valid=replayed_valid=0
    partial_recorded=partial_replayed=partial_total=0
    reasons=Counter()
    durations=[]
    steps=[]
    previous=None
    for row in rows:
        sample=row["imu"]
        tracker.set_imu(sample)
        now=row["monotonic_s"]
        if not seeded and row.get("valid") and row.get("rvec"):
            sensor=rotation_from_quaternion(sample["quaternion_sensor_wxyz"])
            rotation=tracker.cv2.Rodrigues(np.asarray(row["rvec"],float))[0]
            tracker.alignment=rotation@MODEL_TO_IMU.T@sensor.T
            tracker.translation=np.asarray(row["position_camera_m"],float).reshape(3,1)
            tracker.last_match=now
            tracker.epoch=sample.get("recenter_count",0)
            tracker.front.acquisition_span=100.
            seeded=True
        started=time.perf_counter()
        pose=tracker.update(row.get("candidates",[]),now)
        durations.append((time.perf_counter()-started)*1000)
        recorded_valid+=bool(row.get("valid"))
        replayed_valid+=pose.valid
        if pose.valid:
            if previous is not None and now-previous[0] < .1:
                steps.append(float(np.linalg.norm(pose.tvec-previous[1]))*1000)
            previous=(now,pose.tvec.copy())
        if len(row.get("candidates",[])) < 5:
            partial_total+=1
            partial_recorded+=bool(row.get("valid"))
            partial_replayed+=pose.valid
        if not pose.valid:
            reasons[pose.reason.split(":")[0]]+=1
    return {"frames":len(rows),"recorded_valid":recorded_valid,"replayed_valid":replayed_valid,
        "partial_frames":partial_total,"partial_recorded_valid":partial_recorded,
        "partial_replayed_valid":partial_replayed,"matcher_median_ms":float(np.median(durations)) if durations else None,
        "matcher_p95_ms":float(np.percentile(durations,95)) if durations else None,
        "rejection_reasons":dict(reasons),
        "translation_step_p99_mm":float(np.percentile(steps,99)) if steps else None,
        "translation_step_max_mm":max(steps) if steps else None,
        "steps_over_50mm":sum(step > 50 for step in steps),
        "note":"Recorded candidates came from the old ROI; this is not a new physical trial."}


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    for key in ("log","intrinsics","rig"):
        parser.add_argument("--"+key,required=True)
    parser.add_argument("--max-frames",type=int,default=4000)
    parser.add_argument("--end-time",type=float,default=None)
    args=parser.parse_args()
    print(json.dumps(replay(args.log,args.intrinsics,args.rig,args.max_frames,args.end_time),indent=2))
