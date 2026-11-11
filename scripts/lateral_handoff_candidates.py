"""Offline-only handoff experiments; production suite never imports this file."""
from collections import Counter
import math

import numpy as np

from tools.psvr_camera.coherent_imu_led_pose import CoherentImuLedTracker
from tools.psvr_camera.imu_led_pose import ImuLedTracker


class GuidedFallbackTracker(CoherentImuLedTracker):
    def __init__(self, *args, **kwargs):
        self.experiment_counts = Counter()
        super().__init__(*args, **kwargs)

    def try_guided(self, candidates, now):
        if self.translation is None or self.last_match is None or now-self.last_match > 2:
            return None
        # The base matcher publishes its prior before returning. A rejected
        # two-LED hypothesis must not leave a hidden state change behind.
        saved = (self.translation.copy(), self.last_match, self.velocity.copy())
        pose = ImuLedTracker.update(self, candidates, now)
        if pose.valid and len(pose.led_labels or []) >= 3:
            return pose
        self.translation, self.last_match, self.velocity = saved
        self.experiment_counts['guided_rejected: ' + pose.reason.split(':')[0]] += 1
        return None

    def update(self, candidates, now):
        pose = super().update(candidates, now)
        if pose.reason == 'front_reprojection_conflict':
            self.experiment_counts['front_conflict_attempts'] += 1
            guided = self.try_guided(candidates, now)
            if guided is not None:
                self.experiment_counts['front_conflicts_recovered'] += 1
                guided.reason = 'experiment_verified_front_fallback'
                return guided
        return pose


class AngleHandoffTracker(GuidedFallbackTracker):
    def reset(self):
        super().reset()
        self.lateral_mode = False

    def update(self, candidates, now):
        imu_rotation = self.rotation(now)
        if self.alignment is not None and self.translation is not None and imu_rotation is not None:
            rotation = self.matching_rotation(imu_rotation)
            viewing_ray = self.translation.reshape(3)/np.linalg.norm(self.translation)
            angle = math.degrees(math.acos(float(np.clip(rotation[:, 2] @ viewing_ray, -1., 1.))))
            # Hysteresis avoids switching on every noisy sample at 25 degrees.
            if angle >= 25:
                self.lateral_mode = True
            elif angle <= 20:
                self.lateral_mode = False
            if self.lateral_mode:
                self.experiment_counts['angular_guided_attempts'] += 1
                guided = self.try_guided(candidates, now)
                if guided is not None:
                    self.experiment_counts['angular_guided_accepted'] += 1
                    guided.reason = 'experiment_angular_handoff'
                    return guided
        return super().update(candidates, now)
