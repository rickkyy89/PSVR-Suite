"""Deterministic, offline contracts for positional CV estimation."""
from dataclasses import replace

import numpy as np
try:
    import pytest
except ImportError:
    from unittest import SkipTest
    raise SkipTest("Kalman scenario tests require pytest")

from tools.psvr_camera.pose_estimator import PoseEstimator, PoseEstimatorConfig


R = np.eye(3) * .005**2


def seeded(config=None, position=(0., 0., 1.), identity=None):
    estimator = PoseEstimator(config)
    for i in range(estimator.config.recovery_confirmations):
        out = estimator.update(position, R, i * .01, identity=identity)
        assert out['valid'] == (i == estimator.config.recovery_confirmations - 1)
    return estimator


def assert_spd(output):
    p = np.array(output['covariance'])
    assert p.shape == (6, 6)
    np.testing.assert_allclose(p, p.T, atol=1e-14)
    np.linalg.cholesky(p)


def test_defaults_confirmation_and_zero_initial_velocity():
    c = PoseEstimatorConfig()
    assert (c.acceleration_std_m_s2, c.gate_chi2, c.max_prediction_s,
            c.max_gap_s, c.max_speed_m_s, c.max_position_std_m,
            c.reliable_std_m, c.recovery_confirmations) == (3., 16.27, .08, .25, 2., .08, .04, 4)
    e = seeded()
    out = e.diagnostics()
    assert out['tracking_state'] == 'tracking'
    assert out['velocity_m_s'] == [0., 0., 0.]
    assert out['accepted'] == 1
    assert_spd(out)


@pytest.mark.parametrize('change', [dict(acceleration_std_m_s2=0), dict(gate_chi2=np.nan),
    dict(max_prediction_s=.3), dict(max_gap_s=-1), dict(max_speed_m_s=np.inf),
    dict(reliable_std_m=.09), dict(recovery_confirmations=1),
    dict(recovery_confirmations=4.5), dict(recovery_confirmations=True)])
def test_config_validation(change):
    with pytest.raises(ValueError):
        PoseEstimatorConfig(**change)


@pytest.mark.parametrize('position,covariance', [([1, 2], R), ([0, 0, np.nan], R),
    ([0, 0, 1], np.eye(6)), ([0, 0, 1], np.zeros((3, 3))),
    ([0, 0, 1], np.diag([1, -1, 1])), ([0, 0, 1], [[1, 1, 0], [0, 1, 0], [0, 0, 1]]),
    ([0, 0, 1], np.diag([np.inf, 1, 1]))])
def test_measurement_validation_is_atomic(position, covariance):
    e = seeded()
    before = e.diagnostics()
    with pytest.raises(ValueError):
        e.update(position, covariance, .04)
    assert e.diagnostics() == before


def test_quiet_noise_and_anisotropic_depth_covariance():
    e = seeded()
    rng = np.random.default_rng(94)
    r = np.diag([.001**2, .001**2, .004**2])
    raw, estimates = [], []
    for i in range(1, 501):
        z = np.array([0., 0., 1.]) + rng.multivariate_normal(np.zeros(3), r)
        out = e.update(z, r, .03 + i * .02)
        assert out['valid'], out
        assert_spd(out)
        raw.append(z)
        estimates.append(out['position_m'])
    assert np.std(np.array(estimates)[100:, 2]) < np.std(np.array(raw)[100:, 2])
    np.testing.assert_allclose(np.mean(estimates[100:], axis=0), [0, 0, 1], atol=.001)
    np.testing.assert_allclose(out['measurement_covariance'], r)


@pytest.mark.parametrize('velocity', [[.4, -.2, .3], [1.7, 0, 0], [0, 0, 1.7]])
def test_linear_fast_and_depth_with_real_variable_dt(velocity):
    e = seeded()
    velocity = np.array(velocity)
    now = .03
    for i in range(150):
        now += [.007, .013, .023][i % 3]
        z = np.array([0, 0, 1]) + velocity * (now - .03)
        out = e.update(z, R, now)
        assert out['valid'], out
        assert np.linalg.norm(out['velocity_m_s']) <= 2.
        assert_spd(out)
    np.testing.assert_allclose(out['velocity_m_s'], velocity, atol=.001)
    np.testing.assert_allclose(out['position_m'], z, atol=.001)


