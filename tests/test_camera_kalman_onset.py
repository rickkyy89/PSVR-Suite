"""Fixed opt-in transition Q pulse; matched synthetic data, never hardware GT."""
import hashlib
import json

import numpy as np
try:
    import pytest
except ImportError:
    from unittest import SkipTest
    raise SkipTest('Transition Q tests require pytest')

from tools.psvr_camera.pose_estimator import PoseEstimator, PoseEstimatorConfig, _mahalanobis
from tools.psvr_camera.kalman_optimization_benchmark import make_scenarios


R = np.diag([.0015**2, .0015**2, 2 * .005**2])
DT = 1 / 75
BASE = dict(acceleration_std_m_s2=3., adaptive_quiet=True,
            quiet_acceleration_std=.03, measurement_speed_noise_allowance_m=.015,
            recovery_velocity_fit=True)


def quiet_estimator(pulse=5.):
    e = PoseEstimator(PoseEstimatorConfig(**BASE, motion_onset_acceleration_std=pulse))
    for i in range(100):
        result = e.update([0, 0, 1], R, 1 + i * DT)
    assert result['quiet_active'] and result['process_mode'] == 'quiet'
    return e


def predict_independently(diag, root, dt=DT):
    x = np.r_[diag['position_m'], diag['velocity_m_s']]
    p = np.array(diag['covariance'])
    f = np.eye(6)
    f[:3, 3:] = dt * np.eye(3)
    q = root**2 * np.kron([[dt**3 / 3, dt**2 / 2], [dt**2 / 2, dt]], np.eye(3))
    p = f @ p @ f.T + q
    return f @ x, (p + p.T) * .5


def onset_sample(e, pulse=5.):
    before = e.diagnostics()
    x3, p3 = predict_independently(before, 3.)
    _, p5 = predict_independently(before, pulse)
    # A sample rejected by the existing root3 gate but allowed by the fixed
    # alternative. Quiet NIS is stronger, so the existing motion proposal exits.
    delta = (np.sqrt(16.27 * (p3[2, 2] + R[2, 2]))
             + np.sqrt(16.27 * (p5[2, 2] + R[2, 2]))) / 2
    z = x3[:3] + [0, 0, delta]
    return before, z


@pytest.mark.parametrize('value', [-1., 0., np.nan, np.inf, -np.inf, True, False, '5'])
def test_onset_config_rejects_invalid(value):
    with pytest.raises(ValueError):
        PoseEstimatorConfig(motion_onset_acceleration_std=value)


def test_none_and_positive_config_values():
    assert PoseEstimatorConfig().motion_onset_acceleration_std is None
    assert PoseEstimatorConfig(motion_onset_acceleration_std=5.).motion_onset_acceleration_std == 5.


def test_default_none_matches_frozen_pre_pulse_profile_bitwise():
    # Captured before the pulse implementation: core results, not settings
    # dictionaries (the latter necessarily acquire a new optional config key).
    data = make_scenarios()
    outputs = []
    for name in ('stationary', 'depth', 'rapid', 'dropout'):
        # The captured configuration used integer 3: JSON preserves 3 vs 3.0.
        e = PoseEstimator(PoseEstimatorConfig(**{**BASE, 'acceleration_std_m_s2': 3}))
        for row in data[name]['inputs']:
            result = e.update(row['packet']['position_m'], R, row['monotonic_s'],
                              allow_prediction=True, reliable=True)
            # These two added diagnostics expose the pre-switch S, not a state
            # change. Preserve the frozen digest for every pre-existing field.
            result.pop('quiet_probe_innovation_covariance')
            result.pop('quiet_probe_mahalanobis_sq')
            outputs.append(result)
    digest = hashlib.sha256(json.dumps(outputs, sort_keys=True, allow_nan=False,
                                      separators=(',', ':')).encode()).hexdigest()
    assert digest == 'b8f474d5fabe34cfcdd7a1133e5351bc8710663fd93fb5a722c5965799388f65'


def test_same_frame_pulse_uses_saved_prior_once_and_returns_motion_root_next_frame(monkeypatch):
    e = quiet_estimator()
    before, z = onset_sample(e)
    # Count prediction computations, excluding subsequent independent checks.
    calls = []
    original = e._prediction_from
    def capture(x, p, dt, root):
        calls.append((x.copy(), p.copy(), root))
        return original(x, p, dt, root)
    monkeypatch.setattr(e, '_prediction_from', capture)
    now = before['timestamp'] + DT
    result = e.update(z, R, now, allow_prediction=True)
    assert result['valid'] and result['rejection_reason'] is None, result
    assert not result['quiet_active'] and result['process_mode'] == 'motion'
    assert result['effective_acceleration_std'] == 5 and result['effective_q'] == 25
    assert [call[2] for call in calls] == [.03, 5.]
    # Both proposals originate at the SAME posterior, not a double prediction.
    np.testing.assert_array_equal(calls[0][0], calls[1][0])
    np.testing.assert_array_equal(calls[0][1], calls[1][1])
    x, p = predict_independently(before, 5., now - before['timestamp'])
    innovation = z - x[:3]
    s = p[:3, :3] + R
    k = np.linalg.solve(s, p[:, :3].T).T
    a = np.eye(6)
    a[:, :3] -= k
    expected_p = a @ p @ a.T + k @ R @ k.T
    np.testing.assert_allclose(result['position_m'], (x + k @ innovation)[:3], atol=1e-14)
    np.testing.assert_allclose(result['covariance'], expected_p, atol=1e-14)
    np.testing.assert_allclose(result['innovation_covariance'], s, atol=1e-14)
    np.testing.assert_allclose(result['motion_gate_covariance'], s, atol=1e-14)
    assert result['motion_gate_acceleration_std'] == 5.
    assert result['nis_3d'] == pytest.approx(_mahalanobis(innovation, s))
    np.linalg.cholesky(np.array(result['covariance']))
    calls.clear()
    following = e.update(z, R, now + DT, allow_prediction=True)
    assert following['valid'], following
    assert following['effective_acceleration_std'] == 3 and following['effective_q'] == 9
    assert [call[2] for call in calls] == [3.]
    assert following['motion_gate_covariance'] is None


