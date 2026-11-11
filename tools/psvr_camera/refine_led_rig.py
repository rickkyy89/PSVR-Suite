"""Refine front lamp proportions from multiple views at fixed camera intrinsics.

Top width and lamp depth remain the external ruler reference. Only top/bottom
height and bottom width are optimized, jointly with per-view poses. This is
not an absolute scale or anatomical head-origin calibration.
"""

import argparse
import json
from pathlib import Path

import numpy as np

from .calibration import load_intrinsics
from .camera_backend import import_cv2
from .led_pose import FrontLedTracker, load_led_rig


def outline(params, reference):
    points = reference.copy()
    points[[0,1],1] = -params[0]
    points[[3,4],1] = params[1]
    points[3,0] = -params[2]
    points[4,0] = params[2]
    return points


def fit_proportions(images, intrinsics, reference, iterations=40):
    cv2 = import_cv2()
    initial = np.array([-reference[0,1],reference[3,1],reference[4,0]])
    poses = []
    for image in images:
        ok,rs,ts,_ = cv2.solvePnPGeneric(reference,image,intrinsics.camera_matrix,
            intrinsics.distortion_coeffs,flags=cv2.SOLVEPNP_SQPNP)
        if not ok:
            raise ValueError("cannot initialize a calibration view")
        solutions = []
        for r,t in zip(rs,ts):
            projected = cv2.projectPoints(reference,r,t,intrinsics.camera_matrix,intrinsics.distortion_coeffs)[0].reshape(-1,2)
            solutions.append((float(np.sum((projected-image)**2)),r,t))
        _,r,t = min(solutions,key=lambda item:item[0])
        poses.extend([*r.reshape(3),*t.reshape(3)])
    state = np.r_[initial,poses]

    def evaluate(vector, with_jacobian=False):
        points = outline(vector[:3],reference)
        residual = np.zeros(10*len(images))
        jacobian = np.zeros((len(residual),len(vector))) if with_jacobian else None
        for index,image in enumerate(images):
            start = 3+6*index
            r,t = vector[start:start+3],vector[start+3:start+6]
            projected,jpose = cv2.projectPoints(points,r,t,intrinsics.camera_matrix,intrinsics.distortion_coeffs)
            projected = projected.reshape(-1,2)
            rows = slice(index*10,(index+1)*10)
            residual[rows] = (projected-image).reshape(-1)
            if with_jacobian:
                jacobian[rows,start:start+6] = jpose[:,:6]
                for parameter in range(3):
                    shifted = vector[:3].copy()
                    shifted[parameter] += 1e-6
                    p = cv2.projectPoints(outline(shifted,reference),r,t,
                        intrinsics.camera_matrix,intrinsics.distortion_coeffs)[0].reshape(-1,2)
                    jacobian[rows,parameter] = ((p-projected)/1e-6).reshape(-1)
        return residual,jacobian

    damping = .01
    for _ in range(iterations):
        residual,jacobian = evaluate(state,True)
        normal = jacobian.T@jacobian
        regularizer = damping*np.maximum(np.diag(normal),1e-8)
        try:
            delta = np.linalg.solve(normal+np.diag(regularizer),-jacobian.T@residual)
        except np.linalg.LinAlgError:
            raise ValueError("geometry refinement is underconstrained")
        proposal = state+delta
        # Bounds avoid using an implausible deformation to explain bad matches.
        if not (.025 < proposal[0] < .08 and .025 < proposal[1] < .08 and .05 < proposal[2] < .11):
            damping *= 10
            continue
        if np.sum(evaluate(proposal)[0]**2) < np.sum(residual**2):
            state = proposal
            damping = max(1e-7,damping*.3)
            if np.linalg.norm(delta) < 1e-7:
                break
        else:
            damping *= 10
    residual,_ = evaluate(state)
    per_view = np.sqrt(np.mean(residual.reshape(-1,5,2)**2,axis=(1,2))*2)
    return outline(state[:3],reference),per_view


