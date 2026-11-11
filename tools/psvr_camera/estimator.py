"""Metric position estimator from an explicit marker geometry.

This is the honest core of M9: position is computed only from

* **explicit calibrated intrinsics** (``calibration.Intrinsics`` with
  ``calibrated=True``), and
* **measured marker geometry** whose provenance is ``user_measured`` or
  ``datasheet``.

The PSVR built-in LED ring geometry is *not* calibrated here. A geometry with
``provenance == "psvr_builtin"`` is refused, so no code path can silently claim
the headset's LEDs are a known rig. The recommended default is an externally
measurable ArUco marker (see ``docs/PSEYE_TRACKING.md``); a measured blob/LED
rig is the alternative when the operator has real 3D coordinates.

Planar markers use the homography for consensus/inliers plus an optional
physical IPPE two-branch check: a front/front ambiguity is only declared when
both IPPE poses sit entirely in front of the lens and explain the pixels
almost equally well (configurable ambiguity floor); a behind-camera
alternative never invalidates a valid best pose, and poses above the
reprojection limit are rejected. Non-planar rigs use DLT
PnP with RANSAC.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from .calibration import Intrinsics
from .geometry import (
    GeometryError,
    InsufficientCorrespondences,
    depth_conditioning,
    depth_values,
    planar_pose,
    ransac_pnp,
    undistort_points,
)
from .planar_solver import (
    branches_are_duplicates,
    ippe_branches,
    reprojection_ambiguity_ratio,
    rotation_separation_deg,
    validate_camera_matrix,
)
from .protocol import quaternion_from_rotation

MARKER_GEOMETRY_SCHEMA = "psvr.camera.marker_geometry"
MARKER_GEOMETRY_VERSION = 1
KINDS = ("aruco", "blob_led")
PROVENANCES = ("user_measured", "datasheet", "psvr_builtin")

# A placeholder, never applied silently: the operator must measure the printed
# marker. ArUco is the recommended default because it is externally measurable.
DEFAULT_ARUCO_MARKER_LENGTH_M = 0.10

# OpenCV optical frame -> driver world frame: +X right, +Y up, -Z forward.
CAMERA_TO_WORLD = np.diag([1.0, -1.0, -1.0])


class EstimatorError(ValueError):
    pass


def optical_to_world(point: Sequence[float]) -> np.ndarray:
    """Map a point from the OpenCV camera frame to the driver world frame."""
    x, y, z = (float(v) for v in point)
    return np.array([x, -y, -z], dtype=np.float64)


def optical_rotation_to_world(rotation: np.ndarray) -> np.ndarray:
    """Map a camera_from_body rotation to world_from_body (driver axes)."""
    return CAMERA_TO_WORLD @ np.asarray(rotation, dtype=np.float64).reshape(3, 3)


@dataclass(frozen=True)
class MarkerGeometry:
    kind: str
    provenance: str
    object_points_m: tuple
    description: str = ""
    marker_length_m: Optional[float] = None
    aruco: Optional[dict] = None

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise EstimatorError(f"kind must be one of {KINDS}, got {self.kind!r}")
        if self.provenance not in PROVENANCES:
            raise EstimatorError(
                f"provenance must be one of {PROVENANCES}, got {self.provenance!r}"
            )
        if self.provenance == "psvr_builtin":
            raise EstimatorError(
                "refusing PSVR built-in LED geometry: its real 3D layout is not "
                "calibrated. Supply a user-measured marker or measured LED rig."
            )
        points = np.asarray(self.object_points_m, dtype=np.float64)
        if points.ndim != 2 or points.shape[1] != 3:
            raise EstimatorError("object_points_m must be an Nx3 array in metres")
        if points.shape[0] < 4:
            raise EstimatorError("at least four marker points are required")
        if not np.all(np.isfinite(points)):
            raise EstimatorError("object_points_m contains non-finite values")
        spread = points.max(axis=0) - points.min(axis=0)
        if float(np.max(spread)) <= 1e-6:
            raise EstimatorError("object_points_m is degenerate (all points coincide)")
        if self.kind == "aruco":
            if self.marker_length_m is None or not (
                np.isfinite(self.marker_length_m) and self.marker_length_m > 0
            ):
                raise EstimatorError("aruco geometry requires a positive marker_length_m")

    @property
    def source(self) -> str:
        return self.kind

    def is_planar(self, tolerance: float = 1e-6) -> bool:
        points = np.asarray(self.object_points_m, dtype=np.float64)
        return bool(np.max(np.abs(points[:, 2])) <= tolerance)

    def to_dict(self) -> dict:
        return {
            "schema": MARKER_GEOMETRY_SCHEMA,
            "version": MARKER_GEOMETRY_VERSION,
            "kind": self.kind,
            "provenance": self.provenance,
            "description": self.description,
            "marker_length_m": self.marker_length_m,
            "aruco": self.aruco,
            "object_points_m": [[float(v) for v in p] for p in self.object_points_m],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MarkerGeometry":
        if not isinstance(data, dict):
            raise EstimatorError("marker geometry must be a JSON object")
        if data.get("schema") not in (None, MARKER_GEOMETRY_SCHEMA):
            raise EstimatorError("unexpected marker geometry schema")
        if data.get("version") not in (None, MARKER_GEOMETRY_VERSION):
            raise EstimatorError("unsupported marker geometry version")
        for key in ("kind", "provenance", "object_points_m"):
            if key not in data:
                raise EstimatorError(f"marker geometry is missing {key!r}")
        return cls(
            kind=data["kind"],
            provenance=data["provenance"],
            object_points_m=tuple(tuple(float(v) for v in p) for p in data["object_points_m"]),
            description=str(data.get("description", "")),
            marker_length_m=(
                None if data.get("marker_length_m") is None else float(data["marker_length_m"])
            ),
            aruco=data.get("aruco"),
        )


def load_marker_geometry(path) -> MarkerGeometry:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise EstimatorError(f"cannot read marker geometry: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise EstimatorError(f"marker geometry is not valid JSON: {exc}") from exc
    return MarkerGeometry.from_dict(data)


def aruco_geometry(
    marker_length_m: float,
    dictionary: str = "DICT_4X4_50",
    marker_id: int = 0,
    provenance: str = "user_measured",
    description: str = "",
) -> MarkerGeometry:
    """Build a single square ArUco marker centred on its own origin.

    Corner order matches OpenCV's ArUco detection (top-left, top-right,
    bottom-right, bottom-left) so the object/image correspondence is direct.
    """
    half = float(marker_length_m) / 2.0
    corners = (
        (-half, half, 0.0),
        (half, half, 0.0),
        (half, -half, 0.0),
        (-half, -half, 0.0),
    )
    return MarkerGeometry(
        kind="aruco",
        provenance=provenance,
        object_points_m=corners,
        description=description or f"ArUco {dictionary} id {marker_id}, measured square",
        marker_length_m=float(marker_length_m),
        aruco={"dictionary": dictionary, "id": marker_id},
    )


@dataclass
class EstimatorConfig:
    max_reprojection_px: float = 2.0
    ambiguity_ratio_threshold: float = 0.5
    ransac_threshold_px: float = 2.0
    ransac_iterations: int = 200
    ransac_seed: int = 12345
    min_depth_conditioning: float = 0.02
    require_calibrated: bool = True
    # Fail closed on a calibration whose own reprojection RMS is too high: a
    # metric pose from a bad calibration is worse than no pose. None disables.
    max_calibration_rms_px: Optional[float] = 1.0

    def __post_init__(self) -> None:
        if not (np.isfinite(self.max_reprojection_px) and self.max_reprojection_px > 0):
            raise EstimatorError("max_reprojection_px must be a positive number")
        if not (0.0 <= self.ambiguity_ratio_threshold <= 1.0):
            raise EstimatorError("ambiguity_ratio_threshold must be in 0..1")
        if not (np.isfinite(self.ransac_threshold_px) and self.ransac_threshold_px > 0):
            raise EstimatorError("ransac_threshold_px must be a positive number")
        if type(self.ransac_iterations) is not int or self.ransac_iterations < 1:
            raise EstimatorError("ransac_iterations must be a positive integer")
        if not (np.isfinite(self.min_depth_conditioning) and self.min_depth_conditioning >= 0):
            raise EstimatorError("min_depth_conditioning must be >= 0")
        if self.max_calibration_rms_px is not None and not (
            np.isfinite(self.max_calibration_rms_px) and self.max_calibration_rms_px > 0
        ):
            raise EstimatorError("max_calibration_rms_px must be null or positive")


@dataclass
class PositionResult:
    valid: bool
    state: str
    reason: str
    source: str
    rotation_camera_from_body: Optional[np.ndarray] = None
    position_camera_m: Optional[np.ndarray] = None
    position_world_m: Optional[np.ndarray] = None
    orientation_world_wxyz: Optional[list] = None
    reprojection_px: Optional[float] = None
    inliers: Optional[int] = None
    ambiguity_ratio: Optional[float] = None
    # True only when an actual two-branch physical (IPPE) comparison ran.
    # The pure-NumPy homography fallback keeps the historical depth/residual
    # gates but cannot rule out a front/front perspective ambiguity, so its
    # valid poses report False here instead of claiming ambiguity_ratio=0
    # proves uniqueness.
    physical_ambiguity_checked: bool = False

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "state": self.state,
            "reason": self.reason,
            "source": self.source,
            "position_world_m": (
                None if self.position_world_m is None else [float(v) for v in self.position_world_m]
            ),
            "orientation_world_wxyz": self.orientation_world_wxyz,
            "reprojection_px": self.reprojection_px,
            "inliers": self.inliers,
            "ambiguity_ratio": self.ambiguity_ratio,
            "physical_ambiguity_checked": bool(self.physical_ambiguity_checked),
        }


class PositionEstimator:
    def __init__(
        self,
        intrinsics: Intrinsics,
        geometry: MarkerGeometry,
        config: Optional[EstimatorConfig] = None,
    ) -> None:
        if config is None:
            config = EstimatorConfig()
        if config.require_calibrated and not intrinsics.calibrated:
            raise EstimatorError(
                "intrinsics are not calibrated; a metric position cannot be claimed. "
                "Calibrate a chessboard first or set require_calibrated=False for diagnostics."
            )
        if (
            config.max_calibration_rms_px is not None
            and intrinsics.calibrated
            and intrinsics.rms_reprojection_px is not None
            and not (
                np.isfinite(intrinsics.rms_reprojection_px)
                and intrinsics.rms_reprojection_px <= config.max_calibration_rms_px
            )
        ):
            raise EstimatorError(
                f"calibration RMS {intrinsics.rms_reprojection_px:.4f}px exceeds "
                f"the {config.max_calibration_rms_px:.4f}px limit; refusing to "
                "emit a metric pose from this calibration (recalibrate or raise "
                "--max-calibration-rms-px)"
            )
        self.intrinsics = intrinsics
        self.geometry = geometry
        self.config = config
        try:
            self.camera_matrix = validate_camera_matrix(
                intrinsics.camera_matrix, intrinsics.image_size
            )
        except GeometryError as exc:
            raise EstimatorError(f"invalid camera_matrix: {exc}") from exc
        distortion = np.asarray(intrinsics.distortion_coeffs, dtype=np.float64)
        if distortion.size and not np.all(np.isfinite(distortion)):
            raise EstimatorError("distortion_coeffs contains non-finite values")
        self.distortion = distortion
        self.object_points = np.asarray(geometry.object_points_m, dtype=np.float64)

    def _invalid(self, state: str, reason: str) -> PositionResult:
        return PositionResult(
            valid=False, state=state, reason=reason, source=self.geometry.source
        )

    def estimate(self, image_points, object_points=None) -> PositionResult:
        observed = np.asarray(image_points, dtype=np.float64)
        if observed.ndim != 2 or observed.shape[1] != 2:
            return self._invalid("error", "image_points must be an Nx2 array")
        if not np.all(np.isfinite(observed)):
            return self._invalid("error", "image_points contains non-finite values")
        points = self.object_points
        if object_points is not None:
            candidate = np.asarray(object_points, dtype=np.float64)
            if candidate.shape != points.shape:
                return self._invalid("error", "object_points shape does not match geometry")
            points = candidate
        if observed.shape[0] != points.shape[0]:
            return self._invalid(
                "error",
                f"{observed.shape[0]} image points do not match {points.shape[0]} marker points",
            )
        try:
            corrected = undistort_points(self.camera_matrix, self.distortion, observed)
        except GeometryError as exc:
            return self._invalid("error", f"undistortion failed: {exc}")

        planar = bool(np.max(np.abs(points[:, 2])) <= 1e-6)
        try:
            if planar:
                return self._estimate_planar(corrected, points)
            return self._estimate_nonplanar(corrected, points)
        except InsufficientCorrespondences as exc:
            return self._invalid("error", str(exc))
        except GeometryError as exc:
            return self._invalid("error", str(exc))
        except np.linalg.LinAlgError as exc:
            return self._invalid("error", f"linear algebra failure: {exc}")

    def _estimate_planar(self, observed: np.ndarray, points: np.ndarray) -> PositionResult:
        candidates, inliers = planar_pose(
            points,
            observed,
            self.camera_matrix,
            threshold_px=self.config.ransac_threshold_px,
            min_inliers=4,
            iterations=self.config.ransac_iterations,
            seed=self.config.ransac_seed,
        )
        inlier_count = int(np.count_nonzero(inliers))
        if inlier_count < 4:
            return self._invalid("lost", f"only {inlier_count} inlier marker points")
        # Physical front/front check when OpenCV IPPE is available. The
        # homography +/- pair alone cannot see this ambiguity (its mirror
        # branch sits behind the camera, so ambiguity_ratio stays 0).
        # A front/front ambiguity is only declared when BOTH branches sit
        # entirely in front of the lens: a behind-camera alternative is not
        # a competitor and must never invalidate a valid best pose.
        physical = ippe_branches(points[inliers], observed[inliers], self.camera_matrix)
        if physical is not None and len(physical) >= 2:
            best, other = physical[0], physical[1]
            if branches_are_duplicates(best, other):
                # Same hypothesis returned twice (e.g. the exact
                # fronto-parallel square): no second hypothesis to be
                # ambiguous with. Emit the best physical branch when it
                # passes the usual front/residual gates.
                if best.positive_depth_ratio < 1.0:
                    return PositionResult(
                        valid=False,
                        state="ambiguous",
                        reason="planar pose not entirely in front of the camera",
                        source=self.geometry.source,
                        reprojection_px=best.reprojection_px,
                        inliers=inlier_count,
                        ambiguity_ratio=0.0,
                        physical_ambiguity_checked=True,
                    )
                if best.reprojection_px > self.config.max_reprojection_px:
                    return PositionResult(
                        valid=False,
                        state="lost",
                        reason=(
                            f"reprojection {best.reprojection_px:.3f}px > "
                            f"{self.config.max_reprojection_px:.3f}px"
                        ),
                        source=self.geometry.source,
                        reprojection_px=best.reprojection_px,
                        inliers=inlier_count,
                        ambiguity_ratio=0.0,
                        physical_ambiguity_checked=True,
                    )
                return self._build(best, inlier_count, 0.0, True)
            ambiguity = reprojection_ambiguity_ratio(
                best.reprojection_px, other.reprojection_px
            )
            separation = rotation_separation_deg(best.rotation, other.rotation)
            if best.positive_depth_ratio < 1.0:
                return PositionResult(
                    valid=False,
                    state="ambiguous",
                    reason="planar pose not entirely in front of the camera",
                    source=self.geometry.source,
                    reprojection_px=best.reprojection_px,
                    inliers=inlier_count,
                    ambiguity_ratio=ambiguity,
                    physical_ambiguity_checked=True,
                )
            if other.positive_depth_ratio < 1.0:
                # Only one front hypothesis: gate the best pose on its own
                # residual instead of declaring an ambiguity the pixels do
                # not support.
                if best.reprojection_px > self.config.max_reprojection_px:
                    return PositionResult(
                        valid=False,
                        state="lost",
                        reason=(
                            f"reprojection {best.reprojection_px:.3f}px > "
                            f"{self.config.max_reprojection_px:.3f}px"
                        ),
                        source=self.geometry.source,
                        reprojection_px=best.reprojection_px,
                        inliers=inlier_count,
                        ambiguity_ratio=ambiguity,
                        physical_ambiguity_checked=True,
                    )
                return self._build(best, inlier_count, ambiguity, True)
            if ambiguity >= self.config.ambiguity_ratio_threshold:
                return PositionResult(
                    valid=False,
                    state="ambiguous",
                    reason=(
                        f"planar IPPE ambiguity: best {best.reprojection_px:.3f}px vs "
                        f"alternative {other.reprojection_px:.3f}px "
                        f"(ratio {ambiguity:.2f} >= "
                        f"{self.config.ambiguity_ratio_threshold:.2f}, "
                        f"poses {separation:.1f}deg apart)"
                    ),
                    source=self.geometry.source,
                    reprojection_px=best.reprojection_px,
                    inliers=inlier_count,
                    ambiguity_ratio=ambiguity,
                    physical_ambiguity_checked=True,
                )
            if best.reprojection_px > self.config.max_reprojection_px:
                return PositionResult(
                    valid=False,
                    state="lost",
                    reason=(
                        f"reprojection {best.reprojection_px:.3f}px > "
                        f"{self.config.max_reprojection_px:.3f}px"
                    ),
                    source=self.geometry.source,
                    reprojection_px=best.reprojection_px,
                    inliers=inlier_count,
                    ambiguity_ratio=ambiguity,
                    physical_ambiguity_checked=True,
                )
            return self._build(best, inlier_count, ambiguity, True)
        # Pure-NumPy fallback: the +/- homography pair only flips the sign,
        # so ambiguity_ratio here is a depth ratio, not a front/front score.
        # A valid result stays usable (no blanket disable of planar
        # tracking) but reports physical_ambiguity_checked=False so no one
        # reads ambiguity_ratio=0 as proof the physical ambiguity is gone.
        best = candidates[0]
        other = candidates[1] if len(candidates) > 1 else None
        ambiguity = float(other.positive_depth_ratio) if other else 0.0
        if best.positive_depth_ratio < 1.0:
            return PositionResult(
                valid=False,
                state="ambiguous",
                reason="planar pose not entirely in front of the camera",
                source=self.geometry.source,
                reprojection_px=best.reprojection_px,
                inliers=inlier_count,
                ambiguity_ratio=ambiguity,
                physical_ambiguity_checked=False,
            )
        if ambiguity >= self.config.ambiguity_ratio_threshold:
            return PositionResult(
                valid=False,
                state="ambiguous",
                reason=(
                    f"planar two-fold ambiguity: alternative depth ratio {ambiguity:.2f} "
                    f">= {self.config.ambiguity_ratio_threshold:.2f}"
                ),
                source=self.geometry.source,
                reprojection_px=best.reprojection_px,
                inliers=inlier_count,
                ambiguity_ratio=ambiguity,
                physical_ambiguity_checked=False,
            )
        if best.reprojection_px > self.config.max_reprojection_px:
            return PositionResult(
                valid=False,
                state="lost",
                reason=(
                    f"reprojection {best.reprojection_px:.3f}px > "
                    f"{self.config.max_reprojection_px:.3f}px"
                ),
                source=self.geometry.source,
                reprojection_px=best.reprojection_px,
                inliers=inlier_count,
                ambiguity_ratio=ambiguity,
                physical_ambiguity_checked=False,
            )
        return self._build(best, inlier_count, ambiguity, False)

    def _estimate_nonplanar(self, observed: np.ndarray, points: np.ndarray) -> PositionResult:
        candidate, inliers = ransac_pnp(
            points,
            observed,
            self.camera_matrix,
            threshold_px=self.config.ransac_threshold_px,
            min_inliers=min(6, points.shape[0]),
            iterations=self.config.ransac_iterations,
            seed=self.config.ransac_seed,
        )
        inlier_count = int(np.count_nonzero(inliers))
        if inlier_count < min(6, points.shape[0]):
            return self._invalid("lost", f"only {inlier_count} inlier marker points")
        angle = depth_conditioning(points)
        if angle < self.config.min_depth_conditioning:
            return PositionResult(
                valid=False,
                state="ambiguous",
                reason=(
                    f"weak depth conditioning: {angle:.4f} < "
                    f"{self.config.min_depth_conditioning:.4f}"
                ),
                source=self.geometry.source,
                reprojection_px=candidate.reprojection_px,
                inliers=inlier_count,
            )
        if candidate.reprojection_px > self.config.max_reprojection_px:
            return PositionResult(
                valid=False,
                state="lost",
                reason=(
                    f"reprojection {candidate.reprojection_px:.3f}px > "
                    f"{self.config.max_reprojection_px:.3f}px"
                ),
                source=self.geometry.source,
                reprojection_px=candidate.reprojection_px,
                inliers=inlier_count,
            )
        depths = depth_values(candidate.rotation, candidate.translation, points)
        if float(np.mean(depths > 0.0)) < 1.0:
            return PositionResult(
                valid=False,
                state="ambiguous",
                reason="non-planar pose not entirely in front of the camera",
                source=self.geometry.source,
                reprojection_px=candidate.reprojection_px,
                inliers=inlier_count,
            )
        return self._build(candidate, inlier_count, None)

    def _build(
        self,
        candidate,
        inlier_count: int,
        ambiguity: Optional[float],
        physical_ambiguity_checked: bool = False,
    ) -> PositionResult:
        world_position = optical_to_world(candidate.translation)
        world_rotation = optical_rotation_to_world(candidate.rotation)
        return PositionResult(
            valid=True,
            state="tracking",
            reason="ok",
            source=self.geometry.source,
            rotation_camera_from_body=candidate.rotation,
            position_camera_m=np.asarray(candidate.translation, dtype=np.float64),
            position_world_m=world_position,
            orientation_world_wxyz=quaternion_from_rotation(world_rotation),
            reprojection_px=float(candidate.reprojection_px),
            inliers=int(inlier_count),
            ambiguity_ratio=None if ambiguity is None else float(ambiguity),
            physical_ambiguity_checked=bool(physical_ambiguity_checked),
        )


class PositionFilter:
    """Time-based smoothing, deadzone and staleness for a position stream."""

    def __init__(
        self,
        smoothing_time_s: float = 0.05,
        deadzone_m: float = 0.0,
        stale_timeout_s: float = 0.5,
    ) -> None:
        if smoothing_time_s < 0:
            raise EstimatorError("smoothing_time_s must be >= 0")
        if deadzone_m < 0:
            raise EstimatorError("deadzone_m must be >= 0")
        if stale_timeout_s <= 0:
            raise EstimatorError("stale_timeout_s must be > 0")
        self.smoothing_time_s = float(smoothing_time_s)
        self.deadzone_m = float(deadzone_m)
        self.stale_timeout_s = float(stale_timeout_s)
        self._value: Optional[np.ndarray] = None
        self._last_time: Optional[float] = None

    @property
    def value(self) -> Optional[np.ndarray]:
        return None if self._value is None else self._value.copy()

    def reset(self) -> None:
        self._value = None
        self._last_time = None

    def update(self, position, now: float) -> np.ndarray:
        target = np.asarray(position, dtype=np.float64).reshape(3)
        if not np.all(np.isfinite(target)):
            raise EstimatorError("position must be finite")
        if self._value is None or self._last_time is None:
            self._value = target.copy()
        else:
            dt = max(float(now) - self._last_time, 0.0)
            if self.smoothing_time_s <= 0.0:
                alpha = 1.0
            else:
                alpha = 1.0 - math.exp(-dt / self.smoothing_time_s)
            candidate = self._value + alpha * (target - self._value)
            if float(np.linalg.norm(candidate - self._value)) >= self.deadzone_m:
                self._value = candidate
        self._last_time = float(now)
        return self._value.copy()

    def mark_missing(self, now: float) -> bool:
        """Return True while a previously seen position is still fresh."""
        if self._last_time is None:
            return False
        return (float(now) - self._last_time) <= self.stale_timeout_s
