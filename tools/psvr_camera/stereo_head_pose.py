"""Offline stereo head-pose prototype from 5 triangulated 3D points and 2D centroids.

Both ``configurations/ps4_stereo_640x400_calibrated.json`` and
``configurations/psvr_front_leds_refined_20261011.json`` are treated strictly as an
offline geometric hypothesis, never as metrological truth or anatomical head-centre
calibration. Origin remains at the front centre LED of the model rig unless an
explicit model offset is supplied for sensitivity analysis.

No camera hardware is opened, no UDP packets are sent, and no live tracking state
or pose filter is activated. Ambiguous correspondences, RANSAC outliers, excessive
residuals, ill-conditioned Jacobians and search-budget exhaustion fail closed.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations
import json
import math
from numbers import Real
from pathlib import Path

import numpy as np

from .camera_backend import OpenCvUnavailable, import_cv2
from .stereo_calibration import StereoCalibration, load_stereo_calibration, stereo_from_dict

HEAD_POSE_SCHEMA = "psvr.camera.stereo_head_pose"
HEAD_POSE_VERSION = 1
RIG_SCHEMA = "psvr.camera.led_rig"
RIG_VERSION = 1
EXPECTED_LED_COUNT = 5
_MAX_RIG_FILE_BYTES = 256 * 1024

DEFAULT_STEREO_CALIBRATION_PATH = Path("configurations/ps4_stereo_640x400_calibrated.json")
DEFAULT_FRONT_RIG_PATH = Path("configurations/psvr_front_leds_refined_20261011.json")


class _HypothesisBudgetExceeded(Exception):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _finite_number(value, name, *, positive=False, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if positive and number <= 0:
        raise ValueError(f"{name} must be positive")
    if minimum is not None and number < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return number


@dataclass(frozen=True)
class FrontLedRig:
    labels: tuple[str, ...]
    object_points_m: np.ndarray
    provenance: str
    source: str
    notes: str = ""
    refinement: dict | None = None
    physical_scale_validated: bool = False

    def __post_init__(self):
        if (not isinstance(self.labels, (tuple, list))
                or len(self.labels) != EXPECTED_LED_COUNT
                or any(not isinstance(label, str) or not label.strip() for label in self.labels)
                or len(set(self.labels)) != EXPECTED_LED_COUNT):
            raise ValueError("front LED rig requires five distinct non-empty string labels")
        object.__setattr__(self, "labels", tuple(self.labels))
        try:
            raw = np.asarray(self.object_points_m)
            if raw.dtype.kind not in "iuf":
                raise ValueError("non-numeric rig coordinates")
            points = np.array(raw, dtype=np.float64, copy=True)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("object_points_m must be numeric") from exc
        if points.shape != (EXPECTED_LED_COUNT, 3) or not np.isfinite(points).all():
            raise ValueError("front LED rig requires five finite 3D points")
        centered = points - points.mean(axis=0)
        if np.linalg.matrix_rank(centered, tol=1e-6) < 3:
            raise ValueError("front LED rig must span 3D (include non-coplanar centre LED depth)")
        separations = np.linalg.norm(points[:, None, :] - points[None, :, :], axis=2)
        separations += np.eye(EXPECTED_LED_COUNT)
        if float(np.min(separations)) < 1e-4:
            raise ValueError("front LED rig points must be at least 0.1 mm apart")
        points.flags.writeable = False
        object.__setattr__(self, "object_points_m", points)
        for attr in ("provenance", "source"):
            val = getattr(self, attr)
            if not isinstance(val, str) or not val.strip():
                raise ValueError(f"LED rig {attr} must be a non-empty string")
        if not isinstance(self.notes, str):
            raise ValueError("LED rig notes must be a string")
        if self.refinement is not None and not isinstance(self.refinement, dict):
            raise ValueError("LED rig refinement must be a dictionary or None")
        if type(self.physical_scale_validated) is not bool:
            raise ValueError("physical_scale_validated must be a boolean")

    def to_dict(self):
        payload = {
            "schema": RIG_SCHEMA,
            "version": RIG_VERSION,
            "provenance": self.provenance,
            "source": self.source,
            "notes": self.notes,
            "labels": list(self.labels),
            "object_points_m": self.object_points_m.tolist(),
        }
        if self.refinement is not None:
            payload["refinement"] = dict(self.refinement)
        return payload


def front_rig_from_dict(data) -> FrontLedRig:
    if not isinstance(data, dict):
        raise ValueError("LED rig must be a JSON object")
    if data.get("schema") != RIG_SCHEMA or type(data.get("version")) is not int or data.get("version") != RIG_VERSION:
        raise ValueError("unsupported LED rig schema or version")
    refinement = data.get("refinement")
    if refinement is not None and not isinstance(refinement, dict):
        raise ValueError("LED rig refinement must be a JSON object")
    scale_validated = False
    if isinstance(refinement, dict) and "physical_scale_validated" in refinement:
        if type(refinement["physical_scale_validated"]) is not bool:
            raise ValueError("refinement.physical_scale_validated must be a boolean")
        scale_validated = refinement["physical_scale_validated"]
    return FrontLedRig(
        labels=tuple(data.get("labels", ())),
        object_points_m=data.get("object_points_m"),
        provenance=data.get("provenance", ""),
        source=data.get("source", ""),
        notes=data.get("notes", "") if data.get("notes") is not None else "",
        refinement=None if refinement is None else dict(refinement),
        physical_scale_validated=scale_validated,
    )


def load_front_led_rig(path: str | Path = DEFAULT_FRONT_RIG_PATH) -> FrontLedRig:
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(_MAX_RIG_FILE_BYTES + 1)
        if len(raw) > _MAX_RIG_FILE_BYTES:
            raise ValueError("LED rig file is too large")
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read LED rig: {exc}") from exc
    return front_rig_from_dict(data)


def load_default_stereo_and_rig(
    calibration_path: str | Path = DEFAULT_STEREO_CALIBRATION_PATH,
    rig_path: str | Path = DEFAULT_FRONT_RIG_PATH,
) -> tuple[StereoCalibration, FrontLedRig]:
    calibration = load_stereo_calibration(
        calibration_path, expected_image_size=(640, 400), expected_mode="640x400"
    )
    rig = load_front_led_rig(rig_path)
    return calibration, rig


@dataclass(frozen=True)
class StereoHeadPoseConfig:
    max_triangulation_reprojection_px: float = 1.5
    max_epipolar_px: float = 1.0
    min_ray_angle_deg: float = 0.2
    min_distance_m: float = 0.15
    max_distance_m: float = 4.0
    min_span_px: float = 12.0
    ransac_reprojection_px: float = 2.0
    ransac_3d_threshold_m: float = 0.015
    ransac_iterations: int = 100
    ransac_confidence: float = 0.99
    min_inliers: int = 5
    max_rms_reprojection_px: float = 1.5
    max_point_reprojection_px: float = 2.5
    max_rms_3d_residual_m: float = 0.015
    max_point_3d_residual_m: float = 0.025
    min_upright_y_cos: float = 0.5
    min_facing_z_cos: float = 0.35
    ambiguity_margin: float = 0.25
    ambiguity_rms_margin_px: float = 0.25
    pixel_sigma_floor_px: float = 0.75
    position_sigma_floor_m: float = 0.0015
    max_information_condition: float = 1e6
    max_position_std_m: float = 0.08
    max_rotation_std_deg: float = 15.0
    head_offset_model_m: tuple[float, float, float] = (0.0, 0.0, 0.0)
    hypothesis_budget: int = 2_000

    def __post_init__(self):
        for name in (
            "max_triangulation_reprojection_px",
            "max_epipolar_px",
            "min_distance_m",
            "max_distance_m",
            "min_span_px",
            "ransac_reprojection_px",
            "ransac_3d_threshold_m",
            "max_rms_reprojection_px",
            "max_point_reprojection_px",
            "max_rms_3d_residual_m",
            "max_point_3d_residual_m",
            "pixel_sigma_floor_px",
            "position_sigma_floor_m",
            "max_information_condition",
            "max_position_std_m",
            "max_rotation_std_deg",
        ):
            _finite_number(getattr(self, name), name, positive=True)
        for name in ("ambiguity_margin", "ambiguity_rms_margin_px"):
            _finite_number(getattr(self, name), name, minimum=0.0)
        _finite_number(self.min_ray_angle_deg, "min_ray_angle_deg")
        if not 0.0 < self.min_ray_angle_deg < 90.0:
            raise ValueError("min_ray_angle_deg must be strictly between 0 and 90")
        if self.max_distance_m <= self.min_distance_m:
            raise ValueError("max_distance_m must exceed min_distance_m")
        if self.max_point_reprojection_px < self.max_rms_reprojection_px:
            raise ValueError("max_point_reprojection_px must be >= max_rms_reprojection_px")
        if self.max_point_3d_residual_m < self.max_rms_3d_residual_m:
            raise ValueError("max_point_3d_residual_m must be >= max_rms_3d_residual_m")
        if self.max_information_condition <= 1.0:
            raise ValueError("max_information_condition must exceed 1.0")
        _finite_number(self.ransac_confidence, "ransac_confidence")
        if not 0.0 < self.ransac_confidence < 1.0:
            raise ValueError("ransac_confidence must be strictly between 0 and 1")
        _finite_number(self.min_upright_y_cos, "min_upright_y_cos", minimum=-1.0, maximum=1.0)
        _finite_number(self.min_facing_z_cos, "min_facing_z_cos", minimum=-1.0, maximum=1.0)
        if type(self.ransac_iterations) is not int or self.ransac_iterations < 1:
            raise ValueError("ransac_iterations must be a positive integer")
        if type(self.min_inliers) is not int or self.min_inliers != EXPECTED_LED_COUNT:
            raise ValueError(f"min_inliers must be {EXPECTED_LED_COUNT} for the 5-LED front rig")
        if type(self.hypothesis_budget) is not int or self.hypothesis_budget < 1:
            raise ValueError("hypothesis_budget must be a positive integer")
        if (not isinstance(self.head_offset_model_m, (tuple, list, np.ndarray))
                or len(self.head_offset_model_m) != 3):
            raise ValueError("head_offset_model_m must contain 3 finite numbers")
        offset = tuple(
            _finite_number(v, "head_offset_model_m") for v in self.head_offset_model_m
        )
        object.__setattr__(self, "head_offset_model_m", offset)


def _coerce_rig(rig) -> FrontLedRig:
    if isinstance(rig, FrontLedRig):
        return front_rig_from_dict(rig.to_dict())
    if isinstance(rig, dict):
        return front_rig_from_dict(rig)
    if isinstance(rig, tuple) and len(rig) == 2 and isinstance(rig[0], dict):
        parsed = front_rig_from_dict(rig[0])
        pts = np.asarray(rig[1], dtype=np.float64)
        if pts.shape != (EXPECTED_LED_COUNT, 3) or not np.allclose(pts, parsed.object_points_m, atol=1e-12):
            raise ValueError("rig tuple points disagree with rig dictionary")
        return parsed
    raise ValueError("rig must be a FrontLedRig, a valid LED rig dict, or load_led_rig tuple")


def _parse_points_3d(points_left_camera_m) -> np.ndarray:
    if isinstance(points_left_camera_m, np.ndarray):
        if points_left_camera_m.dtype.kind not in "iuf" or points_left_camera_m.ndim != 2 or points_left_camera_m.shape[1] != 3:
            raise ValueError("points_left_camera_m array must be numeric with shape (N, 3)")
        items = points_left_camera_m
    elif isinstance(points_left_camera_m, (list, tuple)):
        items = points_left_camera_m
    else:
        raise ValueError("points_left_camera_m must be a list, tuple or (N, 3) ndarray")
    parsed = []
    for index, item in enumerate(items):
        try:
            value = item["point_left_camera_m"] if isinstance(item, dict) else item
            raw = np.asarray(value)
            if raw.dtype.kind not in "iuf":
                raise ValueError("non-numeric 3D point")
            point = raw.astype(np.float64)
            if point.shape != (3,) or not np.isfinite(point).all():
                raise ValueError("non-finite or wrong-shape 3D point")
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"3D point {index}: invalid point_left_camera_m") from exc
        parsed.append(point)
    return np.asarray(parsed, dtype=np.float64).reshape(-1, 3)


def _parse_centroids_2d(centroids, image_size, side: str) -> np.ndarray:
    if isinstance(centroids, np.ndarray):
        if centroids.dtype.kind not in "iuf" or centroids.ndim != 2 or centroids.shape[1] != 2:
            raise ValueError(f"{side} centroids array must be numeric with shape (N, 2)")
        items = centroids
    elif isinstance(centroids, (list, tuple)):
        items = centroids
    else:
        raise ValueError(f"{side} centroids must be a list, tuple or (N, 2) ndarray")
    size = np.asarray(image_size, dtype=np.float64)
    parsed = []
    for index, item in enumerate(items):
        try:
            value = item["center_px"] if isinstance(item, dict) else item
            raw = np.asarray(value)
            if raw.dtype.kind not in "iuf":
                raise ValueError("non-numeric 2D centroid")
            point = raw.astype(np.float64)
            if (point.shape != (2,) or not np.isfinite(point).all()
                    or not ((point >= 0.0) & (point < size)).all()):
                raise ValueError("centroid outside image or non-finite")
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{side} centroid {index}: invalid 2D coordinates") from exc
        parsed.append(point)
    return np.asarray(parsed, dtype=np.float64).reshape(-1, 2)


def _kabsch_rigid_transform(model_points: np.ndarray, target_points: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    """Closed-form proper rigid fit target ~= R @ model + t (det(R) == +1)."""
    model_mean = model_points.mean(axis=0)
    target_mean = target_points.mean(axis=0)
    p = model_points - model_mean
    q = target_points - target_mean
    u, _, vt = np.linalg.svd(p.T @ q)
    det_sign = 1.0 if np.linalg.det(vt.T @ u.T) >= 0.0 else -1.0
    correction = np.diag([1.0, 1.0, det_sign])
    rotation = vt.T @ correction @ u.T
    translation = target_mean - rotation @ model_mean
    residuals = (rotation @ model_points.T).T + translation - target_points
    rms = float(np.sqrt(np.mean(np.sum(residuals ** 2, axis=1))))
    return rotation, translation, rms


def _rotation_angle_deg(r1: np.ndarray, r2: np.ndarray) -> float:
    trace = float(np.trace(r1.T @ r2))
    cosine = max(-1.0, min(1.0, 0.5 * (trace - 1.0)))
    return math.degrees(math.acos(cosine))


def _project_stereo(cv2, model_points: np.ndarray, rvec: np.ndarray, tvec: np.ndarray,
                    calibration: StereoCalibration) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rvec = np.asarray(rvec, dtype=np.float64).reshape(3)
    tvec = np.asarray(tvec, dtype=np.float64).reshape(3)
    rotation = cv2.Rodrigues(rvec)[0]
    proj_left, jac_left = cv2.projectPoints(
        model_points, rvec, tvec,
        calibration.left.camera_matrix, calibration.left.distortion_coeffs,
    )
    rot_right = calibration.R @ rotation
    rvec_right = cv2.Rodrigues(rot_right)[0].reshape(3)
    tvec_right = calibration.R @ tvec + calibration.T
    proj_right, _ = cv2.projectPoints(
        model_points, rvec_right, tvec_right,
        calibration.right.camera_matrix, calibration.right.distortion_coeffs,
    )
    return proj_left.reshape(-1, 2), proj_right.reshape(-1, 2), jac_left[:, :6]


def _stereo_jacobian(cv2, model_points: np.ndarray, rvec: np.ndarray, tvec: np.ndarray,
                     calibration: StereoCalibration) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (proj_left, proj_right, J_stereo [4*N x 6]) wrt left-camera (rvec, tvec)."""
    rvec = np.asarray(rvec, dtype=np.float64).reshape(3)
    tvec = np.asarray(tvec, dtype=np.float64).reshape(3)
    proj_left, jac_left = cv2.projectPoints(
        model_points, rvec, tvec,
        calibration.left.camera_matrix, calibration.left.distortion_coeffs,
    )
    rotation = cv2.Rodrigues(rvec)[0]
    rot_right = calibration.R @ rotation
    rvec_right = cv2.Rodrigues(rot_right)[0].reshape(3)
    tvec_right = calibration.R @ tvec + calibration.T
    proj_right, jac_right = cv2.projectPoints(
        model_points, rvec_right, tvec_right,
        calibration.right.camera_matrix, calibration.right.distortion_coeffs,
    )
    # Exact chain rule for translation: d(tvec_right)/d(tvec) = calibration.R
    j_right_t = jac_right[:, 3:6] @ calibration.R
    # Central finite differences for left-camera rvec through calibration.R @ Rodrigues(rvec)
    step = 1e-6
    j_right_r_cols = []
    for axis in np.eye(3):
        rp = cv2.Rodrigues(calibration.R @ cv2.Rodrigues(rvec + step * axis)[0])[0]
        rm = cv2.Rodrigues(calibration.R @ cv2.Rodrigues(rvec - step * axis)[0])[0]
        pp = cv2.projectPoints(
            model_points, rp, tvec_right,
            calibration.right.camera_matrix, calibration.right.distortion_coeffs,
        )[0].reshape(-1)
        pm = cv2.projectPoints(
            model_points, rm, tvec_right,
            calibration.right.camera_matrix, calibration.right.distortion_coeffs,
        )[0].reshape(-1)
        j_right_r_cols.append((pp - pm) / (2.0 * step))
    j_right = np.column_stack((*j_right_r_cols, j_right_t))
    j_stereo = np.vstack((jac_left[:, :6], j_right))
    return proj_left.reshape(-1, 2), proj_right.reshape(-1, 2), j_stereo