def test_exact_white_acceleration_prediction_and_joseph_update():
    e = seeded()
    p0 = np.array(e.diagnostics()['covariance'])
    dt = .017
    f = np.eye(6)
    f[:3, 3:] = np.eye(3) * dt
    q = 9 * np.kron([[dt**3 / 3, dt**2 / 2], [dt**2 / 2, dt]], np.eye(3))
    predicted = f @ p0 @ f.T + q
    out = e.update([.003, 0, 1], R, .03 + dt)
    k = np.linalg.solve(predicted[:3, :3] + R, predicted[:, :3].T).T
    a = np.eye(6)
    a[:, :3] -= k
    np.testing.assert_allclose(out['covariance'], a @ predicted @ a.T + k @ R @ k.T)
    np.testing.assert_allclose(out['position_m'], (np.array([0, 0, 1, 0, 0, 0]) + k @ [.003, 0, 0])[:3])


def test_outlier_does_not_correct_filter_or_expand_gate():
    e = seeded()
    reference = seeded()
    out = e.update([1, 0, 1], R, .04, allow_prediction=True)
    pred = reference.update(None, None, .04, allow_prediction=True)
    assert out['valid'] and out['tracking_state'] == 'predicting'
    assert out['rejection_reason'] == 'implied_speed'
    assert out['mahalanobis_sq'] > e.config.gate_chi2
    np.testing.assert_allclose(out['covariance'], pred['covariance'])
    np.testing.assert_allclose(out['velocity_m_s'], pred['velocity_m_s'])
    np.testing.assert_allclose(out['position_m'], pred['position_m'])
    assert out['accepted'] == pred['accepted'] == 1
    out = e.update([0, 0, 1], R, .05)
    assert out['valid'] and out['rejected'] == 1
    np.testing.assert_allclose(out['position_m'], [0, 0, 1])


def test_statistical_gate_independent_of_speed_gate():
    e = seeded(replace(PoseEstimatorConfig(), initial_velocity_std_m_s=.001,
                       acceleration_std_m_s2=.01))
    for i in range(4, 40):
        e.update([0, 0, 1], np.eye(3) * 1e-8, i * .01)
    out = e.update([.005, 0, 1], np.eye(3) * 1e-8, .4)
    assert out['rejection_reason'] == 'mahalanobis_gate'
    assert not out['valid']


@pytest.mark.parametrize('missing', [(None, None), (None, R), ([0, 0, 1], None)])
def test_dropout_permission_and_growth(missing):
    # Keep seed uncertainty below the positional limit at the time boundary;
    # the independent uncertainty-limit contract is tested separately.
    e = seeded(replace(PoseEstimatorConfig(), initial_velocity_std_m_s=.5))
    initial = np.array(e.diagnostics()['covariance'])
    out = e.update(*missing, .04)
    assert not out['valid'] and out['position_m'] is None
    out = e.update(*missing, .06, allow_prediction=True)
    assert out['valid'] and out['tracking_state'] == 'predicting'
    assert np.trace(out['covariance']) > np.trace(initial)
    assert_spd(out)
    out = e.update(*missing, .11, allow_prediction=True)
    assert out['valid']  # inclusive 80ms boundary
    out = e.update(*missing, .111, allow_prediction=True)
    assert not out['valid'] and out['covariance'] is None
    for i in range(4):
        out = e.update([0, 0, 1], R, .12 + .01 * i)
        assert out['valid'] == (i == 3)


def test_unreliable_measurements_cannot_seed_or_recover():
    e = PoseEstimator()
    for i in range(10):
        assert not e.update([0, 0, 1], R, i * .01, reliable=False)['valid']
    for i in range(10, 20):
        out = e.update([0, 0, 1], np.eye(3) * .05**2, i * .01)
        assert not out['valid'] and out['recovery_count'] == 0
    e = seeded()
    out = e.update([.005, 0, 1], R, .04, reliable=False)
    assert out['valid'] and out['tracking_state'] == 'tracking'
    assert out['accepted'] == 2 and out['recovery_count'] == 0
    assert out['position_m'][0] > 0


def test_prediction_uncertainty_limit():
    e = seeded(replace(PoseEstimatorConfig(), initial_velocity_std_m_s=10.))
    out = e.update(None, None, .04, allow_prediction=True)
    assert not out['valid'] and out['reset_reason'] == 'position_uncertainty'


