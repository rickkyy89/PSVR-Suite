"""Chessboard intrinsics: detection, pure-NumPy Zhang solve, strict I/O.

Detection needs OpenCV; the calibration *math* and the strict intrinsics file
format do not. ``solve_intrinsics`` uses OpenCV's optimiser when available and
falls back to a deterministic Zhang linear estimate otherwise, so the offline
path is testable on a machine without cv2.

The loader is deliberately strict: wrong schema/version, non-finite values,
a malformed camera matrix or an image-size mismatch are hard errors. A file is
never silently coerced, because a metric estimator built on guessed intrinsics
is worse than no estimator.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from .camera_backend import OpenCvUnavailable, import_cv2
from .geometry import (
    GeometryError,
    homography_dlt,
    pose_candidates_from_homography,
    reprojection_errors,
)

INTRINSICS_SCHEMA = "psvr.camera.intrinsics"
INTRINSICS_VERSION = 1
DISTORTION_MODELS = ("plumb_bob", "none")
_TOP_LEVEL_KEYS = {
    "schema",
    "version",
    "created_utc",
    "image_size",
    "camera_matrix",
    "distortion_model",
    "distortion_coeffs",
    "calibrated",
    "calibration",
    "provenance",
}


class CalibrationError(ValueError):
    pass


@dataclass(frozen=True)
class ChessboardSpec:
    inner_cols: int
    inner_rows: int
    square_size_m: float

    def __post_init__(self) -> None:
        if type(self.inner_cols) is not int or self.inner_cols < 2:
            raise CalibrationError("inner_cols must be an integer >= 2")
        if type(self.inner_rows) is not int or self.inner_rows < 2:
            raise CalibrationError("inner_rows must be an integer >= 2")
        if not np.isfinite(self.square_size_m) or self.square_size_m <= 0:
            raise CalibrationError("square_size_m must be a positive number")

    @property
    def corner_count(self) -> int:
        return self.inner_cols * self.inner_rows

    def object_points(self) -> np.ndarray:
        """Inner corners on z=0 in metres, row-major with x fastest (OpenCV order)."""
        points = np.zeros((self.corner_count, 3), dtype=np.float64)
        grid = np.array(
            [(i, j) for j in range(self.inner_rows) for i in range(self.inner_cols)],
            dtype=np.float64,
        )
        points[:, :2] = grid * float(self.square_size_m)
        return points

    def to_dict(self) -> dict:
        return {
            "inner_cols": self.inner_cols,
            "inner_rows": self.inner_rows,
            "square_size_m": float(self.square_size_m),
        }


@dataclass
class Intrinsics:
    camera_matrix: np.ndarray
    distortion_coeffs: np.ndarray
    image_size: tuple
    calibrated: bool
    distortion_model: str = "plumb_bob"
    rms_reprojection_px: Optional[float] = None
    views: Optional[int] = None
    created_utc: Optional[str] = None
    source: str = "chessboard"
    notes: str = ""
    chessboard: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "schema": INTRINSICS_SCHEMA,
            "version": INTRINSICS_VERSION,
            "created_utc": self.created_utc or datetime.now(timezone.utc).isoformat(),
            "image_size": [int(self.image_size[0]), int(self.image_size[1])],
            "camera_matrix": [[float(v) for v in row] for row in np.asarray(self.camera_matrix)],
            "distortion_model": self.distortion_model,
            "distortion_coeffs": [float(v) for v in np.asarray(self.distortion_coeffs).reshape(-1)],
            "calibrated": bool(self.calibrated),
            "calibration": {
                "rms_reprojection_px": (
                    None if self.rms_reprojection_px is None else float(self.rms_reprojection_px)
                ),
                "views": self.views,
                "chessboard": self.chessboard,
            },
            "provenance": {
                "source": self.source,
                "notes": self.notes,
                "metric": bool(self.calibrated),
            },
        }


def _require(condition, message: str) -> None:
    if not condition:
        raise CalibrationError(message)


def _as_matrix(value, name: str) -> np.ndarray:
    array = np.asarray(value, dtype=np.float64)
    _require(array.shape == (3, 3), f"{name} must be a 3x3 matrix")
    _require(bool(np.all(np.isfinite(array))), f"{name} contains non-finite values")
    return array


def _as_vector(value, name: str) -> np.ndarray:
    array = np.asarray(value, dtype=np.float64).reshape(-1)
    _require(bool(np.all(np.isfinite(array))), f"{name} contains non-finite values")
    return array


def intrinsics_from_dict(data: dict, strict: bool = True, expected_image_size=None) -> Intrinsics:
    _require(isinstance(data, dict), "intrinsics must be a JSON object")
    if strict:
        unknown = set(data) - _TOP_LEVEL_KEYS
        _require(not unknown, f"unknown intrinsics keys: {sorted(unknown)}")
    _require(data.get("schema") == INTRINSICS_SCHEMA, "unexpected intrinsics schema")
    _require(data.get("version") == INTRINSICS_VERSION, "unsupported intrinsics version")
    size = data.get("image_size")
    _require(
        isinstance(size, list)
        and len(size) == 2
        and all(type(v) is int and v > 0 for v in size),
        "image_size must be [width, height] positive integers",
    )
    if expected_image_size is not None:
        _require(
            (int(size[0]), int(size[1])) == (int(expected_image_size[0]), int(expected_image_size[1])),
            f"intrinsics image_size {size} does not match {list(expected_image_size)}",
        )
    camera = _as_matrix(data.get("camera_matrix"), "camera_matrix")
    _require(camera[0, 0] > 0 and camera[1, 1] > 0, "focal lengths must be positive")
    _require(
        abs(float(camera[1, 0])) < 1e-12,
        "camera_matrix must be an upper-triangular K with K[1,0]=0",
    )
    _require(
        abs(camera[2, 0]) < 1e-12
        and abs(camera[2, 1]) < 1e-12
        and abs(camera[2, 2] - 1.0) < 1e-9,
        "camera_matrix must be an upper-triangular K with K[2,2]=1",
    )
    try:
        inverse = np.linalg.inv(camera)
    except np.linalg.LinAlgError as exc:
        raise CalibrationError(f"camera_matrix is not invertible: {exc}") from exc
    _require(
        bool(np.all(np.isfinite(inverse))),
        "camera_matrix is not invertible",
    )
    _require(0 < camera[0, 2] < size[0], "principal point cx outside the image")
    _require(0 < camera[1, 2] < size[1], "principal point cy outside the image")
    model = data.get("distortion_model", "plumb_bob")
    _require(model in DISTORTION_MODELS, f"unknown distortion_model {model!r}")
    distortion = _as_vector(data.get("distortion_coeffs", []), "distortion_coeffs")
    _require(distortion.size in (0, 5), "distortion_coeffs must be empty or five values")
    if model == "none":
        distortion = np.zeros(5)
    elif distortion.size == 0:
        distortion = np.zeros(5)
    calibrated = data.get("calibrated")
    _require(type(calibrated) is bool, "calibrated must be a boolean")
    provenance = data.get("provenance", {})
    _require(isinstance(provenance, dict), "provenance must be an object")
    source = provenance.get("source", "unknown")
    _require(isinstance(source, str), "provenance.source must be a string")
    calibration = data.get("calibration", {})
    _require(isinstance(calibration, dict), "calibration must be an object")
    rms = calibration.get("rms_reprojection_px")
    if rms is not None:
        _require(np.isfinite(rms) and rms >= 0, "rms_reprojection_px must be finite and >= 0")
    views = calibration.get("views")
    if views is not None:
        _require(type(views) is int and views > 0, "views must be a positive integer")
    return Intrinsics(
        camera_matrix=camera,
        distortion_coeffs=distortion,
        image_size=(int(size[0]), int(size[1])),
        calibrated=calibrated,
        distortion_model=model,
        rms_reprojection_px=None if rms is None else float(rms),
        views=views,
        created_utc=data.get("created_utc"),
        source=source,
        notes=str(provenance.get("notes", "")),
        chessboard=calibration.get("chessboard"),
    )


def load_intrinsics(path, strict: bool = True, expected_image_size=None) -> Intrinsics:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise CalibrationError(f"cannot read intrinsics file: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CalibrationError(f"intrinsics file is not valid JSON: {exc}") from exc
    return intrinsics_from_dict(data, strict=strict, expected_image_size=expected_image_size)


def save_intrinsics(path, intrinsics: Intrinsics) -> None:
    Path(path).write_text(
        json.dumps(intrinsics.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def zhang_intrinsics(homographies: Sequence[np.ndarray]) -> np.ndarray:
    """Zhang's linear intrinsics estimate from >= 3 plane homographies."""
    if len(homographies) < 3:
        raise CalibrationError("Zhang's method needs at least three views")
    rows = []
    for homography in homographies:
        h = np.asarray(homography, dtype=np.float64).reshape(3, 3)

        def v(i: int, j: int) -> np.ndarray:
            return np.array(
                [
                    h[0, i] * h[0, j],
                    h[0, i] * h[1, j] + h[1, i] * h[0, j],
                    h[1, i] * h[1, j],
                    h[2, i] * h[0, j] + h[0, i] * h[2, j],
                    h[2, i] * h[1, j] + h[1, i] * h[2, j],
                    h[2, i] * h[2, j],
                ],
                dtype=np.float64,
            )

        rows.append(v(0, 1))
        rows.append(v(0, 0) - v(1, 1))
    _, _, vt = np.linalg.svd(np.asarray(rows, dtype=np.float64))
    b = vt[-1]
    if b[0] < 0:  # B is defined up to sign; make it positive definite.
        b = -b
    # v_ij order is [B11, B12, B22, B13, B23, B33].
    b11, b12, b22, b13, b23, b33 = b
    denominator = b11 * b22 - b12 * b12
    _require(abs(denominator) > 1e-18, "degenerate Zhang system")
    v0 = (b12 * b13 - b11 * b23) / denominator
    lam = b33 - (b13 * b13 + v0 * (b12 * b13 - b11 * b23)) / b11
    _require(b11 > 0 and lam > 0, "non-physical Zhang solution")
    alpha = float(np.sqrt(lam / b11))
    beta = float(np.sqrt(lam * b11 / denominator))
    gamma = float(-b12 * alpha * alpha * beta / lam)
    u0 = float(gamma * v0 / beta - b13 * alpha * alpha / lam)
    return np.array(
        [[alpha, gamma, u0], [0.0, beta, v0], [0.0, 0.0, 1.0]], dtype=np.float64
    )