def _refine_stereo_lm(cv2, model_points: np.ndarray, left_px: np.ndarray, right_px: np.ndarray,
                      rvec_init: np.ndarray, tvec_init: np.ndarray,
                      calibration: StereoCalibration, max_iters: int = 25) -> tuple[np.ndarray, np.ndarray]:
    """Damped Gauss-Newton / Levenberg-Marquardt joint stereo reprojection refinement."""
    rvec = np.asarray(rvec_init, dtype=np.float64).reshape(3).copy()
    tvec = np.asarray(tvec_init, dtype=np.float64).reshape(3).copy()
    try:
        rvec_lm, tvec_lm = cv2.solvePnPRefineLM(
            model_points, left_px,
            calibration.left.camera_matrix, calibration.left.distortion_coeffs,
            rvec.reshape(3, 1), tvec.reshape(3, 1),
        )
        rvec = np.asarray(rvec_lm, dtype=np.float64).reshape(3)
        tvec = np.asarray(tvec_lm, dtype=np.float64).reshape(3)
    except Exception:
        pass

    damping = 1e-4
    proj_l, proj_r, jac = _stereo_jacobian(cv2, model_points, rvec, tvec, calibration)
    residual = np.concatenate(((proj_l - left_px).reshape(-1), (proj_r - right_px).reshape(-1)))
    cost = float(residual @ residual)
    if not np.isfinite(cost):
        return rvec, tvec

    for _ in range(max_iters):
        if not np.isfinite(jac).all():
            break
        jtj = jac.T @ jac
        gradient = jac.T @ residual
        if float(np.linalg.norm(gradient, ord=np.inf)) < 1e-10:
            break
        accepted_step = False
        for _ in range(8):
            try:
                delta = -np.linalg.solve(jtj + damping * np.eye(6), gradient)
            except np.linalg.LinAlgError:
                damping *= 10.0
                continue
            if not np.isfinite(delta).all():
                damping *= 10.0
                continue
            cand_rvec = rvec + delta[:3]
            cand_tvec = tvec + delta[3:]
            cand_l, cand_r, cand_jac = _stereo_jacobian(
                cv2, model_points, cand_rvec, cand_tvec, calibration
            )
            cand_res = np.concatenate(
                ((cand_l - left_px).reshape(-1), (cand_r - right_px).reshape(-1))
            )
            cand_cost = float(cand_res @ cand_res)
            if np.isfinite(cand_cost) and cand_cost < cost:
                rvec, tvec = cand_rvec, cand_tvec
                proj_l, proj_r, jac = cand_l, cand_r, cand_jac
                residual, cost = cand_res, cand_cost
                damping = max(1e-9, damping * 0.3)
                accepted_step = True
                if float(np.linalg.norm(delta)) < 1e-10:
                    return rvec, tvec
                break
            damping *= 10.0
        if not accepted_step:
            break
    return rvec, tvec


