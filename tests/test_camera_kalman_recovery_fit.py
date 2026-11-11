"""Opt-in recovery fit: synthetic truth, not camera/hardware acceptance."""
from dataclasses import replace
import json

import numpy as np
try:
    import pytest
except ImportError:
    from unittest import SkipTest
    raise SkipTest('Recovery GLS tests require pytest')

from tools.psvr_camera.pose_estimator import PoseEstimator, PoseEstimatorConfig, _mahalanobis
from tools.psvr_camera.kalman_optimization_benchmark import make_scenarios, run_variant


R = np.diag(np.square([.005, .005, .005]))


def config(**changes):
    return replace(PoseEstimatorConfig(recovery_velocity_fit=True,
                   measurement_speed_noise_allowance_m=.015), **changes)


def seed(e, position=(0., 0., 1.), start=1.):
    for i in range(4):
        result = e.update(position, R, start + i / 75)
    assert result['valid'], result
    return result


def test_opt_in_boolean_and_minimum_four_confirmations():
    assert not PoseEstimatorConfig().recovery_velocity_fit
    for value in (0, 1, None, 'true'):
        with pytest.raises(ValueError):
            PoseEstimatorConfig(recovery_velocity_fit=value)
    e = PoseEstimator(config(recovery_confirmations=2))
    for i in range(4):
        result = e.update([0, 0, 1 + 1.7 * i / 75], R, 1 + i / 75)
        assert result['valid'] == (i == 3)
    np.testing.assert_allclose(result['velocity_m_s'], [0, 0, 1.7], atol=1e-12)


def test_joint_full_r_gls_endpoint_covariance_matches_independent_inverse():
    e = PoseEstimator(config())
    a = np.array([[1., .3, -.1], [0, .8, .2], [0, 0, 1.2]])
    full_r = a @ R @ a.T
    times = np.array([1., 1.011, 1.026, 1.044])
    velocity = np.array([.2, -.1, 1.7])
    endpoint = np.array([.3, -.2, 1.2])
    samples = [(t, endpoint + (t - times[-1]) * velocity, full_r * (1 + i / 5))
               for i, t in enumerate(times)]
    for t, z, r in samples:
        result = e.update(z, r, t)
    assert result['valid'], result
    p = np.array(result['covariance'])
    np.linalg.cholesky(p)
    assert np.linalg.norm(p[:3, 3:]) > 1e-5
    np.testing.assert_allclose(result['position_m'], endpoint, atol=1e-12)
    np.testing.assert_allclose(result['velocity_m_s'], velocity, atol=1e-12)
    # Independent numerical integration of the acceleration kernels.
    d = times[-1] - times
    obs = np.zeros((12, 12))
    for i in range(4):
        for j in range(4):
            upper = min(d[i], d[j])
            integration = 0 if upper == 0 else np.trapezoid(
                (d[i] - np.linspace(0, upper, 2001)) *
                (d[j] - np.linspace(0, upper, 2001)), np.linspace(0, upper, 2001))
            obs[3*i:3*i+3, 3*j:3*j+3] = 9 * integration * np.eye(3)
        obs[3*i:3*i+3, 3*i:3*i+3] += samples[i][2]
    h = np.vstack([np.c_[np.eye(3), -dt * np.eye(3)] for dt in d])
    expected = np.linalg.inv(h.T @ np.linalg.solve(obs, h))
    np.testing.assert_allclose(p, expected, rtol=1e-6, atol=1e-10)
    assert result['seed_method'] == 'full_r_cv_gls'
    assert result['seed_fit_quality']['rank'] == 6
    assert result['seed_jump_m'] is None
    json.dumps(result, allow_nan=False)


def test_second_difference_process_covariance_and_noise_allowance(monkeypatch):
    e = PoseEstimator(config(measurement_speed_noise_allowance_m=.1))
    covariances = []
    def capture(residual, covariance):
        covariances.append(covariance.copy())
        return _mahalanobis(residual, covariance)
    monkeypatch.setattr('tools.psvr_camera.pose_estimator._mahalanobis', capture)
    e.update([0, 0, 1], R, 1.)
    e.update([0, 0, 1.02], R, 1.01)
    e.update([0, 0, 1.06], R, 1.03)
    expected = R + 9 * R + 4 * R + np.eye(3) * 9 * .02**2 * (.01 + .02) / 3
    np.testing.assert_allclose(covariances[-1], expected, rtol=1e-12)
    # Even a configured allowance >15mm may not hide a candidate teleport.
    result = e.update([0, 0, 1.12], R, 1.04)
    assert result['recovery_count'] == 1 and not result['valid']