def _validate_views(views, spec: ChessboardSpec):
    _require(len(views) >= 3, "at least three chessboard views are required")
    object_points = spec.object_points()
    parsed = []
    for index, image_points in enumerate(views):
        array = np.asarray(image_points, dtype=np.float64)
        _require(
            array.ndim == 2 and array.shape[1] == 2,
            f"view {index} image_points must be Nx2",
        )
        _require(
            array.shape[0] == object_points.shape[0],
            f"view {index} has {array.shape[0]} points, expected {object_points.shape[0]}",
        )
        _require(bool(np.all(np.isfinite(array))), f"view {index} has non-finite points")
        parsed.append(array)
    return object_points, parsed


def _validate_camera_estimate(camera) -> np.ndarray:
    """Fail closed on a non-physical solver output (B5-01/B5-03)."""
    estimated = _as_matrix(camera, "camera_matrix")
    _require(
        estimated[0, 0] > 0 and estimated[1, 1] > 0,
        "focal lengths must be positive",
    )
    _require(
        abs(float(estimated[1, 0])) < 1e-12,
        "camera_matrix must be an upper-triangular K with K[1,0]=0",
    )
    _require(
        abs(estimated[2, 0]) < 1e-12
        and abs(estimated[2, 1]) < 1e-12
        and abs(estimated[2, 2] - 1.0) < 1e-9,
        "camera_matrix must be an upper-triangular K with K[2,2]=1",
    )
    try:
        inverse = np.linalg.inv(estimated)
    except np.linalg.LinAlgError as exc:
        raise CalibrationError(f"camera_matrix is not invertible: {exc}") from exc
    _require(
        bool(np.all(np.isfinite(inverse))),
        "camera_matrix is not invertible",
    )
    return estimated