def _associate_eye_centroids(projected_px: np.ndarray, centroids_px: np.ndarray,
                             gate_px: float, ambiguity_margin: float):
    """Find unique 1-to-1 assignment from 5 projected 3D points to 5 eye centroids."""
    distances = np.linalg.norm(projected_px[:, None, :] - centroids_px[None, :, :], axis=2)
    scored = []
    for perm in permutations(range(EXPECTED_LED_COUNT)):
        d = distances[np.arange(EXPECTED_LED_COUNT), perm]
        in_gate = int(np.count_nonzero(d <= gate_px))
        # Sort key prioritizes full 5/5 in-gate, then 4/5 in-gate (single outlier),
        # then sum of squared normalized errors of the best 4 points, then total cost.
        sorted_d = np.sort(d)
        cost4 = float(np.sum((sorted_d[:4] / gate_px) ** 2))
        cost5 = float(np.sum((d / gate_px) ** 2))
        scored.append((in_gate, cost4, cost5, perm, d))
    scored.sort(key=lambda item: (-min(item[0], 4), -item[0], item[1], item[2], item[3]))
    best = scored[0]
    runner = scored[1]
    ambiguous = False
    if best[0] == EXPECTED_LED_COUNT and runner[0] == EXPECTED_LED_COUNT:
        if runner[2] <= best[2] + ambiguity_margin + 1e-12:
            ambiguous = True
    elif best[0] == 4 and runner[0] >= 4:
        if runner[1] <= best[1] + ambiguity_margin + 1e-12:
            ambiguous = True
    return {
        "permutation": best[3],
        "distances_px": best[4],
        "in_gate_count": best[0],
        "best_cost": best[2],
        "runner_up_cost": runner[2],
        "ambiguous": ambiguous,
    }


def _check_stereo_pair_geometry(cv2, calibration: StereoCalibration,
                                points_3d: np.ndarray,
                                left_aligned_px: np.ndarray,
                                right_aligned_px: np.ndarray,
                                config: StereoHeadPoseConfig):
    """Compute per-point epipolar distance, ray angle and depth/distance validity."""
    n = len(points_3d)
    right_points = (calibration.R @ points_3d.T).T + calibration.T
    lu = cv2.undistortPoints(
        left_aligned_px.reshape(-1, 1, 2),
        calibration.left.camera_matrix,
        calibration.left.distortion_coeffs,
    ).reshape(-1, 2)
    ru = cv2.undistortPoints(
        right_aligned_px.reshape(-1, 1, 2),
        calibration.right.camera_matrix,
        calibration.right.distortion_coeffs,
    ).reshape(-1, 2)
    lh = np.column_stack((lu, np.ones(n)))
    rh = np.column_stack((ru, np.ones(n)))
    lpx = (calibration.left.camera_matrix @ lh.T).T
    rpx = (calibration.right.camera_matrix @ rh.T).T
    lpx /= lpx[:, 2:3]
    rpx /= rpx[:, 2:3]
    fundamental = calibration.F / np.linalg.norm(calibration.F)

    epipolar_px = np.zeros(n, dtype=np.float64)
    ray_angles_deg = np.zeros(n, dtype=np.float64)
    depth_ok = np.zeros(n, dtype=bool)
    distance_ok = np.zeros(n, dtype=bool)
    epipolar_ok = np.zeros(n, dtype=bool)
    angle_ok = np.zeros(n, dtype=bool)

    for k in range(n):
        p_left = points_3d[k]
        p_right = right_points[k]
        depth_ok[k] = bool(p_left[2] > 0.0 and p_right[2] > 0.0)
        d_left = float(np.linalg.norm(p_left))
        d_right = float(np.linalg.norm(p_right))
        distance_ok[k] = bool(
            config.min_distance_m <= min(d_left, d_right)
            and max(d_left, d_right) <= config.max_distance_m
        )
        line_r = fundamental @ lpx[k]
        line_l = fundamental.T @ rpx[k]
        denoms = (float(np.linalg.norm(line_r[:2])), float(np.linalg.norm(line_l[:2])))
        if min(denoms) < 1e-15 or not np.isfinite(denoms).all():
            epipolar_px[k] = float("inf")
            epipolar_ok[k] = False
        else:
            res = abs(float(rpx[k] @ line_r))
            epi = max(res / d for d in denoms)
            epipolar_px[k] = epi
            epipolar_ok[k] = bool(np.isfinite(epi) and epi <= config.max_epipolar_px)

        ray_l = lh[k] / np.linalg.norm(lh[k])
        ray_r = calibration.R.T @ (rh[k] / np.linalg.norm(rh[k]))
        angle = math.degrees(
            math.atan2(float(np.linalg.norm(np.cross(ray_l, ray_r))), float(ray_l @ ray_r))
        )
        ray_angles_deg[k] = angle
        angle_ok[k] = bool(min(angle, 180.0 - angle) > config.min_ray_angle_deg)

    return {
        "right_points_m": right_points,
        "epipolar_px": epipolar_px,
        "ray_angles_deg": ray_angles_deg,
        "depth_ok": depth_ok,
        "distance_ok": distance_ok,
        "epipolar_ok": epipolar_ok,
        "angle_ok": angle_ok,
    }


