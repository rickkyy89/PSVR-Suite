"""Opt-in quiet-Q policy contracts and deterministic synthetic metrics.

No hardware validation. Local synthetic tests and shared matched benchmark data
are distinguished explicitly. The baseline recurrence is checked bitwise.
"""
from dataclasses import replace
import math

import numpy as np
try:
    import pytest
except ImportError:
    from unittest import SkipTest
    raise SkipTest('Adaptive Kalman tests require pytest')

from tools.psvr_camera.pose_estimator import PoseEstimator, PoseEstimatorConfig


R = np.eye(3) * .005**2


def seeded(config=None, r=R):
    e = PoseEstimator(config)
    for i in range(4):
        e.update([0, 0, 1], r, i * .01)
    assert e.diagnostics()['valid']
    return e


def optimized(**changes):
    return replace(PoseEstimatorConfig(), acceleration_std_m_s2=1.5,
                   adaptive_quiet=True, measurement_speed_noise_allowance_m=.015,
                   **changes)


def quiet_estimator(r=R, config=None):
    e = seeded(optimized() if config is None else config, r)
    for i in range(1, 121):
        out = e.update([0, 0, 1], r, .03 + i * .02)
    assert out['quiet_active'] and out['process_mode'] == 'quiet'
    return e


def prediction(x, p, dt, root_q):
    f = np.eye(6)
    f[:3, 3:] = np.eye(3) * dt
    q = root_q**2 * np.kron([[dt**3 / 3, dt**2 / 2], [dt**2 / 2, dt]], np.eye(3))
    x = f @ x
    p = f @ p @ f.T + q
    return x, (p + p.T) * .5


def posterior(x, p, z, r):
    innovation = z - x[:3]
    s = p[:3, :3] + r
    k = np.linalg.solve(s, p[:, :3].T).T
    x = x + k @ innovation
    a = np.eye(6)
    a[:, :3] -= k
    p = a @ p @ a.T + k @ r @ k.T
    return x, (p + p.T) * .5, s


def assert_spd(out):
    p = np.array(out['covariance'])
    assert p.shape == (6, 6)
    np.testing.assert_array_equal(p, p.T)
    np.linalg.cholesky(p)
    if out['innovation_covariance'] is not None:
        s = np.array(out['innovation_covariance'])
        assert s.shape == (3, 3)
        np.linalg.cholesky(s)


def test_original_defaults_and_optional_policy_defaults():
    c = PoseEstimatorConfig()
    assert c.acceleration_std_m_s2 == 3.
    assert not c.adaptive_quiet and c.measurement_speed_noise_allowance_m == 0.
    assert (c.quiet_acceleration_std, c.quiet_enter_s, c.quiet_speed_enter_m_s,
            c.quiet_speed_exit_m_s, c.quiet_innovation_exit_chi2,
            c.quiet_trend_window_s, c.quiet_motion_sigma) == (.03, .25, .03, .08, 7.815, .12, 3.)
    assert c.quiet_coherent_exit_chi2 == 11.34


@pytest.mark.parametrize('root_q', [.5, 1., 1.5, 2., 3.])
def test_disabled_policy_is_bitwise_identical_to_legacy_recurrence(root_q):
    c = replace(PoseEstimatorConfig(), acceleration_std_m_s2=root_q)
    e = seeded(c)
    x, p = np.r_[[0, 0, 1], [0, 0, 0]], np.zeros((6, 6))
    p[:3, :3], p[3:, 3:] = R, np.eye(3)
    now = .03
    for i in range(120):
        previous = now
        now += [.007, .013, .021][i % 3]
        # Use the actual subtraction, not the nominal dt, as the old core did.
        x, p = prediction(x, p, now - previous, root_q)
        if i % 17 == 16:
            out = e.update(None, None, now, allow_prediction=True)
        else:
            z = np.array([.3 * (now - .03), -.1 * (now - .03), 1.])
            r = R * (1 + .2 * (i % 3))
            x, p, s = posterior(x, p, z, r)
            out = e.update(z, r, now, reliable=(i % 7 != 0))
            np.testing.assert_array_equal(out['innovation_covariance'], s)
        assert out['valid'], out
        np.testing.assert_array_equal(out['position_m'], x[:3])
        np.testing.assert_array_equal(out['velocity_m_s'], x[3:])
        np.testing.assert_array_equal(out['covariance'], p)
        assert out['process_mode'] == 'motion' and out['effective_q'] == root_q**2
        assert out['quiet_sample_count'] == 0 and out['quiet_trend'] is None