def test_long_gap_even_with_measurement_requires_confirmed_reacquisition():
    e = seeded()
    for i in range(4):
        out = e.update([1, 0, 1], R, .5 + i * .01)
        assert out['valid'] == (i == 3)
    assert out['velocity_m_s'] == [0., 0., 0.]


def test_repeated_rejections_recover_only_after_consistent_cluster():
    e = seeded()
    for i, x in enumerate([1., -1., 2., -2., 1., 1., 1., 1.]):
        out = e.update([x, 0, 1], R, .04 + i * .01)
        assert out['valid'] == (i == 7)
        if i < 7:
            assert out['position_m'] is None
    assert out['velocity_m_s'] == [0., 0., 0.]
    assert out['reset_reason'] == 'confirmed_reacquisition'


def test_recovery_statistical_cluster_and_missing_break_confirmation():
    c = replace(PoseEstimatorConfig(), acceleration_std_m_s2=.01)
    e = PoseEstimator(c)
    r = np.eye(3) * 1e-8
    for i in range(8):
        out = e.update([.01 * (i % 2), 0, 1], r, i * .01)
        assert not out['valid'] and out['recovery_count'] <= 2
    e.update(None, None, .08)
    assert e.diagnostics()['recovery_count'] == 0


@pytest.mark.parametrize('timestamp', [float('nan'), float('inf'), -1, .03, .02, 'bad'])
def test_timestamp_anomaly_resets_without_seeding(timestamp):
    e = seeded()
    out = e.update([1, 0, 1], R, timestamp)
    assert out['tracking_state'] == 'timestamp_error'
    assert not out['valid'] and out['covariance'] is None
    assert out['accepted'] == 0 and out['recovery_count'] == 0
    for i in range(4):
        out = e.update([1, 0, 1], R, 1 + i * .01)
        assert out['valid'] == (i == 3)


@pytest.mark.parametrize('field', ['session', 'epoch', 'alignment'])
def test_session_recenter_alignment_and_identity_snapshot(field):
    identity = dict(session='a', epoch=0, alignment=np.eye(3))
    e = seeded(identity=identity)
    if field == 'alignment':
        identity[field][0, 0] = -1
    else:
        identity[field] = 'b' if field == 'session' else 1
    out = e.update([1, 0, 1], R, .04, identity=identity)
    assert not out['valid'] and out['reset_reason'] == 'identity_changed'
    assert out['accepted'] == 0 and out['recovery_count'] == 1
    for i in range(1, 4):
        out = e.update([1, 0, 1], R, .04 + i * .01, identity=identity)
    assert out['valid']
    e.reset('recenter')
    assert e.diagnostics()['reset_reason'] == 'recenter'
    assert e.diagnostics()['covariance'] is None


def test_external_adaptive_r_changes_gain_and_is_not_mutated():
    low, high = seeded(), seeded()
    a = low.update([.005, 0, 1], R, .04)
    r = np.eye(3) * .03**2
    original = r.copy()
    b = high.update([.005, 0, 1], r, .04)
    assert a['valid'] and b['valid']
    assert a['position_m'][0] > b['position_m'][0]
    np.testing.assert_array_equal(r, original)
    a['covariance'][0][0] = -1
    low.diagnostics()['position_m'][0] = 100
    assert low.diagnostics()['covariance'][0][0] > 0
    assert low.diagnostics()['position_m'][0] < .005


def test_identity_change_cannot_seed_on_regressive_timestamp():
    e = seeded(identity=('session-a', 0))
    out = e.update([1, 0, 1], R, 0, identity=('session-b', 0))
    assert not out['valid'] and out['tracking_state'] == 'timestamp_error'
    assert out['recovery_count'] == 0


@pytest.mark.parametrize('allow_prediction', [False, True])
def test_posterior_speed_limit_discards_unsafe_filter(allow_prediction):
    c = replace(PoseEstimatorConfig(), acceleration_std_m_s2=100.,
                initial_velocity_std_m_s=.001)
    e = seeded(c)
    out = e.update([.036, 0, 1], np.eye(3) * 1e-8, .05,
                   allow_prediction=allow_prediction)
    assert not out['valid'] and out['rejection_reason'] == 'posterior_speed'
    assert out['velocity_m_s'] is None and out['covariance'] is None
    assert out['rejected'] == 1


