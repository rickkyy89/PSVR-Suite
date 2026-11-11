"""Physical two-branch planar solver with an honest pure-NumPy fallback.

The homography ``+/-`` decomposition in :mod:`psvr_camera.geometry` only flips
the sign of the same solution (one branch lands behind the camera). It cannot
produce the real perspective ambiguity where *both* planar poses sit in front
of the lens. This module adds that missing branch:

* :func:`ippe_branches` -- two physically distinct front candidates from
  OpenCV's generic ``SOLVEPNP_IPPE``. The solver always uses the generic
  flag, never ``SOLVEPNP_IPPE_SQUARE``: the square variant is only valid
  for centred, correctly ordered squares, and misusing it on a generic
  four-point quad (trapezoid, blob order, shifted centre) returns a wrong
  second hypothesis. OpenCV is imported lazily; when it is missing, the
  build lacks the flag, or the solve fails, the function returns ``None``
  so the caller keeps the existing pure-NumPy diagnostics.
* Skew handling: OpenCV's ``solvePnPGeneric`` ignores ``K[0, 1]``, so
  pixels are normalised here with the full ``K`` inverse and IPPE runs on
  normalised rays against an identity camera. Reprojection is then scored
  back in pixels with the full ``K`` (skew included). A skewed ``K`` is
  therefore supported correctly instead of being silently mis-solved.
* :func:`reprojection_ambiguity_ratio` -- ``best_rms / other_rms`` in 0..1.
  Near 1 means the two front poses explain the pixels almost equally well
  (ambiguous); near 0 means the best pose clearly wins. The caller compares
  it against the existing ``ambiguity_ratio_threshold`` (default 0.5), and
  only when *both* branches sit entirely in front of the lens.
* :func:`branches_are_duplicates` -- pure numeric dedup: two branches are
  the same hypothesis returned twice (e.g. the exact fronto-parallel
  square) only when rotation *and* translation agree to ``1e-4`` deg /
  ``1e-9`` m. This is not a perceptual threshold, so it cannot mask a real
  ambiguity (genuine second poses differ by degrees and millimetres).

The pure-NumPy fallback can still reject behind-camera and high-residual
poses, but it **cannot** rule out the physical front/front ambiguity. Callers
must surface that via ``physical_ambiguity_checked`` (``True`` only when an
actual two-branch IPPE comparison ran) instead of reading
``ambiguity_ratio=0`` as proof of uniqueness.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

import numpy as np

from .geometry import GeometryError, PoseCandidate, rms_reprojection

_EPS = 1e-12
_RMS_EPS = 1e-12
#: Numeric-dedup tolerances: two IPPE branches are the *same* solution
#: returned twice only when both hold. Genuine alternative poses differ by
#: degrees and fractions of a millimetre (fixture: ~35 deg, ~0.7 mm), so
#: these tight tolerances never collapse a real ambiguity.
DEDUP_ROTATION_DEG = 1e-4
DEDUP_TRANSLATION_M = 1e-9


def validate_camera_matrix(matrix, image_size=None) -> np.ndarray:
    """Check a pinhole ``K`` and return it as a (3, 3) float array.

    Requires finite entries, positive focals, ``K[1, 0] == K[2, 0] ==
    K[2, 1] == 0`` with ``K[2, 2] == 1`` (upper-triangular pinhole), and a
    non-singular matrix. A non-zero skew ``K[0, 1]`` is accepted: IPPE runs
    on rays normalised with the full ``K`` inverse (see :func:`ippe_branches`).
    When ``image_size`` is a valid ``(w, h)`` pair the principal point must
    also fall inside the image. Raises :class:`GeometryError` on any
    violation.
    """
    try:
        camera = np.asarray(matrix, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise GeometryError(f"camera_matrix must be a 3x3 matrix: {exc}") from exc
    if camera.shape != (3, 3):
        raise GeometryError("camera_matrix must be a 3x3 matrix")
    if not np.all(np.isfinite(camera)):
        raise GeometryError("camera_matrix contains non-finite values")
    if not (camera[0, 0] > 0 and camera[1, 1] > 0):
        raise GeometryError("focal lengths must be positive")
    if abs(float(camera[1, 0])) > 1e-9:
        raise GeometryError("camera_matrix must be upper-triangular (K[1,0]==0)")
    if abs(float(camera[2, 0])) > 1e-12 or abs(float(camera[2, 1])) > 1e-12:
        raise GeometryError("camera_matrix must have K[2,0]==K[2,1]==0")
    if abs(float(camera[2, 2]) - 1.0) > 1e-9:
        raise GeometryError("camera_matrix must have K[2,2]==1")
    try:
        determinant = float(np.linalg.det(camera))
    except np.linalg.LinAlgError as exc:
        raise GeometryError(f"camera_matrix is singular: {exc}") from exc
    if not np.isfinite(determinant) or abs(determinant) < 1e-18:
        raise GeometryError("camera_matrix is singular")
    if image_size is not None:
        try:
            width, height = int(image_size[0]), int(image_size[1])
        except (TypeError, ValueError, IndexError):
            width, height = 0, 0
        if width > 0 and height > 0:
            cx, cy = float(camera[0, 2]), float(camera[1, 2])
            if not (0.0 < cx < float(width) and 0.0 < cy < float(height)):
                raise GeometryError("principal point outside the image")
    return camera


def rotation_separation_deg(first: np.ndarray, second: np.ndarray) -> float:
    """Angle in degrees between two rotation matrices."""
    first = np.asarray(first, dtype=np.float64).reshape(3, 3)
    second = np.asarray(second, dtype=np.float64).reshape(3, 3)
    cos_angle = (float(np.trace(first.T @ second)) - 1.0) / 2.0
    cos_angle = max(-1.0, min(1.0, cos_angle))
    return float(math.degrees(math.acos(cos_angle)))


def branches_are_duplicates(first: PoseCandidate, second: PoseCandidate) -> bool:
    """True only when two branches are numerically the same hypothesis.

    Requires *both* the rotation separation (``< 1e-4`` deg) and the
    translation distance (``< 1e-9`` m) to be at solver-noise level. A real
    alternative pose clears at least one of them by orders of magnitude,
    so this dedup cannot hide a genuine ambiguity.
    """
    if rotation_separation_deg(first.rotation, second.rotation) >= DEDUP_ROTATION_DEG:
        return False
    distance = float(
        np.linalg.norm(
            np.asarray(first.translation, dtype=np.float64).reshape(3)
            - np.asarray(second.translation, dtype=np.float64).reshape(3)
        )
    )
    if not np.isfinite(distance):
        return False
    return distance < DEDUP_TRANSLATION_M


def reprojection_ambiguity_ratio(best_rms: float, other_rms: float) -> float:
    """Score the front/front ambiguity in 0..1 (high means ambiguous).

    ``best_rms / other_rms`` with a guard for a near-zero denominator: two
    equally perfect fits score 1 (genuine tie), a clear winner scores near 0.
    Only meaningful when both branches sit entirely in front of the lens;
    the caller must enforce that (a behind-camera branch is not a
    competitor and must never invalidate a valid best pose).
    """
    best = float(best_rms)
    other = float(other_rms)
    if not (np.isfinite(best) and np.isfinite(other)) or best < 0 or other < 0:
        raise GeometryError("reprojection RMS values must be finite and >= 0")
    if other <= _RMS_EPS:
        return 1.0 if best <= _RMS_EPS else 0.0
    ratio = best / other
    if not np.isfinite(ratio):
        raise GeometryError("cannot score the planar ambiguity")
    return float(max(0.0, min(1.0, ratio)))


def _as_planar_points(value, name: str, minimum: int) -> np.ndarray:
    array = np.asarray(value, dtype=np.float64)
    if array.ndim != 2 or array.shape[1] != 3:
        raise GeometryError(f"{name} must be an Nx3 array")
    if array.shape[0] < minimum:
        raise GeometryError(f"{name} needs at least {minimum} points")
    if not np.all(np.isfinite(array)):
        raise GeometryError(f"{name} contains non-finite values")
    return array


def ippe_branches(
    object_points: Sequence[Sequence[float]],
    image_points: Sequence[Sequence[float]],
    camera_matrix: np.ndarray,
    cv2_module=None,
) -> Optional[list]:
    """Return the two physical IPPE front candidates, best first.

    Always uses the generic ``SOLVEPNP_IPPE`` flag on rays normalised with
    the full ``K`` inverse against an identity camera, so skewed ``K`` and
    generic (non-square, arbitrarily ordered) quads are solved correctly.
    Branches with non-finite ``rvec``/``tvec`` output are dropped. Returns
    ``None`` when OpenCV is unavailable, the build lacks the flag, or the
    solve itself fails -- the caller must then use the honest pure-NumPy
    fallback (which cannot check the front/front ambiguity) and report
    ``physical_ambiguity_checked=False``.
    """
    objects = _as_planar_points(object_points, "object_points", 4)
    images = np.asarray(image_points, dtype=np.float64)
    if images.ndim != 2 or images.shape[1] != 2 or images.shape[0] != objects.shape[0]:
        raise GeometryError("object/image point counts differ")
    if not np.all(np.isfinite(images)):
        raise GeometryError("image_points contains non-finite values")
    if float(np.max(np.abs(objects[:, 2]))) > 1e-6:
        raise GeometryError("ippe_branches requires object points on z=0")
    camera = validate_camera_matrix(camera_matrix)

    from .camera_backend import OpenCvUnavailable, import_cv2

    try:
        cv2 = import_cv2(cv2_module)
    except OpenCvUnavailable:
        return None
    flag = getattr(cv2, "SOLVEPNP_IPPE", None)
    if flag is None:
        return None
    # Normalise with the full K (skew included): OpenCV's PnP ignores
    # K[0, 1], so passing skewed pixels + K directly mis-solves. Identity
    # camera on normalised rays is exactly equivalent for zero skew and
    # correct for non-zero skew; scoring stays in pixels with the full K.
    try:
        inverse = np.linalg.inv(camera)
    except np.linalg.LinAlgError:
        return None
    homogeneous = np.column_stack([images, np.ones(images.shape[0])])
    rays = (inverse @ homogeneous.T).T
    depths = rays[:, 2:3]
    if not np.all(np.isfinite(rays)) or np.any(np.abs(depths) < _EPS):
        return None
    normalised = rays[:, :2] / depths
    if not np.all(np.isfinite(normalised)):
        return None
    try:
        result = cv2.solvePnPGeneric(
            np.ascontiguousarray(objects, dtype=np.float64),
            np.ascontiguousarray(normalised, dtype=np.float64),
            np.ascontiguousarray(np.eye(3, dtype=np.float64)),
            np.zeros(5, dtype=np.float64),
            flags=int(flag),
        )
    except Exception:
        return None
    try:
        ok, rvecs, tvecs, _ = result
    except (TypeError, ValueError):
        return None
    if not ok or rvecs is None or tvecs is None or len(rvecs) == 0:
        return None
    branches: list = []
    for rvec, tvec in zip(list(rvecs), list(tvecs)):
        rvec_array = np.asarray(rvec, dtype=np.float64)
        tvec_array = np.asarray(tvec, dtype=np.float64)
        if rvec_array.size != 3 or tvec_array.size != 3:
            continue
        if not (np.all(np.isfinite(rvec_array)) and np.all(np.isfinite(tvec_array))):
            continue
        try:
            rotation, _ = cv2.Rodrigues(rvec_array.reshape(3, 1))
        except Exception:
            continue
        rotation = np.asarray(rotation, dtype=np.float64).reshape(3, 3)
        if not np.all(np.isfinite(rotation)):
            continue
        translation = np.asarray(tvec_array, dtype=np.float64).reshape(3)
        if not np.all(np.isfinite(translation)):
            continue
        try:
            rms = rms_reprojection(camera, rotation, translation, objects, images)
        except GeometryError:
            continue
        if not np.isfinite(rms):
            continue
        depths = ((rotation @ objects.T).T + translation)[:, 2]
        if not np.all(np.isfinite(depths)):
            continue
        branches.append(
            PoseCandidate(
                rotation=rotation,
                translation=np.asarray(translation, dtype=np.float64),
                reprojection_px=float(rms),
                positive_depth_ratio=float(np.mean(depths > 0.0)),
            )
        )
    if not branches:
        return None
    branches.sort(key=lambda c: c.reprojection_px)
    return branches


__all__ = [
    "DEDUP_ROTATION_DEG",
    "DEDUP_TRANSLATION_M",
    "branches_are_duplicates",
    "ippe_branches",
    "reprojection_ambiguity_ratio",
    "rotation_separation_deg",
    "validate_camera_matrix",
]
