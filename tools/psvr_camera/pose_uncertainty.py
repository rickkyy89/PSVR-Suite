"""Local measurement covariance at HEAD, not at the PnP/LED origin.

No pose is solved or changed here. Intrinsics, rig, scale and alignment are
treated as calibrated constants. This linearized covariance does not cover
wrong correspondences, multiple PnP branches or systematic rig errors.
"""

import numpy as np

from .camera_backend import OpenCvUnavailable, import_cv2

PIXEL_SIGMA_FLOOR_PX = 0.75
POSITION_SIGMA_FLOOR_M = 0.0015
IMU_ANGULAR_SIGMA_RAD = np.deg2rad(1.0)
MIN_SPAN_PX = 12.0
MAX_INFORMATION_CONDITION = 1e6
MAX_UPDATE_STD_M = 0.08
MAX_REINIT_STD_M = 0.04


def inflate_optical_covariance(covariance, alignment, variance_factor=1.0, extra_std_m=0.0):
    """Conservative PSD addition along camera depth, expressed in the IMU frame.

    Intrinsics/PnP are untouched. A variance factor of 2 means std times sqrt(2),
    not std times 2. Cross-correlations and transverse projections are preserved.
    None stays unobservable; this function never creates a fallback covariance.
    """
    if (not np.isfinite(variance_factor) or variance_factor < 1
            or not np.isfinite(extra_std_m) or extra_std_m < 0):
        raise ValueError('invalid optical covariance tuning')
    if covariance is None:
        return None
    try:
        r = np.asarray(covariance, float)
    except (ValueError, TypeError):
        raise ValueError('invalid optical covariance')
    if (r.shape != (3, 3) or not np.isfinite(r).all()
            or not np.allclose(r, r.T)):
        raise ValueError('invalid optical covariance')
    if variance_factor == 1.0 and extra_std_m == 0.0:
        return r.copy()
    a = np.asarray(alignment, float)
    if (a.shape != (3, 3) or not np.isfinite(a).all()
            or not np.allclose(a.T @ a, np.eye(3), atol=1e-6)
            or not np.isclose(np.linalg.det(a), 1.0, atol=1e-6)):
        raise ValueError('invalid optical-axis alignment')
    u = a.T @ np.array([0., 0., 1.])
    increment = (variance_factor - 1) * float(u @ r @ u) + extra_std_m**2
    effective = r + increment * np.outer(u, u)
    return (effective + effective.T) * .5


def _svd(matrix):
    """Relative information cutoff; never silently invert unobservable modes."""
    u, s, vt = np.linalg.svd(matrix, full_matrices=False)
    rank = int(np.count_nonzero(s > s[0] / np.sqrt(MAX_INFORMATION_CONDITION)))
    cond = float((s[0] / s[-1]) ** 2) if s[-1] > 0 else None
    return u, s, vt, rank, cond