def test_fast_motion_with_noise_has_nonzero_safe_seed():
    rng = np.random.default_rng(623)
    e = PoseEstimator(config())
    first = None
    for i in range(150):
        raw = np.array([0, 0, 1 + 1.7 * i / 75]) + rng.normal(0, .005, 3)
        result = e.update(raw, R, 1 + i / 75, allow_prediction=True)
        if result['valid']:
            np.linalg.cholesky(np.array(result['covariance']))
            assert np.linalg.norm(result['velocity_m_s']) <= 2
            if first is None:
                first = result
    assert first is not None
    assert first['velocity_m_s'][2] > 1
    assert first['seed_fit_quality']['accepted']


def test_direction_change_breaks_candidate_then_fits_new_direction():
    e = PoseEstimator(config())
    r = np.eye(3) * .0001**2
    # An abrupt reversal is inconsistent with this precise candidate cluster.
    z_values = [1., 1.022, 1.044, 1.022, 1., .978, .956]
    for i, z in enumerate(z_values):
        result = e.update([0, 0, z], r, 1 + i / 75)
        if i == 3:
            assert result['recovery_count'] == 1 and not result['valid']
    assert result['valid'], result
    np.testing.assert_allclose(result['velocity_m_s'], [0, 0, -1.65], atol=1e-10)


def test_candidate_outlier_never_corrects_tracking_or_trains_quiet():
    c = config(adaptive_quiet=True)
    e, reference = PoseEstimator(c), PoseEstimator(c)
    seed(e)
    seed(reference)
    now = 1 + 4 / 75
    rejected = e.update([.7, 0, 1], R, now, allow_prediction=True)
    predicted = reference.update(None, None, now, allow_prediction=True)
    assert rejected['rejection_reason'] == 'implied_speed'
    for field in ('position_m', 'velocity_m_s', 'covariance', 'accepted', 'quiet_sample_count'):
        assert rejected[field] == predicted[field]
    result = e.update([-.7, 0, 1], R, now + 1 / 75, allow_prediction=True)
    assert result['recovery_count'] == 1
    assert result['accepted'] == 1


def test_reseed_still_requires_immediately_preceding_invalid_and_logs_jump():
    e = PoseEstimator(config())
    seed(e)
    outputs = [e.update([1, 0, 1], R, 1 + (4 + i) / 75, allow_prediction=True)
               for i in range(5)]
    assert all(o['valid'] and o['tracking_state'] == 'predicting' for o in outputs[:3])
    assert not outputs[3]['valid']
    assert outputs[4]['valid'] and outputs[4]['publication_continuity'] == 'new_segment'
    assert outputs[4]['seed_jump_m'] == pytest.approx(1.)
    assert outputs[4]['seed_fit_quality']['sample_count'] == 4
    np.testing.assert_allclose(outputs[4]['velocity_m_s'], 0, atol=1e-12)
    outputs[4]['seed_fit_quality']['accepted'] = False
    assert e.diagnostics()['seed_fit_quality']['accepted']


def test_failed_fit_never_clamps_or_falls_back_to_zero():
    e = PoseEstimator(config())
    # Pairwise motion is permitted by the bounded noise allowance, but the
    # precise coherent fit exceeds the physical velocity limit.
    for i in range(12):
        result = e.update([0, 0, 1 + 2.2 * i / 75], R, 1 + i / 75)
        assert not result['valid'] and result['accepted'] == 0
        assert result['velocity_m_s'] is None
        if i == 3:
            assert result['recovery_fit']['reason'] == 'fit_speed'
            assert result['recovery_count'] == 1
    samples = [(1 + i / 75, np.array([(-1)**i * .15, 0, 1]), R) for i in range(4)]
    x, p, diagnostics = e._recovery_fit(samples)
    assert x is None and p is None and diagnostics['reason'] == 'fit_residual'


@pytest.mark.parametrize('boundary', ['missing', 'unreliable', 'uncertain', 'identity', 'clock', 'gap'])
def test_boundaries_clear_pending_cluster(boundary):
    e = PoseEstimator(config())
    for i in range(3):
        e.update([0, 0, 1], R, 1 + i / 75, identity='a')
    now = 1 + 3 / 75
    kwargs = dict(identity='a')
    z, r = [0, 0, 1], R
    if boundary == 'missing':
        z = r = None
    elif boundary == 'unreliable':
        kwargs['reliable'] = False  # Includes 3-LED observations by caller contract.
    elif boundary == 'uncertain':
        r = np.eye(3) * .05**2
    elif boundary == 'identity':
        kwargs['identity'] = 'b'
    elif boundary == 'clock':
        now = 1.
    else:
        now += .4
    result = e.update(z, r, now, **kwargs)
    assert not result['valid'] and result['recovery_count'] <= 1
    assert result['accepted'] == 0


