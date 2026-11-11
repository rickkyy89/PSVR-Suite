"""Front-five stereo tracking for the existing Suite worker.

Free stereo PnP is used only for initial absolute alignment (at most once per
second). Afterwards all 120 fixed-IMU-rotation associations are evaluated in
vectorized 3D/2D, with stereo-derived metric translation, not free PnP per frame.
Rejected guided frames also sample a detached, asynchronous free optical solve
(one in flight, at most once every five seconds). Only fully validated five-point
results may slowly correct the optical matching model, never alignment or the
published IMU rotation. No free optical pose is published or used as a fallback.
There are no lateral/rear points or two-LED fallbacks. Geometry/scale remain
experimental; this module never owns HID, sends UDP or changes driver state.
"""
from copy import deepcopy
from itertools import permutations
import threading
import time

import numpy as np

from .camera_backend import import_cv2
from .driver_imu_source import MAX_AGE_S
from .imu_led_pose import MODEL_TO_IMU, rotation_from_quaternion
from .led_pose import LedPose
from .pose_uncertainty import estimate_pose_covariance
from .ps4_front5_selection import unique_front5_subset
from .stereo_head_pose import (StereoHeadPoseConfig,
                               estimate_stereo_head_pose_from_triangulation)
from .stereo_leds import triangulate_candidates


# Correction policy only: the existing stereo/pose acceptance gates are unchanged.
_CORRECTION_MAX_ANGLE_RAD = np.deg2rad(3.)
_CORRECTION_MAX_INNOVATION_RAD = np.deg2rad(5.)
_CORRECTION_MAX_STEP_RAD = np.deg2rad(.25)
_CORRECTION_TAU_S = 2.
_CORRECTION_DT_CAP_S = .25
_CORRECTION_MAX_RESULT_AGE_S = 2.
_CORRECTION_HOLD_S = 8.
_CORRECTION_DECAY_TAU_S = 10.


def _free_optical_diagnostic(job, solver, calibration, rig, tri, left, right,
                             rotation, config):
    """Write only a detached job result, never tracker state or a publishable pose."""
    started = time.perf_counter()
    result = dict(state="error", error_type="interrupted")
    try:
        report = solver(calibration, rig, tri, left, right, config=config)
        result = dict(state="complete", valid=report["valid"] is True,
                      reason=report["reason"], solver_state=report.get("state"),
                      residuals=deepcopy(report.get("residuals")),
                      ransac_inlier_count=report.get("ransac", {}).get("inlier_count"),
                      delta_rotation_deg=None, delta_matching_rotation_deg=None,
                      best_permutation=None)
        if report["valid"] is True:
            optical = np.asarray(report["pose"]["rotation_matrix_left_camera"], float)
            relative_matching = optical @ rotation.T
            target = relative_matching @ job["matching_correction"]
            # Preserve delta_rotation_deg's original optical-vs-IMU meaning.
            cosine = (np.trace(target) - 1.) / 2.
            if np.isfinite(cosine):
                result["delta_rotation_deg"] = float(np.degrees(np.arccos(np.clip(cosine, -1., 1.))))
            cosine = (np.trace(relative_matching) - 1.) / 2.
            if np.isfinite(cosine):
                result["delta_matching_rotation_deg"] = float(np.degrees(np.arccos(np.clip(cosine, -1., 1.))))
            result["best_permutation"] = [c["point_3d_index"]
                                           for c in report["pose"]["correspondences"]]
            if (report.get("ransac", {}).get("inlier_count") == 5
                    and sorted(result["best_permutation"]) == list(range(5))):
                # Convert a residual relative to the sampled MATCHING rotation
                # back to an absolute camera-frame correction of alignment*IMU.
                # This private matrix is not a pose and is never logged/published.
                job["correction_target"] = target.copy()
    except Exception as exc:
        # Diagnostic failures (including fake solvers) must not affect tracking.
        result = dict(state="error", error_type=type(exc).__name__)
    finally:
        job["result"] = dict(result, duration_ms=(time.perf_counter() - started) * 1000.)
        job["done"].set()