def _coherent_rms(camera, object_points, parsed) -> float:
    """Shared-K reprojection RMS with per-view SO3 poses (B5-01).

    Each view keeps its own pose, but the pose comes from decomposing that
    view's homography with the *returned* K (orthonormalized SO3 ``[R|t]``)
    and reprojecting with ``K[R|t]``. A free homography per view can fit any
    planar view almost exactly even when no single K explains all views, so
    that score is optimistic and must not gate a metric calibration. The
    coherent score is conservative by construction: on exact synthetic data
    it stays near zero, while on views from incompatible cameras it exposes
    the compromise fit (see the incoherent-views regression test).
    """
    points3d = np.asarray(object_points, dtype=np.float64)
    total_sq = 0.0
    total_count = 0
    for index, points in enumerate(parsed):
        homography = homography_dlt(points3d[:, :2], points)
        try:
            candidates = pose_candidates_from_homography(
                homography, camera, points3d, points
            )
        except GeometryError as exc:
            raise CalibrationError(
                f"view {index} pose decomposition failed: {exc}"
            ) from exc
        if not candidates:
            raise CalibrationError(f"view {index} has no valid planar pose")
        best = candidates[0]
        try:
            errors = reprojection_errors(
                camera, best.rotation, best.translation, points3d, points
            )
        except GeometryError as exc:
            raise CalibrationError(
                f"view {index} reprojection failed: {exc}"
            ) from exc
        _require(
            bool(np.all(np.isfinite(errors))),
            f"view {index} has non-finite reprojection errors",
        )
        total_sq += float(np.sum(errors * errors))
        total_count += int(errors.shape[0])
    _require(total_count > 0, "no reprojection errors to score")
    rms = float(np.sqrt(total_sq / float(total_count)))
    _require(bool(np.isfinite(rms)) and rms >= 0, "coherent RMS is non-finite")
    return rms