def test_implied_speed_cannot_be_hidden_by_large_external_covariance():
    e = seeded()
    out = e.update([10, 0, 1], np.eye(3) * 100., .04)
    assert out['mahalanobis_sq'] < e.config.gate_chi2
    assert not out['valid'] and out['rejection_reason'] == 'implied_speed'
    assert out['recovery_count'] == 0


def test_stale_measurement_after_dropout_cannot_resume_without_confirmations():
    e = seeded()
    e.update(None, None, .04)
    for i in range(4):
        out = e.update([0, 0, 1], R, .12 + .01 * i)
        assert out['valid'] == (i == 3)


def test_prediction_extrapolates_nonzero_velocity():
    e = seeded()
    for i in range(1, 31):
        now = .03 + .01 * i
        out = e.update([.5 * (now - .03), 0, 1], R, now)
    before = np.array(out['position_m'])
    velocity = np.array(out['velocity_m_s'])
    out = e.update(None, None, now + .02, allow_prediction=True)
    assert out['valid']
    np.testing.assert_allclose(out['position_m'], before + velocity * .02)


def test_precise_fast_linear_motion_can_acquire_without_stationary_assumption():
    e = PoseEstimator()
    r = np.eye(3) * .0001**2
    for i in range(4):
        out = e.update([1.7 * i * .01, 0, 1], r, i * .01)
        assert out['valid'] == (i == 3)
    assert out['velocity_m_s'] == [0., 0., 0.]
    for i in range(4, 30):
        out = e.update([1.7 * i * .01, 0, 1], r, i * .01)
        assert out['valid']
    np.testing.assert_allclose(out['velocity_m_s'], [1.7, 0, 0], atol=.001)


def test_candidate_speed_limit_cannot_be_hidden_by_uncertainty():
    e = PoseEstimator()
    for i in range(8):
        out = e.update([.03 * i, 0, 1], R, .01 * i)
        assert not out['valid'] and out['recovery_count'] == 1


def test_large_finite_covariance_cannot_overflow_into_reliable_seed():
    e = PoseEstimator()
    r = np.eye(3) * 1e308
    with np.errstate(over='raise', invalid='raise'):
        for i in range(4):
            out = e.update([0, 0, 1], r, .01 * i)
            assert not out['valid'] and out['recovery_count'] == 0
            assert np.isfinite(out['measurement_covariance']).all()


def test_unreliable_observable_updates_match_reliable_tracking_updates():
    a, b = seeded(), seeded()
    # Observability and R are supplied externally (e.g. three visible LEDs).
    # Updates keep refreshing tracking time, beyond the prediction-only budget.
    for i in range(1, 31):
        now = .03 + .01 * i
        position = [.5 * (now - .03), 0, 1]
        low_confidence = a.update(position, R, now, reliable=False)
        reliable = b.update(position, R, now, reliable=True)
        assert low_confidence == reliable
        assert low_confidence['valid'] and low_confidence['tracking_state'] == 'tracking'


@pytest.mark.parametrize('allow_prediction', [False, True])
def test_unreliable_outliers_never_form_recovery_cluster(allow_prediction):
    e = seeded(replace(PoseEstimatorConfig(), initial_velocity_std_m_s=.5))
    for i in range(4):
        out = e.update([1, 0, 1], R, .04 + .01 * i,
                       reliable=False, allow_prediction=allow_prediction)
        assert out['valid'] == allow_prediction
        assert out['tracking_state'] == ('predicting' if allow_prediction else 'rejected')
        assert out['accepted'] == 1 and out['rejected'] == i + 1
        assert out['recovery_count'] == 0
    # Prolonged loss cannot be cured by an observable-but-unreliable sample.
    out = e.update([1, 0, 1], R, .12, reliable=False, allow_prediction=True)
    assert not out['valid'] and out['covariance'] is None
    for i in range(10):
        out = e.update([1, 0, 1], R, .13 + .01 * i, reliable=False)
        assert not out['valid'] and out['recovery_count'] == 0