def estimate_pose_covariance(pose, intrinsics, points, labels, alignment, *,
                             scale=1.0, head_offset_model_m=(0, 0, 0), guided=False):
    """Return (3x3 covariance in m², JSON quality), or (None, rejection).

    ``points`` and ``labels`` describe the full rig. ``pose.led_labels`` selects
    and orders the visible subset; absent labels mean full-rig order (front PnP).
    Propagation is for ``alignment.T @ (scale*t + R@offset)``: the offset is
    already metric and is NOT scaled. Free PnP marginalizes all six parameters.
    Guided translation uses a 1-degree isotropic local angular nuisance: its
    effect on the fixed-rotation translation fit and on the offset is correlated,
    not an independent, arbitrarily added diagonal error.
    """
    quality = dict(accepted=False, reason=None, guided=bool(guided), reliable=False,
                   led_count=0, visible_labels=[], rms_px=None, span_px=None,
                   cond=None, rank=None, sigma_px=None, dof=None,
                   eigenvalues_m2=None, worst_std_m=None,
                   position_sigma_floor_m=POSITION_SIGMA_FLOOR_M,
                   imu_angular_sigma_deg=1.0 if guided else None)

    def reject(reason):
        quality['reason'] = reason
        # Strict JSON, including overflow diagnostics from extreme bad inputs.
        for key, value in quality.items():
            if isinstance(value, float) and not np.isfinite(value):
                quality[key] = None
        return None, quality

    try:
        if not pose.valid:
            return reject('invalid_pose')
        if isinstance(labels, str) or (pose.led_labels is not None
                                       and isinstance(pose.led_labels, str)):
            return reject('invalid_correspondences')
        if isinstance(scale, bool) or not isinstance(scale, (int, float, np.integer, np.floating)):
            return reject('invalid_input')
        model = np.asarray(points, dtype=np.float64)
        rig_labels = list(labels)
        image = np.asarray(pose.image_points, dtype=np.float64)
        if image.ndim == 3 and image.shape[1:] == (1, 2):
            image = image.reshape(-1, 2)
        visible = list(pose.led_labels) if pose.led_labels is not None else rig_labels
        if (model.shape != (len(rig_labels), 3) or not np.isfinite(model).all()
                or image.shape != (len(visible), 2)
                or not all(isinstance(v, str) for v in rig_labels + visible)
                or len(set(rig_labels)) != len(rig_labels)
                or len(set(visible)) != len(visible)
                or any(v not in rig_labels for v in visible)):
            return reject('invalid_correspondences')
        quality.update(led_count=len(visible), visible_labels=visible)
        if len(visible) < 3:
            return reject('insufficient_leds')
        model = model[[rig_labels.index(v) for v in visible]]
        rvec = np.asarray(pose.rvec, dtype=np.float64).reshape(3)
        tvec = np.asarray(pose.tvec, dtype=np.float64).reshape(3)
        offset = np.asarray(head_offset_model_m, dtype=np.float64).reshape(3)
        a = np.asarray(alignment, dtype=np.float64)
        k = np.asarray(intrinsics.camera_matrix, dtype=np.float64)
        dist = np.asarray(intrinsics.distortion_coeffs, dtype=np.float64).reshape(-1)
        scale = float(scale)
        if (not all(np.isfinite(v).all() for v in (model, image, rvec, tvec, offset, a, k, dist))
                or not np.isfinite(scale) or scale <= 0 or a.shape != (3, 3)
                or not np.allclose(a.T @ a, np.eye(3), atol=1e-6)
                or not np.isclose(np.linalg.det(a), 1.0, atol=1e-6)
                or not intrinsics.calibrated or k.shape != (3, 3)
                or k[0, 0] <= 0 or k[1, 1] <= 0
                or not np.allclose(k[2], [0, 0, 1])
                or dist.size not in (0, 5)):
            return reject('invalid_input')
        span = float(np.max(np.ptp(image, axis=0)))
        quality['span_px'] = span
        if span < MIN_SPAN_PX:
            return reject('small_span')
        if (np.linalg.matrix_rank(model - model.mean(axis=0), tol=1e-9) < 2
                or np.linalg.matrix_rank(image - image.mean(axis=0), tol=1e-6) < 2):
            return reject('degenerate_geometry')
        cv2 = import_cv2()
        rotation = cv2.Rodrigues(rvec)[0]
        if np.min((rotation @ model.T + tvec[:, None])[2]) <= 0:
            return reject('invalid_depth')
        projected, jac = cv2.projectPoints(model, rvec, tvec, k, dist)
        residual = projected.reshape(-1, 2) - image
        jr, jt = jac[:, :3], jac[:, 3:6]
        j = jt if guided else jac[:, :6]
        u, s, vt, rank, cond = _svd(j)
        quality.update(rank=rank, cond=cond if cond is not None and np.isfinite(cond) else None)
        if rank != j.shape[1]:
            return reject('rank_deficient')
        dof = max(1, 2 * len(visible) - j.shape[1])
        squared_error = float(np.sum(residual ** 2))
        sigma = max(PIXEL_SIGMA_FLOOR_PX, np.sqrt(squared_error / dof))
        quality.update(rms_px=float(np.sqrt(squared_error / len(visible))),
                       sigma_px=float(sigma), dof=dof)
        parameter_cov = (vt.T * (sigma / s) ** 2) @ vt
        # Central derivative in the same Rodrigues coordinates as projectPoints.
        step = 1e-6
        d_offset = np.column_stack([
            (cv2.Rodrigues(rvec + step * axis)[0] @ offset
             - cv2.Rodrigues(rvec - step * axis)[0] @ offset) / (2 * step)
            for axis in np.eye(3)])
        if guided:
            # Convert isotropic tangent-space IMU error to Rodrigues increments.
            # Left perturbations live in camera axes; isotropy is basis invariant.
            angular_step = 1e-4  # Above OpenCV's matrix-to-vector zero threshold.
            local_to_rvec = np.column_stack([
                (cv2.Rodrigues(cv2.Rodrigues(angular_step * axis)[0] @ rotation)[0].reshape(3)
                 - cv2.Rodrigues(cv2.Rodrigues(-angular_step * axis)[0] @ rotation)[0].reshape(3))
                / (2 * angular_step) for axis in np.eye(3)])
            # Exact Rodrigues identity is numerically unstable in OpenCV near 0.
            if np.linalg.norm(rvec) < 1e-5:
                local_to_rvec = np.eye(3)
            translation_response = -(vt.T / s) @ u.T @ jr @ local_to_rvec
            angular_response = scale * translation_response + d_offset @ local_to_rvec
            camera_cov = (scale ** 2 * parameter_cov
                          + IMU_ANGULAR_SIGMA_RAD ** 2 * angular_response @ angular_response.T)
        else:
            head_j = np.column_stack((d_offset, scale * np.eye(3)))
            camera_cov = head_j @ parameter_cov @ head_j.T
        covariance = a.T @ camera_cov @ a + POSITION_SIGMA_FLOOR_M ** 2 * np.eye(3)
        covariance = (covariance + covariance.T) / 2
        if not np.isfinite(covariance).all():
            return reject('numerical_error')
        eigenvalues = np.linalg.eigvalsh(covariance)
        if eigenvalues[0] <= 0:
            return reject('numerical_error')
        worst = float(np.sqrt(eigenvalues[-1]))
        quality.update(eigenvalues_m2=eigenvalues.tolist(), worst_std_m=worst)
        if worst > MAX_UPDATE_STD_M:
            return reject('excessive_uncertainty')
        quality.update(accepted=True, reason='ok',
                       reliable=bool(len(visible) >= 4 and worst <= MAX_REINIT_STD_M))
        return covariance, quality
    except OpenCvUnavailable:
        return reject('opencv_unavailable')
    except (ValueError, TypeError, AttributeError, OverflowError, np.linalg.LinAlgError):
        return reject('invalid_input')
    except Exception as exc:
        # cv2.error without importing OpenCV at module import time.
        if type(exc).__module__ == 'cv2':
            return reject('projection_error')
        raise