def solve_intrinsics(
    views,
    image_size,
    spec: ChessboardSpec,
    prefer_opencv: bool = True,
    module=None,
    max_rms_px: Optional[float] = None,
) -> Intrinsics:
    """Estimate intrinsics from detected chessboard corners (one array per view).

    ``max_rms_px`` is a fail-closed gate: when given, a solution whose
    reprojection RMS exceeds it raises :class:`CalibrationError` instead of
    producing a calibration file that would feed a wrong metric pose. When
    OpenCV is used the RMS is OpenCV's own ``calibrateCamera`` return value;
    otherwise it is the shared-K coherent RMS: each view is reposed by
    decomposing its homography with the returned K (orthonormal SO3
    ``[R|t]``) and reprojecting with ``K[R|t]``. A free homography per view
    fits any single planar view, so it cannot gate a shared camera; the
    coherent score is conservative and exposes views from incompatible
    cameras. The deterministic path stays pure NumPy; it is also the fallback
    when the optional OpenCV solve is unavailable or raises.
    """
    object_points, parsed = _validate_views(views, spec)
    width, height = int(image_size[0]), int(image_size[1])
    _require(width > 0 and height > 0, "image_size must be positive")

    opencv_used = False
    opencv_rms = None
    if prefer_opencv:
        try:
            cv2 = import_cv2(module)
        except OpenCvUnavailable:
            cv2 = None
        if cv2 is not None and hasattr(cv2, "calibrateCamera"):
            obj = [object_points.astype(np.float32) for _ in parsed]
            img = [p.reshape(-1, 1, 2).astype(np.float32) for p in parsed]
            try:
                ret, camera, distortion, _, _ = cv2.calibrateCamera(
                    obj, img, (width, height), None, None
                )
                camera = np.asarray(camera, dtype=np.float64)
                distortion = np.asarray(distortion, dtype=np.float64).reshape(-1)
                if distortion.size < 5:
                    distortion = np.pad(distortion, (0, 5 - distortion.size))
                if ret is not None and np.isfinite(float(ret)):
                    opencv_rms = float(ret)
                opencv_used = True
            except Exception:  # noqa: BLE001 - fall back to the deterministic path
                cv2 = None
        if not opencv_used:
            homographies = [homography_dlt(object_points[:, :2], p) for p in parsed]
            camera = zhang_intrinsics(homographies)
            distortion = np.zeros(5)
    else:
        homographies = [homography_dlt(object_points[:, :2], p) for p in parsed]
        camera = zhang_intrinsics(homographies)
        distortion = np.zeros(5)

    camera = _validate_camera_estimate(camera)
    distortion = _as_vector(distortion, "distortion_coeffs")
    _require(distortion.size == 5, "distortion_coeffs must be five values")
    if opencv_used:
        # A successful OpenCV solve reports its own distortion-aware RMS.
        _require(
            opencv_rms is not None and np.isfinite(opencv_rms) and opencv_rms >= 0,
            "OpenCV calibration did not return a finite RMS; refusing calibration",
        )
        rms = float(opencv_rms)
    else:
        # Pure-NumPy Zhang path (requested, OpenCV missing, or OpenCV error
        # fallback): score the shared K with per-view SO3 poses, not free Hs.
        rms = _coherent_rms(camera, object_points, parsed)
    if max_rms_px is not None:
        _require(max_rms_px > 0, "max_rms_px must be positive")
        _require(
            np.isfinite(rms) and rms <= float(max_rms_px),
            f"calibration RMS {rms:.4f}px exceeds the {float(max_rms_px):.4f}px limit; "
            "refusing to emit an unusable calibration",
        )

    return Intrinsics(
        camera_matrix=camera,
        distortion_coeffs=distortion,
        image_size=(width, height),
        calibrated=True,
        distortion_model="plumb_bob",
        rms_reprojection_px=rms,
        views=len(parsed),
        source="chessboard_opencv" if opencv_used else "chessboard_zhang",
        notes=(
            "OpenCV calibrateCamera" if opencv_used
            else "deterministic Zhang linear estimate; verify before metric use"
        ),
        chessboard=spec.to_dict(),
    )