class LegacyRecovery(PoseEstimator):
    """Frozen pre-fit recovery arithmetic, including the old q*dt^3/3 gate."""
    def _confirm(self, z, r, timestamp, reliable):
        if not reliable or np.sqrt(np.linalg.eigvalsh(r)[-1]) > self.config.reliable_std_m:
            self._candidate = None
            return False
        count = 1
        velocity = previous_r_for_velocity = previous_dt = None
        if self._candidate is not None:
            previous, previous_r, previous_time, previous_count, candidate_velocity, older_r, older_dt = self._candidate
            dt = timestamp - previous_time
            delta = z - previous
            residual = delta
            if candidate_velocity is None:
                s = r + previous_r + np.eye(3) * (self.config.initial_velocity_std_m_s**2 * dt**2)
            else:
                residual = delta - candidate_velocity * dt
                ratio = dt / older_dt
                s = r + (1 + ratio)**2 * previous_r + ratio**2 * older_r
            s = s + np.eye(3) * self.config.acceleration_std_m_s2**2 * dt**3 / 3
            if (0 < dt <= self.config.max_prediction_s
                    and np.linalg.norm(delta) <= self.config.max_speed_m_s * dt
                    and _mahalanobis(residual, s) <= self.config.recovery_gate_chi2):
                count = previous_count + 1
                velocity = delta / dt
                previous_r_for_velocity, previous_dt = previous_r.copy(), dt
        self._candidate = (z.copy(), r.copy(), timestamp, count, velocity, previous_r_for_velocity, previous_dt)
        if count < self.config.recovery_confirmations or self._last['valid']:
            return False
        self._lose('confirmed_reacquisition' if self._accepted else 'confirmed_initialization')
        self._x = np.r_[z, np.zeros(3)]
        self._p = np.zeros((6, 6))
        self._p[:3, :3] = r
        self._p[3:, 3:] = np.eye(3) * self.config.initial_velocity_std_m_s**2
        self._last_measurement = z.copy()
        self._last_measurement_time = timestamp
        self._last_measurement_covariance = r.copy()
        self._candidate = None
        self._accepted += 1
        if self.config.adaptive_quiet:
            self._quiet_history = [(timestamp, z.copy(), r.copy(), self._x[3:].copy())]
        return True


@pytest.mark.parametrize('adaptive', [False, True])
def test_false_is_bitwise_legacy_at_75hz(adaptive):
    c = PoseEstimatorConfig(adaptive_quiet=adaptive, measurement_speed_noise_allowance_m=.015 if adaptive else 0)
    current, old = PoseEstimator(c), LegacyRecovery(c)
    data = make_scenarios()
    for name in ('stationary', 'noisy_fast_depth', 'depth', 'dropout'):
        current.reset()
        old.reset()
        for entry in data[name]['inputs']:
            z = entry['packet']['position_m']
            kwargs = dict(allow_prediction=True, reliable=entry['quality']['reliable'])
            a = current.update(z, entry['covariance'], entry['monotonic_s'], **kwargs)
            b = old.update(z, entry['covariance'], entry['monotonic_s'], **kwargs)
            assert a == b  # Exact floats and all diagnostic keys, not allclose.
            assert 'seed_method' not in a


def test_shared_noisy_fast_benchmark_spd_and_publication_limits():
    data = make_scenarios()['noisy_fast_depth']
    kwargs = dict(acceleration_std_m_s2=3., adaptive_quiet=True,
                  quiet_acceleration_std=.03, measurement_speed_noise_allowance_m=.015)
    old = run_variant(data, kalman=kwargs, uncertainty=2)
    fitted = run_variant(data, kalman={**kwargs, 'recovery_velocity_fit': True}, uncertainty=2)
    assert sum(row['wire_valid'] for row in fitted) > sum(row['wire_valid'] for row in old)
    for row in fitted:
        diag = row['diagnostics']
        if diag['covariance'] is not None:
            np.linalg.cholesky(np.array(diag['covariance']))
        if diag['published_speed_m_s'] is not None:
            assert diag['published_speed_m_s'] <= 2 + 1e-9


def test_three_leds_cannot_reinitialize_and_cluster_storage_is_bounded():
    data = make_scenarios()['partial_leds']
    rows = run_variant(data, kalman={'recovery_velocity_fit': True}, uncertainty=2)
    assert all(not row['estimator_valid'] for row in rows[30:60])
    assert rows[63]['estimator_valid']  # Four consecutive reliable 4-LED samples.
    e = PoseEstimator(config())
    seed(e)
    for i in range(4):
        e.update([1, 0, 1], R, 1 + (4 + i) / 75, allow_prediction=True)
        assert len(e._recovery_samples) <= 4
    result = e.update(None, None, 1 + 8 / 75, allow_prediction=True)
    assert result['recovery_count'] == 0 and e._recovery_samples == []
