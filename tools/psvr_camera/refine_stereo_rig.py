"""Offline metric front-rig refinement from recorded stereo points (no devices).

The stereo baseline supplies scale; this is not a caliper measurement, physical
label verification, or calibration of the anatomical head origin. Only proper,
upright/front-facing rotations are allowed when resolving the 120 permutations.
"""
from __future__ import annotations

import argparse
import hashlib
from itertools import permutations
import json
from pathlib import Path

import numpy as np

from .stereo_calibration import load_stereo_calibration
from .stereo_head_pose import load_front_led_rig

_PERMS = np.array(list(permutations(range(5))))


def _points(value, shape, name):
    raw = np.asarray(value)
    if raw.dtype.kind not in "iuf" or raw.shape != shape or not np.isfinite(raw).all():
        raise ValueError(f"{name} requires finite numeric shape {shape}")
    return np.array(raw, dtype=float, copy=True)


def best_alignment(reference, observed, *, similarity=False):
    """Exhaustive correspondence fit; returns proper R, t, scale, RMS, permutation."""
    reference = _points(reference, (5, 3), "reference")
    observed = _points(observed, (5, 3), "observed")
    p = reference - reference.mean(0)
    if np.linalg.matrix_rank(p, tol=1e-6) < 2:
        raise ValueError("degenerate reference")
    targets = observed[_PERMS]
    q = targets - targets.mean(1)[:, None, :]
    u, singular, vt = np.linalg.svd(np.einsum("ij,njk->nik", p.T, q))
    sign = np.linalg.det(vt.transpose(0, 2, 1) @ u.transpose(0, 2, 1))
    correction = np.tile(np.eye(3), (len(_PERMS), 1, 1))
    correction[:, 2, 2] = np.where(sign < 0, -1., 1.)
    rotations = vt.transpose(0, 2, 1) @ correction @ u.transpose(0, 2, 1)
    scales = (np.sum(singular * np.diagonal(correction, axis1=1, axis2=2), axis=1)
              / np.sum(p*p)) if similarity else np.ones(len(_PERMS))
    translations = targets.mean(1) - scales[:, None] * np.einsum("nij,j->ni", rotations, reference.mean(0))
    fitted = scales[:, None, None] * np.einsum("nij,kj->nki", rotations, reference) + translations[:, None, :]
    rms = np.sqrt(np.mean(np.sum((fitted-targets)**2, axis=2), axis=1))
    allowed = (rotations[:, 1, 1] >= .5) & (rotations[:, 2, 2] >= .35) & (scales > 0)
    if not allowed.any():
        raise ValueError("no upright/front-facing correspondence")
    index = int(np.argmin(np.where(allowed, rms, np.inf)))
    return rotations[index], translations[index], float(scales[index]), float(rms[index]), _PERMS[index].copy()


def symmetric_outline(parameters):
    """Six dimensions, with an x-rotation gauge inherited from initialization.

    Top/bottom depths can differ. The resulting frame is conventional, not a
    measured headset frame; shape comparisons should use a proper rigid fit.
    """
    tw, th, bw, bh, td, bd = parameters
    return np.array([[-tw, -th, td], [tw, -th, td], [0., 0., 0.],
                     [-bw, bh, bd], [bw, bh, bd]])


def _outline_projection(points):
    # Translation gauge: always subtract the fitted centre LED, never the mean.
    p = points - points[2]
    return symmetric_outline([(p[1, 0]-p[0, 0])/2, -np.mean(p[:2, 1]),
                              (p[4, 0]-p[3, 0])/2, np.mean(p[3:, 1]),
                              np.mean(p[:2, 2]), np.mean(p[3:, 2])])