class Ps4SuiteTracker:
    def __init__(self, calibration, rig, allow_two_led=False):
        self.calibration, self.rig = calibration, rig
        self.points = rig.object_points_m
        self.labels = list(rig.labels)
        self.cv2 = import_cv2()
        self.config = StereoHeadPoseConfig()
        self.allow_two_led = allow_two_led
        self._permutations = np.asarray(list(permutations(range(5))))
        self._optical_job = None
        self._last_optical_sample = None
        self.reset()

    def reset(self):
        self.alignment = None
        self.imu = None
        self.session_id = None
        self.epoch = None
        self.translation = None
        self.last_match = None
        self._last_acquisition = None
        self._alignment_at = None
        # Do not release an occupied worker slot on reset: an old solve may
        # finish, but its generation can never appear in the new session's log.
        self._diagnostic_generation = object()
        self._reset_matching_correction()
        self._diagnostics = {}

    def _reset_matching_correction(self):
        self.matching_correction = np.eye(3)
        self._correction_at = None
        self._correction_validated_at = None
        self._correction_consumed_job = None
        self._correction_step_used = 0.
        self._correction_reason = "reset"

    def _advance_matching_correction(self, now):
        """Decay without optical evidence; never extrapolate a correction target."""
        previous = self._correction_at
        self._correction_step_used = 0.
        if not np.isfinite(now) or (previous is not None and now < previous):
            self._reset_matching_correction()
            self._diagnostic_generation = object()
            self._correction_reason = "clock_reset"
            return
        self._correction_at = now
        validated = self._correction_validated_at
        if previous is None or validated is None or now - validated <= _CORRECTION_HOLD_S:
            return
        dt = min(_CORRECTION_DT_CAP_S, max(0., now - max(previous, validated + _CORRECTION_HOLD_S)))
        vector = self.cv2.Rodrigues(self.matching_correction)[0].reshape(3)
        step = vector * (1. - np.exp(-dt / _CORRECTION_DECAY_TAU_S))
        length = float(np.linalg.norm(step))
        maximum = _CORRECTION_MAX_STEP_RAD
        if length > maximum:
            step *= maximum / length
        self._correction_step_used = float(np.linalg.norm(step))
        self.matching_correction = self.cv2.Rodrigues(vector - step)[0]
        self._correction_reason = "decaying_without_validated_five"

    def _consume_matching_correction(self, now):
        """Consume each completed five-point proof once, only on the camera thread."""
        job = self._optical_job
        if job is None or job is self._correction_consumed_job or not job["done"].is_set():
            return
        self._correction_consumed_job = job
        if job["generation"] is not self._diagnostic_generation:
            self._correction_reason = "discarded_generation"
            return
        stamp = job["sample_monotonic_s"]
        if not 0. <= now - stamp <= _CORRECTION_MAX_RESULT_AGE_S:
            self._correction_reason = "discarded_stale_result"
            return
        result = job["result"]
        target = job.get("correction_target")
        if (result.get("state") != "complete" or result.get("valid") is not True
                or result.get("ransac_inlier_count") != 5 or target is None):
            self._correction_reason = "discarded_unvalidated_five"
            return
        target = np.asarray(target, float)
        if (target.shape != (3, 3) or not np.isfinite(target).all()
                or not np.allclose(target.T @ target, np.eye(3), atol=1e-6)
                or not np.isclose(np.linalg.det(target), 1., atol=1e-6)):
            self._correction_reason = "discarded_invalid_rotation"
            return
        innovation = target @ self.matching_correction.T
        angle = np.arccos(np.clip((np.trace(innovation) - 1.) / 2., -1., 1.))
        if angle > _CORRECTION_MAX_INNOVATION_RAD:
            self._correction_reason = "discarded_large_innovation"
            return
        previous = self._correction_validated_at
        if previous is None:
            previous = self._alignment_at if self._alignment_at is not None else stamp - _CORRECTION_DT_CAP_S
        dt = min(_CORRECTION_DT_CAP_S, max(0., stamp - previous))
        if dt <= 0.:
            self._correction_reason = "discarded_nonincreasing_sample"
            return
        vector = self.cv2.Rodrigues(innovation)[0].reshape(3)
        step = vector * (1. - np.exp(-dt / _CORRECTION_TAU_S))
        length = float(np.linalg.norm(step))
        maximum = max(0., _CORRECTION_MAX_STEP_RAD - self._correction_step_used)
        if length > maximum:
            step *= maximum / length
        candidate = self.cv2.Rodrigues(step)[0] @ self.matching_correction
        vector = self.cv2.Rodrigues(candidate)[0].reshape(3)
        length = float(np.linalg.norm(vector))
        if length > _CORRECTION_MAX_ANGLE_RAD:
            vector *= _CORRECTION_MAX_ANGLE_RAD / length
        self.matching_correction = self.cv2.Rodrigues(vector)[0]
        self._correction_step_used += float(np.linalg.norm(step))
        self._correction_validated_at = stamp
        self._correction_reason = "validated_five_update"

    def set_imu(self, sample):
        if sample is not None:
            session = sample.get("session_id")
            if session != self.session_id:
                self.reset()
                self.session_id = session
            epoch = sample.get("recenter_epoch")
            if self.epoch is not None and epoch != self.epoch:
                # A relates absolute frames: driver recenter must not rotate A.
                self.translation = None
                self.last_match = None
                self._diagnostic_generation = object()
                self._reset_matching_correction()
            self.epoch = epoch
        self.imu = sample

    def search_region(self, width, height, now):
        # Full image in both eyes: no copied monocular ROI that could hide LEDs.
        return None, 1.0

    def diagnostics(self):
        result = dict(deepcopy(self._diagnostics), algorithm="ps4_stereo_front5_imu",
                     front_only=True, two_led_supported=False,
                     covariance_model="reference_eye_front5_approximation",
                     physical_scale_validated=False)
        vector = self.cv2.Rodrigues(self.matching_correction)[0].reshape(3)
        result["optical_matching_correction"] = dict(
            rotation_vector_deg=np.rad2deg(vector).tolist(),
            angle_deg=float(np.rad2deg(np.linalg.norm(vector))),
            validated_at_monotonic_s=self._correction_validated_at,
            reason=self._correction_reason, published_rotation="alignment_imu_only",
            max_angle_deg=3., max_step_deg=.25, tau_s=_CORRECTION_TAU_S,
            max_innovation_deg=5., integration_dt_cap_s=_CORRECTION_DT_CAP_S,
            max_result_age_s=_CORRECTION_MAX_RESULT_AGE_S,
            hold_s=_CORRECTION_HOLD_S, decay_tau_s=_CORRECTION_DECAY_TAU_S)
        job = self._optical_job
        if job is not None and job["generation"] is self._diagnostic_generation:
            comparison = dict(diagnostic_only=False, published=False,
                              use="validated_bounded_matching_correction_only",
                              sample_monotonic_s=job["sample_monotonic_s"],
                              guided=deepcopy(job["guided"]))
            comparison.update(deepcopy(job["result"]) if job["done"].is_set()
                              else {"state": "pending"})
            result["free_optical_comparison"] = comparison
        return result

    def _diagnose_guided_rejection(self, tri, left, right, rotation, now,
                                   residual3d, residual_left, residual_right, rms3d, rms2d):
        """Rank failed hypotheses for logging only; acceptance above is unchanged."""
        cfg = self.config
        score = ((rms3d / cfg.max_rms_3d_residual_m) ** 2
                 + (rms2d / cfg.max_rms_reprojection_px) ** 2)
        best = int(np.argmin(score))
        e3d, el, er = residual3d[best], residual_left[best], residual_right[best]
        diagnostic = dict(
            ranking="normalized_rms_2d_3d_squared_sum",
            best_permutation=self._permutations[best].tolist(), led_labels=list(self.labels),
            matched_candidate_indices=[[m["left_index"], m["right_index"]] for m in tri["matches"]],
            per_led_3d_m=e3d.tolist(), per_led_left_px=el.tolist(), per_led_right_px=er.tolist(),
            left_rms_px=float(np.sqrt(np.mean(el ** 2))),
            right_rms_px=float(np.sqrt(np.mean(er ** 2))), stereo_rms_px=float(rms2d[best]),
            left_max_px=float(el.max()), right_max_px=float(er.max()),
            max_reprojection_px=float(max(el.max(), er.max())),
            rms_3d_m=float(rms3d[best]), max_3d_m=float(e3d.max()),
            gates=dict(rms_3d=bool(rms3d[best] <= cfg.max_rms_3d_residual_m),
                       max_3d=bool(e3d.max() <= cfg.max_point_3d_residual_m),
                       rms_2d=bool(rms2d[best] <= cfg.max_rms_reprojection_px),
                       max_2d=bool(max(el.max(), er.max()) <= cfg.max_point_reprojection_px)),
            imu_age_s=float(now - self.imu["monotonic_s"]),
            alignment_age_s=None if self._alignment_at is None else float(now - self._alignment_at))
        self._diagnostics["guided_residual_rejection"] = diagnostic
        job = self._optical_job
        if job is not None and not job["done"].is_set():
            self._diagnostics["free_optical_sampling"] = "busy"
            return
        if self._last_optical_sample is not None and now - self._last_optical_sample < 5.:
            self._diagnostics["free_optical_sampling"] = "throttled"
            return
        self._last_optical_sample = now
        # Mirror acquisition's five-element remapping, but detach EVERY input.
        # No queue, no join and no solve on the capture/publication thread.
        matches = tri["matches"]
        solver_tri = dict(tri, matches=[dict(m, left_index=i, right_index=i)
                                       for i, m in enumerate(matches)],
                          candidate_counts={"left": 5, "right": 5},
                          unmatched_left_indices=[], unmatched_right_indices=[])
        inputs = deepcopy((self.calibration, self.rig, solver_tri,
                           [left[m["left_index"]] for m in matches],
                           [right[m["right_index"]] for m in matches], rotation, cfg))
        job = dict(generation=self._diagnostic_generation, done=threading.Event(),
                   sample_monotonic_s=float(now), guided=deepcopy(diagnostic),
                   matching_correction=self.matching_correction.copy())
        self._optical_job = job
        try:
            threading.Thread(target=_free_optical_diagnostic,
                             args=(job, estimate_stereo_head_pose_from_triangulation, *inputs),
                             name="ps4-optical-diagnostic", daemon=True).start()
        except Exception as exc:
            job["result"] = dict(state="error", error_type=type(exc).__name__)
            job["done"].set()
        self._diagnostics["free_optical_sampling"] = "sampled"

    def _invalid(self, reason):
        self._diagnostics["reason"] = reason
        return LedPose(False, reason)

    def _imu_rotation(self, now):
        sample = self.imu
        if (not isinstance(sample, dict) or not sample.get("session_id")
                or sample.get("valid") is not True
                or sample.get("connected", True) is not True):
            return None
        stamp = sample.get("monotonic_s")
        if (type(stamp) not in (int, float) or not np.isfinite(stamp)
                or not 0 <= now - stamp <= MAX_AGE_S):
            return None
        try:
            return rotation_from_quaternion(sample["quaternion_sensor_wxyz"])
        except (ValueError, KeyError, TypeError):
            return None

    def update_stereo(self, left_candidates, right_candidates, now):
        self._diagnostics = {"left_candidate_count": len(left_candidates),
                             "right_candidate_count": len(right_candidates)}
        self._advance_matching_correction(now)
        if self.allow_two_led:
            return self._invalid("ps4_stereo_two_led_unsupported_front5_required")
        imu_rotation = self._imu_rotation(now)
        if imu_rotation is None:
            return self._invalid("ps4_stereo_stale_or_invalid_driver_imu")
        tri = triangulate_candidates(self.calibration, left_candidates, right_candidates)
        self._diagnostics.update(stereo_state=tri["state"],
                                 stereo_matches=len(tri["matches"]))
        if tri["state"] != "points" or len(tri["matches"]) < 5:
            return self._invalid("ps4_stereo_five_unambiguous_matches_required")
        # Five certified 3D correspondences are required, not exactly five raw
        # 2D blobs: unpaired daylight reflections cannot invalidate otherwise
        # unique stereo evidence. Never accept 'partial'/'ambiguous' reports.
        # With additional unique stereo points, require exactly ONE possible
        # rigid front-five distance signature before the unchanged pose gates.
        subset, possibilities = unique_front5_subset(
            tri["matches"], self.points, self.config.ransac_3d_threshold_m)
        self._diagnostics["front5_distance_subsets"] = possibilities
        if subset is None:
            return self._invalid("ps4_stereo_front5_subset_ambiguous" if possibilities > 1
                                 else "ps4_stereo_front5_rigid_subset_missing")
        tri = dict(tri, matches=[tri["matches"][int(i)] for i in subset])
        self._diagnostics["selected_stereo_matches"] = 5
        self._diagnostics["matched_candidate_indices"] = [
            [m["left_index"], m["right_index"]] for m in tri["matches"]]
        self._diagnostics["unmatched_2d_counts"] = {
            "left": len(left_candidates) - 5, "right": len(right_candidates) - 5}
        if self.alignment is None:
            if self._last_acquisition is not None and now - self._last_acquisition < 1.0:
                return self._invalid("ps4_stereo_await_alignment_retry")
            self._last_acquisition = now
            # The strict five-point solver requires matching 5-element centroid
            # arrays in the order of its triangulated points. Preserve the raw
            # detector indices above, then remap this detached solver input.
            solver_left = [left_candidates[m["left_index"]] for m in tri["matches"]]
            solver_right = [right_candidates[m["right_index"]] for m in tri["matches"]]
            solver_tri = dict(tri, matches=[dict(m, left_index=i, right_index=i)
                                          for i, m in enumerate(tri["matches"])],
                              candidate_counts={"left": 5, "right": 5},
                              unmatched_left_indices=[], unmatched_right_indices=[])
            report = estimate_stereo_head_pose_from_triangulation(
                self.calibration, self.rig, solver_tri, solver_left, solver_right)
            if not report["valid"]:
                return self._invalid("ps4_stereo_alignment_" + report["reason"])
            result = report["pose"]
            rotation = np.asarray(result["rotation_matrix_left_camera"], float)
            published_rotation = rotation
            translation = np.asarray(result["tvec_left_camera_m"], float)
            image = np.asarray([c["left_centroid_px"]
                                for c in result["correspondences"]], float)
            candidate_alignment = rotation @ MODEL_TO_IMU.T @ imu_rotation.T
            rms = report["residuals"]["stereo_rms_px"]
            reason = "ps4_stereo_initial_alignment"
        else:
            candidate_alignment = self.alignment
            rotation = self.alignment @ imu_rotation @ MODEL_TO_IMU
            published_rotation = rotation
            cfg = self.config
            if rotation[1, 1] < cfg.min_upright_y_cos or rotation[2, 2] < cfg.min_facing_z_cos:
                return self._invalid("ps4_stereo_front_orientation_gate")
            self._consume_matching_correction(now)
            rotation = self.matching_correction @ rotation
            matches = tri["matches"]
            points = np.asarray([m["point_left_camera_m"] for m in matches], float)
            left = np.asarray([left_candidates[m["left_index"]]["center_px"] for m in matches])
            right = np.asarray([right_candidates[m["right_index"]]["center_px"] for m in matches])
            if min(np.max(np.ptp(left, axis=0)), np.max(np.ptp(right, axis=0))) < cfg.min_span_px:
                return self._invalid("ps4_stereo_small_span")
            rotated = (rotation @ self.points.T).T
            # All five points participate; no monocular depth or rig-scale fit.
            translation = points.mean(axis=0) - rotated.mean(axis=0)
            model_left = rotated + translation
            model_right = (self.calibration.R @ model_left.T).T + self.calibration.T
            distances = np.linalg.norm(np.concatenate((model_left, model_right)), axis=1)
            if (min(model_left[:, 2].min(), model_right[:, 2].min()) <= 0
                    or distances.min() < cfg.min_distance_m or distances.max() > cfg.max_distance_m):
                return self._invalid("ps4_stereo_distance_gate")
            projected_left = self.cv2.projectPoints(
                model_left, np.zeros(3), np.zeros(3), self.calibration.left.camera_matrix,
                self.calibration.left.distortion_coeffs)[0].reshape(5, 2)
            projected_right = self.cv2.projectPoints(
                model_right, np.zeros(3), np.zeros(3), self.calibration.right.camera_matrix,
                self.calibration.right.distortion_coeffs)[0].reshape(5, 2)
            order = self._permutations
            residual3d = np.linalg.norm(model_left - points[order], axis=2)
            residual_left = np.linalg.norm(projected_left - left[order], axis=2)
            residual_right = np.linalg.norm(projected_right - right[order], axis=2)
            rms3d = np.sqrt(np.mean(residual3d ** 2, axis=1))
            rms2d = np.sqrt(np.mean((residual_left ** 2 + residual_right ** 2) / 2, axis=1))
            passed = ((rms3d <= cfg.max_rms_3d_residual_m)
                      & (residual3d.max(axis=1) <= cfg.max_point_3d_residual_m)
                      & (rms2d <= cfg.max_rms_reprojection_px)
                      & (np.maximum(residual_left, residual_right).max(axis=1)
                         <= cfg.max_point_reprojection_px))
            valid = np.flatnonzero(passed)
            if not len(valid):
                try:
                    self._diagnose_guided_rejection(
                        tri, left_candidates, right_candidates, rotation, now,
                        residual3d, residual_left, residual_right, rms3d, rms2d)
                except Exception as exc:
                    self._diagnostics["guided_residual_diagnostic_error"] = type(exc).__name__
                return self._invalid("ps4_stereo_guided_residual_gate")
            ranked = valid[np.argsort(rms2d[valid])]
            best = ranked[0]
            if len(ranked) > 1 and rms2d[ranked[1]] - rms2d[best] <= cfg.ambiguity_rms_margin_px:
                return self._invalid("ps4_stereo_guided_ambiguous_association")
            if self.last_match is not None and now - self.last_match <= 2.0:
                maximum_jump = min(.25, .015 + 2 * max(0., now - self.last_match))
                if np.linalg.norm(translation - self.translation) > maximum_jump:
                    return self._invalid("ps4_stereo_translation_jump")
            image = left[order[best]]
            rms = float(rms2d[best])
            self._diagnostics["rms_3d_m"] = float(rms3d[best])
            reason = "ps4_stereo_imu_guided"
        pose = LedPose(True, reason, image,
                       # Only matching/translation sees the optical correction;
                       # exported rotation and the downstream head lever arm do not.
                       self.cv2.Rodrigues(published_rotation)[0].reshape(3),
                       translation.reshape(3, 1), float(rms),
                       self.config.max_rms_reprojection_px,
                       float(np.max(np.ptp(image, axis=0))))
        pose.led_labels = list(self.labels)
        pose.imu_age_s = now - self.imu["monotonic_s"]
        # A reference-eye covariance approximation: front FIVE only,
        # all six pose parameters marginalized (also for fixed-rotation updates).
        # It retains angular/offset uncertainty and uses the existing frame/scale
        # propagation. The single-eye PnP covariance is not a proven conservative
        # bound for the stereo 3D-mean translation estimator, and does not model
        # calibrated stereo or systematic uncertainty.
        _, quality = estimate_pose_covariance(
            pose, self.calibration.left, self.points, self.labels, candidate_alignment)
        if not quality["accepted"]:
            return self._invalid("ps4_stereo_uncertainty_" + quality["reason"])
        if self.alignment is None:
            self._alignment_at = now
        self.alignment = candidate_alignment
        self.translation = translation.copy()
        self.last_match = now
        return pose