@pytest.mark.parametrize('change', [dict(adaptive_quiet=1),
    dict(quiet_acceleration_std=0), dict(quiet_acceleration_std=float('nan')),
    dict(quiet_enter_s=-1), dict(quiet_speed_enter_m_s=.08),
    dict(quiet_speed_exit_m_s=.02), dict(quiet_motion_sigma=0),
    dict(quiet_trend_window_s=0), dict(quiet_innovation_exit_chi2=float('inf')),
    dict(quiet_coherent_exit_chi2=0), dict(quiet_coherent_exit_chi2=float('nan')),
    dict(measurement_speed_noise_allowance_m=-.001),
    dict(adaptive_quiet=True, quiet_acceleration_std=4.)])
def test_policy_validation(change):
    with pytest.raises(ValueError):
        PoseEstimatorConfig(**change)


def test_innovation_covariance_and_3d_nis_are_actual_preupdate_statistics():
    e = seeded()
    before = e.diagnostics()
    x = np.r_[before['position_m'], before['velocity_m_s']]
    p = np.array(before['covariance'])
    x, p = prediction(x, p, .05 - .03, 3.)
    z = np.array([.005, 0, 1])
    out = e.update(z, R, .05)
    s = p[:3, :3] + R
    np.testing.assert_array_equal(out['innovation_covariance'], s)
    expected = float((z - x[:3]) @ np.linalg.solve(s, z - x[:3]))
    assert out['nis_3d'] == pytest.approx(expected)
    assert out['nis_3d'] == out['mahalanobis_sq']
    assert out['nis_optical'] is None  # No camera axis in the independent core.
    out['innovation_covariance'][0][0] = -1
    assert e.diagnostics()['innovation_covariance'][0][0] > 0


def test_full_covariance_weighted_trend_and_uncertainty():
    r = np.array([[4, 1, .5], [1, 3, .2], [.5, .2, 9]]) * 1e-6
    velocity = np.array([.01, -.02, .03])
    samples = [(t, np.array([0, 0, 1]) + velocity * t, r, velocity)
               for t in (0., .017, .03, .055, .08, .12)]
    fit, covariance = PoseEstimator._optical_trend(samples, .12)
    np.testing.assert_allclose(fit, velocity, atol=1e-14)
    times = np.array([entry[0] for entry in samples])
    np.testing.assert_allclose(covariance, r / np.sum((times - times.mean())**2), rtol=1e-12)
    np.linalg.cholesky(covariance)


def test_static_quiet_has_dwell_and_no_freeze_or_pseudo_measurements():
    e = seeded(optimized())
    quiet_times = []
    for i in range(1, 101):
        now = .03 + i * .013
        out = e.update([0, 0, 1], R, now)
        assert_spd(out)
        if out['quiet_active']:
            quiet_times.append(now)
    assert quiet_times and quiet_times[0] >= .03 + .75 * .12 + .25
    before = e.diagnostics()
    assert before['process_mode'] == 'quiet' and before['effective_q'] == .03**2
    z = np.array([.0001, 0, 1])
    out = e.update(z, R, before['timestamp'] + .013)
    assert 0 < out['position_m'][0] < .0001
    assert out['velocity_m_s'][0] != 0
    np.testing.assert_array_equal(out['measurement_covariance'], R)


def test_motion_onset_exits_same_frame_from_saved_prior_not_double_prediction():
    e = quiet_estimator()
    before = e.diagnostics()
    now = before['timestamp'] + .02
    z = np.array([.023, 0, 1])  # 1.15 m/s: safe raw and publication speed.
    x = np.r_[before['position_m'], before['velocity_m_s']]
    p = np.array(before['covariance'])
    motion_x, motion_p = prediction(x, p, now - before['timestamp'], 1.5)
    expected_x, expected_p, expected_s = posterior(motion_x, motion_p, z, R)
    out = e.update(z, R, now)
    assert out['valid'] and out['process_mode'] == 'motion' and not out['quiet_active']
    np.testing.assert_array_equal(out['position_m'], expected_x[:3])
    np.testing.assert_array_equal(out['velocity_m_s'], expected_x[3:])
    np.testing.assert_array_equal(out['covariance'], expected_p)
    np.testing.assert_array_equal(out['innovation_covariance'], expected_s)
    assert out['baseline_motion_mahalanobis_sq'] < e.config.gate_chi2
    assert_spd(out)