def fit_stereo_rig(views, reference, *, model="outline", iterations=40):
    """Robust alternating Procrustes fit, without accessing logs or hardware.

    A global positive scale is initialized from per-view similarity fits. Shape
    updates use medians of inverse-aligned observations and bilateral symmetry;
    entire bad constellations are rejected rather than inventing a missing LED.
    Held-out observations must be supplied separately by the caller.
    """
    reference = _points(reference, (5, 3), "reference")
    raw = np.asarray(views)
    if raw.ndim != 3 or len(raw) < 8:
        raise ValueError("need at least eight five-point training views")
    observations = _points(raw, (len(raw), 5, 3), "views")
    if model not in ("similarity", "outline"):
        raise ValueError("model must be similarity or outline")
    if type(iterations) is not int or iterations < 1:
        raise ValueError("iterations must be positive")
    initial = [best_alignment(reference, p, similarity=True) for p in observations]
    scale = float(np.median([f[2] for f in initial]))
    if not .25 < scale < 4:
        raise ValueError("implausible stereo/reference scale ratio")
    points = reference * scale
    mask = np.ones(len(observations), dtype=bool)
    for _ in range(iterations):
        fits = [best_alignment(points, p) for p in observations]
        errors = np.array([f[3] for f in fits])
        median = np.median(errors)
        mad = 1.4826*np.median(np.abs(errors-median))
        threshold = min(.02, max(.003, median+4*mad))
        mask = errors <= threshold
        if mask.sum() < max(8, int(np.ceil(.6*len(observations)))):
            raise ValueError("insufficient inlier training constellations")
        if model == "similarity":
            scales = [best_alignment(reference, p, similarity=True)[2]
                      for p, keep in zip(observations, mask) if keep]
            proposal = reference * np.median(scales)
        else:
            aligned = [((p[f[4]]-f[1]) @ f[0]) for p, f, keep
                       in zip(observations, fits, mask) if keep]
            proposal = _outline_projection(np.median(aligned, axis=0))
        if np.linalg.matrix_rank(proposal-proposal.mean(0), tol=1e-6) < 3:
            raise ValueError("refined rig lost centre depth")
        change = np.max(np.abs(proposal-points))
        points = proposal
        if change < 1e-8:
            break
    errors = np.array([best_alignment(points, p)[3] for p in observations])
    return points, {"model": model, "initial_similarity_scale": scale,
                    "training_inlier_indices": np.flatnonzero(mask).tolist(),
                    "training_outlier_indices": np.flatnonzero(~mask).tolist(),
                    "training_rms_3d_m": errors.tolist(),
                    "physical_scale_validated": False}


def read_views(log):
    """Stream JSONL, retain only finite 5-match records; hash the exact source."""
    digest = hashlib.sha256()
    views = []
    total = 0
    with Path(log).open("rb") as stream:
        for total, line in enumerate(stream, 1):
            digest.update(line)
            try:
                row = json.loads(line)
                p = _points([m["p"] for m in row["matches"]], (5, 3), "points")
                left = _points(row["left"], (5, 2), "left")
                right = _points(row["right"], (5, 2), "right")
                t = float(row["t"])
                if not np.isfinite(t) or np.any(p[:, 2] <= 0):
                    continue
                views.append({"seq": row["seq"], "t": t, "points": p,
                              "left": left, "right": right, "line": total})
            except (KeyError, TypeError, ValueError):
                continue
    return views, {"sha256": digest.hexdigest(), "total_rows": total, "usable_five_point_rows": len(views)}


def sample_views(views, max_views=60, min_time_gap_s=.3):
    """Farthest-point sampling in distance/time, avoiding 60Hz near-duplicates."""
    if type(max_views) is not int or not 12 <= max_views <= 60:
        raise ValueError("max_views must be between 12 and 60")
    if len(views) < 12 or not np.isfinite(min_time_gap_s) or min_time_gap_s <= 0:
        raise ValueError("insufficient views or invalid time gap")
    times = np.array([v["t"] for v in views])
    distance = np.array([np.linalg.norm(v["points"].mean(0)) for v in views])
    if np.ptp(times) < 3 or np.ptp(distance) < .05:
        raise ValueError("insufficient distance/time diversity")
    descriptors = np.column_stack(((times-times.min())/np.ptp(times),
                                   (distance-distance.min())/np.ptp(distance)))
    selected = [int(np.argmin(distance))]
    while len(selected) < max_views:
        d = np.min(np.sum((descriptors[:, None]-descriptors[selected])**2, axis=2), axis=1)
        eligible = np.min(np.abs(times[:, None]-times[selected]), axis=1) >= min_time_gap_s
        if not eligible.any():
            break
        selected.append(int(np.argmax(np.where(eligible, d, -1))))
    return [views[i] for i in sorted(selected, key=lambda i: times[i])]


