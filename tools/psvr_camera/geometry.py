"""Pure-NumPy camera geometry for metric position estimation.

No OpenCV: every routine here is deterministic linear algebra so it can be unit
tested with synthetic projections on a machine without OpenCV installed. The
OpenCV adapter (``camera_backend``/``calibration``) only supplies detections;
the pose is always solved and rejected here.

Planar markers (ArUco/chessboard) are solved from the homography and both
two-fold candidates are returned so the caller can apply the positive-depth and
reprojection rejection. Non-planar correspondences (measured blob/LED rigs) use
a DLT PnP with RANSAC.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence

import numpy as np


class GeometryError(ValueError):
    pass


class InsufficientCorrespondences(GeometryError):
    pass


_EPS = 1e-12


def _points(value, columns: int, minimum: int, name: str) -> np.ndarray:
    array = np.asarray(value, dtype=np.float64)
    if array.ndim != 2 or array.shape[1] != columns:
        raise GeometryError(f"{name} must be an Nx{columns} array")
    if array.shape[0] < minimum:
        raise InsufficientCorrespondences(
            f"{name} needs at least {minimum} points, got {array.shape[0]}"
        )
    if not np.all(np.isfinite(array)):
        raise GeometryError(f"{name} contains non-finite values")
    return array


def skew(vector: Sequence[float]) -> np.ndarray:
    x, y, z = (float(v) for v in vector)
    return np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]], dtype=np.float64)


def rodrigues(rotation_vector: Sequence[float]) -> np.ndarray:
    rvec = np.asarray(rotation_vector, dtype=np.float64).reshape(3)
    theta = float(np.linalg.norm(rvec))
    if theta < _EPS:
        return np.eye(3)
    k = skew(rvec / theta)
    return np.eye(3) + math.sin(theta) * k + (1.0 - math.cos(theta)) * (k @ k)


def rotation_to_rodrigues(rotation: np.ndarray) -> np.ndarray:
    matrix = np.asarray(rotation, dtype=np.float64).reshape(3, 3)
    value = (float(np.trace(matrix)) - 1.0) / 2.0
    theta = math.acos(max(-1.0, min(1.0, value)))
    if theta < 1e-8:
        return np.zeros(3)
    if theta > math.pi - 1e-2:
        # Stable branch near pi: the antisymmetric part scales with
        # sin(theta), so recovering the axis from it amplifies trace noise by
        # ~1/sin^2. The symmetric part carries the axis exactly instead:
        # R_ii = cos + (1 - cos) a_i^2, S_ij = (1 - cos) a_i a_j.
        # This identity holds for any angle; the branch is taken only near pi
        # where the standard formula degrades (it stays exact near zero).
        cos_theta = math.cos(theta)
        scale = 1.0 - cos_theta
        squares = (
            np.array(
                [
                    matrix[0, 0] - cos_theta,
                    matrix[1, 1] - cos_theta,
                    matrix[2, 2] - cos_theta,
                ],
                dtype=np.float64,
            )
            / scale
        )
        order = int(np.argmax(squares))
        axis = np.zeros(3, dtype=np.float64)
        axis[order] = math.sqrt(max(0.0, squares[order]))
        if axis[order] > 0.0:
            symmetric = (matrix + matrix.T) / 2.0
            for j in range(3):
                if j != order:
                    # Linear recovery: no sqrt noise amplification for tiny
                    # components, exact signs, no copysign needed.
                    axis[j] = symmetric[order, j] / (scale * axis[order])
        # The symmetric part fixes the axis only up to its sign. Resolve it
        # against the antisymmetric part, which carries sin(theta): for an
        # input past pi this flips the axis so the round-trip keeps the
        # equivalent (pi - eps) angle instead of mirroring the rotation.
        axial = np.array(
            [
                matrix[2, 1] - matrix[1, 2],
                matrix[0, 2] - matrix[2, 0],
                matrix[1, 0] - matrix[0, 1],
            ],
            dtype=np.float64,
        )
        if float(np.dot(axis, axial)) < 0.0:
            axis = -axis
        return axis * theta
    axis = np.array(
        [
            matrix[2, 1] - matrix[1, 2],
            matrix[0, 2] - matrix[2, 0],
            matrix[1, 0] - matrix[0, 1],
        ],
        dtype=np.float64,
    )
    return axis * (theta / (2.0 * math.sin(theta)))


def orthonormalize_rotation(matrix: np.ndarray) -> np.ndarray:
    """Nearest proper rotation to ``matrix`` via SVD (Kabsch)."""
    u, _, vt = np.linalg.svd(np.asarray(matrix, dtype=np.float64).reshape(3, 3))
    rotation = u @ vt
    if np.linalg.det(rotation) < 0.0:
        u = u.copy()
        u[:, -1] *= -1.0
        rotation = u @ vt
    return rotation


def project_points(
    camera_matrix: np.ndarray,
    rotation: np.ndarray,
    translation: Sequence[float],
    object_points: Sequence[Sequence[float]],
) -> np.ndarray:
    points = _points(object_points, 3, 1, "object_points")
    camera = np.asarray(camera_matrix, dtype=np.float64).reshape(3, 3)
    translation = np.asarray(translation, dtype=np.float64).reshape(3)
    camera_points = (rotation @ points.T).T + translation
    depths = camera_points[:, 2]
    if np.any(np.abs(depths) < _EPS):
        raise GeometryError("a point projects onto the camera plane (z=0)")
    homogeneous = (camera @ camera_points.T).T
    return homogeneous[:, :2] / homogeneous[:, 2:3]


def reprojection_errors(
    camera_matrix: np.ndarray,
    rotation: np.ndarray,
    translation: Sequence[float],
    object_points: Sequence[Sequence[float]],
    image_points: Sequence[Sequence[float]],
) -> np.ndarray:
    projected = project_points(camera_matrix, rotation, translation, object_points)
    observed = _points(image_points, 2, 1, "image_points")
    if projected.shape[0] != observed.shape[0]:
        raise GeometryError("object/image point counts differ")
    return np.linalg.norm(projected - observed, axis=1)


def rms_reprojection(
    camera_matrix: np.ndarray,
    rotation: np.ndarray,
    translation: Sequence[float],
    object_points: Sequence[Sequence[float]],
    image_points: Sequence[Sequence[float]],
) -> float:
    errors = reprojection_errors(
        camera_matrix, rotation, translation, object_points, image_points
    )
    return float(math.sqrt(float(np.mean(errors * errors))))


def undistort_points(
    camera_matrix: np.ndarray,
    distortion: Sequence[float],
    image_points: Sequence[Sequence[float]],
    iterations: int = 10,
) -> np.ndarray:
    """Iterative plumb-bob undistortion (k1,k2,p1,p2,k3) in pure NumPy."""
    points = _points(image_points, 2, 1, "image_points")
    camera = np.asarray(camera_matrix, dtype=np.float64).reshape(3, 3)
    coeffs = np.asarray(distortion, dtype=np.float64).reshape(-1)
    if coeffs.size == 0 or float(np.max(np.abs(coeffs))) < 1e-15:
        return points.copy()
    k1, k2, p1, p2 = (float(coeffs[i]) if coeffs.size > i else 0.0 for i in range(4))
    k3 = float(coeffs[4]) if coeffs.size > 4 else 0.0
    homogeneous = np.column_stack([points, np.ones(points.shape[0])])
    normalized = (np.linalg.inv(camera) @ homogeneous.T).T
    x = normalized[:, 0].copy()
    y = normalized[:, 1].copy()
    xd, yd = x.copy(), y.copy()
    for _ in range(int(iterations)):
        r2 = x * x + y * y
        radial = 1.0 + k1 * r2 + k2 * r2 * r2 + k3 * r2 * r2 * r2
        if np.any(np.abs(radial) < _EPS):
            raise GeometryError("distortion model is singular")
        tangential_x = 2.0 * p1 * x * y + p2 * (r2 + 2.0 * x * x)
        tangential_y = p1 * (r2 + 2.0 * y * y) + 2.0 * p2 * x * y
        x = (xd - tangential_x) / radial
        y = (yd - tangential_y) / radial
    corrected = np.column_stack([x, y, np.ones_like(x)])
    pixels = (camera @ corrected.T).T
    return pixels[:, :2] / pixels[:, 2:3]


def homography_dlt(
    source: Sequence[Sequence[float]], target: Sequence[Sequence[float]]
) -> np.ndarray:
    """Homography mapping ``source`` points to ``target`` points (H @ src = tgt)."""
    src = _points(source, 2, 4, "source")
    dst = _points(target, 2, 4, "target")
    if src.shape[0] != dst.shape[0]:
        raise GeometryError("source/target point counts differ")
    rows = []
    for (x, y), (u, v) in zip(src, dst):
        rows.append([-x, -y, -1.0, 0.0, 0.0, 0.0, u * x, u * y, u])
        rows.append([0.0, 0.0, 0.0, -x, -y, -1.0, v * x, v * y, v])
    matrix = np.asarray(rows, dtype=np.float64)
    singular = np.linalg.svd(matrix, compute_uv=False)
    # The null vector is the homography. If the second-smallest singular value
    # collapses, the null space is not one-dimensional (collinear or coincident
    # points) and the "solution" is arbitrary: fail closed rather than return a
    # degenerate pose.
    if singular.size < 2 or singular[-2] <= singular[0] * 1e-9:
        raise GeometryError("degenerate homography (rank-deficient correspondences)")
    _, _, vt = np.linalg.svd(matrix)
    homography = vt[-1].reshape(3, 3)
    if abs(homography[2, 2]) > _EPS:
        homography = homography / homography[2, 2]
    if not np.all(np.isfinite(homography)):
        raise GeometryError("degenerate homography")
    return homography


@dataclass
class PoseCandidate:
    rotation: np.ndarray
    translation: np.ndarray
    reprojection_px: float
    positive_depth_ratio: float


def pose_candidates_from_homography(
    homography: np.ndarray,
    camera_matrix: np.ndarray,
    object_points: Sequence[Sequence[float]],
    image_points: Sequence[Sequence[float]],
) -> list:
    """Return the two planar two-fold PnP candidates, best first.

    The candidates project identically in the noise-free case; the caller must
    apply positive-depth and reprojection rejection and treat near-ties as
    ambiguous instead of emitting a metric position.
    """
    camera = np.asarray(camera_matrix, dtype=np.float64).reshape(3, 3)
    inverse = np.linalg.inv(camera)
    normalized = inverse @ np.asarray(homography, dtype=np.float64).reshape(3, 3)
    h1, h2, h3 = normalized[:, 0], normalized[:, 1], normalized[:, 2]
    n1, n2 = float(np.linalg.norm(h1)), float(np.linalg.norm(h2))
    if n1 < _EPS or n2 < _EPS:
        raise GeometryError("degenerate homography columns")
    scale = 2.0 / (n1 + n2)
    r1, r2 = scale * h1, scale * h2
    third = np.cross(r1, r2)
    candidates = []
    for sign in (1.0, -1.0):
        rotation = orthonormalize_rotation(
            np.column_stack([sign * r1, sign * r2, third])
        )
        translation = sign * scale * h3
        try:
            errors = reprojection_errors(
                camera, rotation, translation, object_points, image_points
            )
        except GeometryError:
            continue
        depths = (rotation @ _points(object_points, 3, 1, "object_points").T).T + translation
        positive = float(np.mean(depths[:, 2] > 0.0))
        candidates.append(
            PoseCandidate(
                rotation=rotation,
                translation=translation,
                reprojection_px=float(math.sqrt(float(np.mean(errors * errors)))),
                positive_depth_ratio=positive,
            )
        )
    if not candidates:
        raise GeometryError("no valid planar pose candidate")
    candidates.sort(key=lambda c: (-c.positive_depth_ratio, c.reprojection_px))
    return candidates


def dlt_pnp(
    object_points: Sequence[Sequence[float]],
    image_points: Sequence[Sequence[float]],
    camera_matrix: np.ndarray,
) -> PoseCandidate:
    """General DLT PnP for non-planar 3D correspondences."""
    objects = _points(object_points, 3, 6, "object_points")
    images = _points(image_points, 2, 6, "image_points")
    if objects.shape[0] != images.shape[0]:
        raise GeometryError("object/image point counts differ")
    camera = np.asarray(camera_matrix, dtype=np.float64).reshape(3, 3)
    inverse = np.linalg.inv(camera)
    homogeneous = np.column_stack([images, np.ones(images.shape[0])])
    normalized = (inverse @ homogeneous.T).T
    normalized = normalized[:, :2] / normalized[:, 2:3]
    rows = []
    for (x, y, z), (u, v) in zip(objects, normalized):
        rows.append([x, y, z, 1.0, 0.0, 0.0, 0.0, 0.0, -u * x, -u * y, -u * z, -u])
        rows.append([0.0, 0.0, 0.0, 0.0, x, y, z, 1.0, -v * x, -v * y, -v * z, -v])
    _, _, vt = np.linalg.svd(np.asarray(rows, dtype=np.float64))
    matrix = vt[-1].reshape(3, 4)
    linear = matrix[:, :3]
    determinant = float(np.linalg.det(linear))
    if abs(determinant) < _EPS:
        raise GeometryError("degenerate PnP linear part")
    scale = math.copysign(abs(determinant) ** (1.0 / 3.0), determinant)
    rotation = orthonormalize_rotation(linear / scale)
    translation = matrix[:, 3] / scale
    errors = reprojection_errors(camera, rotation, translation, objects, images)
    depths = (rotation @ objects.T).T + translation
    return PoseCandidate(
        rotation=rotation,
        translation=translation,
        reprojection_px=float(math.sqrt(float(np.mean(errors * errors)))),
        positive_depth_ratio=float(np.mean(depths[:, 2] > 0.0)),
    )


def _consensus(
    object_points,
    image_points,
    camera_matrix,
    solver,
    threshold_px,
    min_inliers,
    seed,
    iterations,
    sample_size,
):
    objects = _points(object_points, 3, 4, "object_points")
    images = _points(image_points, 2, 4, "image_points")
    count = objects.shape[0]
    if min_inliers > count:
        raise InsufficientCorrespondences(
            f"min_inliers {min_inliers} exceeds {count} correspondences"
        )
    if count < sample_size:
        raise InsufficientCorrespondences(
            f"need at least {sample_size} correspondences for a minimal solve, got {count}"
        )
    rng = np.random.default_rng(seed)
    best = None
    attempts = max(int(iterations), 1)
    for _ in range(attempts):
        sample = rng.choice(count, size=sample_size, replace=False)
        try:
            candidate = solver(objects[sample], images[sample])
        except GeometryError:
            continue
        errors = reprojection_errors(
            camera_matrix,
            candidate.rotation,
            candidate.translation,
            objects,
            images,
        )
        inliers = errors < threshold_px
        score = int(np.count_nonzero(inliers))
        if best is None or score > best[0]:
            best = (score, inliers, candidate)
    if best is None or best[0] < min_inliers:
        raise GeometryError("no consensus pose above the inlier floor")
    score, inliers, _ = best
    # Refine on all inliers with the solver.
    refined = solver(objects[inliers], images[inliers])
    errors = reprojection_errors(
        camera_matrix, refined.rotation, refined.translation, objects, images
    )
    return PoseCandidate(
        rotation=refined.rotation,
        translation=refined.translation,
        reprojection_px=float(
            math.sqrt(float(np.mean(errors[inliers] * errors[inliers])))
        ),
        positive_depth_ratio=refined.positive_depth_ratio,
    ), inliers


def ransac_pnp(
    object_points,
    image_points,
    camera_matrix,
    threshold_px: float = 2.0,
    min_inliers: int = 6,
    iterations: int = 200,
    seed: int = 12345,
):
    """DLT PnP with RANSAC outlier rejection. Returns (candidate, inlier_mask)."""
    if threshold_px <= 0:
        raise GeometryError("threshold_px must be positive")
    return _consensus(
        object_points,
        image_points,
        camera_matrix,
        lambda o, i: dlt_pnp(o, i, camera_matrix),
        threshold_px,
        min_inliers,
        seed,
        iterations,
        sample_size=6,
    )


def planar_pose(
    object_points,
    image_points,
    camera_matrix,
    threshold_px: float = 2.0,
    min_inliers: int = 4,
    iterations: int = 200,
    seed: int = 12345,
):
    """RANSAC homography + planar decomposition. Returns (candidates, inliers).

    ``candidates`` is the two-fold list for the consensus homography, best
    first; the caller applies the ambiguity policy.
    """
    objects = _points(object_points, 3, 4, "object_points")
    images = _points(image_points, 2, 4, "image_points")
    if np.max(np.abs(objects[:, 2])) > 1e-9:
        raise GeometryError("planar_pose requires object points on z=0")
    if threshold_px <= 0:
        raise GeometryError("threshold_px must be positive")

    def homography_solver(o, i):
        return homography_dlt(o[:, :2], i)

    best = None
    rng = np.random.default_rng(seed)
    count = objects.shape[0]
    for _ in range(max(int(iterations), 1)):
        sample = rng.choice(count, size=4, replace=False)
        try:
            homography = homography_solver(objects[sample], images[sample])
        except GeometryError:
            continue
        # Homography maps the plane model (X, Y, 1) to pixels.
        model = np.column_stack([objects[:, 0], objects[:, 1], np.ones(count)])
        mapped = (homography @ model.T).T
        denominator = mapped[:, 2:3]
        with np.errstate(divide="ignore", invalid="ignore"):
            mapped = mapped[:, :2] / denominator
        finite = np.all(np.isfinite(mapped), axis=1)
        errors = np.where(
            finite, np.linalg.norm(mapped - images, axis=1), np.inf
        )
        inliers = errors < threshold_px
        score = int(np.count_nonzero(inliers))
        if best is None or score > best[0]:
            best = (score, inliers)
    if best is None or best[0] < min_inliers:
        raise GeometryError("no consensus homography above the inlier floor")
    _, inliers = best
    homography = homography_dlt(objects[inliers][:, :2], images[inliers])
    candidates = pose_candidates_from_homography(
        homography, camera_matrix, objects[inliers], images[inliers]
    )
    return candidates, inliers


def depth_conditioning(points) -> float:
    """Smallest/largest singular value of the centred object points.

    Near zero means the correspondences are (nearly) coplanar, so the PnP depth
    is weakly conditioned and the pose should not be trusted; a volumetric rig
    has a healthy ratio. This is a property of the measured geometry alone.
    """
    objects = _points(points, 3, 3, "points")
    centered = objects - objects.mean(axis=0)
    singular = np.linalg.svd(centered, compute_uv=False)
    largest = float(singular[0]) if singular.size else 0.0
    if largest < _EPS:
        return 0.0
    return float(singular[-1] / largest)


def look_at_rotation(
    eye: Sequence[float], target: Sequence[float], up: Sequence[float] = (0.0, 1.0, 0.0)
) -> np.ndarray:
    """Rotation whose columns are the camera axes (x right, y down, z forward)."""
    eye = np.asarray(eye, dtype=np.float64).reshape(3)
    target = np.asarray(target, dtype=np.float64).reshape(3)
    forward = target - eye
    norm = float(np.linalg.norm(forward))
    if norm < _EPS:
        raise GeometryError("eye and target coincide")
    forward = forward / norm
    up = np.asarray(up, dtype=np.float64).reshape(3)
    right = np.cross(up, forward)
    right_norm = float(np.linalg.norm(right))
    if right_norm < _EPS:
        raise GeometryError("up vector is parallel to the view direction")
    right = right / right_norm
    down = np.cross(forward, right)
    return np.column_stack([right, down, forward])


def depth_values(rotation: np.ndarray, translation: Sequence[float], points) -> np.ndarray:
    objects = _points(points, 3, 1, "points")
    return ((rotation @ objects.T).T + np.asarray(translation, dtype=np.float64))[:, 2]
