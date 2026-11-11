"""Offline, fail-closed PS4 stereo calibration. No acquisition or hardware claims.

Coordinates satisfy X_right = R @ X_left + T, with T in metres. Input corners
must already correspond in detector order; in particular a symmetric board's
180-degree ambiguity is never 'fixed' by sorting. Reserve independent views
before calling solve_stereo, then pass them to validate_stereo (no refitting).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import uuid

import numpy as np

from .calibration import (
    CalibrationError, ChessboardSpec, Intrinsics, intrinsics_from_dict,
    solve_intrinsics,
)
from .camera_backend import import_cv2

STEREO_SCHEMA = "psvr.camera.stereo_intrinsics"
STEREO_VERSION = 1
MIN_STEREO_VIEWS = 8
_MAX_FILE_BYTES = 1024 * 1024
_RECT_SHAPES = {"R1": (3, 3), "R2": (3, 3), "P1": (3, 4),
                "P2": (3, 4), "Q": (4, 4)}


def _require(condition, message):
    if not condition:
        raise CalibrationError(message)


def _keys(value, keys, name):
    _require(isinstance(value, dict), f"{name} must be an object")
    _require(set(value) == set(keys), f"{name} has missing or unknown keys")


def _size(value):
    _require(isinstance(value, (tuple, list)) and len(value) == 2
             and all(type(x) is int and x > 0 for x in value),
             "image_size must contain two positive integers")
    return tuple(value)


def _number(value, name, positive=False):
    try:
        valid = (type(value) in (int, float) and np.isfinite(float(value))
                 and (value > 0 if positive else value >= 0))
    except (ValueError, OverflowError, TypeError):
        valid = False
    _require(valid,
             f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    return float(value)


def _array(value, shape, name):
    try:
        _require(np.asarray(value).dtype.kind in "iuf", f"{name} must contain numbers")
        result = np.asarray(value, dtype=np.float64)
    except (ValueError, TypeError, OverflowError) as exc:
        raise CalibrationError(f"{name} must be numeric") from exc
    _require(result.shape == shape and np.isfinite(result).all(),
             f"{name} must be finite with shape {shape}")
    return result


def _rotation(value, name):
    result = _array(value, (3, 3), name)
    _require(np.allclose(result.T @ result, np.eye(3), atol=1e-6, rtol=0)
             and abs(np.linalg.det(result) - 1) < 1e-6,
             f"{name} must be a proper orthonormal rotation")
    return result


def _cross(t):
    x, y, z = t
    return np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])


def _same_up_to_scale(actual, expected, name):
    norm = np.linalg.norm(actual)
    reference = np.linalg.norm(expected)
    _require(norm > 1e-15 and reference > 1e-15, f"{name} is degenerate")
    a, b = actual / norm, expected / reference
    _require(min(np.linalg.norm(a - b), np.linalg.norm(a + b)) < 1e-5,
             f"{name} is inconsistent with intrinsics/R/T")


def _fingerprint(left, right):
    digest = hashlib.sha256()
    for points in (left, right):
        digest.update(np.asarray(points, dtype="<f8").tobytes())
    return digest.hexdigest()


def _observations(left_views, right_views, image_size, spec, minimum):
    size = _size(image_size)
    _require(isinstance(spec, ChessboardSpec), "spec must be a ChessboardSpec")
    _require(len(left_views) == len(right_views), "left/right view counts must match")
    _require(len(left_views) >= minimum, f"need at least {minimum} paired views")
    parsed = []
    for name, views in (("left", left_views), ("right", right_views)):
        side = []
        for index, value in enumerate(views):
            points = _array(value, (spec.corner_count, 2), f"{name} view {index}")
            _require(((points >= 0) & (points < np.array(size))).all(),
                     f"{name} view {index} has points outside image_size")
            # A stationary board must not count as distinct calibration evidence.
            _require(not any(np.sqrt(np.mean((points - old) ** 2)) < 0.25
                             for old in side),
                     f"{name} has duplicate or near-identical views")
            side.append(points.copy())
        parsed.append(side)
    return parsed


@dataclass
class StereoCalibration:
    left: Intrinsics
    right: Intrinsics
    R: np.ndarray
    T: np.ndarray
    E: np.ndarray
    F: np.ndarray
    rms: float
    sample_count: int
    image_size: tuple
    spec: ChessboardSpec
    R1: np.ndarray | None = None
    R2: np.ndarray | None = None
    P1: np.ndarray | None = None
    P2: np.ndarray | None = None
    Q: np.ndarray | None = None
    mode: str = ""
    source: str = "PS4_OV580"
    created_utc: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    training_fingerprints: tuple = ()
    validation: dict | None = None

    @property
    def rms_reprojection_px(self):
        return self.rms

    @property
    def views(self):
        return self.sample_count

    def to_dict(self):
        rect = {name: getattr(self, name) for name in _RECT_SHAPES}
        return {
            "schema": STEREO_SCHEMA, "version": STEREO_VERSION,
            "created_utc": self.created_utc, "image_size": list(self.image_size),
            "mode": self.mode or f"{self.image_size[0]}x{self.image_size[1]}",
            "left": self.left.to_dict(), "right": self.right.to_dict(),
            "R": np.asarray(self.R).tolist(), "T": np.asarray(self.T).tolist(),
            "E": np.asarray(self.E).tolist(), "F": np.asarray(self.F).tolist(),
            "rms_reprojection_px": self.rms, "sample_count": self.sample_count,
            "chessboard": self.spec.to_dict(),
            "rectification": (None if all(x is None for x in rect.values()) else
                              {key: None if value is None else np.asarray(value).tolist()
                               for key, value in rect.items()}),
            "training_fingerprints": list(self.training_fingerprints),
            "validation": self.validation,
            "provenance": {"source": self.source, "translation_units": "metres",
                           "transform": "X_right = R @ X_left + T",
                           "stream_labels": ["left", "right"],
                           "physical_order_verified": False,
                           "hardware_acceptance": "unverified"},
        }


def _intrinsics(data, size, count, spec):
    # The existing loader owns numerical K/distortion checks; add exact nested
    # structure checks here because the stereo schema must fail closed.
    template = Intrinsics(np.eye(3), np.zeros(5), size, True).to_dict()
    _keys(data, template, "intrinsics")
    _keys(data["calibration"], template["calibration"], "intrinsics calibration")
    _keys(data["provenance"], template["provenance"], "intrinsics provenance")
    _require(type(data["version"]) is int and
             isinstance(data["provenance"]["source"], str) and
             isinstance(data["provenance"]["notes"], str),
             "invalid intrinsics version/provenance types")
    _require(type(data["calibration"]["rms_reprojection_px"]) in (int, float),
             "intrinsics RMS must be numeric")
    try:
        result = intrinsics_from_dict(data, strict=True, expected_image_size=size)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CalibrationError(f"invalid stereo intrinsics: {exc}") from exc
    _require(result.calibrated and data["provenance"]["metric"] is True,
             "stereo requires calibrated metric intrinsics")
    _require(result.views == count and result.chessboard == spec.to_dict(),
             "intrinsics views/chessboard disagree with stereo calibration")
    _number(result.rms_reprojection_px, "intrinsics RMS")
    return result


def stereo_from_dict(data, *, expected_image_size=None, expected_mode=None):
    keys = {"schema", "version", "created_utc", "image_size", "mode", "left", "right",
            "R", "T", "E", "F", "rms_reprojection_px", "sample_count", "chessboard",
            "rectification", "training_fingerprints", "provenance", "validation"}
    _keys(data, keys, "stereo calibration")
    _require(data["schema"] == STEREO_SCHEMA, "unexpected stereo schema")
    _require(type(data["version"]) is int and data["version"] == STEREO_VERSION,
             "unsupported stereo version")
    size = _size(data["image_size"])
    if expected_image_size is not None:
        _require(size == _size(expected_image_size), "stereo image_size mismatch")
    mode = data["mode"]
    _require(mode == f"{size[0]}x{size[1]}", "mode must match per-eye image_size")
    if expected_mode is not None:
        _require(mode == expected_mode, "stereo mode mismatch")
    timestamp = data["created_utc"]
    try:
        _require(isinstance(timestamp, str) and
                 datetime.fromisoformat(timestamp).utcoffset() is not None,
                 "created_utc must be an ISO timezone-aware timestamp")
    except (TypeError, ValueError) as exc:
        raise CalibrationError("invalid created_utc") from exc
    count = data["sample_count"]
    _require(type(count) is int and count >= MIN_STEREO_VIEWS,
             "sample_count must be an integer >= 8")
    rms = _number(data["rms_reprojection_px"], "stereo RMS")
    _keys(data["chessboard"], {"inner_cols", "inner_rows", "square_size_m"}, "chessboard")
    _number(data["chessboard"]["square_size_m"], "square_size_m", positive=True)
    try:
        spec = ChessboardSpec(**data["chessboard"])
    except (TypeError, ValueError) as exc:
        raise CalibrationError("invalid chessboard") from exc
    left = _intrinsics(data["left"], size, count, spec)
    right = _intrinsics(data["right"], size, count, spec)
    rotation = _rotation(data["R"], "R")
    translation = _array(data["T"], (3,), "T")
    _require(np.linalg.norm(translation) > 1e-4, "stereo baseline is near zero")
    essential = _array(data["E"], (3, 3), "E")
    fundamental = _array(data["F"], (3, 3), "F")
    expected_e = _cross(translation) @ rotation
    _same_up_to_scale(essential, expected_e, "E")
    _same_up_to_scale(fundamental, np.linalg.inv(right.camera_matrix).T @
                      expected_e @ np.linalg.inv(left.camera_matrix), "F")
    provenance = data["provenance"]
    _keys(provenance, {"source", "translation_units", "transform", "stream_labels",
                       "physical_order_verified", "hardware_acceptance"}, "provenance")
    _require(isinstance(provenance["source"], str) and provenance["source"].strip(),
             "source must be a nonempty string")
    _require(provenance["translation_units"] == "metres" and
             provenance["transform"] == "X_right = R @ X_left + T" and
             provenance["stream_labels"] == ["left", "right"] and
             provenance["physical_order_verified"] is False and
             provenance["hardware_acceptance"] == "unverified",
             "invalid units, transform or unverified PS4 stream provenance")
    fingerprints = data["training_fingerprints"]
    _require(isinstance(fingerprints, list) and len(fingerprints) == count and
             len(set(str(x) for x in fingerprints)) == count and
             all(isinstance(x, str) and len(x) == 64 and
                 all(c in "0123456789abcdef" for c in x) for x in fingerprints),
             "invalid training_fingerprints")
    rect = data["rectification"]
    parsed_rect = {}
    if rect is not None:
        _keys(rect, _RECT_SHAPES, "rectification")
        for name, shape in _RECT_SHAPES.items():
            parsed_rect[name] = (_rotation(rect[name], name) if name.startswith("R")
                                 else _array(rect[name], shape, name))
        r1, r2 = parsed_rect["R1"], parsed_rect["R2"]
        p1, p2, q = (parsed_rect[n] for n in ("P1", "P2", "Q"))
        _require(np.allclose(r2 @ rotation, r1, atol=1e-5, rtol=0),
                 "rectification rotations disagree with R")
        for p in (p1, p2):
            _require(p[0, 0] > 0 and p[1, 1] > 0 and
                     np.linalg.matrix_rank(p[:, :3]) == 3 and
                     np.allclose(p[2], [0, 0, 1, 0], atol=1e-7),
                     "invalid rectification projection")
        _require(np.linalg.matrix_rank(q) == 4, "Q is singular")
        rectified_t = r2 @ translation
        _require(np.allclose(p1[:, 3], 0, atol=1e-7) and
                 np.allclose(p2[:, 3], p2[:, :3] @ rectified_t, atol=1e-5),
                 "rectification projections disagree with T")
        axis = int(np.argmax(np.abs(rectified_t[:2])))
        baseline = rectified_t[axis]
        other = rectified_t.copy()
        other[axis] = 0
        _require(abs(baseline) > 1e-4 and np.linalg.norm(other) < 1e-5,
                 "rectified baseline must align with an image axis")
        expected_q = np.array([[1., 0., 0., -p1[0, 2]],
                               [0., 1., 0., -p1[1, 2]],
                               [0., 0., 0., p1[0, 0]],
                               [0., 0., -1 / baseline,
                                (p1[axis, 2] - p2[axis, 2]) / baseline]])
        _same_up_to_scale(q, expected_q, "Q")
    validation = data["validation"]
    if validation is not None:
        _keys(validation, {"accepted", "sample_count", "rms_reprojection_px",
                           "max_view_rms_px", "epipolar_rms_px", "max_view_epipolar_px",
                           "per_view_rms_px", "per_view_epipolar_px",
                           "max_reprojection_px", "max_epipolar_px"}, "validation")
        _require(validation["accepted"] is True, "validation must record accepted heldout evidence")
        n = validation["sample_count"]
        _require(type(n) is int and n >= 2, "validation needs >=2 independent views")
        for metric in ("rms_reprojection_px", "max_view_rms_px", "epipolar_rms_px",
                       "max_view_epipolar_px", "max_reprojection_px", "max_epipolar_px"):
            _number(validation[metric], metric, positive=metric in
                    ("max_reprojection_px", "max_epipolar_px"))
        for metric, rms_key, max_key in (
                ("per_view_rms_px", "rms_reprojection_px", "max_view_rms_px"),
                ("per_view_epipolar_px", "epipolar_rms_px", "max_view_epipolar_px")):
            values = _array(validation[metric], (n,), metric)
            _require((values >= 0).all() and
                     np.isclose(np.sqrt(np.mean(values**2)), validation[rms_key]) and
                     np.isclose(max(values), validation[max_key]),
                     "inconsistent heldout validation metrics")
        _gate(validation, validation["max_reprojection_px"], validation["max_epipolar_px"])
    return StereoCalibration(left, right, rotation, translation, essential, fundamental,
                             rms, count, size, spec, **parsed_rect, mode=mode,
                             source=provenance["source"], created_utc=timestamp,
                             training_fingerprints=tuple(fingerprints), validation=validation)


def _score(calibration, left_views, right_views, cv2):
    obj = calibration.spec.object_points()
    left, right = calibration.left, calibration.right
    per_view, epipolar = [], []
    for index, (lp, rp) in enumerate(zip(left_views, right_views)):
        ok, rvec, tvec = cv2.solvePnP(obj, lp, left.camera_matrix, left.distortion_coeffs)
        _require(ok, f"view {index}: left pose estimation failed")
        board_r = cv2.Rodrigues(rvec)[0]
        board_t = np.asarray(tvec).reshape(3)
        points_left = (board_r @ obj.T).T + board_t
        points_right = (calibration.R @ points_left.T).T + calibration.T
        _require(np.isfinite(points_right).all() and (points_left[:, 2] > 0).all()
                 and (points_right[:, 2] > 0).all(), "board is behind a stereo camera")
        proj_left = cv2.projectPoints(obj, rvec, tvec, left.camera_matrix,
                                     left.distortion_coeffs)[0].reshape(-1, 2)
        rr = cv2.Rodrigues(calibration.R @ board_r)[0]
        rt = calibration.R @ board_t + calibration.T
        proj_right = cv2.projectPoints(obj, rr, rt, right.camera_matrix,
                                      right.distortion_coeffs)[0].reshape(-1, 2)
        per_view.append(float(np.sqrt(np.mean(np.concatenate((
            np.sum((proj_left - lp)**2, axis=1),
            np.sum((proj_right - rp)**2, axis=1)))))))
        # F is in ideal pixel coordinates: remove distortion but retain K.
        ul = cv2.undistortPoints(lp.reshape(-1, 1, 2), left.camera_matrix,
                                 left.distortion_coeffs, P=left.camera_matrix).reshape(-1, 2)
        ur = cv2.undistortPoints(rp.reshape(-1, 1, 2), right.camera_matrix,
                                 right.distortion_coeffs, P=right.camera_matrix).reshape(-1, 2)
        xl = np.column_stack((ul, np.ones(len(ul))))
        xr = np.column_stack((ur, np.ones(len(ur))))
        lines_r, lines_l = xl @ calibration.F.T, xr @ calibration.F
        numerator = np.sum(xr * lines_r, axis=1)**2
        dl, dr = np.sum(lines_l[:, :2]**2, axis=1), np.sum(lines_r[:, :2]**2, axis=1)
        _require((dl > 1e-30).all() and (dr > 1e-30).all(), "degenerate epipolar lines")
        epipolar.append(float(np.sqrt(np.mean(0.5 * numerator * (1 / dl + 1 / dr)))))
    _require(np.isfinite(per_view).all() and np.isfinite(epipolar).all(),
             "non-finite stereo validation errors")
    return {"sample_count": len(per_view),
            "rms_reprojection_px": float(np.sqrt(np.mean(np.square(per_view)))),
            "max_view_rms_px": max(per_view),
            "epipolar_rms_px": float(np.sqrt(np.mean(np.square(epipolar)))),
            "max_view_epipolar_px": max(epipolar),
            "per_view_rms_px": per_view, "per_view_epipolar_px": epipolar}


def _gate(report, max_rms_px, max_epipolar_px):
    _number(max_rms_px, "max_rms_px", positive=True)
    _number(max_epipolar_px, "max_epipolar_px", positive=True)
    _require(report["max_view_rms_px"] <= max_rms_px,
             "stereo per-view RMS exceeds limit; check pairing and corner ordering")
    _require(report["max_view_epipolar_px"] <= max_epipolar_px,
             "stereo epipolar RMS exceeds limit; check pairing and corner ordering")


def _check_tilt_diversity(views, intrinsic, spec, cv2):
    """Pure translations/fronto-parallel spins cannot constrain planar K."""
    normals = []
    for points in views:
        ok, rvec, _ = cv2.solvePnP(spec.object_points(), points,
                                  intrinsic.camera_matrix, intrinsic.distortion_coeffs)
        _require(ok, "cannot estimate training board pose")
        normals.append(cv2.Rodrigues(rvec)[0][:, 2])
    normals = np.asarray(normals)
    _require(np.isfinite(normals).all(), "non-finite board pose")
    spread = np.linalg.svd(normals - normals.mean(axis=0), compute_uv=False)
    _require(spread[1] > 0.01,
             "insufficient pose diversity: tilt the board about both image axes")


def _refined_intrinsics(initial, camera, distortion, views, spec, cv2):
    """Report per-eye training RMS for the jointly refined K/distortion."""
    intrinsic = replace(
        initial, camera_matrix=np.asarray(camera, dtype=np.float64).copy(),
        distortion_coeffs=np.asarray(distortion, dtype=np.float64).reshape(-1).copy(),
        source="chessboard_opencv_stereo_refined",
        notes=("Monocular OpenCV calibrateCamera initialization followed by joint "
               "OpenCV stereoCalibrate refinement (CALIB_USE_INTRINSIC_GUESS), "
               "training pairs only. Per-eye RMS recomputed with per-view "
               "solvePnP/projectPoints using refined K/distortion; offline geometry, "
               "not hardware acceptance."))
    obj = spec.object_points()
    squared_errors = []
    for index, points in enumerate(views):
        ok, rvec, tvec = cv2.solvePnP(
            obj, points, intrinsic.camera_matrix, intrinsic.distortion_coeffs)
        _require(ok, f"view {index}: refined intrinsics pose estimation failed")
        camera_points = (cv2.Rodrigues(rvec)[0] @ obj.T).T + np.asarray(tvec).reshape(3)
        _require(np.isfinite(camera_points).all() and (camera_points[:, 2] > 0).all(),
                 "board is behind a camera with refined intrinsics")
        projected = cv2.projectPoints(
            obj, rvec, tvec, intrinsic.camera_matrix,
            intrinsic.distortion_coeffs)[0].reshape(-1, 2)
        squared_errors.append(np.sum((projected - points)**2, axis=1))
    rms = float(np.sqrt(np.mean(squared_errors)))
    _number(rms, "refined intrinsics RMS")
    return replace(intrinsic, rms_reprojection_px=rms)


def solve_stereo(left_views, right_views, image_size, spec, max_rms_px=1.0,
                 *, mode=None, source="PS4_OV580", module=None):
    """Fit >=8 distinct atomic pairs; caller should target >=15 plus holdout.

    Requires OpenCV for *both* intrinsics and stereo: the existing intrinsics
    NumPy fallback is explicitly rejected here. Monocular estimates initialize
    joint refinement of both eyes and R/T on these training pairs only; heldout
    views belong exclusively to validate_stereo. Never changes corner ordering.
    """
    _number(max_rms_px, "max_rms_px", positive=True)
    left_views, right_views = _observations(left_views, right_views, image_size,
                                           spec, MIN_STEREO_VIEWS)
    cv2 = import_cv2(module)
    left = solve_intrinsics(left_views, image_size, spec, prefer_opencv=True,
                            module=cv2, max_rms_px=max_rms_px)
    right = solve_intrinsics(right_views, image_size, spec, prefer_opencv=True,
                             module=cv2, max_rms_px=max_rms_px)
    _require(left.source == right.source == "chessboard_opencv",
             "stereo requires successful OpenCV intrinsics; no NumPy fallback")
    obj = [spec.object_points().astype(np.float32) for _ in left_views]
    try:
        _check_tilt_diversity(left_views, left, spec, cv2)
        _check_tilt_diversity(right_views, right, spec, cv2)
        result = cv2.stereoCalibrate(
            obj, [p.astype(np.float32) for p in left_views],
            [p.astype(np.float32) for p in right_views],
            left.camera_matrix.copy(), left.distortion_coeffs.copy(),
            right.camera_matrix.copy(), right.distortion_coeffs.copy(), tuple(image_size),
            flags=cv2.CALIB_USE_INTRINSIC_GUESS,
            criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_MAX_ITER, 200, 1e-10))
        rms, k1, d1, k2, d2, rotation, translation, essential, fundamental = result
        _require(np.isfinite(rms) and 0 <= rms <= max_rms_px,
                 "stereo RMS exceeds limit; check pairing and corner ordering")
        # The monocular fits are initializers, not fixed evidence: retain the
        # optimizer's returned K/D and measure their own per-eye errors afresh.
        left = _refined_intrinsics(left, k1, d1, left_views, spec, cv2)
        right = _refined_intrinsics(right, k2, d2, right_views, spec, cv2)
        rect = cv2.stereoRectify(left.camera_matrix, left.distortion_coeffs,
                                right.camera_matrix, right.distortion_coeffs,
                                tuple(image_size), rotation, translation,
                                flags=cv2.CALIB_ZERO_DISPARITY, alpha=0)
    except CalibrationError:
        raise
    except Exception as exc:
        raise CalibrationError(f"OpenCV stereo solve/rectification failed: {exc}") from exc
    calibration = StereoCalibration(
        left, right, rotation, np.asarray(translation).reshape(3), essential, fundamental,
        float(rms), len(left_views), tuple(image_size), spec,
        **dict(zip(_RECT_SHAPES, rect[:5])),
        mode=mode or f"{image_size[0]}x{image_size[1]}", source=source,
        training_fingerprints=tuple(_fingerprint(l, r) for l, r in zip(left_views, right_views)))
    calibration = stereo_from_dict(calibration.to_dict())
    try:
        _gate(_score(calibration, left_views, right_views, cv2), max_rms_px, max_rms_px)
    except CalibrationError:
        raise
    except Exception as exc:
        raise CalibrationError(f"OpenCV training validation failed: {exc}") from exc
    return calibration


def validate_stereo(calibration, left_views, right_views, max_epipolar_px=1.0,
                    max_reprojection_px=1.5, *, module=None):
    """Score >=2 held-out pairs without updating K/R/T; reject exact train reuse.

    Fingerprints catch accidental direct reuse, not independently recaptured
    stationary boards: collection must still reserve genuinely independent poses.
    A passing report is offline geometry evidence, not PS4 hardware acceptance.
    """
    original = calibration
    # A failed revalidation must not leave an older accepted report attached.
    original.validation = None
    calibration = stereo_from_dict(calibration.to_dict())
    left_views, right_views = _observations(left_views, right_views, calibration.image_size,
                                           calibration.spec, 2)
    _require(not any(_fingerprint(l, r) in calibration.training_fingerprints
                     for l, r in zip(left_views, right_views)),
             "held-out validation cannot reuse training observations")
    try:
        report = _score(calibration, left_views, right_views, import_cv2(module))
    except CalibrationError:
        raise
    except Exception as exc:
        raise CalibrationError(f"stereo validation failed: {exc}") from exc
    _gate(report, max_reprojection_px, max_epipolar_px)
    report.update(accepted=True, max_reprojection_px=float(max_reprojection_px),
                  max_epipolar_px=float(max_epipolar_px))
    original.validation = dict(report)
    return report


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_stereo_calibration(path, expected_image_size=None, *, expected_mode=None):
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(_MAX_FILE_BYTES + 1)
        _require(len(raw) <= _MAX_FILE_BYTES, "stereo calibration file is too large")
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CalibrationError(f"cannot read stereo calibration: {exc}") from exc
    return stereo_from_dict(data, expected_image_size=expected_image_size,
                            expected_mode=expected_mode)


def save_stereo_calibration(path, calibration):
    """Validate, fsync a unique sibling, publish atomically without replacement.

    Hard-link publication has exclusive-create semantics even with concurrent
    writers. Unsupported filesystems fail closed; no overwrite/partial fallback.
    """
    data = calibration.to_dict()
    stereo_from_dict(data)
    payload = json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n"
    _require(len(payload.encode("utf-8")) <= _MAX_FILE_BYTES, "stereo calibration too large")
    target = Path(path)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    owned = False
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            owned = True
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
    finally:
        if owned:
            temporary.unlink(missing_ok=True)


# Short aliases parallel calibration.load_intrinsics/save_intrinsics.
load_stereo = load_stereo_calibration
save_stereo = save_stereo_calibration