def _summary(values):
    a = np.asarray(values)
    return {"median": float(np.median(a)), "p95": float(np.percentile(a, 95)), "max": float(np.max(a))}


def refine(log, calibration_path, rig_path, output, *, max_views=60, model="outline"):
    """Save a new rig only after held-out rigid 3D residuals improve."""
    output = Path(output)
    if output.exists() or not output.parent.is_dir():
        raise ValueError("select a new output in an existing directory")
    calibration = load_stereo_calibration(calibration_path)
    rig = load_front_led_rig(rig_path)
    views, source = read_views(log)
    selected = sample_views(views, max_views)
    start, end = views[0]["t"], views[-1]["t"]
    # Whole time blocks are excluded from fitting (not random adjacent frames).
    heldout_blocks = (2, 5, 8)
    block = lambda v: min(9, int(10*(v["t"]-start)/(end-start)))
    training = [v for v in selected if block(v) not in heldout_blocks]
    heldout = [v for v in selected if block(v) in heldout_blocks]
    if len(heldout) < 6:
        raise ValueError("insufficient held-out time blocks")
    points, fit = fit_stereo_rig([v["points"] for v in training], rig.object_points_m, model=model)
    old = [best_alignment(rig.object_points_m, v["points"])[3] for v in heldout]
    new = [best_alignment(points, v["points"])[3] for v in heldout]
    report = {**source, **fit, "log": str(log), "calibration": str(calibration_path),
              "calibration_sha256": hashlib.sha256(Path(calibration_path).read_bytes()).hexdigest(),
              "reference_rig": str(rig_path),
              "reference_rig_sha256": hashlib.sha256(Path(rig_path).read_bytes()).hexdigest(),
              "stereo_baseline_m": float(np.linalg.norm(calibration.T)),
              "scale_source": "stereo_calibration_baseline_not_caliper",
              "training_sequences": [v["seq"] for v in training],
              "heldout_sequences": [v["seq"] for v in heldout],
              "heldout_time_blocks_of_ten": list(heldout_blocks),
              "selected_views": len(selected), "training_views": len(training), "heldout_views": len(heldout),
              "sampling_min_time_gap_s": .3,
              "heldout_before_rigid_rms_3d_m": _summary(old),
              "heldout_after_rigid_rms_3d_m": _summary(new),
              "hardware_accessed": False, "tracking_activated": False,
              "physical_order_verified": False, "anatomical_origin_calibrated": False}
    if np.median(new) >= .8*np.median(old) or np.percentile(new, 95) > .015:
        raise ValueError("held-out refinement checks failed: "+json.dumps(report))
    payload = rig.to_dict()
    payload.update(object_points_m=points.tolist(), provenance="stereo_baseline_scaled_robust_front_"+model,
                   source=str(log), refinement=report,
                   notes="Offline stereo-fitted geometric hypothesis. Metric scale from the stereo calibration baseline, not caliper-validated. Symmetric outline, proper upright/front-facing permutation fits, robust view rejection. Labels are conventional, not physically verified. Origin remains centre front LED, NOT anatomical head origin. Original external rig retained only as initialization; its dimensions are not fixed.")
    with output.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, allow_nan=False)
        stream.write("\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", required=True)
    parser.add_argument("--calibration", required=True)
    parser.add_argument("--rig", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--max-views", type=int, default=60)
    parser.add_argument("--model", choices=("similarity", "outline"), default="outline")
    args = parser.parse_args()
    try:
        print(json.dumps(refine(args.log, args.calibration, args.rig, args.out,
                                max_views=args.max_views, model=args.model), indent=2))
    except ValueError as exc:
        parser.exit(3, str(exc)+"\n")
