"""State safety of experimental partial hypotheses; no hardware or production edits."""
from collections import Counter
import unittest
from unittest.mock import patch

import numpy as np

from scripts.lateral_handoff_candidates import GuidedFallbackTracker
from tools.psvr_camera.imu_led_pose import ImuLedTracker
from tools.psvr_camera.led_pose import LedPose


class HandoffStateTests(unittest.TestCase):
    def tracker(self):
        tracker = GuidedFallbackTracker.__new__(GuidedFallbackTracker)
        tracker.translation = np.array([[0.], [0.], [1.]])
        tracker.last_match = 1.
        tracker.velocity = np.zeros((3, 1))
        tracker.experiment_counts = Counter()
        return tracker

    def proposal(self, tracker, labels):
        tracker.translation = np.array([[.1], [.2], [.8]])
        tracker.last_match = 1.01
        tracker.velocity = np.ones((3, 1))
        pose = LedPose(True, 'imu_guided_pattern')
        pose.led_labels = labels
        return pose

    def test_rejected_pair_restores_all_motion_state(self):
        tracker = self.tracker()
        with patch.object(ImuLedTracker, 'update', side_effect=lambda instance, c, t: self.proposal(tracker, ['centre', 'side_left'])):
            self.assertIsNone(tracker.try_guided([], 1.01))
        np.testing.assert_array_equal(tracker.translation, [[0.], [0.], [1.]])
        np.testing.assert_array_equal(tracker.velocity, np.zeros((3, 1)))
        self.assertEqual(tracker.last_match, 1.)

    def test_verified_triple_keeps_published_motion_state(self):
        tracker = self.tracker()
        with patch.object(ImuLedTracker, 'update', side_effect=lambda instance, c, t: self.proposal(tracker, ['centre', 'upper_left', 'side_left'])):
            pose = tracker.try_guided([], 1.01)
        self.assertTrue(pose.valid)
        np.testing.assert_array_equal(tracker.translation, [[.1], [.2], [.8]])
        self.assertEqual(tracker.last_match, 1.01)

    def test_lost_prior_is_not_reacquired_twice_in_one_frame(self):
        tracker = self.tracker()
        with patch.object(ImuLedTracker, 'update') as update:
            self.assertIsNone(tracker.try_guided([], 4.))
            update.assert_not_called()


if __name__ == '__main__':
    unittest.main()