def test_single_95_percent_nis_excursion_does_not_exit_without_coherent_trend():
    e = quiet_estimator()
    before = e.diagnostics()
    now = before['timestamp'] + .02
    x = np.r_[before['position_m'], before['velocity_m_s']]
    p = np.array(before['covariance'])
    _, p = prediction(x, p, now - before['timestamp'], .03)
    # An isolated 9-NIS sample, not a coherent temporal trajectory.
    z = [math.sqrt(9 * (p[0, 0] + R[0, 0])), 0, 1]
    out = e.update(z, R, now)
    assert out['valid'] and out['nis_3d'] == pytest.approx(9.)
    assert out['process_mode'] == 'quiet' and out['quiet_active']


def test_outlier_cannot_expand_gate_or_feed_quiet_detector():
    e = quiet_estimator()
    before = e.diagnostics()
    # Under the raw speed limit, but beyond the NORMAL motion-Q statistical gate.
    out = e.update([.1, 0, 1], R, before['timestamp'] + .06, allow_prediction=True)
    assert out['rejection_reason'] == 'mahalanobis_gate'
    assert out['baseline_motion_mahalanobis_sq'] > e.config.gate_chi2
    assert out['quiet_sample_count'] == 0 and out['quiet_trend'] is None
    assert not out['quiet_active'] and out['process_mode'] == 'quiet'
    assert out['quiet_next_mode'] == 'motion'
    assert out['quiet_exit_reason'] == 'measurement_rejected'
    x = np.r_[before['position_m'], before['velocity_m_s']]
    expected_x, expected_p = prediction(x, np.array(before['covariance']),
                                      out['timestamp'] - before['timestamp'], .03)
    np.testing.assert_array_equal(out['position_m'], expected_x[:3])
    np.testing.assert_array_equal(out['covariance'], expected_p)
    next_out = e.update([0, 0, 1], R, out['timestamp'] + .013)
    assert next_out['valid'] and next_out['tracking_state'] == 'tracking'
    assert next_out['process_mode'] == 'motion'
    assert_spd(next_out)


@pytest.mark.parametrize('sample', [(None, R, True), ([0, 0, 1], None, True),
                                  ([0, 0, 1], R, False),
                                  ([0, 0, 1], np.eye(3) * .05**2, True)])
def test_missing_and_partial_observations_do_not_train_detector(sample):
    e = quiet_estimator()
    before = e.diagnostics()
    z, r, reliable = sample
    out = e.update(z, r, before['timestamp'] + .013,
                   reliable=reliable, allow_prediction=True)
    assert out['valid']
    assert out['quiet_sample_count'] == before['quiet_sample_count']
    assert out['quiet_trend'] == before['quiet_trend']
    assert out['quiet_active']
    if not reliable or (z is not None and r is not None and np.sqrt(r[0, 0]) > .04):
        assert out['tracking_state'] == 'tracking' and out['process_mode'] == 'motion'


def test_slow_precise_drift_is_detected_statistically_not_frozen():
    r = np.eye(3) * .0001**2
    e = quiet_estimator(r)
    start = e.diagnostics()['timestamp']
    exit_time = None
    for i in range(1, 41):
        now = start + .02 * i
        out = e.update([.01 * (now - start), 0, 1], r, now)
        assert out['valid']
        assert_spd(out)
        if not out['quiet_active'] and exit_time is None:
            exit_time = now - start
    assert exit_time is not None and exit_time <= .12 + 1e-12
    assert out['position_m'][0] == pytest.approx(.008, abs=.0002)
    assert out['quiet_trend']['normalized_velocity'] > 3.


def test_variable_timestamps_spd_reset_session_and_recenter():
    e = seeded(optimized())
    now = .03
    modes = set()
    for i in range(150):
        now += (.007, .021, .013)[i % 3]
        out = e.update([0, 0, 1], R, now, identity=('a', 0))
        if out['covariance'] is not None:
            assert_spd(out)
        modes.add(out['process_mode'])
    assert modes == {'motion', 'quiet'}
    out = e.update([1, 0, 1], R, now + .01, identity=('a', 1))
    assert not out['valid'] and not out['quiet_active'] and out['quiet_sample_count'] == 0
    assert out['reset_reason'] == 'identity_changed' and out['process_mode'] == 'motion'
    e.reset('recenter')
    out = e.diagnostics()
    assert out['quiet_trend'] is None and out['quiet_dwell_s'] == 0
    assert out['effective_q'] == 1.5**2