@pytest.mark.parametrize('allow_prediction', [False, True])
def test_recovery_reseed_is_preceded_by_invalid_publication(allow_prediction):
    e = seeded()
    outputs = []
    for i in range(5):
        outputs.append(e.update([1, 0, 1], R, .04 + .01 * i,
                                allow_prediction=allow_prediction))
    if allow_prediction:
        assert all(out['valid'] and out['tracking_state'] == 'predicting'
                   for out in outputs[:3])
        assert not outputs[3]['valid'] and outputs[3]['tracking_state'] == 'rejected'
        assert outputs[3]['recovery_count'] == 4
        first_tracking = 4
    else:
        assert all(not out['valid'] for out in outputs[:3])
        first_tracking = 3
    assert outputs[first_tracking]['valid']
    assert outputs[first_tracking]['tracking_state'] == 'tracking'
    assert not outputs[first_tracking - 1]['valid']
    assert outputs[first_tracking]['velocity_m_s'] == [0., 0., 0.]
    np.testing.assert_allclose(outputs[first_tracking]['position_m'], [1, 0, 1])


def test_earlier_invalid_then_prediction_still_cannot_teleport_to_seed():
    e = seeded()
    assert not e.update([1, 0, 1], R, .04)['valid']
    for now in (.05, .06):
        assert e.update([1, 0, 1], R, now, allow_prediction=True)['valid']
    out = e.update([1, 0, 1], R, .07, allow_prediction=True)
    assert not out['valid'] and out['tracking_state'] == 'rejected'
    out = e.update([1, 0, 1], R, .08, allow_prediction=True)
    assert out['valid'] and out['tracking_state'] == 'tracking'


def test_unreliable_sample_breaks_pending_recovery_confirmation():
    e = seeded()
    for now in (.04, .05, .06):
        e.update([1, 0, 1], R, now, allow_prediction=True)
    out = e.update([1, 0, 1], R, .07, reliable=False, allow_prediction=True)
    assert out['valid'] and out['tracking_state'] == 'predicting'
    assert out['recovery_count'] == 0 and out['accepted'] == 1
    out = e.update([1, 0, 1], R, .08, allow_prediction=True)
    assert out['valid'] and out['recovery_count'] == 1


@pytest.mark.parametrize('missing', [(None, R), ([0, 0, 1], None), (None, None)])
def test_unobservable_is_not_an_update_even_when_reliable(missing):
    e = seeded()
    out = e.update(*missing, .04, reliable=True, allow_prediction=True)
    assert out['tracking_state'] == 'predicting' and out['valid']
    assert out['rejection_reason'] == 'unobservable'
    assert out['measurement_covariance'] is None
    assert out['accepted'] == 1 and out['rejected'] == 0
    assert out['recovery_count'] == 0


def test_statistically_gated_outlier_can_predict_without_measurement_correction():
    c = replace(PoseEstimatorConfig(), initial_velocity_std_m_s=.001,
                acceleration_std_m_s2=.01)
    e, reference = seeded(c), seeded(c)
    for i in range(4, 40):
        for estimator in (e, reference):
            estimator.update([0, 0, 1], np.eye(3) * 1e-8, i * .01)
    out = e.update([.005, 0, 1], np.eye(3) * 1e-8, .4, allow_prediction=True)
    pred = reference.update(None, None, .4, allow_prediction=True)
    assert out['valid'] and out['tracking_state'] == 'predicting'
    assert out['rejection_reason'] == 'mahalanobis_gate'
    assert out['rejected'] == 1
    np.testing.assert_allclose(out['position_m'], pred['position_m'])
    np.testing.assert_allclose(out['covariance'], pred['covariance'])


def test_rejected_prediction_respects_uncertainty_limit():
    e = seeded(replace(PoseEstimatorConfig(), initial_velocity_std_m_s=10.))
    out = e.update([1, 0, 1], R, .04, allow_prediction=True)
    assert not out['valid'] and out['reset_reason'] == 'position_uncertainty'
    assert out['covariance'] is None


def test_rejected_prediction_budget_uses_last_accepted_not_last_outlier():
    e = seeded(replace(PoseEstimatorConfig(), initial_velocity_std_m_s=.5))
    for now in (.04, .06, .08, .11):
        # Unreliable outliers cannot trigger recovery, irrespective of R.
        out = e.update([1, 0, 1], R, now, reliable=False, allow_prediction=True)
        assert out['valid'] and out['tracking_state'] == 'predicting'
        assert out['accepted'] == 1
    out = e.update([1, 0, 1], R, .111, reliable=False, allow_prediction=True)
    assert not out['valid'] and out['reset_reason'] == 'prediction_expired'
    assert out['covariance'] is None


