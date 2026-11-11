"""Triangulate side/rear lamp centres using IMU rotation and front anchors.

Front geometry stays fixed. Translation is solved from front lamps only, so
the side/rear estimate does not train on its own previous pose translation.
Held-out rays gate each proposed update. Absolute scale remains unvalidated.
"""

import argparse
import json
from pathlib import Path
import numpy as np

from .calibration import load_intrinsics
from .camera_backend import import_cv2
from .imu_led_pose import load_rotation_rig


def ray_system(normalized):
    return np.array([[1,0,-normalized[0]],[0,1,-normalized[1]]],float)


def refine(log,intrinsics_path,rig_path,output):
    output = Path(output)
    if output.exists() or not output.parent.is_dir():
        raise ValueError("new output file in an existing directory required")
    cv2 = import_cv2()
    intrinsics = load_intrinsics(intrinsics_path)
    rig,points,normals = load_rotation_rig(rig_path)
    labels = rig["labels"]
    observations = {i:[] for i in range(5,9)}
    seen = {i:set() for i in range(5,9)}
    with open(log,encoding="utf-8") as stream:
        for line in stream:
            if '"reason": "imu_guided_pattern"' not in line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not row.get("valid") or not row.get("led_labels"):
                continue
            ids = [labels.index(name) for name in row["led_labels"]]
            front = [j for j,i in enumerate(ids) if i < 5]
            if len(front) < 2:
                continue
            rvec = np.asarray(row["rvec"],float)
            rotation = cv2.Rodrigues(rvec)[0]
            image = np.asarray(row["image_points"],float)
            rays = cv2.undistortPoints(image.reshape(-1,1,2),intrinsics.camera_matrix,
                                       intrinsics.distortion_coeffs).reshape(-1,2)
            matrix = np.vstack([ray_system(rays[j]) for j in front])
            values = np.hstack([-ray_system(rays[j])@rotation@points[ids[j]] for j in front])
            if np.linalg.cond(matrix) > 250:
                continue
            translation = np.linalg.lstsq(matrix,values,rcond=None)[0]
            if translation[2] <= .15:
                continue
            projected = cv2.projectPoints(points[[ids[j] for j in front]],rvec,translation,
                intrinsics.camera_matrix,intrinsics.distortion_coeffs)[0].reshape(-1,2)
            if np.sqrt(np.mean(np.sum((projected-image[front])**2,axis=1))) > 1.5:
                continue
            for j,i in enumerate(ids):
                if i < 5:
                    continue
                bucket = tuple(np.round(rvec/.025).astype(int))
                if bucket in seen[i]:
                    continue
                seen[i].add(bucket)
                a = ray_system(rays[j])
                observations[i].append((a@rotation,-a@translation,rotation,translation,image[j]))
    report = {}
    changed = False
    for i,views in observations.items():
        entry = {"views":len(views),"accepted":False}
        report[labels[i]] = entry
        if len(views) < 18:
            entry["reason"] = "insufficient independent angular views"
            continue
        training = [v for j,v in enumerate(views) if j%3]
        holdout = [v for j,v in enumerate(views) if not j%3]
        matrix = np.vstack([v[0] for v in training])
        values = np.hstack([v[1] for v in training])
        if np.linalg.cond(matrix) > 100:
            entry["reason"] = "insufficient ray diversity"
            continue
        point = np.linalg.lstsq(matrix,values,rcond=None)[0]
        # Robustly discard inconsistent rays; never move a point arbitrarily.
        residual = np.linalg.norm((matrix@point-values).reshape(-1,2),axis=1)
        cutoff = max(.002,float(np.median(residual))*2.5)
        keep = [v for j,v in enumerate(training) if residual[j] <= cutoff]
        if len(keep) < 12:
            entry["reason"] = "too few consistent rays"
            continue
        point = np.linalg.lstsq(np.vstack([v[0] for v in keep]),np.hstack([v[1] for v in keep]),rcond=None)[0]
        if np.linalg.norm(point-points[i]) > .06:
            entry["reason"] = "update exceeds 6cm plausibility bound"
            continue
        def errors(p):
            result=[]
            for _,_,r,t,pixel in holdout:
                projected=cv2.projectPoints(p.reshape(1,3),cv2.Rodrigues(r)[0],t,
                    intrinsics.camera_matrix,intrinsics.distortion_coeffs)[0].reshape(2)
                result.append(float(np.linalg.norm(projected-pixel)))
            return np.asarray(result)
        old,new = errors(points[i]),errors(point)
        entry.update({"before_median_px":float(np.median(old)),"after_median_px":float(np.median(new)),
                      "after_p90_px":float(np.percentile(new,90)),"proposed_point_m":point.tolist()})
        if np.median(new) < np.median(old)*.8 and np.percentile(new,90) < 4:
            points[i]=point
            entry["accepted"]=True
            changed=True
        else:
            entry["reason"]="held-out improvement/precision check failed"
    if changed:
        rig["object_points_m"]=points.tolist()
        rig["provenance"]="refined_front_and_imu_anchored_side_rays"
        rig["notes"] += " Accepted side/rear updates use IMU-guided rotation and translation from front anchors only; unaccepted points retain external estimates."
        rig["rotation_refinement"]={"log":str(log),"report":report,"physical_scale_validated":False}
        with output.open("x",encoding="utf-8") as stream:
            json.dump(rig,stream,indent=2)
    return {"saved":changed,"report":report}


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    for key in ("log","intrinsics","rig","out"):
        parser.add_argument("--"+key,required=True)
    args=parser.parse_args()
    print(json.dumps(refine(args.log,args.intrinsics,args.rig,args.out),indent=2))