def test_noise_allowance_is_covariance_based_capped_and_does_not_change_publish_limit():
    c = replace(PoseEstimatorConfig(), measurement_speed_noise_allowance_m=.015,
                initial_velocity_std_m_s=.001, acceleration_std_m_s2=.01)
    strict, relaxed = seeded(replace(c, measurement_speed_noise_allowance_m=0)), seeded(c)
    # Raw speed 2.5 m/s, but a tiny current correction with large optical R.
    z = [.025, 0, 1]
    a, b = strict.update(z, R, .04), relaxed.update(z, R, .04)
    assert a['rejection_reason'] == 'implied_speed'
    assert b['valid'] and b['measurement_speed_noise_allowance_m'] == .015
    assert b['published_speed_m_s'] <= 2. + 1e-9
    expected = min(.015, 3 * math.sqrt(np.linalg.eigvalsh(2 * R)[-1]))
    assert b['measurement_speed_noise_allowance_m'] == pytest.approx(expected)
    assert b['policy_flags']['measurement_speed_noise_allowance_m'] == .015
    before_r = relaxed._last_measurement_covariance.copy()
    out = relaxed.update([.5, 0, 1], np.eye(3) * 100., .05, allow_prediction=True)
    assert out['rejection_reason'] == 'implied_speed'
    assert out['measurement_speed_noise_allowance_m'] == .015
    np.testing.assert_array_equal(relaxed._last_measurement_covariance, before_r)
    # Fine covariance must NOT receive the entire arbitrary cap.
    assert relaxed._noise_allowance(np.eye(3) * 1e-10, np.eye(3) * 1e-10) < .00005


def static_metrics(config, count=1200):
    r = np.diag([.001**2, .001**2, .005**2])
    e = seeded(config, r)
    rng = np.random.default_rng(81)
    positions, modes = [], []
    for i in range(1, count + 1):
        z = np.array([0, 0, 1]) + rng.multivariate_normal(np.zeros(3), r)
        out = e.update(z, r, .03 + i / 75., allow_prediction=True)
        assert out['valid'], out
        assert_spd(out)
        positions.append(out['position_m'][2])
        modes.append(out['process_mode'])
    values = np.array(positions)[300:]
    return dict(static_z_std_mm=float(np.std(values) * 1000),
                static_z_rmse_mm=float(np.sqrt(np.mean((values - 1)**2)) * 1000),
                quiet_fraction=modes[300:].count('quiet') / len(modes[300:]))


def test_static_5mm_depth_noise_enters_quiet_and_improves_variance():
    original = static_metrics(PoseEstimatorConfig())
    base = static_metrics(replace(PoseEstimatorConfig(), acceleration_std_m_s2=1.5))
    adaptive = static_metrics(optimized())
    print('synthetic static depth (5mm optical sigma), original Q-root 3:', original,
          'Q-root 1.5:', base,
          'adaptive:', adaptive)
    assert adaptive['quiet_fraction'] > .5
    assert adaptive['static_z_std_mm'] < .7 * base['static_z_std_mm']


def test_synthetic_base_q_sweep_metrics():
    metrics = {root: static_metrics(replace(PoseEstimatorConfig(),
                                          acceleration_std_m_s2=root), count=700)
               for root in (.5, 1., 1.5, 2., 3.)}
    print('synthetic static Q-root sweep:', metrics)
    assert metrics[.5]['static_z_std_mm'] < metrics[3.]['static_z_std_mm']


def test_rejected_posterior_does_not_commit_provisional_quiet_exit():
    r = np.eye(3) * .03**2
    e = quiet_estimator(r)
    before = e.diagnostics()
    now = before['timestamp'] + .013
    out = e.update([.03, 0, 1], np.eye(3) * 1e-8, now, allow_prediction=True)
    assert out['rejection_reason'] == 'posterior_position_step'
    assert out['baseline_motion_mahalanobis_sq'] < e.config.gate_chi2
    assert not out['quiet_active'] and out['process_mode'] == 'quiet'
    assert out['quiet_next_mode'] == 'motion'
    assert out['quiet_sample_count'] == 0 and out['quiet_trend'] is None
    x = np.r_[before['position_m'], before['velocity_m_s']]
    p = np.array(before['covariance'])
    expected_x, expected_p = prediction(x, p, now - before['timestamp'], .03)
    np.testing.assert_array_equal(out['position_m'], expected_x[:3])
    np.testing.assert_array_equal(out['covariance'], expected_p)
    np.testing.assert_array_equal(out['innovation_covariance'], expected_p[:3, :3] + np.eye(3) * 1e-8)