def _compute_uncertainty(cv2, model_points: np.ndarray, rvec: np.ndarray, tvec: np.ndarray,
                         stereo_residuals_px: np.ndarray, calibration: StereoCalibration,
                         config: StereoHeadPoseConfig) -> dict:
    """Compute 6x6 pose covariance and 3x3 head position/rotation uncertainty."""
    dof = max(1, 4 * len(model_points) - 6)
    squared_error = float(np.sum(stereo_residuals_px ** 2))
    sigma_px = float(max(config.pixel_sigma_floor_px, math.sqrt(squared_error / dof)))
    _, _, j_stereo = _stereo_jacobian(cv2, model_points, rvec, tvec, calibration)
    if not np.isfinite(j_stereo).all():
        return {
            "accepted": False, "reason": "numerical_error",
            "dof": dof, "sigma_px": sigma_px,
            "pixel_sigma_floor_px": config.pixel_sigma_floor_px,
            "position_sigma_floor_m": config.position_sigma_floor_m,
            "rank": 0, "condition_number": None,
            "pose_covariance_6x6": None, "position_covariance_m2": None,
            "rotation_covariance_rad2": None, "position_eigenvalues_m2": None,
            "position_axis_std_m": None, "worst_position_std_m": None,
            "worst_rotation_std_deg": None,
        }
    _, s, vt = np.linalg.svd(j_stereo, full_matrices=False)
    cutoff = s[0] / math.sqrt(config.max_information_condition) if s[0] > 0 else 0.0
    rank = int(np.count_nonzero(s > cutoff))
    cond = float((s[0] / s[-1]) ** 2) if s[-1] > 0 and np.isfinite((s[0] / s[-1]) ** 2) else None
    if rank < 6 or cond is None or cond > config.max_information_condition:
        return {
            "accepted": False, "reason": "rank_deficient",
            "dof": dof, "sigma_px": sigma_px,
            "pixel_sigma_floor_px": config.pixel_sigma_floor_px,
            "position_sigma_floor_m": config.position_sigma_floor_m,
            "rank": rank, "condition_number": cond,
            "pose_covariance_6x6": None, "position_covariance_m2": None,
            "rotation_covariance_rad2": None, "position_eigenvalues_m2": None,
            "position_axis_std_m": None, "worst_position_std_m": None,
            "worst_rotation_std_deg": None,
        }

    pose_cov = (vt.T * (sigma_px / s) ** 2) @ vt
    pose_cov = 0.5 * (pose_cov + pose_cov.T)

    offset = np.asarray(config.head_offset_model_m, dtype=np.float64)
    step = 1e-6
    d_offset = np.column_stack([
        (cv2.Rodrigues(rvec + step * axis)[0] @ offset
         - cv2.Rodrigues(rvec - step * axis)[0] @ offset) / (2.0 * step)
        for axis in np.eye(3)
    ])
    head_j = np.column_stack((d_offset, np.eye(3)))
    pos_cov = head_j @ pose_cov @ head_j.T + (config.position_sigma_floor_m ** 2) * np.eye(3)
    pos_cov = 0.5 * (pos_cov + pos_cov.T)
    rot_cov = 0.5 * (pose_cov[:3, :3] + pose_cov[:3, :3].T)

    if not (np.isfinite(pose_cov).all() and np.isfinite(pos_cov).all() and np.isfinite(rot_cov).all()):
        return {
            "accepted": False, "reason": "numerical_error",
            "dof": dof, "sigma_px": sigma_px,
            "pixel_sigma_floor_px": config.pixel_sigma_floor_px,
            "position_sigma_floor_m": config.position_sigma_floor_m,
            "rank": rank, "condition_number": cond,
            "pose_covariance_6x6": None, "position_covariance_m2": None,
            "rotation_covariance_rad2": None, "position_eigenvalues_m2": None,
            "position_axis_std_m": None, "worst_position_std_m": None,
            "worst_rotation_std_deg": None,
        }

    pos_eigs = np.linalg.eigvalsh(pos_cov)
    rot_eigs = np.linalg.eigvalsh(rot_cov)
    if pos_eigs[0] <= 0.0 or rot_eigs[0] <= 0.0:
        return {
            "accepted": False, "reason": "numerical_error",
            "dof": dof, "sigma_px": sigma_px,
            "pixel_sigma_floor_px": config.pixel_sigma_floor_px,
            "position_sigma_floor_m": config.position_sigma_floor_m,
            "rank": rank, "condition_number": cond,
            "pose_covariance_6x6": None, "position_covariance_m2": None,
            "rotation_covariance_rad2": None, "position_eigenvalues_m2": None,
            "position_axis_std_m": None, "worst_position_std_m": None,
            "worst_rotation_std_deg": None,
        }

    worst_pos_std = float(math.sqrt(pos_eigs[-1]))
    worst_rot_std_deg = float(math.degrees(math.sqrt(rot_eigs[-1])))
    axis_std = [float(math.sqrt(max(0.0, pos_cov[i, i]))) for i in range(3)]
    accepted = (
        worst_pos_std <= config.max_position_std_m
        and worst_rot_std_deg <= config.max_rotation_std_deg
    )
    reason = "ok" if accepted else "excessive_uncertainty"
    return {
        "accepted": accepted,
        "reason": reason,
        "dof": dof,
        "sigma_px": sigma_px,
        "pixel_sigma_floor_px": config.pixel_sigma_floor_px,
        "position_sigma_floor_m": config.position_sigma_floor_m,
        "rank": rank,
        "condition_number": cond,
        "pose_covariance_6x6": pose_cov.tolist(),
        "position_covariance_m2": pos_cov.tolist(),
        "rotation_covariance_rad2": rot_cov.tolist(),
        "position_eigenvalues_m2": pos_eigs.tolist(),
        "position_axis_std_m": axis_std,
        "worst_position_std_m": worst_pos_std,
        "worst_rotation_std_deg": worst_rot_std_deg,
    }


