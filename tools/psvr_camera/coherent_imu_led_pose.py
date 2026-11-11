"""Full/partial LED tracking with one optical geometry and a fixed world basis.

The slow optical correction compensates model/IMU disagreement only for LED
projection. It never changes driver orientation or camera-to-world alignment.
"""
import math

import numpy as np

from .imu_led_pose import ImuLedTracker, MODEL_TO_IMU
from .led_pose import LedPose


class CoherentImuLedTracker(ImuLedTracker):
    def __init__(self, *args, allow_two_led=True, **kwargs):
        self.allow_two_led = allow_two_led
        super().__init__(*args, **kwargs)

    def minimum_matched_leds(self):
        return 2 if self.allow_two_led else 3

    def reset(self):
        super().reset()
        self.optical_correction = np.eye(3)
        self.correction_at = None
        self._partial_match_at = None
        self.last_front_reason = None

    def diagnostics(self):
        return dict(algorithm='coherent_imu_led_v2',
                    optical_correction_camera_deg=np.rad2deg(self.cv2.Rodrigues(self.optical_correction)[0].reshape(3)).tolist(),
                    front_reason=self.last_front_reason,
                    world_alignment_policy='fixed', innovation_reference='current_optical_correction',
                    max_incremental_innovation_deg=10., partial_pixel_sigma=1.5,
                    two_led_experimental=self.allow_two_led,
                    partial_position_sigma_floor_m=.003, partial_velocity_sigma_m_s=.5)

    def matching_rotation(self, imu_rotation):
        return self.optical_correction @ super().matching_rotation(imu_rotation)

    def match_pattern(self, rotation, centers, visible, forecast, now):
        self._partial_match_at = now
        try:
            return super().match_pattern(rotation, centers, visible, forecast, now)
        finally:
            self._partial_match_at = None

    def solve_translation(self, rotation, ids, image):
        if len(ids) > 5 and set(range(5)).issubset(set(ids)):
            # A ruler-estimated side/rear LED must not pull an already complete
            # refined front constellation to a different depth. Keep extra
            # LEDs for correspondence/reprojection scoring, not for this fit.
            indices = [list(ids).index(index) for index in range(5)]
            ids = np.arange(5)
            image = image[indices]
        translation = super().solve_translation(rotation, ids, image)
        if (translation is None or self._partial_match_at is None
                or self.translation is None or self.last_match is None):
            return translation
        elapsed = self._partial_match_at-self.last_match
        if not 0 < elapsed <= .15:
            return translation
        # Ray intersection is much weaker in depth than laterally. Use a
        # metric motion prior in the fit itself, instead of accepting a noisy
        # depth first and letting that poison correspondence on the next frame.
        # The image term still dominates well-observed motion; uncertainty grows
        # with the real sample interval, so gaps are not treated as 75Hz data.
        normalized = self.cv2.undistortPoints(image.reshape(-1, 1, 2), self.k, self.dist).reshape(-1, 2)
        rotated = (rotation @ self.points[ids].T).T
        matrix = []
        values = []
        for (x, y), (px, py, pz) in zip(normalized, rotated):
            matrix.extend([[1., 0., -x], [0., 1., -y]])
            values.extend([x*pz-px, y*pz-py])
        pixel_sigma = 1.5
        ray_sigma = pixel_sigma*float(self.translation[2, 0])/float((self.k[0, 0]+self.k[1, 1])*.5)
        motion_sigma = .003+.5*elapsed
        weight = ray_sigma/motion_sigma
        forecast = self.translation+self.velocity*min(.1, elapsed)
        matrix = np.vstack([matrix, np.eye(3)*weight])
        values = np.concatenate([values, forecast.reshape(3)*weight])
        fitted = np.linalg.lstsq(matrix, values, rcond=None)[0].reshape(3, 1)
        if np.min(rotated[:, 2]+fitted[2, 0]) <= .1 or not .15 < fitted[2, 0] < 4:
            return None
        return fitted

    def update(self, candidates, now):
        imu_rotation = self.rotation(now)
        if self.alignment is None or imu_rotation is None:
            if self.alignment is None:
                self.optical_correction = np.eye(3)
                self.correction_at = None
            pose = super().update(candidates, now)
            if pose.valid:
                self.correction_at = now
            return pose

        lost = self.last_match is None or now-self.last_match > 2
        if lost and self.session_id is not None:
            # Clear the poisoned motion prior once, not the immutable basis.
            # Update front confirmations only once per real camera frame.
            if self.translation is not None or self.last_match is not None:
                self.translation = None
                self.last_match = None
                self.velocity = np.zeros((3, 1))
                self.front.reset()
        optical = self.front.update(candidates, now)
        self.last_front_reason = optical.reason
        if not optical.valid:
            if lost and self.session_id is not None:
                return optical
            return super().update(candidates, now)

        observed_rotation = self.cv2.Rodrigues(optical.rvec)[0]
        expected_rotation = self.alignment @ imu_rotation @ MODEL_TO_IMU
        target = observed_rotation @ expected_rotation.T
        # Gate new disagreement against the already tracked optical correction,
        # not against activation. Slow drift may legitimately accumulate beyond
        # ten degrees over a long session without a single inconsistent frame.
        innovation = self.cv2.Rodrigues(target @ self.optical_correction.T)[0]
        if np.linalg.norm(innovation) > math.radians(10):
            # A reliable complete pattern must not be contradicted by a pair
            # chosen with an incompatible IMU/model orientation.
            return LedPose(False, 'front_orientation_conflict')

        dt = min(.1, max(0., now-(self.correction_at if self.correction_at is not None else now)))
        error = innovation
        step = error * (1-math.exp(-dt/.75))
        length = float(np.linalg.norm(step))
        maximum = math.radians(2)*dt
        if length > maximum:
            step *= maximum/length
        correction = self.cv2.Rodrigues(step)[0] @ self.optical_correction
        rotation = correction @ expected_rotation
        ids = np.arange(5)
        translation = self.solve_translation(rotation, ids, optical.image_points)
        if translation is None:
            return LedPose(False, 'front_translation_conflict')
        rvec = self.cv2.Rodrigues(rotation)[0]
        projected = self.cv2.projectPoints(self.points[:5], rvec, translation, self.k, self.dist)[0].reshape(-1, 2)
        rms = float(np.sqrt(np.mean(np.sum((projected-optical.image_points)**2, axis=1))))
        limit = max(3., self.front.reprojection_limit(optical.image_span_px))
        if rms > limit:
            # Trust the confirmed correspondence enough to converge the small
            # orientation innovation, but never publish its inconsistent pose.
            self.optical_correction = correction
            self.correction_at = now
            return LedPose(False, 'front_reprojection_conflict', rms_px=rms, rms_limit_px=limit)
        if self.translation is not None and self.last_match is not None:
            elapsed = max(0., now-self.last_match)
            if np.linalg.norm(translation-self.translation) > min(.25, .015+2*elapsed):
                return LedPose(False, 'front_motion_conflict')
            if elapsed > .001:
                measured = (translation-self.translation)/elapsed
                speed = np.linalg.norm(measured)
                if speed > 2:
                    measured *= 2/speed
                self.velocity = self.velocity*.75+measured*.25
        self.optical_correction = correction
        self.correction_at = now
        self.translation = translation
        self.last_match = now
        pose = LedPose(True, 'imu_guided_front_anchor', optical.image_points, rvec,
                       translation, rms, limit, optical.image_span_px)
        pose.led_labels = self.labels[:5]
        pose.imu_age_s = now-self.imu['monotonic_s']
        return pose