def detect_chessboard(image, spec: ChessboardSpec, module=None):
    """Detect chessboard corners with OpenCV. Returns ``(found, corners Nx2)``."""
    cv2 = import_cv2(module)
    gray = image
    if getattr(image, "ndim", 2) == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    flags = getattr(cv2, "CALIB_CB_ADAPTIVE_THRESH", 0) | getattr(
        cv2, "CALIB_CB_NORMALIZE_IMAGE", 0
    )
    found, corners = cv2.findChessboardCorners(
        gray, (spec.inner_cols, spec.inner_rows), flags
    )
    if not found:
        return False, None
    criteria = (
        getattr(cv2, "TERM_CRITERIA_EPS", 3) | getattr(cv2, "TERM_CRITERIA_MAX_ITER", 2),
        30,
        0.001,
    )
    corners = cv2.cornerSubPix(
        gray, corners, (11, 11), (-1, -1), criteria
    )
    return True, np.asarray(corners, dtype=np.float64).reshape(-1, 2)


def calibrate_from_images(
    image_paths,
    spec: ChessboardSpec,
    prefer_opencv: bool = True,
    module=None,
    max_rms_px: Optional[float] = None,
) -> Intrinsics:
    """Detect the board in each image and solve intrinsics (OpenCV required).

    ``image_size`` is fixed by the first view where the board is found; an
    image without a board never defines it. Later usable views with a
    different size fail closed instead of mixing resolutions in one solve.
    """
    cv2 = import_cv2(module)
    views = []
    image_size = None
    skipped = []
    for path in image_paths:
        image = cv2.imread(str(path), getattr(cv2, "IMREAD_GRAYSCALE", 0))
        if image is None:
            skipped.append(f"{path}: unreadable")
            continue
        found, corners = detect_chessboard(image, spec, module=module)
        if not found:
            skipped.append(f"{path}: board not found")
            continue
        size = (int(image.shape[1]), int(image.shape[0]))
        if image_size is None:
            image_size = size
        elif size != image_size:
            raise CalibrationError(
                f"calibration image size changed from {image_size} to {size} "
                f"at {path}; refusing to mix resolutions in one calibration"
            )
        views.append(corners)
    if image_size is None:
        raise CalibrationError(f"no usable chessboard views; skipped: {skipped}")
    if len(views) < 3:
        raise CalibrationError(
            f"only {len(views)} usable views (need >= 3); skipped: {skipped}"
        )
    intrinsics = solve_intrinsics(
        views, image_size, spec, prefer_opencv=prefer_opencv, module=module,
        max_rms_px=max_rms_px,
    )
    if skipped:
        intrinsics.notes = (intrinsics.notes + f"; skipped {len(skipped)} image(s)").strip("; ")
    return intrinsics