def _evaluate_pose_candidate(cv2, model_points: np.ndarray, obs_3d: np.ndarray,
                             obs_left_px: np.ndarray, obs_right_px: np.ndarray,
                             rvec: np.ndarray, tvec: np.ndarray,
                             perm: tuple[int, ...], rigid_rms_3d_m: float,
                             calibration: StereoCalibration,
                             config: StereoHeadPoseConfig) -> dict | None:
    rvec = np.asarray(rvec, dtype=np.float64).reshape(3)
    tvec = np.asarray(tvec, dtype=np.float64).reshape(3)
    if not (np.isfinite(rvec).all() and np.isfinite(tvec).all()):
        return None
    rotation = cv2.Rodrigues(rvec)[0]
    transformed_left = (rotation @ model_points.T).T + tvec
    transformed_right = (calibration.R @ transformed_left.T).T + calibration.T
    if not (np.isfinite(transformed_left).all() and np.isfinite(transformed_right).all()):
        return None

    depths_ok = bool(
        np.all(transformed_left[:, 2] > 0.0) and np.all(transformed_right[:, 2] > 0.0)
    )
    dists_l = np.linalg.norm(transformed_left, axis=1)
    dists_r = np.linalg.norm(transformed_right, axis=1)
    distance_ok = bool(
        depths_ok
        and np.all(dists_l >= config.min_distance_m)
        and np.all(dists_r >= config.min_distance_m)
        and np.all(dists_l <= config.max_distance_m)
        and np.all(dists_r <= config.max_distance_m)
    )
    orientation_ok = bool(
        rotation[1, 1] >= config.min_upright_y_cos
        and rotation[2, 2] >= config.min_facing_z_cos
    )

    proj_left, proj_right, _ = _project_stereo(cv2, model_points, rvec, tvec, calibration)
    if not (np.isfinite(proj_left).all() and np.isfinite(proj_right).all()):
        return None

    err_left = np.linalg.norm(proj_left - obs_left_px, axis=1)
    err_right = np.linalg.norm(proj_right - obs_right_px, axis=1)
    err_3d = np.linalg.norm(transformed_left - obs_3d, axis=1)
    if not (np.isfinite(err_left).all() and np.isfinite(err_right).all() and np.isfinite(err_3d).all()):
        return None

    left_rms = float(np.sqrt(np.mean(err_left ** 2)))
    right_rms = float(np.sqrt(np.mean(err_right ** 2)))
    stereo_rms = float(np.sqrt(0.5 * (left_rms ** 2 + right_rms ** 2)))
    max_reproj = float(max(np.max(err_left), np.max(err_right)))
    rms_3d = float(np.sqrt(np.mean(err_3d ** 2)))
    max_3d = float(np.max(err_3d))

    inlier_mask = (
        (np.maximum(err_left, err_right) <= config.ransac_reprojection_px)
        & (err_3d <= config.ransac_3d_threshold_m)
    )
    inlier_count = int(np.count_nonzero(inlier_mask))
    reproj_gate_ok = bool(
        left_rms <= config.max_rms_reprojection_px
        and right_rms <= config.max_rms_reprojection_px
        and stereo_rms <= config.max_rms_reprojection_px
        and max_reproj <= config.max_point_reprojection_px
    )
    residual_3d_gate_ok = bool(
        rms_3d <= config.max_rms_3d_residual_m
        and max_3d <= config.max_point_3d_residual_m
    )
    cost = float(
        (stereo_rms / config.max_rms_reprojection_px) ** 2
        + (rms_3d / config.max_rms_3d_residual_m) ** 2
    )
    return {
        "perm": perm,
        "rvec": rvec,
        "tvec": tvec,
        "rotation": rotation,
        "transformed_left_m": transformed_left,
        "transformed_right_m": transformed_right,
        "proj_left_px": proj_left,
        "proj_right_px": proj_right,
        "err_left_px": err_left,
        "err_right_px": err_right,
        "err_3d_m": err_3d,
        "left_rms_px": left_rms,
        "right_rms_px": right_rms,
        "stereo_rms_px": stereo_rms,
        "max_reprojection_px": max_reproj,
        "rms_3d_m": rms_3d,
        "max_3d_m": max_3d,
        "rigid_alignment_rms_3d_m": rigid_rms_3d_m,
        "inlier_mask": inlier_mask,
        "inlier_count": inlier_count,
        "depths_ok": depths_ok,
        "distance_ok": distance_ok,
        "orientation_ok": orientation_ok,
        "reproj_gate_ok": reproj_gate_ok,
        "residual_3d_gate_ok": residual_3d_gate_ok,
        "all_gates_ok": bool(
            distance_ok
            and orientation_ok
            and inlier_count == EXPECTED_LED_COUNT
            and reproj_gate_ok
            and residual_3d_gate_ok
        ),
        "cost": cost,
    }


def _deduplicate_solutions(solutions: list[dict]) -> list[dict]:
    """Merge identical (permutation, pose) local minima; preserve distinct poses/branches."""
    ordered = sorted(solutions, key=lambda item: (item["cost"], item["stereo_rms_px"], item["perm"]))
    unique = []
    for candidate in ordered:
        duplicate = False
        for existing in unique:
            if (existing["perm"] == candidate["perm"]
                    and float(np.linalg.norm(existing["tvec"] - candidate["tvec"])) <= 1e-3
                    and _rotation_angle_deg(existing["rotation"], candidate["rotation"]) <= 0.25):
                duplicate = True
                break
        if not duplicate:
            unique.append(candidate)
    return unique


def _find_four_led_ransac_consensus(
    cv2,
    model_points: np.ndarray,
    points_3d: np.ndarray,
    left_aligned_px: np.ndarray,
    right_aligned_px: np.ndarray,
    point_pair_valid: np.ndarray,
    calibration: StereoCalibration,
    config: StereoHeadPoseConfig,
    budget: list[int],
) -> dict | None:
    """Deterministic 4-of-5 RANSAC consensus search to detect a single outlier LED."""
    rig_dists = np.linalg.norm(model_points[:, None, :] - model_points[None, :, :], axis=2)
    obs_dists = np.linalg.norm(points_3d[:, None, :] - points_3d[None, :, :], axis=2)
    max_pair_diff = 2.0 * config.ransac_3d_threshold_m + 1e-9

    best_consensus = None
    seen_subsets = set()
    for perm in permutations(range(EXPECTED_LED_COUNT)):
        for outlier_led in range(EXPECTED_LED_COUNT):
            subset_leds = tuple(i for i in range(EXPECTED_LED_COUNT) if i != outlier_led)
            subset_obs = tuple(perm[i] for i in subset_leds)
            key = (subset_leds, subset_obs)
            if key in seen_subsets:
                continue
            seen_subsets.add(key)
            if not np.all(point_pair_valid[list(subset_obs)]):
                continue
            sub_rig_d = rig_dists[np.ix_(subset_leds, subset_leds)]
            sub_obs_d = obs_dists[np.ix_(subset_obs, subset_obs)]
            if float(np.max(np.abs(sub_rig_d - sub_obs_d))) > max_pair_diff:
                continue

            budget[0] += 1
            if budget[0] > budget[1]:
                raise _HypothesisBudgetExceeded

            p_sub = model_points[list(subset_leds)]
            y_sub = points_3d[list(subset_obs)]
            ul_sub = left_aligned_px[list(subset_obs)]
            ur_sub = right_aligned_px[list(subset_obs)]
            rot_init, t_init, _ = _kabsch_rigid_transform(p_sub, y_sub)
            rvec_init = cv2.Rodrigues(rot_init)[0].reshape(3)
            rvec_ref, tvec_ref = _refine_stereo_lm(
                cv2, p_sub, ul_sub, ur_sub, rvec_init, t_init, calibration, max_iters=15
            )
            obs_3d_all = points_3d[list(perm)]
            obs_l_all = left_aligned_px[list(perm)]
            obs_r_all = right_aligned_px[list(perm)]
            _, _, rigid_rms = _kabsch_rigid_transform(model_points, obs_3d_all)
            evaluated = _evaluate_pose_candidate(
                cv2, model_points, obs_3d_all, obs_l_all, obs_r_all,
                rvec_ref, tvec_ref, perm, rigid_rms, calibration, config,
            )
            if evaluated is None or not (evaluated["distance_ok"] and evaluated["orientation_ok"]):
                continue
            # All 4 subset LEDs must satisfy the RANSAC inlier thresholds and tight RMS
            sub_indices = list(subset_leds)
            sub_inliers = bool(np.all(evaluated["inlier_mask"][sub_indices]))
            sub_rms_l = float(np.sqrt(np.mean(evaluated["err_left_px"][sub_indices] ** 2)))
            sub_rms_r = float(np.sqrt(np.mean(evaluated["err_right_px"][sub_indices] ** 2)))
            sub_rms_3d = float(np.sqrt(np.mean(evaluated["err_3d_m"][sub_indices] ** 2)))
            if (sub_inliers
                    and sub_rms_l <= config.max_rms_reprojection_px
                    and sub_rms_r <= config.max_rms_reprojection_px
                    and sub_rms_3d <= config.max_rms_3d_residual_m
                    and evaluated["inlier_count"] == 4):
                sub_cost = (
                    0.5 * (sub_rms_l ** 2 + sub_rms_r ** 2) / (config.max_rms_reprojection_px ** 2)
                    + (sub_rms_3d / config.max_rms_3d_residual_m) ** 2
                )
                if best_consensus is None or sub_cost < best_consensus["subset_cost"]:
                    evaluated["subset_cost"] = float(sub_cost)
                    best_consensus = evaluated
    return best_consensus


