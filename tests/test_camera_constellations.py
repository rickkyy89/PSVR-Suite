"""Known-pattern recognition, geometry, state isolation, and bounded reacquisition."""
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np

from tools.psvr_camera.calibration import Intrinsics
from tools.psvr_camera.constellation_led_pose import ConstellationLedTracker
from tools.psvr_camera.imu_led_pose import load_rotation_rig
from tools.psvr_camera.coherent_imu_led_pose import CoherentImuLedTracker
from tools.psvr_camera.led_pose import LedPose


class ConstellationTests(unittest.TestCase):
    def setUp(self):
        try:
            import cv2
        except ImportError:
            self.skipTest('OpenCV unavailable')
        self.cv2 = cv2
        rig, points, normals = load_rotation_rig(Path(__file__).resolve().parents[1]/'configurations/psvr_nine_leds_rotation_trial.json')
        intrinsics = Intrinsics(np.array([[500., 0, 320], [0, 500., 240], [0, 0, 1]]), np.zeros(5), (640, 480), True)
        self.tracker = ConstellationLedTracker(intrinsics, points, normals, rig['labels'], allow_two_led=False)
        self.translation = np.array([[0.], [0.], [1.]])
        for index in range(4):
            pose = self.frame(1+index*.02)
        self.assertTrue(pose.valid, pose.reason)

    def frame(self, now, angle=0., clutter=False, bias=0., epoch=0, session='session'):
        self.tracker.set_imu(dict(valid=True, state='tracking', monotonic_s=now-.001,
                                 quaternion_sensor_wxyz=[np.cos((angle+bias)/2), 0., -np.sin((angle+bias)/2), 0.],
                                 session_id=session, recenter_epoch=epoch, recenter_count=epoch))
        rotation = self.cv2.Rodrigues(np.array([0., angle, 0.]))[0]
        ids, projected = self.tracker.predicted(rotation, self.translation)
        candidates = [dict(center_px=projected[i].tolist()) for i in ids[::-1]]
        if clutter:
            candidates.extend([dict(center_px=[20., 20.]), dict(center_px=[620., 460.])])
        return self.tracker.update(candidates, now)

    def test_both_lateral_patterns_with_clutter_and_unchanged_world_basis(self):
        alignment = self.tracker.alignment.copy()
        sequence = list(range(5, 91, 5))+list(range(85, -91, -5))+list(range(-85, 1, 5))
        for index, degrees in enumerate(sequence):
            pose = self.frame(1.1+index*.02, angle=np.deg2rad(degrees), clutter=True)
            self.assertTrue(pose.valid, '%s: %s' % (degrees, pose.reason))
            self.assertGreaterEqual(len(pose.led_labels), 3)
            np.testing.assert_allclose(pose.tvec, self.translation, atol=1e-5)
        np.testing.assert_array_equal(self.tracker.alignment, alignment)

    def test_outlier_does_not_veto_supported_front_subset_or_mutate_prior(self):
        image = self.cv2.projectPoints(self.tracker.points[:5], np.zeros(3), self.translation,
                                       self.tracker.k, self.tracker.dist)[0].reshape(-1, 2)
        image[0] += [10., -4.]
        before = self.tracker.translation.copy()
        match, reason = self.tracker._recognize(np.eye(3), image, np.arange(5), before, 1.08)
        self.assertIsNotNone(match, reason)
        self.assertGreaterEqual(len(match[4]), 3)
        self.assertNotIn(0, match[4])
        np.testing.assert_allclose(match[3], self.translation, atol=1e-5)
        np.testing.assert_array_equal(self.tracker.translation, before)
        self.assertEqual(self.tracker.last_match, 1.06)

    def test_front_reacquisition_with_extra_lights_does_not_enumerate_120_orders(self):
        self.tracker.front.reset()
        with patch.object(self.tracker.front, 'solve', wraps=self.tracker.front.solve) as solve:
            pose = self.frame(1.08, clutter=True)
            self.assertTrue(pose.valid, pose.reason)
            self.assertEqual(solve.call_count, 1)
        self.assertEqual(self.tracker.front.confirmations, 1)

    def test_pair_remains_experimental_and_rejection_does_not_poison_position(self):
        for index, degrees in enumerate(range(5, 171, 5)):
            self.frame(1.1+index*.02, angle=np.deg2rad(degrees))
        before = self.tracker.translation.copy()
        pose = self.frame(1.8, angle=np.pi)
        self.assertFalse(pose.valid)
        np.testing.assert_array_equal(self.tracker.translation, before)
        self.tracker.allow_two_led = True
        self.assertTrue(self.frame(1.82, angle=np.pi).valid)

    def test_gradual_drift_and_recenter_do_not_change_world_alignment(self):
        alignment = self.tracker.alignment.copy()
        for index in range(900):
            pose = self.frame(1.08+index*.04, bias=np.deg2rad(18)*index/899)
            self.assertTrue(pose.valid, pose.reason)
        self.frame(37.1, bias=np.deg2rad(18), epoch=1)
        np.testing.assert_array_equal(self.tracker.alignment, alignment)

    def test_stale_imu_never_publishes_a_constellation(self):
        image = self.cv2.projectPoints(self.tracker.points[:5], np.zeros(3), self.translation,
                                       self.tracker.k, self.tracker.dist)[0].reshape(-1, 2)
        before = self.tracker.translation.copy()
        pose = self.tracker.update([dict(center_px=p.tolist()) for p in image], 2.)
        self.assertFalse(pose.valid)
        np.testing.assert_array_equal(self.tracker.translation, before)

    def test_depth_ambiguity_cannot_be_overridden_by_legacy_fallback(self):
        with patch.object(self.tracker, '_recognize', return_value=(None, 'constellation_ambiguous_depth')):
            with patch.object(CoherentImuLedTracker, 'match_pattern') as legacy:
                match, reason = self.tracker.match_pattern(np.eye(3), np.zeros((4, 2)), np.arange(5), self.translation, 1.08)
                legacy.assert_not_called()
        self.assertIsNone(match)
        self.assertEqual(reason, 'constellation_ambiguous_depth')

    def _weak_front_outer(self, now, rms_px=2.2):
        """Drive the outer update with a controlled weak front pose."""
        self.tracker.set_imu(dict(valid=True, state='tracking', monotonic_s=now-.001,
                                 quaternion_sensor_wxyz=[1., 0., 0., 0.],
                                 session_id='session', recenter_epoch=0, recenter_count=0))
        before_t = self.tracker.translation.copy()
        before_v = self.tracker.velocity.copy()
        before_last = self.tracker.last_match
        before_optical = self.tracker.optical_correction.copy()
        before_correction_at = self.tracker.correction_at
        alignment = self.tracker.alignment.copy()
        after_t = before_t+np.array([[0.01], [0.], [0.]])
        after_v = before_v+np.array([[0.001], [0.], [0.]])
        after_optical = self.cv2.Rodrigues(np.array([0.01, 0., 0.]))[0] @ before_optical

        def fake_update(slf, candidates, now_inner):
            slf.translation = after_t.copy()
            slf.velocity = after_v.copy()
            slf.last_match = now_inner
            slf.optical_correction = after_optical.copy()
            slf.correction_at = now_inner
            return LedPose(True, 'imu_guided_front_anchor', np.zeros((5, 2)),
                           np.zeros((3, 1)), after_t.copy(), rms_px, 3.0, 100.0)

        candidates = [dict(center_px=[320., 240.]) for _ in range(5)]
        before = (before_t, before_v, before_last, before_optical,
                  before_correction_at, alignment)
        after = (after_t, after_v, after_optical)
        return candidates, fake_update, before, after

    def test_outer_update_ambiguity_rejects_weak_front_and_restores_prior(self):
        now = 1.08
        candidates, fake_update, before, after = self._weak_front_outer(now)
        before_t, before_v, before_last, _, _, alignment = before
        _, _, after_optical = after
        with patch.object(CoherentImuLedTracker, 'update', fake_update):
            with patch.object(self.tracker, '_recognize',
                              return_value=(None, 'constellation_ambiguous_depth')):
                pose = self.tracker.update(candidates, now)
        self.assertFalse(pose.valid)
        self.assertEqual(pose.reason, 'constellation_ambiguous_depth')
        np.testing.assert_allclose(self.tracker.translation, before_t)
        np.testing.assert_allclose(self.tracker.velocity, before_v)
        self.assertEqual(self.tracker.last_match, before_last)
        # The valid optical correction converged by super().update is kept.
        np.testing.assert_allclose(self.tracker.optical_correction, after_optical)
        self.assertEqual(self.tracker.correction_at, now)
        np.testing.assert_array_equal(self.tracker.alignment, alignment)

    def test_outer_update_no_verified_pattern_keeps_weak_front(self):
        now = 1.08
        candidates, fake_update, _, after = self._weak_front_outer(now)
        after_t, after_v, after_optical = after
        with patch.object(CoherentImuLedTracker, 'update', fake_update):
            with patch.object(self.tracker, '_recognize',
                              return_value=(None, 'constellation_no_verified_pattern')):
                pose = self.tracker.update(candidates, now)
        self.assertTrue(pose.valid)
        self.assertEqual(pose.reason, 'imu_guided_front_anchor')
        self.assertGreaterEqual(self.tracker.minimum_matched_leds(), 3)
        np.testing.assert_allclose(self.tracker.translation, after_t)
        np.testing.assert_allclose(self.tracker.velocity, after_v)
        self.assertEqual(self.tracker.last_match, now)
        np.testing.assert_allclose(self.tracker.optical_correction, after_optical)

    def test_outer_update_accepted_alternative_publishes_constellation(self):
        now = 1.08
        candidates, fake_update, before, _ = self._weak_front_outer(now)
        before_t = before[0]
        matched_translation = before_t+np.array([[0.005], [0.], [0.]])
        ids = np.array([0, 2, 3, 5])
        image = np.zeros((4, 2))

        def fake_recognize(rotation, centers, visible, forecast, now_inner, front_pose=None):
            self.tracker.constellation = 'diamond_left'
            return (4, -5.0, 1.0, matched_translation.copy(), ids, image, 3.0), None

        with patch.object(CoherentImuLedTracker, 'update', fake_update):
            with patch.object(self.tracker, '_recognize', side_effect=fake_recognize):
                pose = self.tracker.update(candidates, now)
        self.assertTrue(pose.valid)
        self.assertEqual(pose.reason, 'imu_guided_constellation')
        self.assertEqual(self.tracker.constellation, 'diamond_left')
        self.assertGreaterEqual(len(pose.led_labels), 3)
        np.testing.assert_allclose(self.tracker.translation, matched_translation)
        self.assertEqual(self.tracker.last_match, now)

    def test_outer_update_strong_front_fast_path_ignores_later_ambiguity(self):
        now = 1.08
        candidates, fake_update, _, after = self._weak_front_outer(now, rms_px=1.5)
        after_t, _, _ = after
        with patch.object(CoherentImuLedTracker, 'update', fake_update):
            with patch.object(self.tracker, '_recognize',
                              return_value=(None, 'constellation_ambiguous_depth')) as recognize:
                pose = self.tracker.update(candidates, now)
                recognize.assert_not_called()
        self.assertTrue(pose.valid)
        self.assertEqual(pose.reason, 'imu_guided_front_anchor')
        self.assertEqual(self.tracker.constellation, 'front')
        np.testing.assert_allclose(self.tracker.translation, after_t)
        self.assertEqual(self.tracker.last_match, now)


if __name__ == '__main__':
    unittest.main()