def test_none_gate_rejects_same_onset_that_fixed_pulse_accepts():
    e = quiet_estimator(None)
    before, z = onset_sample(e)
    result = e.update(z, R, before['timestamp'] + DT, allow_prediction=True)
    assert result['rejection_reason'] == 'mahalanobis_gate'
    assert result['effective_acceleration_std'] == .03


@pytest.mark.parametrize('pulse', [5., 8.])
def test_500mm_teleport_never_admitted_or_learned(pulse):
    e, reference = quiet_estimator(pulse), quiet_estimator(pulse)
    before = e.diagnostics()
    now = before['timestamp'] + DT
    result = e.update([.5, 0, 1], R, now, allow_prediction=True, reliable=False)
    predicted = reference.update(None, None, now, allow_prediction=True)
    assert result['rejection_reason'] == 'implied_speed'
    assert result['accepted'] == before['accepted']
    # Unreliable inputs use ordinary motion Q and cannot learn/recover.
    x, p = predict_independently(before, 3., now - before['timestamp'])
    np.testing.assert_allclose(result['position_m'], x[:3], atol=1e-14)
    np.testing.assert_allclose(result['covariance'], p, atol=1e-14)
    assert result['recovery_count'] == 0
    assert result['quiet_sample_count'] == 0  # Rejection clears policy history.
    assert result['quiet_coherent_statistic'] is None
    assert predicted['accepted'] == result['accepted']


def test_reliable_teleport_retains_quiet_prediction_not_alternative():
    e = quiet_estimator(8.)
    before = e.diagnostics()
    now = before['timestamp'] + DT
    result = e.update([0, 0, 1.5], R, now, allow_prediction=True)
    x, p = predict_independently(before, .03, now - before['timestamp'])
    assert result['rejection_reason'] == 'implied_speed'
    np.testing.assert_allclose(result['position_m'], x[:3], atol=1e-14)
    np.testing.assert_allclose(result['covariance'], p, atol=1e-14)
    assert result['effective_acceleration_std'] == .03
    assert result['motion_gate_acceleration_std'] == 8
    assert result['quiet_sample_count'] == 0
    assert result['accepted'] == before['accepted']
    actual_s = p[:3, :3] + R
    np.testing.assert_allclose(result['innovation_covariance'], actual_s, atol=1e-14)
    assert result['nis_3d'] == pytest.approx(_mahalanobis(np.array([0, 0, 1.5]) - x[:3], actual_s))


def test_posterior_rejection_rolls_back_pulse_and_does_not_train(monkeypatch):
    e = quiet_estimator()
    before, z = onset_sample(e)
    now = before['timestamp'] + DT
    # Exercise the final publication safety rejection independently of raw/NIS.
    monkeypatch.setattr(e, '_publication_speed_safe', lambda speed: speed is None or speed < .001)
    result = e.update(z, R, now, allow_prediction=True)
    assert result['rejection_reason'] == 'posterior_position_step'
    x, p = predict_independently(before, .03, now - before['timestamp'])
    np.testing.assert_allclose(result['position_m'], x[:3], atol=1e-14)
    np.testing.assert_allclose(result['covariance'], p, atol=1e-14)
    assert result['effective_acceleration_std'] == .03
    assert result['accepted'] == before['accepted']
    assert result['quiet_sample_count'] == 0 and result['quiet_coherent_statistic'] is None
    assert result['motion_gate_acceleration_std'] == 5.
    actual_s = p[:3, :3] + R
    np.testing.assert_allclose(result['innovation_covariance'], actual_s, atol=1e-14)
    assert not np.allclose(actual_s, result['motion_gate_covariance'], atol=1e-12)
    assert result['nis_3d'] == pytest.approx(_mahalanobis(z - x[:3], actual_s))


def test_quiet_prediction_and_low_noise_profile_not_replaced_by_gate_pulse():
    normal, pulse = quiet_estimator(None), quiet_estimator(8.)
    now = normal.diagnostics()['timestamp']
    for i in range(100):
        now += DT
        z = [0., 0., 1. + .0001 * np.sin(i)]
        a = normal.update(z, R, now)
        b = pulse.update(z, R, now)
        assert a['quiet_active'] and b['quiet_active']
        for key in a:
            # Only the separately logged alternative gate statistic differs.
            if key != 'baseline_motion_mahalanobis_sq':
                assert a[key] == b[key], key
        assert b['effective_acceleration_std'] == .03
        assert b['motion_gate_acceleration_std'] == 8


def test_pulse_unused_when_adaptivity_is_off():
    a = PoseEstimator(PoseEstimatorConfig(recovery_velocity_fit=True))
    b = PoseEstimator(PoseEstimatorConfig(recovery_velocity_fit=True, motion_onset_acceleration_std=8.))
    for i in range(100):
        z = [0, 0, 1 + .1 * i * DT]
        x = a.update(z, R, 1 + i * DT)
        y = b.update(z, R, 1 + i * DT)
        assert all(x[key] == y[key] for key in x)
        assert y['motion_gate_covariance'] is None
        assert y['effective_acceleration_std'] == 3.
