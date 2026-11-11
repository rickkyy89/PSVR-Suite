"""Compete known LED constellations using the same optical/IMU geometry.

The full front pattern is a high-confidence fast path. Weak/full conflicting
patterns compete with geometrically verified mixed subsets before declaring
loss. Rejected hypotheses never modify the position history.
"""
from itertools import combinations

import numpy as np

from .coherent_imu_led_pose import CoherentImuLedTracker
from .led_pose import FrontLedTracker, LedPose


class _PredictedFrontTracker(FrontLedTracker):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.hint = None

    def update(self, candidates, now):
        return super().update(candidates, now, acquisition_hint=self.hint)


class ConstellationLedTracker(CoherentImuLedTracker):
    def __init__(self, intrinsics, points, normals, labels, max_rms_px=2.5, allow_two_led=False, **kwargs):
        super().__init__(intrinsics, points, normals, labels, max_rms_px, allow_two_led=allow_two_led, **kwargs)
        self.front = _PredictedFrontTracker(intrinsics, points[:5], max_rms_px)

    PATTERNS = (
        ('front', (0, 1, 2, 3, 4)),
        ('diamond_left', (0, 2, 3, 5)),
        ('diamond_right', (1, 2, 4, 6)),
        ('side_left', (0, 3, 5, 7)),
        ('side_right', (1, 4, 6, 8)),
        ('rear_left', (5, 7)),
        ('rear_right', (6, 8)),
        ('rear', (7, 8)),
    )

    def reset(self):
        super().reset()
        self.constellation = None
        self.match_diagnostics = {}

    def diagnostics(self):
        return dict(super().diagnostics(), algorithm='constellation_led_v1',
                    constellation=self.constellation, recognition=self.match_diagnostics)

    def _recognize(self, rotation, centers, visible, forecast, now, front_pose=None):
        minimum = self.minimum_matched_leds()
        if len(centers) < minimum or not np.isfinite(centers).all():
            return None, 'constellation_insufficient_leds'
        rvec = self.cv2.Rodrigues(rotation)[0]
        _, predicted = self.predicted(rotation, forecast, relaxed=True)
        span = float(np.max(np.ptp(predicted[visible], axis=0))) if len(visible) else 0.
        gate = max(20., min(80., span*.4))
        limit = max(3., self.front.reprojection_limit(float(np.max(np.ptp(centers, axis=0)))))
        maximum_jump = min(.25, .015+2*max(0., now-self.last_match))
        seeds = []
        for i in visible:
            distances = np.linalg.norm(centers-predicted[i], axis=1)
            seeds.extend((int(i), int(j)) for j in np.argsort(distances)[:2] if distances[j] <= gate)
        patterns = [(name, tuple(i for i in ids if i in visible)) for name, ids in self.PATTERNS]
        patterns = [(name, ids) for name, ids in patterns if len(ids) >= minimum]
        solved = {}
        proposals = {}

        def evaluate(assignment, name):
            assignment = tuple(sorted(assignment))
            if len(assignment) < minimum:
                return
            key = assignment
            if key in solved:
                return
            solved[key] = True
            ids = np.asarray([i for i, j in assignment])
            image = centers[[j for i, j in assignment]]
            if len(ids) >= 3 and np.linalg.matrix_rank(self.points[ids]-self.points[ids].mean(axis=0), tol=1e-8) < 2:
                return
            translation = self.solve_translation(rotation, ids, image)
            if translation is None or np.linalg.norm(translation-self.translation) > maximum_jump:
                return
            projected = self.cv2.projectPoints(self.points[ids], rvec, translation, self.k, self.dist)[0].reshape(-1, 2)
            errors = np.linalg.norm(projected-image, axis=1)
            rms = float(np.sqrt(np.mean(errors**2)))
            if rms > limit or np.max(errors) > limit*2:
                # A single spurious/occluded front LED must not veto a coherent
                # remaining constellation. Never trim below the explicit mode.
                if len(assignment) > minimum:
                    worst = int(np.argmax(errors))
                    evaluate([pair for n, pair in enumerate(assignment) if n != worst], name)
                return
            prior = float(np.linalg.norm(translation-forecast))
            score = 2*len(ids)-float(np.sum((errors/limit)**2))-prior/.01*.25
            if name == self.constellation:
                score += .25  # Evidence hysteresis, not an angle threshold.
            proposals[key] = (score, name, rms, translation, ids, image, limit)

        def assign(projected, allowed):
            distances = np.linalg.norm(projected[np.asarray(allowed), None, :]-centers[None, :, :], axis=2)
            pairs, used_ids, used_centers = [], set(), set()
            for flat in np.argsort(distances, axis=None):
                a, j = np.unravel_index(flat, distances.shape)
                if distances[a, j] > limit*2:
                    break
                i = allowed[a]
                if i not in used_ids and j not in used_centers:
                    pairs.append((i, int(j)))
                    used_ids.add(i)
                    used_centers.add(j)
            return pairs

        self._partial_match_at = now
        try:
            if front_pose is not None and front_pose.valid:
                distances = np.linalg.norm(front_pose.image_points[:, None]-centers[None], axis=2)
                order = np.argmin(distances, axis=1)
                if len(set(order)) == 5:
                    evaluate(list(enumerate(order.tolist())), 'front')
            for first, second in combinations(seeds, 2):
                if first[0] == second[0] or first[1] == second[1]:
                    continue
                ids = np.asarray([first[0], second[0]])
                image = centers[[first[1], second[1]]]
                translation = self.solve_translation(rotation, ids, image)
                if translation is None or np.linalg.norm(translation-self.translation) > maximum_jump:
                    continue
                projected = self.cv2.projectPoints(self.points, rvec, translation, self.k, self.dist)[0].reshape(-1, 2)
                for name, allowed in patterns:
                    evaluate(assign(projected, allowed), name)
        finally:
            self._partial_match_at = None
        ordered = sorted(proposals.values(), key=lambda item: (len(item[4]) >= 3, item[0]), reverse=True)
        self.match_diagnostics = dict(hypotheses=len(solved), accepted=len(ordered), min_leds=minimum)
        if not ordered:
            return None, 'constellation_no_verified_pattern'
        best = ordered[0]
        for other in ordered[1:]:
            if ((len(other[4]) >= 3) == (len(best[4]) >= 3) and best[0]-other[0] < .2
                    and np.linalg.norm(best[3]-other[3]) > .03):
                return None, 'constellation_ambiguous_depth'
        self.constellation = best[1]
        return (len(best[4]), -best[0], best[2], best[3], best[4], best[5], best[6]), None

    def match_pattern(self, rotation, centers, visible, forecast, now):
        result, reason = self._recognize(rotation, centers, visible, forecast, now)
        if result is not None:
            return result, None
        if reason == 'constellation_ambiguous_depth':
            # The compatibility fallback must not bypass an explicit ambiguity
            # between verified constellations at different depths.
            return None, reason
        # Retain an already supported legacy hypothesis if the template library
        # does not cover its visible subset. No new two-LED authorization here.
        return super().match_pattern(rotation, centers, visible, forecast, now)

    def update(self, candidates, now):
        self.match_diagnostics = {}
        self.front.hint = None
        imu_rotation = self.rotation(now)
        if (self.alignment is not None and self.translation is not None and imu_rotation is not None
                and self.last_match is not None and 0 <= now-self.last_match <= 2):
            forecast = self.translation+self.velocity*min(.1, now-self.last_match)
            self.front.hint = self.predicted(self.matching_rotation(imu_rotation), forecast, relaxed=True)[1][:5]
        before = None if self.translation is None else (self.translation.copy(), self.velocity.copy(), self.last_match)
        pose = super().update(candidates, now)
        if (before is None or before[2] is None or now-before[2] > 2
                or self.rotation(now) is None):
            return pose
        if pose.valid and pose.reason == 'imu_guided_front_anchor' and pose.rms_px <= 2:
            self.constellation = 'front'
            self.match_diagnostics = dict(fast_path='verified_front', matched_leds=5, rms_px=pose.rms_px)
            return pose
        if pose.reason not in ('front_reprojection_conflict', 'imu_guided_front_anchor'):
            return pose
        after = (self.translation.copy(), self.velocity.copy(), self.last_match)
        self.translation, self.velocity, self.last_match = before
        rotation = self.matching_rotation(self.rotation(now))
        forecast = self.translation+self.velocity*min(.1, max(0., now-self.last_match))
        visible, _ = self.predicted(rotation, forecast, relaxed=True)
        centers = np.asarray([c['center_px'] for c in candidates], dtype=float).reshape(-1, 2)
        match, reason = self._recognize(rotation, centers, visible, forecast, now, pose if pose.valid else None)
        if match is None:
            if reason == 'constellation_ambiguous_depth':
                # Explicit ambiguity between verified constellations at
                # different depths: reject the weak front pose as well and
                # restore the motion prior. Keep the valid optical correction
                # converged by super().update().
                self.translation, self.velocity, self.last_match = before
                return LedPose(False, reason)
            self.translation, self.velocity, self.last_match = after
            return pose
        _, _, rms, translation, ids, image, limit = match
        dt = now-self.last_match
        if dt > .001:
            measured = (translation-self.translation)/dt
            speed = np.linalg.norm(measured)
            if speed > 2:
                measured *= 2/speed
            self.velocity = self.velocity*.75+measured*.25
        self.translation = translation
        self.last_match = now
        result = LedPose(True, 'imu_guided_constellation', image, self.cv2.Rodrigues(rotation)[0],
                         translation, rms, limit, float(np.max(np.ptp(image, axis=0))))
        result.led_labels = [self.labels[i] for i in ids]
        result.predicted_labels = [self.labels[i] for i in visible]
        result.imu_age_s = now-self.imu['monotonic_s']
        return result