def test_unreliable_observation_cannot_initialize_new_identity():
    e = seeded(identity=('a', 0))
    for i in range(5):
        out = e.update([0, 0, 1], R, .04 + .01 * i,
                       identity=('b', 0), reliable=False, allow_prediction=True)
        assert not out['valid'] and out['covariance'] is None
        assert out['accepted'] == 0 and out['recovery_count'] == 0


def test_accepted_unreliable_observation_clears_pending_recovery():
    e = seeded()
    for now in (.04, .05, .06):
        e.update([1, 0, 1], R, now, allow_prediction=True)
    out = e.update([.001, 0, 1], R, .07, reliable=False)
    assert out['valid'] and out['tracking_state'] == 'tracking'
    assert out['accepted'] == 2 and out['recovery_count'] == 0
    out = e.update([1, 0, 1], R, .08, allow_prediction=True)
    assert out['valid'] and out['tracking_state'] == 'predicting'
    assert out['recovery_count'] == 1


def correction_step_estimator():
    """A slow latent velocity with stale positional uncertainty/adaptive R.

    A high-R observation barely moves the filter. The next precise observation
    is physically close to that raw sample but far from the published position.
    This reproduces the legacy correction-step failure without private mutation.
    """
    e = PoseEstimator(replace(PoseEstimatorConfig(),
                             initial_velocity_std_m_s=.001,
                             acceleration_std_m_s2=.01))
    for i in range(4):
        e.update([0, 0, 1], np.eye(3) * .03**2, .01 * i)
    out = e.update([.04, 0, 1], np.eye(3) * .1**2, .07)
    assert out['valid'] and out['tracking_state'] == 'tracking'
    return e


@pytest.mark.parametrize('allow_prediction', [False, True])
@pytest.mark.parametrize('reliable', [False, True])
def test_posterior_correction_step_rejected_despite_safe_latent_velocity(
        allow_prediction, reliable):
    e, reference = correction_step_estimator(), correction_step_estimator()
    before = e.diagnostics()
    now, dt = .083, .013
    z, r = np.array([.065, 0, 1]), np.eye(3) * 1e-8
    pred = reference.update(None, None, now, allow_prediction=True)
    # Independently reconstruct the posterior that would previously be committed.
    p = np.array(pred['covariance'])
    x = np.r_[pred['position_m'], pred['velocity_m_s']]
    k = np.linalg.solve(p[:3, :3] + r, p[:, :3].T).T
    candidate = x + k @ (z - x[:3])
    assert np.linalg.norm(candidate[3:]) < e.config.max_speed_m_s
    assert np.linalg.norm(z - [.04, 0, 1]) / dt < e.config.max_speed_m_s
    step = candidate[:3] - before['position_m']
    assert np.linalg.norm(step) / dt > 4.68
    out = e.update(z, r, now, allow_prediction=allow_prediction, reliable=reliable)
    assert out['rejection_reason'] == 'posterior_position_step'
    assert out['mahalanobis_sq'] < e.config.gate_chi2
    assert out['accepted'] == before['accepted'] and out['rejected'] == 1
    np.testing.assert_allclose(out['posterior_position_step_m'], step)
    assert out['posterior_position_speed_m_s'] == pytest.approx(np.linalg.norm(step) / dt)
    assert out['posterior_position_dt_s'] == pytest.approx(dt)
    # Rejected correction changes neither predicted x nor predicted P.
    np.testing.assert_allclose(out['covariance'], pred['covariance'])
    np.testing.assert_allclose(out['velocity_m_s'], pred['velocity_m_s'])
    assert out['valid'] == allow_prediction
    if allow_prediction:
        assert out['tracking_state'] == 'predicting'
        np.testing.assert_allclose(out['position_m'], pred['position_m'])
        assert out['published_speed_m_s'] <= e.config.max_speed_m_s + 1e-9
        assert out['publication_continuity'] == 'continuous'
    else:
        assert out['tracking_state'] == 'rejected' and out['position_m'] is None
        assert out['publication_continuity'] == 'invalid'


