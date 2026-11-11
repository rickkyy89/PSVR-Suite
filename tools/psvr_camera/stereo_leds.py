"""Offline stereo geometry for unordered light patches, never LED IDs or a pose.

The calibration transform is X_right = R @ X_left + T (metres). ``left`` and
``right`` are stream labels, not verified physical camera order. No disparity
sign, spatial ordering, lamp model, temporal identity or head centre is assumed.

Assignment maximizes cardinality first, then minimizes summed normalized squared
epipolar/reprojection errors. Every reported edge is present in *all* assignments
of that cardinality within ``ambiguity_margin`` of the optimum. In particular,
repeated epipolar rows can have excellent residuals and still be ambiguous in
depth. Search limits fail closed rather than returning an unproven optimum.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import math
from numbers import Real

import numpy as np

from .camera_backend import import_cv2
from .stereo_calibration import StereoCalibration, stereo_from_dict


@dataclass(frozen=True)
class StereoLedConfig:
    max_epipolar_px: float = 1.0
    max_reprojection_px: float = 1.0
    min_ray_angle_deg: float = 0.2
    min_distance_m: float = 0.05
    max_distance_m: float = 20.0
    ambiguity_margin: float = 0.25
    max_candidates_per_eye: int = 16
    assignment_budget: int = 20_000

    def __post_init__(self):
        for name in ("max_epipolar_px", "max_reprojection_px", "min_ray_angle_deg",
                     "min_distance_m", "max_distance_m", "ambiguity_margin"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, Real)
                    or not math.isfinite(value) or value < 0):
                raise ValueError(f"{name} must be finite and nonnegative")
        if self.max_epipolar_px == 0 or self.max_reprojection_px == 0:
            raise ValueError("pixel gates must be positive")
        if not 0 < self.min_ray_angle_deg < 90:
            raise ValueError("min_ray_angle_deg must be strictly between 0 and 90")
        if self.max_distance_m <= self.min_distance_m:
            raise ValueError("distance bounds must be ordered with positive maximum")
        if (type(self.max_candidates_per_eye) is not int
                or not 1 <= self.max_candidates_per_eye <= 16):
            raise ValueError("max_candidates_per_eye must be an integer in 1..16")
        if type(self.assignment_budget) is not int or self.assignment_budget < 1:
            raise ValueError("assignment_budget must be a positive integer")


class _AssignmentLimit(Exception):
    pass


def _centres(candidates, size, side):
    # Area and bbox are detector diagnostics, not matching/identity evidence.
    points = []
    for index, candidate in enumerate(candidates):
        try:
            value = candidate["center_px"]
            raw = np.asarray(value)
            if raw.dtype.kind not in "iuf":
                raise ValueError("non-numeric centre")
            point = raw.astype(np.float64)
            if (point.shape != (2,) or not np.isfinite(point).all()
                    or not ((point >= 0) & (point < np.asarray(size))).all()):
                raise ValueError("centre outside image or non-finite")
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{side} candidate {index}: invalid center_px") from exc
        points.append(point)
    return np.asarray(points, dtype=np.float64).reshape(-1, 2)


def _assign(edges, nleft, budget, forbidden=None):
    """Two best distinct maximum-cardinality assignments; shared work budget."""
    @lru_cache(maxsize=None)
    def visit(index, used):
        budget[0] += 1
        if budget[0] > budget[1]:
            raise _AssignmentLimit
        if index == nleft:
            return ((0, 0.0, ()),)
        alternatives = list(visit(index + 1, used))  # unmatched is explicit
        for right, cost in edges[index]:
            if used & (1 << right) or forbidden == (index, right):
                continue
            for cardinality, total, pairs in visit(index + 1, used | (1 << right)):
                alternatives.append((cardinality + 1, total + cost,
                                     ((index, right),) + pairs))
        alternatives.sort(key=lambda item: (-item[0], item[1], item[2]))
        cardinality = alternatives[0][0]
        return tuple(item for item in alternatives if item[0] == cardinality)[:2]

    return visit(0, 0)


def triangulate_candidates(calibration, left_candidates, right_candidates,
                           config=None):
    """Return JSON-safe diagnostic points in the left *stream camera* frame.

    ``calibration`` must be a ``StereoCalibration`` (normally obtained with the
    strict ``load_stereo_calibration``); it is revalidated, not fitted or guessed.
    Candidate lists contain BlueLedDetector dictionaries with ``center_px``;
    other detector metadata is unused. Indices always refer to the input lists.

    States: ``points`` (unique geometry, not a valid pose), ``partial`` (only
    certified edges returned), ``ambiguous``, ``no_matches``, ``candidate_limit``
    and ``assignment_limit``. Limits never truncate candidates or publish points.
    Malformed inputs/configuration raise ValueError; missing OpenCV has no fallback.
    ``reprojection_px`` is the maximum distorted-image residual across both eyes;
    ``epipolar_px`` is the maximum of both calibrated point-to-line distances in
    undistorted pixels. The angle gate rejects ill-conditioned/near-zero parallax;
    it is not a measured depth uncertainty or a covariance estimate.
    """
    if config is None:
        config = StereoLedConfig()
    if not isinstance(config, StereoLedConfig):
        raise ValueError("config must be StereoLedConfig")
    if not isinstance(calibration, StereoCalibration):
        raise ValueError("calibration must be a strictly loaded StereoCalibration")
    # Also reject inconsistent matrices or mutation after a successful strict load.
    c = stereo_from_dict(calibration.to_dict())
    for candidates in (left_candidates, right_candidates):
        if not isinstance(candidates, (list, tuple)):
            raise ValueError("candidates must be lists or tuples of detector dictionaries")
    nl, nr = len(left_candidates), len(right_candidates)
    report = {
        "state": "no_matches", "matches": [],
        "identities_assigned": False, "position_published": False,
        "coordinate_frame": "left_camera", "translation_units": "metres",
        "physical_order_verified": False,
        "candidate_counts": {"left": nl, "right": nr},
        "unmatched_left_indices": [], "unmatched_right_indices": [],
        "withheld_pairs": [], "rejections": {}, "assignment": None,
    }
    if max(nl, nr) > config.max_candidates_per_eye:
        report["state"] = "candidate_limit"
        return report
    report["unmatched_left_indices"] = list(range(nl))
    report["unmatched_right_indices"] = list(range(nr))
    lp = _centres(left_candidates, c.image_size, "left")
    rp = _centres(right_candidates, c.image_size, "right")
    if not nl or not nr:
        return report
    cv2 = import_cv2()
    # Normalized points with [I|0], [R|T] keep the triangulation units in metres.
    lu = cv2.undistortPoints(lp.reshape(-1, 1, 2), c.left.camera_matrix,
                             c.left.distortion_coeffs).reshape(-1, 2)
    ru = cv2.undistortPoints(rp.reshape(-1, 1, 2), c.right.camera_matrix,
                             c.right.distortion_coeffs).reshape(-1, 2)
    lh = np.column_stack((lu, np.ones(nl)))
    rh = np.column_stack((ru, np.ones(nr)))
    lpx = (c.left.camera_matrix @ lh.T).T
    rpx = (c.right.camera_matrix @ rh.T).T
    lpx /= lpx[:, 2:3]
    rpx /= rpx[:, 2:3]
    pleft = np.column_stack((np.eye(3), np.zeros(3)))
    pright = np.column_stack((c.R, c.T))
    right_rvec = cv2.Rodrigues(c.R)[0]
    # F is homogeneous: normalize to make numerical degeneracy tests scale-free.
    fundamental = c.F / np.linalg.norm(c.F)
    pairs, edges = {}, [[] for _ in range(nl)]

    def reject(reason):
        report["rejections"][reason] = report["rejections"].get(reason, 0) + 1

    for i in range(nl):
        for j in range(nr):
            line_r = fundamental @ lpx[i]
            line_l = fundamental.T @ rpx[j]
            denominators = (np.linalg.norm(line_r[:2]), np.linalg.norm(line_l[:2]))
            if (not np.isfinite([*lu[i], *ru[j], *denominators]).all()
                    or min(denominators) < 1e-15):
                reject("degenerate_epipolar")
                continue
            residual = abs(float(rpx[j] @ line_r))
            epi = max(residual / d for d in denominators)
            if not np.isfinite(epi) or epi > config.max_epipolar_px:
                reject("epipolar")
                continue
            homogeneous = cv2.triangulatePoints(pleft, pright, lu[i:i+1].T,
                                                ru[j:j+1].T).reshape(4)
            if (not np.isfinite(homogeneous).all() or abs(homogeneous[3]) < 1e-15):
                reject("degenerate_triangulation")
                continue
            point = homogeneous[:3] / homogeneous[3]
            right_point = c.R @ point + c.T
            if not np.isfinite([*point, *right_point]).all():
                reject("nonfinite_point")
                continue
            if point[2] <= 0 or right_point[2] <= 0:
                reject("nonpositive_depth")
                continue
            ray_l = lh[i] / np.linalg.norm(lh[i])
            ray_r = c.R.T @ (rh[j] / np.linalg.norm(rh[j]))
            angle = math.degrees(math.atan2(np.linalg.norm(np.cross(ray_l, ray_r)),
                                           float(ray_l @ ray_r)))
            if min(angle, 180 - angle) <= config.min_ray_angle_deg:
                reject("ray_angle")
                continue
            distances = (np.linalg.norm(point), np.linalg.norm(right_point))
            if (min(distances) < config.min_distance_m
                    or max(distances) > config.max_distance_m):
                reject("distance")
                continue
            projected_l = cv2.projectPoints(point.reshape(1, 3), np.zeros(3),
                                           np.zeros(3), c.left.camera_matrix,
                                           c.left.distortion_coeffs)[0].reshape(2)
            projected_r = cv2.projectPoints(point.reshape(1, 3), right_rvec, c.T,
                                           c.right.camera_matrix,
                                           c.right.distortion_coeffs)[0].reshape(2)
            errors = [float(np.linalg.norm(projected_l - lp[i])),
                      float(np.linalg.norm(projected_r - rp[j]))]
            reproj = max(errors)
            if not np.isfinite(errors).all() or reproj > config.max_reprojection_px:
                reject("reprojection")
                continue
            cost = float((epi / config.max_epipolar_px)**2
                         + (reproj / config.max_reprojection_px)**2)
            pairs[i, j] = {"left_index": i, "right_index": j,
                           "point_left_camera_m": point.tolist(),
                           "reprojection_px": reproj,
                           "reprojection_per_eye_px": errors,
                           "epipolar_px": float(epi), "ray_angle_deg": angle}
            edges[i].append((j, cost))
    if not pairs:
        return report

    budget = [0, config.assignment_budget]
    try:
        solutions = _assign(edges, nl, budget)
        cardinality, best_cost, best_pairs = solutions[0]
        runner_cost = solutions[1][1] if len(solutions) > 1 else None
        ambiguous = (runner_cost is not None
                     and runner_cost <= best_cost + config.ambiguity_margin + 1e-12)
        stable, withheld = [], []
        for pair in best_pairs:
            if ambiguous:
                alternative = _assign(edges, nl, budget, forbidden=pair)[0]
                if (alternative[0] == cardinality
                        and alternative[1] <= best_cost + config.ambiguity_margin + 1e-12):
                    withheld.append(pair)
                    continue
            stable.append(pair)
    except _AssignmentLimit:
        report["state"] = "assignment_limit"
        report["assignment"] = {"complete": False, "states_evaluated": budget[0],
                                "budget": config.assignment_budget}
        return report

    report["assignment"] = {
        "complete": True, "maximum_cardinality": cardinality,
        "best_cost": best_cost, "runner_up_cost": runner_cost,
        "cost_gap": None if runner_cost is None else runner_cost - best_cost,
        "ambiguity_margin": float(config.ambiguity_margin),
        "states_evaluated": budget[0], "budget": config.assignment_budget,
    }
    report["matches"] = [pairs[pair] for pair in stable]
    report["withheld_pairs"] = [{"left_index": i, "right_index": j} for i, j in withheld]
    report["unmatched_left_indices"] = sorted(set(range(nl)) - {i for i, _ in stable})
    report["unmatched_right_indices"] = sorted(set(range(nr)) - {j for _, j in stable})
    report["state"] = ("partial" if stable else "ambiguous") if withheld else "points"
    return report