def estimate_stereo_head_pose(
    calibration: StereoCalibration,
    rig: FrontLedRig | dict | tuple,
    points_left_camera_m,
    left_centroids_px,
    right_centroids_px,
    config: StereoHeadPoseConfig | None = None,
    *,
    module=None,
) -> dict:
    """Estimate offline 6DoF rigid head pose in ``left_camera`` without publishing or tracking."""
    if config is None:
        config = StereoHeadPoseConfig()
    if not isinstance(config, StereoHeadPoseConfig):
        raise ValueError("config must be a StereoHeadPoseConfig")
    if not isinstance(calibration, StereoCalibration):
        raise ValueError("calibration must be a strictly loaded StereoCalibration")
    # Revalidate calibration dictionary to catch post-load mutation or inconsistent matrices.
    cal = stereo_from_dict(calibration.to_dict())
    front_rig = _coerce_rig(rig)

    points_3d = _parse_points_3d(points_left_camera_m)
    left_px = _parse_centroids_2d(left_centroids_px, cal.image_size, "left")
    right_px = _parse_centroids_2d(right_centroids_px, cal.image_size, "right")

    report = {
        "schema": HEAD_POSE_SCHEMA,
        "version": HEAD_POSE_VERSION,
        "valid": False,
        "state": "rejected",
        "reason": "uninitialized",
        "udp_published": False,
        "position_published": False,
        "tracking_activated": False,
        "tracking_6dof": False,
        "hardware_accessed": False,
        "metrological_truth": False,
        "geometry_hypothesis_only": True,
        "physical_scale_validated": False,
        "physical_order_verified": False,
        "coordinate_frame": "left_camera",
        "pose_origin": (
            "front_centre_led_not_anatomical_head_centre"
            if config.head_offset_model_m == (0.0, 0.0, 0.0)
            else "model_offset_from_front_centre_led_not_metrological_head_centre"
        ),
        "translation_units": "metres",
        "provenance": {
            "calibration_schema": cal.to_dict()["schema"],
            "calibration_source": cal.source,
            "calibration_transform": "X_right = R @ X_left + T",
            "rig_schema": RIG_SCHEMA,
            "rig_provenance": front_rig.provenance,
            "rig_source": front_rig.source,
            "rig_physical_scale_validated": front_rig.physical_scale_validated,
            "rig_role": "geometric_hypothesis_not_metrological_truth",
        },
        "input_counts": {
            "points_3d": int(len(points_3d)),
            "left_centroids_2d": int(len(left_px)),
            "right_centroids_2d": int(len(right_px)),
        },
        "pose": None,
        "residuals": None,
        "ransac": {
            "inlier_count": 0,
            "min_inliers_required": config.min_inliers,
            "inlier_led_indices": [],
            "outlier_led_indices": list(range(EXPECTED_LED_COUNT)),
            "inlier_point_3d_indices": [],
            "outlier_point_3d_indices": [],
            "ransac_reprojection_threshold_px": float(config.ransac_reprojection_px),
            "ransac_3d_threshold_m": float(config.ransac_3d_threshold_m),
            "hypotheses_evaluated": 0,
            "candidate_solutions_evaluated": 0,
            "budget": config.hypothesis_budget,
        },
        "uncertainty": None,
        "ambiguity": {
            "ambiguous": False,
            "best_cost": None,
            "runner_up_cost": None,
            "cost_gap": None,
            "ambiguity_margin": float(config.ambiguity_margin),
            "best_stereo_rms_px": None,
            "runner_up_stereo_rms_px": None,
            "rms_gap_px": None,
            "ambiguity_rms_margin_px": float(config.ambiguity_rms_margin_px),
            "competing_hypotheses": 0,
            "runner_up": None,
        },
    }

    if (len(points_3d) != EXPECTED_LED_COUNT
            or len(left_px) != EXPECTED_LED_COUNT
            or len(right_px) != EXPECTED_LED_COUNT):
        report["state"] = "point_count_mismatch"
        report["reason"] = "require_five_points_and_five_centroids_per_eye"
        return report

    try:
        cv2 = import_cv2(module)
    except OpenCvUnavailable:
        report["state"] = "rejected"
        report["reason"] = "opencv_unavailable"
        return report

    span_left = float(np.max(np.ptp(left_px, axis=0)))
    span_right = float(np.max(np.ptp(right_px, axis=0)))
    if min(span_left, span_right) < config.min_span_px:
        report["state"] = "rejected"
        report["reason"] = "small_span"
        return report

    if (np.linalg.matrix_rank(points_3d - points_3d.mean(axis=0), tol=1e-6) < 2
            or np.linalg.matrix_rank(left_px - left_px.mean(axis=0), tol=1e-4) < 2
            or np.linalg.matrix_rank(right_px - right_px.mean(axis=0), tol=1e-4) < 2):
        report["state"] = "rejected"
        report["reason"] = "degenerate_geometry"
        return report

    right_points_check = (cal.R @ points_3d.T).T + cal.T
    if not (np.all(points_3d[:, 2] > 0.0) and np.all(right_points_check[:, 2] > 0.0)):
        report["state"] = "rejected"
        report["reason"] = "nonpositive_depth"
        return report

    dists_l0 = np.linalg.norm(points_3d, axis=1)
    dists_r0 = np.linalg.norm(right_points_check, axis=1)
    if (np.any(dists_l0 < config.min_distance_m)
            or np.any(dists_r0 < config.min_distance_m)
            or np.any(dists_l0 > config.max_distance_m)
            or np.any(dists_r0 > config.max_distance_m)):
        report["state"] = "rejected"
        report["reason"] = "distance_gate_failed"
        return report

    # Project the 5 triangulated 3D points into both cameras to verify 2D centroid association.
    proj_3d_left = cv2.projectPoints(
        points_3d, np.zeros(3), np.zeros(3),
        cal.left.camera_matrix, cal.left.distortion_coeffs,
    )[0].reshape(-1, 2)
    right_rvec = cv2.Rodrigues(cal.R)[0]
    proj_3d_right = cv2.projectPoints(
        points_3d, right_rvec, cal.T,
        cal.right.camera_matrix, cal.right.distortion_coeffs,
    )[0].reshape(-1, 2)

    assoc_left = _associate_eye_centroids(
        proj_3d_left, left_px, config.max_triangulation_reprojection_px, config.ambiguity_margin
    )
    assoc_right = _associate_eye_centroids(
        proj_3d_right, right_px, config.max_triangulation_reprojection_px, config.ambiguity_margin
    )
    if assoc_left["ambiguous"] or assoc_right["ambiguous"]:
        report["state"] = "ambiguous"
        report["reason"] = "ambiguous_2d_3d_association"
        report["ambiguity"]["ambiguous"] = True
        return report

    perm_left = assoc_left["permutation"]
    perm_right = assoc_right["permutation"]
    left_aligned_px = left_px[list(perm_left)]
    right_aligned_px = right_px[list(perm_right)]

    pair_geom = _check_stereo_pair_geometry(
        cv2, cal, points_3d, left_aligned_px, right_aligned_px, config
    )
    tri_err_px = np.maximum(assoc_left["distances_px"], assoc_right["distances_px"])
    tri_max_px = float(np.max(tri_err_px))
    epi_max_px = float(np.max(pair_geom["epipolar_px"]))
    point_pair_valid = (
        (tri_err_px <= config.max_triangulation_reprojection_px)
        & pair_geom["depth_ok"]
        & pair_geom["distance_ok"]
        & pair_geom["epipolar_ok"]
        & pair_geom["angle_ok"]
    )
    valid_pair_count = int(np.count_nonzero(point_pair_valid))

    if valid_pair_count < 4:
        report["state"] = "triangulation_2d_inconsistent"
        if np.any(tri_err_px > config.max_triangulation_reprojection_px):
            report["reason"] = "triangulation_reprojection_mismatch"
        elif not np.all(pair_geom["epipolar_ok"]):
            report["reason"] = "epipolar_consistency_failed"
        else:
            report["reason"] = "ray_angle_gate_failed"
        return report

    model_points = front_rig.object_points_m
    rig_dists = np.linalg.norm(model_points[:, None, :] - model_points[None, :, :], axis=2)
    obs_dists = np.linalg.norm(points_3d[:, None, :] - points_3d[None, :, :], axis=2)
    # Loose pairwise distance prefilter (3x point 3D residual bound) keeps all plausible poses.
    loose_3d_bound = max(3.0 * config.max_point_3d_residual_m, 2.0 * config.ransac_3d_threshold_m)

    budget = [0, config.hypothesis_budget]
    all_evaluated = []

    try:
        # With >=4 stereo-consistent point pairs the full 120-permutation solve is
        # still executed: final gates and RANSAC, not the consistency gate alone,
        # decide whether a contaminated LED is present.
        if valid_pair_count >= 4:
            for perm in permutations(range(EXPECTED_LED_COUNT)):
                budget[0] += 1
                if budget[0] > budget[1]:
                    raise _HypothesisBudgetExceeded

                obs_3d = points_3d[list(perm)]
                obs_l = left_aligned_px[list(perm)]
                obs_r = right_aligned_px[list(perm)]
                rot_k, t_k, rigid_rms = _kabsch_rigid_transform(model_points, obs_3d)

                perm_obs_d = obs_dists[np.ix_(perm, perm)]
                if float(np.max(np.abs(rig_dists - perm_obs_d))) > loose_3d_bound:
                    # Still record Kabsch candidate so diagnostics have the best rigid fit.
                    rvec_k = cv2.Rodrigues(rot_k)[0].reshape(3)
                    cand_k = _evaluate_pose_candidate(
                        cv2, model_points, obs_3d, obs_l, obs_r,
                        rvec_k, t_k, perm, rigid_rms, cal, config,
                    )
                    if cand_k is not None:
                        all_evaluated.append(cand_k)
                    continue

                seeds = [(cv2.Rodrigues(rot_k)[0].reshape(3), t_k)]
                try:
                    ok_r, rvec_r, tvec_r, _ = cv2.solvePnPRansac(
                        model_points, obs_l,
                        cal.left.camera_matrix, cal.left.distortion_coeffs,
                        useExtrinsicGuess=False,
                        iterationsCount=config.ransac_iterations,
                        reprojectionError=config.ransac_reprojection_px,
                        confidence=config.ransac_confidence,
                        flags=cv2.SOLVEPNP_SQPNP,
                    )
                    if ok_r and rvec_r is not None and tvec_r is not None:
                        seeds.append((np.asarray(rvec_r, float).reshape(3),
                                      np.asarray(tvec_r, float).reshape(3)))
                except Exception:
                    pass

                try:
                    ok_g, rvecs_g, tvecs_g, _ = cv2.solvePnPGeneric(
                        model_points, obs_l,
                        cal.left.camera_matrix, cal.left.distortion_coeffs,
                        flags=cv2.SOLVEPNP_SQPNP,
                    )
                    if ok_g:
                        for rv, tv in zip(rvecs_g, tvecs_g):
                            seeds.append((np.asarray(rv, float).reshape(3),
                                          np.asarray(tv, float).reshape(3)))
                except Exception:
                    pass

                dedup_seeds = []
                for rv, tv in seeds:
                    if not (np.isfinite(rv).all() and np.isfinite(tv).all()):
                        continue
                    rot_s = cv2.Rodrigues(rv)[0]
                    if any(float(np.linalg.norm(tv - tv0)) < 1e-3
                           and _rotation_angle_deg(rot_s, rot0) < 0.5
                           for _, tv0, rot0 in dedup_seeds):
                        continue
                    dedup_seeds.append((rv, tv, rot_s))

                for rv, tv, _ in dedup_seeds:
                    rvec_ref, tvec_ref = _refine_stereo_lm(
                        cv2, model_points, obs_l, obs_r, rv, tv, cal
                    )
                    cand = _evaluate_pose_candidate(
                        cv2, model_points, obs_3d, obs_l, obs_r,
                        rvec_ref, tvec_ref, perm, rigid_rms, cal, config,
                    )
                    if cand is not None:
                        all_evaluated.append(cand)

        unique_solutions = _deduplicate_solutions(all_evaluated)
        passing = [sol for sol in unique_solutions if sol["all_gates_ok"]]

        # A pose failing ONLY the upright/front-facing convention is reported as
        # such, never misattributed to a contaminated LED by the outlier search.
        orientation_only = [
            sol for sol in unique_solutions
            if (sol["distance_ok"] and not sol["orientation_ok"]
                and sol["inlier_count"] == EXPECTED_LED_COUNT
                and sol["reproj_gate_ok"] and sol["residual_3d_gate_ok"])
        ] if not passing else []

        # If no full 5-LED pose passed all gates, run deterministic 4-of-5 RANSAC consensus
        # to check whether 4 LEDs form an inlier consensus and 1 LED is an outlier.
        four_consensus = None
        if not passing and not orientation_only:
            four_consensus = _find_four_led_ransac_consensus(
                cv2, model_points, points_3d, left_aligned_px, right_aligned_px,
                point_pair_valid, cal, config, budget,
            )

    except _HypothesisBudgetExceeded:
        report["state"] = "hypothesis_limit"
        report["reason"] = "hypothesis_budget_exceeded"
        report["ransac"]["hypotheses_evaluated"] = budget[0]
        report["ransac"]["candidate_solutions_evaluated"] = len(all_evaluated)
        return report

    report["ransac"]["hypotheses_evaluated"] = budget[0]
    report["ransac"]["candidate_solutions_evaluated"] = len(all_evaluated)

    def build_residuals_dict(sol: dict) -> dict:
        return {
            "left_rms_px": sol["left_rms_px"],
            "right_rms_px": sol["right_rms_px"],
            "stereo_rms_px": sol["stereo_rms_px"],
            "max_reprojection_px": sol["max_reprojection_px"],
            "per_led_left_px": [float(v) for v in sol["err_left_px"]],
            "per_led_right_px": [float(v) for v in sol["err_right_px"]],
            "rms_3d_m": sol["rms_3d_m"],
            "max_3d_m": sol["max_3d_m"],
            "per_led_3d_m": [float(v) for v in sol["err_3d_m"]],
            "rigid_alignment_rms_3d_m": sol["rigid_alignment_rms_3d_m"],
            "triangulation_consistency_max_px": tri_max_px if np.isfinite(tri_max_px) else None,
            "epipolar_max_px": epi_max_px if np.isfinite(epi_max_px) else None,
        }

    def update_ransac_from_solution(sol: dict):
        inlier_leds = [int(i) for i, ok in enumerate(sol["inlier_mask"]) if ok]
        outlier_leds = [int(i) for i, ok in enumerate(sol["inlier_mask"]) if not ok]
        report["ransac"].update({
            "inlier_count": int(sol["inlier_count"]),
            "inlier_led_indices": inlier_leds,
            "outlier_led_indices": outlier_leds,
            "inlier_point_3d_indices": [int(sol["perm"][i]) for i in inlier_leds],
            "outlier_point_3d_indices": [int(sol["perm"][i]) for i in outlier_leds],
        })

    if not passing:
        if orientation_only:
            update_ransac_from_solution(orientation_only[0])
            report["residuals"] = build_residuals_dict(orientation_only[0])
            report["state"] = "rejected"
            report["reason"] = "orientation_gate_failed"
            return report
        if four_consensus is not None:
            update_ransac_from_solution(four_consensus)
            report["residuals"] = build_residuals_dict(four_consensus)
            report["state"] = "ransac_outlier"
            report["reason"] = "ransac_inlier_gate_failed"
            return report

        if valid_pair_count < EXPECTED_LED_COUNT:
            report["state"] = "triangulation_2d_inconsistent"
            if np.any(tri_err_px > config.max_triangulation_reprojection_px):
                report["reason"] = "triangulation_reprojection_mismatch"
            elif not np.all(pair_geom["epipolar_ok"]):
                report["reason"] = "epipolar_consistency_failed"
            else:
                report["reason"] = "ray_angle_gate_failed"
            return report

        if unique_solutions:
            # Prefer an upright/front-facing candidate for residual diagnostics if available.
            oriented = [s for s in unique_solutions if s["orientation_ok"] and s["distance_ok"]]
            best_failed = oriented[0] if oriented else unique_solutions[0]
            update_ransac_from_solution(best_failed)
            report["residuals"] = build_residuals_dict(best_failed)
            if not best_failed["distance_ok"]:
                report["reason"] = "depth_or_distance_gate_failed"
            elif not best_failed["orientation_ok"]:
                report["reason"] = "orientation_gate_failed"
            elif not best_failed["reproj_gate_ok"]:
                if (best_failed["left_rms_px"] > config.max_rms_reprojection_px
                        or best_failed["right_rms_px"] > config.max_rms_reprojection_px
                        or best_failed["stereo_rms_px"] > config.max_rms_reprojection_px):
                    report["reason"] = "reprojection_rms_exceeded"
                else:
                    report["reason"] = "point_reprojection_exceeded"
            elif not best_failed["residual_3d_gate_ok"]:
                report["reason"] = "residual_3d_exceeded"
            else:
                report["reason"] = "ransac_inlier_gate_failed"
        else:
            report["reason"] = "no_valid_pnp_solution"
        report["state"] = "rejected"
        return report

    best = passing[0]
    update_ransac_from_solution(best)
    report["residuals"] = build_residuals_dict(best)

    # Only fully gate-passing hypotheses compete: a runner-up must be a plausible
    # alternative pose, not a permutation with arbitrarily bad residuals.
    competitors = [
        sol for sol in passing[1:]
        if (sol["perm"] != best["perm"]
            or float(np.linalg.norm(sol["tvec"] - best["tvec"])) > 0.005
            or _rotation_angle_deg(sol["rotation"], best["rotation"]) > 1.0)
    ]
    runner = competitors[0] if competitors else None
    cost_gap = None if runner is None else float(runner["cost"] - best["cost"])
    rms_gap = None if runner is None else float(runner["stereo_rms_px"] - best["stereo_rms_px"])
    ambiguous = bool(
        runner is not None
        and (cost_gap <= config.ambiguity_margin + 1e-12
             or rms_gap <= config.ambiguity_rms_margin_px + 1e-12)
    )
    report["ambiguity"] = {
        "ambiguous": ambiguous,
        "best_cost": best["cost"],
        "runner_up_cost": None if runner is None else runner["cost"],
        "cost_gap": cost_gap,
        "ambiguity_margin": float(config.ambiguity_margin),
        "best_stereo_rms_px": best["stereo_rms_px"],
        "runner_up_stereo_rms_px": None if runner is None else runner["stereo_rms_px"],
        "rms_gap_px": rms_gap,
        "ambiguity_rms_margin_px": float(config.ambiguity_rms_margin_px),
        "competing_hypotheses": len(passing),
        "runner_up": None if runner is None else {
            "perm": list(runner["perm"]),
            "stereo_rms_px": runner["stereo_rms_px"],
            "rms_3d_m": runner["rms_3d_m"],
            "cost": runner["cost"],
            "tvec_left_camera_m": runner["tvec"].tolist(),
            "rotation_angle_from_best_deg": float(
                _rotation_angle_deg(runner["rotation"], best["rotation"])),
        },
    }
    if ambiguous:
        report["state"] = "ambiguous"
        report["reason"] = "ambiguous_pose_hypotheses"
        return report

    stereo_res_vec = np.concatenate((
        (best["proj_left_px"] - left_aligned_px[list(best["perm"])]).reshape(-1),
        (best["proj_right_px"] - right_aligned_px[list(best["perm"])]).reshape(-1),
    ))
    uncertainty = _compute_uncertainty(
        cv2, model_points, best["rvec"], best["tvec"], stereo_res_vec, cal, config
    )
    report["uncertainty"] = uncertainty
    if not uncertainty["accepted"]:
        report["state"] = "rejected"
        report["reason"] = uncertainty["reason"]
        return report

    offset_vec = np.asarray(config.head_offset_model_m, dtype=np.float64)
    head_pos_left = best["tvec"] + best["rotation"] @ offset_vec
    rot_right = cal.R @ best["rotation"]
    rvec_right = cv2.Rodrigues(rot_right)[0].reshape(3)
    tvec_right = cal.R @ best["tvec"] + cal.T

    correspondences = []
    for led_idx in range(EXPECTED_LED_COUNT):
        pt_idx = int(best["perm"][led_idx])
        l_idx = int(perm_left[pt_idx])
        r_idx = int(perm_right[pt_idx])
        correspondences.append({
            "led_index": led_idx,
            "led_label": front_rig.labels[led_idx],
            "point_3d_index": pt_idx,
            "left_2d_index": l_idx,
            "right_2d_index": r_idx,
            "model_point_m": model_points[led_idx].tolist(),
            "transformed_model_left_m": best["transformed_left_m"][led_idx].tolist(),
            "triangulated_point_left_m": points_3d[pt_idx].tolist(),
            "left_centroid_px": left_px[l_idx].tolist(),
            "right_centroid_px": right_px[r_idx].tolist(),
            "projected_left_px": best["proj_left_px"][led_idx].tolist(),
            "projected_right_px": best["proj_right_px"][led_idx].tolist(),
            "reprojection_left_px": float(best["err_left_px"][led_idx]),
            "reprojection_right_px": float(best["err_right_px"][led_idx]),
            "residual_3d_m": float(best["err_3d_m"][led_idx]),
        })

    report["valid"] = True
    report["state"] = "pose"
    report["reason"] = "ok"
    report["pose"] = {
        "rvec_left_camera_rad": best["rvec"].tolist(),
        "tvec_left_camera_m": best["tvec"].tolist(),
        "rotation_matrix_left_camera": best["rotation"].tolist(),
        "head_position_left_camera_m": head_pos_left.tolist(),
        "head_offset_model_m": list(config.head_offset_model_m),
        "rvec_right_camera_rad": rvec_right.tolist(),
        "tvec_right_camera_m": tvec_right.tolist(),
        "led_labels": list(front_rig.labels),
        "correspondences": correspondences,
    }
    return report