def test_tracking_correction_uses_latest_prediction_publish_timestamp():
    e = correction_step_estimator()
    pred = e.update(None, None, .08, allow_prediction=True)
    assert pred['valid'] and pred['publication_dt_s'] == pytest.approx(.01)
    out = e.update([.065, 0, 1], np.eye(3) * 1e-8, .083, allow_prediction=True)
    assert out['rejection_reason'] == 'posterior_position_step'
    assert out['posterior_position_dt_s'] == pytest.approx(.003)
    assert out['publication_dt_s'] == pytest.approx(.003)
    step = np.linalg.norm(out['posterior_position_step_m'])
    assert out['posterior_position_speed_m_s'] == pytest.approx(step / .003)
    assert out['valid'] and out['tracking_state'] == 'predicting'
    assert out['publication_segment'] == pred['publication_segment']


def test_reseed_after_position_step_rejection_is_separated_and_logged():
    e = correction_step_estimator()
    segment = e.diagnostics()['publication_segment']
    out = e.update([.065, 0, 1], np.eye(3) * 1e-8, .083)
    assert not out['valid'] and out['rejection_reason'] == 'posterior_position_step'
    assert out['publication_continuity'] == 'invalid'
    # A distant new cluster still requires four reliable confirmations. The
    # earlier rejected correction is not consistent with this cluster.
    for i in range(4):
        out = e.update([1, 0, 1], R, .093 + .01 * i)
        assert out['valid'] == (i == 3)
        assert out['publication_segment'] == segment + (i == 3)
    assert out['reset_reason'] == 'confirmed_reacquisition'
    assert out['velocity_m_s'] == [0., 0., 0.]
    assert out['publication_continuity'] == 'new_segment'
    assert out['publication_dt_s'] is None and out['published_speed_m_s'] is None
    np.testing.assert_allclose(out['position_m'], [1, 0, 1])
    out = e.update([1.001, 0, 1], R, .133)
    assert out['valid'] and out['publication_continuity'] == 'continuous'
    assert out['published_speed_m_s'] <= 2. + 1e-9


def test_invalid_interval_ends_baseline_without_bypassing_reliable_reseed():
    e = correction_step_estimator()
    segment = e.diagnostics()['publication_segment']
    e.update(None, None, .08)
    # Existing tracking history may resume via a gated observable update; it is
    # not a reseed, and reliable=False remains supported. The invalid interval
    # is explicitly represented rather than logging a misleading cross-gap speed.
    out = e.update([.065, 0, 1], np.eye(3) * 1e-8, .083, reliable=False)
    assert out['valid'] and out['tracking_state'] == 'tracking'
    assert out['reset_reason'] != 'confirmed_reacquisition'
    assert out['publication_segment'] == segment + 1
    assert out['publication_continuity'] == 'new_segment'
    assert out['published_speed_m_s'] is None


def test_speed_of_every_continuous_valid_publish_is_bounded_with_adaptive_r():
    e = correction_step_estimator()
    rng = np.random.default_rng(83)
    now = .07
    previous = e.diagnostics()
    for i in range(200):
        now += (.007, .013, .021)[i % 3]
        z = [min(.04 + .8 * (now - .07), 1.5), 0, 1]
        z[0] += rng.normal(0, .01)
        r = np.eye(3) * (.001 if i % 5 == 0 else .04)**2
        out = e.update(z, r, now, allow_prediction=True)
        if out['valid'] and previous['valid']:
            measured_speed = np.linalg.norm(np.array(out['position_m']) -
                                            previous['position_m']) / (now - previous['timestamp'])
            assert measured_speed <= e.config.max_speed_m_s + 1e-9
            assert out['published_speed_m_s'] == pytest.approx(measured_speed)
            assert out['publication_continuity'] == 'continuous'
            assert out['publication_segment'] == previous['publication_segment']
        elif out['valid']:
            assert out['publication_continuity'] == 'new_segment'
            assert out['published_speed_m_s'] is None
        previous = out


def test_published_speed_limit_numerical_tolerance():
    e = seeded()
    assert e._publication_speed_safe(2.)
    assert e._publication_speed_safe(2. + 5e-10)
    assert not e._publication_speed_safe(2. + 2e-9)
