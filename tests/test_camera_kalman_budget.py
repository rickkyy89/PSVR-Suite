"""Degraded publication policy, optical-axis inflation and unchanged v2 wire."""
import copy
from pathlib import Path
import socket
import json
import unittest

import numpy as np

from tools.psvr_camera.driver_position import DriverPositionSender, validate_driver_position_packet
from tools.psvr_camera.geometry import rodrigues
from tools.psvr_camera.pose_uncertainty import inflate_optical_covariance
from tools.psvr_camera.position_pipeline import PositionPipeline, load_position_config


def sample(i, valid=True):
    now = 1 + i / 75
    return dict(protocol='psvr.position', version=2, frame='psvr_imu_unrecentered', units='m',
        session_id='budget', recenter_epoch=1, imu_seq=i, imu_sample_time_s=now,
        seq=i, monotonic_s=now, host_time_ns=0, valid=valid,
        state='tracking' if valid else 'lost', source='blob_led',
        position_m=[0., 0., 1.] if valid else None)


class BudgetAndInflationTests(unittest.TestCase):
    def test_internal_prediction_cannot_rearm_driver_hold(self):
        pipe = PositionPipeline('kalman', prediction_output='driver_hold')
        r = np.eye(3) * .003**2
        last_optical_time = None
        rows = []
        for i in range(16):
            pkt = sample(i, valid=i < 8)
            out = pipe.filter_packet(pkt, np.eye(3), pkt['monotonic_s'], covariance=r if i < 8 else None,
                quality={'reliable': True}, prediction_allowed=True, context_valid=True)
            if out['valid']:
                last_optical_time = out['monotonic_s']
            rows.append((out, pipe.diagnostics(), last_optical_time))
            validate_driver_position_packet(out)
            for key in ('session_id', 'recenter_epoch', 'imu_seq', 'imu_sample_time_s', 'seq', 'monotonic_s', 'host_time_ns'):
                self.assertEqual(out[key], pkt[key])
        self.assertTrue(rows[8][1]['estimator_valid'])
        self.assertFalse(rows[8][0]['valid'])
        self.assertIsNone(rows[8][0]['position_m'])
        self.assertTrue(all(not row[0]['valid'] for row in rows[8:]))
        self.assertEqual(rows[-1][2], sample(7)['monotonic_s'])
        # PositionBridge's existing grace tests prove invalid cannot renew this
        # last-good clock. Even with fresh IMU, its 80ms hold is now exhausted.
        self.assertGreater(rows[14][0]['monotonic_s'] - last_optical_time, .08)
        self.assertFalse(rows[-1][1]['estimator_valid'])

    def test_camera_policy_remains_original_baseline(self):
        pipe = PositionPipeline('kalman')
        for i in range(9):
            pkt = sample(i, i < 8)
            # B6-05: the optical-dropout prediction here needs an explicit
            # valid IMU context, as production callers always provide.
            out = pipe.filter_packet(pkt, np.eye(3), pkt['monotonic_s'],
                covariance=np.eye(3)*.003**2 if i < 8 else None,
                quality={'reliable': True}, prediction_allowed=True,
                context_valid=True)
        self.assertTrue(out['valid'])
        self.assertEqual(pipe.diagnostics()['prediction_output'], 'camera')

    def test_psd_inflation_follows_camera_axis_and_preserves_transverse_variance(self):
        a = rodrigues([.3, .7, -.2])
        r = a.T @ np.diag([.002**2, .003**2, .006**2]) @ a
        original = r.copy()
        inflated = inflate_optical_covariance(r, a, 2)
        u = a.T @ [0., 0., 1.]
        transverse = a.T @ [1., 0., 0.]
        self.assertAlmostEqual(u @ inflated @ u, 2*(u @ r @ u))
        self.assertAlmostEqual(transverse @ inflated @ transverse, transverse @ r @ transverse)
        self.assertGreaterEqual(np.linalg.eigvalsh(inflated-r)[0], -1e-18)
        np.testing.assert_equal(r, original)
        self.assertIsNone(inflate_optical_covariance(None, a, 2))
        np.testing.assert_equal(inflate_optical_covariance(r, None), r)

    def test_inflated_uncertainty_cannot_initialize_with_old_reliability_flag(self):
        pipe = PositionPipeline('kalman', uncertainty={'depth_variance_factor': 4})
        r = np.diag([.005**2, .005**2, .035**2])
        for i in range(10):
            out = pipe.filter_packet(sample(i), np.eye(3), 1+i/75, covariance=r,
                quality={'reliable': True, 'led_count': 5}, prediction_allowed=True)
            self.assertFalse(out['valid'])
        self.assertFalse(pipe.diagnostics()['quality']['reliable'])

    def test_invalid_imu_and_two_leds_remain_invalid_wire(self):
        pipe = PositionPipeline('kalman', prediction_output='driver_hold')
        for i in range(8):
            pipe.filter_packet(sample(i), np.eye(3), 1+i/75, covariance=np.eye(3)*.003**2,
                quality={'reliable': True}, context_valid=True)
        out = pipe.filter_packet(sample(8), np.eye(3), 1+8/75, covariance=None,
            quality={'reliable': False, 'led_count': 2}, prediction_allowed=True, context_valid=True)
        self.assertFalse(out['valid'])
        self.assertTrue(pipe.diagnostics()['estimator_valid'])
        out = pipe.filter_packet(sample(9, False), np.eye(3), 1+9/75, covariance=None,
            prediction_allowed=False, context_valid=False)
        self.assertFalse(out['valid'])
        self.assertFalse(pipe.diagnostics()['estimator_valid'])

    def test_live_udp_keeps_bound_packet_small_and_prediction_invalid(self):
        receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        receiver.bind(('127.0.0.1', 0))
        receiver.settimeout(1)
        sender = DriverPositionSender(port=receiver.getsockname()[1])
        pipe = PositionPipeline('kalman', prediction_output='driver_hold')
        try:
            for i in range(9):
                pkt = sample(i, i < 8)
                before = copy.deepcopy(pkt)
                out = pipe.filter_packet(pkt, np.eye(3), pkt['monotonic_s'],
                    covariance=np.eye(3)*.003**2 if i < 8 else None,
                    quality={'reliable': True}, prediction_allowed=True)
                self.assertTrue(sender.send(out))
                payload, _ = receiver.recvfrom(2048)
                self.assertLessEqual(len(payload), 1024)
                received = json.loads(payload)
                validate_driver_position_packet(received)
                self.assertEqual(pkt, before)
            self.assertFalse(received['valid'])
        finally:
            sender.close()
            receiver.close()

    def test_optimized_configuration_and_rollback_flags(self):
        root = Path(__file__).resolve().parents[1]
        optimized = load_position_config(root/'configurations/position_kalman_optimized_20261013.json')
        baseline = load_position_config(root/'configurations/position_kalman_experimental.json')
        self.assertTrue(optimized['kalman']['adaptive_quiet'])
        self.assertTrue(optimized['kalman']['recovery_velocity_fit'])
        self.assertEqual(optimized['prediction_output'], 'driver_hold')
        self.assertNotIn('adaptive_quiet', baseline['kalman'])
        for cfg in ({'depth_variance_factor': .9}, {'depth_extra_std_m': -1}):
            with self.assertRaises(ValueError):
                PositionPipeline('kalman', uncertainty=cfg)

    def test_missing_alignment_fails_closed_without_crashing_or_fabricating_covariance(self):
        pipe = PositionPipeline('kalman', uncertainty={'depth_variance_factor':2})
        out = pipe.filter_packet(sample(0), None, 1., covariance=np.eye(3)*.003**2,
                                 quality={'reliable':True}, prediction_allowed=True, context_valid=True)
        self.assertFalse(out['valid'])
        self.assertFalse(pipe.diagnostics()['estimator_valid'])
        self.assertEqual(pipe.diagnostics()['quality']['reason'], 'invalid_alignment')


if __name__ == '__main__':
    unittest.main()