def detect_and_solve_live(
    index: int,
    backend: Optional[str],
    spec: ChessboardSpec,
    target_views: int = 15,
    max_seconds: float = 60.0,
    min_gap_s: float = 1.0,
    prefer_opencv: bool = True,
    module=None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    fps: Optional[float] = None,
    dll_path=None,
    library=None,
    max_read_failures: int = 60,
    max_rms_px: Optional[float] = None,
):
    """Collect chessboard views from a live camera until ``target_views``.

    Works with either backend: the OpenCV stack or the native PS3 Eye
    (``backend="pseye"``). OpenCV is required for the chessboard detection in
    both cases. The first frame fixes the image size and every later frame must
    match it, so a mid-stream resolution change fails closed instead of
    producing intrinsics for the wrong size. Returns ``(intrinsics, report)``.
    """
    import time

    from .camera_source import open_camera_source

    if target_views < 3:
        raise CalibrationError("target_views must be at least three")
    if max_seconds <= 0:
        raise CalibrationError("max_seconds must be positive")
    if min_gap_s < 0:
        raise CalibrationError("min_gap_s must be >= 0")
    cv2 = import_cv2(module)
    cap = None
    views = []
    skipped = 0
    image_size = None
    last_capture = 0.0
    consecutive_failures = 0
    started = time.monotonic()
    descriptor_dict = None
    try:
        cap, descriptor = open_camera_source(
            index, backend=backend, width=width, height=height, fps=fps,
            dll_path=dll_path, library=library, module=module,
        )
        if descriptor is not None:
            descriptor_dict = descriptor.to_dict()
        while len(views) < target_views and time.monotonic() - started < max_seconds:
            ok, frame = cap.read()
            if not ok or frame is None:
                skipped += 1
                consecutive_failures += 1
                if consecutive_failures >= max(int(max_read_failures), 1):
                    raise CalibrationError(
                        "live calibration aborted after repeated camera read failures"
                    )
                continue
            consecutive_failures = 0
            size = (int(frame.shape[1]), int(frame.shape[0]))
            if image_size is None:
                image_size = size
            elif size != image_size:
                raise CalibrationError(
                    f"live frame size changed from {image_size} to {size}; "
                    "refusing to mix resolutions in one calibration"
                )
            now = time.monotonic()
            if now - last_capture < min_gap_s:
                continue
            found, corners = detect_chessboard(frame, spec, module=module)
            if found:
                views.append(corners)
                last_capture = now
    finally:
        if cap is not None:
            cap.release()
    if image_size is None or len(views) < 3:
        raise CalibrationError(
            f"live calibration collected {len(views)} usable views; need >= 3"
        )
    intrinsics = solve_intrinsics(
        views, image_size, spec, prefer_opencv=prefer_opencv, module=module,
        max_rms_px=max_rms_px,
    )
    report = {
        "index": index,
        "backend": backend or "any",
        "image_size": [int(image_size[0]), int(image_size[1])],
        "negotiated": descriptor_dict,
        "views": len(views),
        "dropped_frames": skipped,
        "duration_s": round(time.monotonic() - started, 3),
    }
    return intrinsics, report


__all__ = [
    "CalibrationError",
    "ChessboardSpec",
    "Intrinsics",
    "INTRINSICS_SCHEMA",
    "INTRINSICS_VERSION",
    "intrinsics_from_dict",
    "load_intrinsics",
    "save_intrinsics",
    "zhang_intrinsics",
    "solve_intrinsics",
    "detect_chessboard",
    "calibrate_from_images",
    "detect_and_solve_live",
]