def estimate_stereo_head_pose_from_triangulation(
    calibration: StereoCalibration,
    rig: FrontLedRig | dict | tuple,
    triangulation_report: dict,
    left_candidates,
    right_candidates,
    config: StereoHeadPoseConfig | None = None,
    *,
    module=None,
) -> dict:
    """Adapter consuming a ``stereo_leds.triangulate_candidates`` report and eye candidates.

    Only unambiguous ``state == 'points'`` reports with 5 matched 3D points and 5
    centroids per eye proceed to pose estimation; partial/ambiguous stereo reports
    fail closed without guessing missing LEDs.
    """
    if not isinstance(triangulation_report, dict):
        raise ValueError("triangulation_report must be a dictionary")
    matches = triangulation_report.get("matches")
    if not isinstance(matches, list):
        raise ValueError("triangulation_report['matches'] must be a list")
    state = triangulation_report.get("state")
    if state != "points":
        # Parse candidates/points first so malformed inputs still raise ValueError.
        cal = stereo_from_dict(calibration.to_dict()) if isinstance(calibration, StereoCalibration) else None
        if cal is None:
            raise ValueError("calibration must be a strictly loaded StereoCalibration")
        _coerce_rig(rig)
        if config is not None and not isinstance(config, StereoHeadPoseConfig):
            raise ValueError("config must be a StereoHeadPoseConfig")
        _parse_points_3d(matches)
        left_px = _parse_centroids_2d(left_candidates, cal.image_size, "left")
        right_px = _parse_centroids_2d(right_candidates, cal.image_size, "right")
        report = estimate_stereo_head_pose(
            calibration, rig, [], left_px[:0], right_px[:0], config, module=module
        )
        report["input_counts"] = {
            "points_3d": len(matches),
            "left_centroids_2d": len(left_px),
            "right_centroids_2d": len(right_px),
        }
        report["state"] = "ambiguous" if state in ("ambiguous", "partial") else "point_count_mismatch"
        report["reason"] = f"triangulation_state_{state}"
        if state in ("ambiguous", "partial"):
            report["ambiguity"]["ambiguous"] = True
        return report

    return estimate_stereo_head_pose(
        calibration, rig, matches, left_candidates, right_candidates, config, module=module
    )