def ordered_views(log, intrinsics, reference, max_views=36):
    cv2 = import_cv2()
    matcher = FrontLedTracker(intrinsics,reference,max_rms_px=50)
    samples = []
    with open(log,encoding="utf-8") as stream:
        for index,line in enumerate(stream):
            if index % 20:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("event") != "pose" or row.get("candidate_count") != 5:
                continue
            raw = np.asarray([c["center_px"] for c in row.get("candidates",[])],dtype=float)
            if raw.shape != (5,2):
                continue
            hull = cv2.convexHull(raw.astype(np.float32),returnPoints=False).reshape(-1)
            if len(hull) != 4:
                continue
            center = next(i for i in range(5) if i not in hull)
            corners = raw[hull]
            angles = np.arctan2(corners[:,1]-raw[center,1],corners[:,0]-raw[center,0])
            corners = corners[np.argsort(angles)]
            best = None
            # Eight cyclic/reflected mappings of the quadrilateral, with the
            # interior point fixed as centre; upright/front constraints decide.
            for winding in (corners,corners[::-1]):
                for shift in range(4):
                    quad = np.roll(winding,shift,axis=0)
                    ordered = np.array([quad[0],quad[1],raw[center],quad[3],quad[2]])
                    matcher.best_fit = None
                    for pose in matcher.solve(ordered):
                        if best is None or pose.rms_px < best.rms_px:
                            best = pose
            if best is None:
                continue
            p = best.image_points
            span = float(np.max(np.ptp(p,axis=0)))
            roll = np.arctan2(*(p[1]-p[0])[::-1])
            descriptor = np.r_[p.mean(axis=0)/80,span/40,roll/.2]
            samples.append((p,descriptor))
    if len(samples) < 12:
        raise ValueError("need at least twelve usable five-LED views")
    descriptors = np.array([s[1] for s in samples])
    selected = [int(np.argmax(descriptors[:,2]))]  # include close view
    for _ in range(min(max_views,len(samples))-1):
        distances = np.min(np.sum((descriptors[:,None,:]-descriptors[selected][None,:,:])**2,axis=2),axis=1)
        candidate = int(np.argmax(distances))
        if distances[candidate] < .0025:
            break
        selected.append(candidate)
    if len(selected) < 12 or np.ptp(descriptors[selected,2])*40 < 40:
        raise ValueError("insufficient view/size diversity for refinement")
    return [samples[i][0] for i in selected]


def score(images, intrinsics, points):
    matcher = FrontLedTracker(intrinsics,points,max_rms_px=50)
    errors = []
    for image in images:
        matcher.best_fit = None
        solutions = matcher.solve(image)
        errors.append(min((p.rms_px for p in solutions),default=float("inf")))
    return np.asarray(errors)


def refine(log, intrinsics_path, rig_path, output):
    output = Path(output)
    if output.exists() or not output.parent.is_dir():
        raise ValueError("select a new output file in an existing directory")
    intrinsics = load_intrinsics(intrinsics_path)
    rig,reference = load_led_rig(rig_path)
    images = ordered_views(log,intrinsics,reference)
    training = [p for i,p in enumerate(images) if i%3 != 0]
    heldout = [p for i,p in enumerate(images) if i%3 == 0]
    points,train_errors = fit_proportions(training,intrinsics,reference)
    old = score(heldout,intrinsics,reference)
    new = score(heldout,intrinsics,points)
    report = {"training_views":len(training),"heldout_views":len(heldout),
        "training_median_rms_px":float(np.median(train_errors)),
        "heldout_before_median_rms_px":float(np.median(old)),
        "heldout_after_median_rms_px":float(np.median(new)),
        "heldout_after_max_rms_px":float(np.max(new)),
        "physical_scale_validated":False,"fixed_top_width_m":float(reference[1,0]-reference[0,0]),
        "fixed_depth_reference":True}
    if not np.isfinite(new).all() or np.median(new) >= np.median(old)*.8 or np.max(new) > 2.5:
        raise ValueError("refinement failed held-out checks: "+json.dumps(report))
    rig["object_points_m"] = points.tolist()
    rig["provenance"] = "external_scale_monocular_refined_outline"
    rig["notes"] += " Front heights and bottom width refined from recorded camera views at fixed intrinsics. Top width and depths retained from external estimate; physical scale/head origin not calibrated."
    rig["refinement"] = {"log":str(log),"intrinsics":str(intrinsics_path),**report}
    with output.open("x",encoding="utf-8") as stream:
        json.dump(rig,stream,indent=2)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--log",required=True)
    parser.add_argument("--intrinsics",required=True)
    parser.add_argument("--rig",required=True)
    parser.add_argument("--out",required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(refine(args.log,args.intrinsics,args.rig,args.out),indent=2))
    except ValueError as exc:
        parser.exit(3,str(exc)+"\n")