def test_synthetic_depth_motion_metrics_and_onset_latency():
    r = np.diag([.001**2, .001**2, .005**2])
    rng = np.random.default_rng(91)
    noise = rng.multivariate_normal(np.zeros(3), r, 900)
    metrics = {}
    for label, config in [('base_q3', PoseEstimatorConfig()),
                          ('motion_q1.5', replace(PoseEstimatorConfig(), acceleration_std_m_s2=1.5)),
                          ('adaptive_q1.5_quiet.03', optimized())]:
        e = seeded(config, r)
        errors, onset_exit = [], None
        for i in range(900):
            t = (i + 1) / 75.
            moving_time = max(0., t - 4.)
            truth = np.array([0, 0, 1 + .15 * math.sin(2 * math.pi * .5 * moving_time)])
            out = e.update(truth + noise[i], r, .03 + t, allow_prediction=True)
            assert out['valid'], out
            if moving_time > 0:
                errors.append(out['position_m'][2] - truth[2])
                if out['process_mode'] == 'motion' and onset_exit is None:
                    onset_exit = moving_time
            if out['published_speed_m_s'] is not None:
                assert out['published_speed_m_s'] <= config.max_speed_m_s + 1e-9
        metrics[label] = dict(depth_motion_rmse_mm=float(np.sqrt(np.mean(np.array(errors)**2)) * 1000),
                              onset_motion_mode_latency_ms=onset_exit * 1000)
    print('synthetic depth sine (150mm amplitude, .5Hz, 5mm optical sigma):', metrics)
    assert metrics['adaptive_q1.5_quiet.03']['onset_motion_mode_latency_ms'] <= 120.


def test_quiet_policy_also_enters_with_original_motion_q3_and_strict_raw_gate():
    metrics = static_metrics(replace(PoseEstimatorConfig(), adaptive_quiet=True))
    print('synthetic static default-motion Q-root 3, quiet .03, noise allowance OFF:', metrics)
    assert metrics['quiet_fraction'] > .5
    assert metrics['static_z_std_mm'] < 2.5


def observation_with_whitened_innovation(e, now, whitened):
    before = e.diagnostics()
    x = np.r_[before['position_m'], before['velocity_m_s']]
    p = np.array(before['covariance'])
    x, p = prediction(x, p, now - before['timestamp'], e.config.quiet_acceleration_std)
    return x[:3] + np.linalg.cholesky(p[:3, :3] + R) @ np.array(whitened)


@pytest.mark.parametrize('root_q', [3., 5.])
@pytest.mark.parametrize('threshold,amplitude,exits', [(11.34, 2.5, True),
    (7.815, 2.1, True), (11.34, 2.1, False)])
def test_two_coherent_innovations_exit_current_frame_without_double_prediction(
        root_q, threshold, amplitude, exits):
    config = replace(optimized(), acceleration_std_m_s2=root_q,
                     quiet_coherent_exit_chi2=threshold)
    e = quiet_estimator(config=config)
    now = e.diagnostics()['timestamp'] + .02
    z = observation_with_whitened_innovation(e, now, [0, 0, amplitude])
    first = e.update(z, R, now)
    assert first['valid'] and first['quiet_active']
    assert first['nis_3d'] == pytest.approx(amplitude**2)
    assert first['nis_3d'] < 7.815  # Neither observation triggers the old 95% exit.
    before = e.diagnostics()
    now += .02
    z = observation_with_whitened_innovation(e, now, [0, 0, amplitude])
    x = np.r_[before['position_m'], before['velocity_m_s']]
    p = np.array(before['covariance'])
    motion_x, motion_p = prediction(x, p, now - before['timestamp'], root_q)
    expected_x, expected_p, expected_s = posterior(motion_x, motion_p, z, R)
    out = e.update(z, R, now)
    assert out['valid']
    assert out['quiet_coherent_statistic'] == pytest.approx(2 * amplitude**2)
    assert out['quiet_coherent_cosine'] == pytest.approx(1.)
    assert out['quiet_active'] == (not exits)
    assert_spd(out)
    if exits:
        assert out['process_mode'] == 'motion'
        assert out['quiet_exit_reason'] == 'coherent_innovation'
        np.testing.assert_array_equal(out['position_m'], expected_x[:3])
        np.testing.assert_array_equal(out['velocity_m_s'], expected_x[3:])
        np.testing.assert_array_equal(out['covariance'], expected_p)
        np.testing.assert_array_equal(out['innovation_covariance'], expected_s)


@pytest.mark.parametrize('direction', [[0, 0, -2.5], [2.5, 0, 0]])
def test_opposite_or_orthogonal_innovations_do_not_trigger_coherent_exit(direction):
    e = quiet_estimator()
    now = e.diagnostics()['timestamp'] + .02
    e.update(observation_with_whitened_innovation(e, now, [0, 0, 2.5]), R, now)
    now += .02
    out = e.update(observation_with_whitened_innovation(e, now, direction), R, now)
    assert out['valid'] and out['quiet_active']
    assert out['quiet_coherent_cosine'] <= .8
    assert out['quiet_exit_reason'] is None


@pytest.mark.parametrize('interrupt', ['missing', 'partial', 'uncertain', 'reject', 'reset'])
def test_coherent_innovation_buffer_never_bridges_unobservable_or_refused_samples(interrupt):
    e = quiet_estimator()
    now = e.diagnostics()['timestamp'] + .02
    e.update(observation_with_whitened_innovation(e, now, [0, 0, 2.5]), R, now)
    assert e._quiet_whitened_prev is not None
    now += .013
    if interrupt == 'missing':
        out = e.update(None, None, now, allow_prediction=True)
    elif interrupt == 'partial':
        out = e.update(e.diagnostics()['position_m'], R, now, reliable=False)
    elif interrupt == 'uncertain':
        out = e.update(e.diagnostics()['position_m'], np.eye(3) * .05**2, now)
    elif interrupt == 'reject':
        out = e.update([.5, 0, 1], R, now, allow_prediction=True)
        assert out['rejection_reason'] == 'implied_speed'
        assert not out['quiet_active'] and out['quiet_next_mode'] == 'motion'
    else:
        e.reset('recenter')
        out = e.diagnostics()
    assert e._quiet_whitened_prev is None
    assert out['quiet_coherent_statistic'] is None and out['quiet_coherent_cosine'] is None


def test_single_500mm_outlier_never_changes_current_prediction_or_gate():
    e = quiet_estimator(config=replace(optimized(), acceleration_std_m_s2=5.))
    before = e.diagnostics()
    now = before['timestamp'] + .013
    x = np.r_[before['position_m'], before['velocity_m_s']]
    predicted_x, predicted_p = prediction(x, np.array(before['covariance']),
                                        now - before['timestamp'], .03)
    out = e.update([.5, 0, 1], R, now, allow_prediction=True)
    assert out['valid'] and out['tracking_state'] == 'predicting'
    assert out['rejection_reason'] == 'implied_speed'
    assert out['accepted'] == before['accepted'] and out['rejected'] == before['rejected'] + 1
    np.testing.assert_array_equal(out['position_m'], predicted_x[:3])
    np.testing.assert_array_equal(out['velocity_m_s'], predicted_x[3:])
    np.testing.assert_array_equal(out['covariance'], predicted_p)
    assert out['process_mode'] == 'quiet' and out['quiet_next_mode'] == 'motion'
    out = e.update([0, 0, 1], R, now + .013)
    assert out['valid'] and out['tracking_state'] == 'tracking'
    assert out['process_mode'] == 'motion' and out['recovery_count'] == 0
    np.testing.assert_allclose(out['position_m'], [0, 0, 1], atol=1e-14)
    assert_spd(out)


@pytest.fixture(scope='module')
def matched_benchmark_results():
    """Import shared benchmark, never edit it or write configurations/results."""
    from tools.psvr_camera import kalman_optimization_benchmark as bench
    data = bench.make_scenarios(frames=900, seed=6130)
    variants = {'static_q1.5': ({'acceleration_std_m_s2': 1.5}, 1.),
                'adaptive_q1.5_r1': ({'acceleration_std_m_s2': 1.5, 'adaptive_quiet': True,
                                     'measurement_speed_noise_allowance_m': .015}, 1.)}
    for root_q in (3., 5.):
        for factor in (1., 2.):
            variants[f'adaptive_q{root_q:g}_r{factor:g}'] = (
                {'acceleration_std_m_s2': root_q, 'adaptive_quiet': True,
                 'quiet_acceleration_std': .03, 'quiet_coherent_exit_chi2': 11.34,
                 'measurement_speed_noise_allowance_m': .015}, factor)
    report = {}
    # Six matched base scenarios plus precisely rotated depth and noisy 1.7 m/s
    # motion: same packets, noise RNG order, R and start_s=2 for every variant.
    for scenario in (*bench.BASE_SCENARIOS, 'tilted_depth', 'noisy_fast_depth'):
        dataset = data[scenario]
        per_variant = {}
        for label, (config, factor) in variants.items():
            rows = bench.run_variant(dataset, kalman=config,
                                     uncertainty={'depth_factor': factor})
            summary = bench.summarize(rows, dataset['reference'], dataset['classification'])
            per_variant[label] = summary
            std = summary['xyz_std_m'][2] * 1000 if scenario in ('stationary', 'outlier', 'dropout') else None
            print('MATCHED', label, scenario, dict(static_z_std_mm=std,
                  optical_rmse_mm=summary['optical_rmse_m'] * 1000,
                  lag_ms=None if summary['response_delay_s'] is None else summary['response_delay_s'] * 1000,
                  valid=summary['valid_frames'], frames=summary['frames'],
                  max_step_mm=summary['step_max_m'] * 1000,
                  max_reacquisition_jump_mm=None if summary['reacquisition_jump_max_m'] is None
                  else summary['reacquisition_jump_max_m'] * 1000,
                  max_speed_m_s=summary['speed_max_m_s'],
                  acquiring_frames=sum(row['monotonic_s'] >= 2. and
                                       row['diagnostics']['tracking_state'] == 'acquiring'
                                       for row in rows),
                  gate_rejections=summary['clean_gate_rejected'],
                  quiet_fraction=summary['quiet_active_fraction']))
            assert summary['speed_max_m_s'] <= 2. + 1e-9
            assert summary['velocity_max_m_s'] <= 2. + 1e-9
            # Unknown camera axis in core: the benchmark projects S independently.
            # The independent core has no camera axis; the pipeline projects S.
            for row in rows:
                diag = row['diagnostics']
                if diag['innovation_covariance'] is None:
                    assert diag['nis_optical'] is None
                else:
                    u = np.asarray(row['optical_axis_world_m'])
                    nu = np.asarray(diag['innovation_m'])
                    s = np.asarray(diag['innovation_covariance'])
                    assert diag['nis_optical'] == pytest.approx((u @ nu)**2 / (u @ s @ u))
        report[scenario] = per_variant
    return report


def test_matched_six_benchmarks_static_noise_fast_lag_and_rotated_depth(matched_benchmark_results):
    report = matched_benchmark_results
    assert all(name in report for name in ('stationary', 'linear', 'rapid', 'depth', 'outlier', 'dropout'))
    baseline_std = report['stationary']['static_q1.5']['xyz_std_m'][2]
    for label in ('adaptive_q3_r1', 'adaptive_q3_r2', 'adaptive_q5_r1', 'adaptive_q5_r2'):
        assert report['stationary'][label]['xyz_std_m'][2] < baseline_std
        assert report['stationary'][label]['valid_frames'] == report['stationary'][label]['frames']
        assert report['depth'][label]['response_delay_s'] is not None
        assert report['tilted_depth'][label]['optical_rmse_m'] is not None
        assert report['outlier'][label]['reacquisition_count'] == 0
    # Lock the matched baseline, not only the much easier local sine benchmark.
    assert report['depth']['static_q1.5']['optical_rmse_m'] == pytest.approx(.004576376537660546)
    assert report['depth']['static_q1.5']['response_delay_s'] == .006
    selected = report['depth']['adaptive_q5_r1']
    assert selected['optical_rmse_m'] < report['depth']['static_q1.5']['optical_rmse_m']
    assert selected['response_delay_s'] < report['depth']['static_q1.5']['response_delay_s']
    # Metrics include availability and discontinuity separately. Do not turn
    # low conditional RMSE on noisy-fast into an assertion of tracking readiness.
    fast = report['noisy_fast_depth']['adaptive_q5_r1']
    assert 0 < fast['valid_frames'] <= fast['frames']
    assert fast['reacquisition_jump_max_m'] is not None
