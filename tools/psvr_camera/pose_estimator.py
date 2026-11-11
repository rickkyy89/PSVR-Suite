"""Independent positional CV Kalman filter, in metres and timestamp seconds.

No orientation or camera/session validation is inferred here. The caller owns R,
identity (session, epoch AND external alignment), and permission to predict.
reliable only authorizes acquisition/reseed; observable measurements with valid
R can still correct an existing filter when reliable=False and gating succeeds.
Malformed measurements raise ValueError without changing history; malformed time
instead invalidates and resets history, without seeding from that frame.
"""
from copy import deepcopy
from dataclasses import dataclass, fields
import math
from typing import Optional

import numpy as np


@dataclass(frozen=True)
class PoseEstimatorConfig:
    """Q uses continuous white acceleration: spectral density is std squared.

    recovery_confirmations consecutive reliable, mutually consistent samples
    are required both at startup and after loss. initial_velocity_std_m_s sets
    the zero-mean seed velocity uncertainty, not an assumed measured velocity.
    recovery_gate_chi2 is a fixed gate on a separate candidate CV trajectory;
    its uncertainty includes sample noise and positional process noise. The
    first candidate pair uses initial velocity uncertainty, so precise linear
    motion is not mistakenly required to be stationary during acquisition.

    adaptive_quiet is opt-in: only Q changes, never R, the gate, or velocity via
    pseudo-measurements. quiet_acceleration_std is sqrt(acceleration PSD), in the
    same convention as acceleration_std_m_s2. The detector uses covariance-
    weighted linear fits of accepted reliable optical samples, a time-smoothed
    trend, mean latent velocity and statistical hysteresis. No single 95%-NIS
    excursion switches mode without coherent motion evidence. A strong excursion
    (>16.27, fixed 3D threshold) may exit only AFTER the ordinary motion-Q gate
    (or the explicitly configured fixed onset-Q gate) and raw-jump check pass.
    Quiet entry applies quiet Q on the NEXT prediction; motion exit uses the
    alternative prediction from this frame's saved prior, never a second step.
    quiet_coherent_exit_chi2 adds a same-frame two-sample test: squared norm of
    (w_previous + w_current)/sqrt(2), with each innovation whitened by its S and
    direction cosine > .8. Its approximate null is chi-square with 3 DOF; temporal
    correlation/model mismatch may affect calibration. Only accepted reliable
    samples commit the pair. Missing/refused/partial observations break it.
    A quiet rejection disables quiet/history for the NEXT frame only: retained
    current predicted x/P and its gate are not changed to admit the observation.
    Unreliable or overly uncertain measurements can still update tracking, using
    motion Q, but do not train the detector. Quiet policy state is retained across
    a brief partial observation; reset/loss clears it. Dwell is interrupted by missing, refused,
    unreliable or overly uncertain observations.
    measurement_speed_noise_allowance_m is a separate opt-in displacement cap:
    min(cap, 3*sqrt(largest eigenvalue(R_current + R_last_accepted))). It relaxes
    only the raw measurement delta check, never latent/published speed limits.

    recovery_velocity_fit is opt-in and leaves the legacy zero-velocity recovery
    path unchanged when False. True requires at least four reliable confirmations
    (the caller's reliability contract includes >=4 LEDs). A separate candidate
    cluster seeds position at the LAST timestamp and velocity jointly by full-R
    generalized least squares. Its observation covariance includes the exact
    backward integrated-white-acceleration kernel q*min(a,b)^2*(3*max(a,b)-min(a,b))/6.
    This accounts for temporal process correlation and gives a full correlated
    6x6 endpoint covariance under the CV/diffuse-endpoint-prior model, rather than
    independent diagonal inflation. Optical R inputs are assumed independent.
    Fits failing residual, velocity or position uncertainty limits never seed,
    clamp velocity or fall back to zero. Rejected candidate measurements never
    correct the accepted filter or train its quiet detector.

    motion_onset_acceleration_std is an optional fixed root acceleration PSD.
    None uses the ordinary motion root, preserving the existing path. During a
    quiet prediction ONLY, this root replaces motion Q in the one alternative
    gate prediction and in a same-frame proposed exit, always from the saved
    previous posterior. It never changes quiet Q, performs retries/increases Q
    until a sample passes, or bypasses raw/posterior/publication speed limits.
    A rejected exit rolls back to the original quiet prediction and does not
    train the detector. An accepted exit logs the actual pulse root/Q/S; the
    following dynamic prediction uses acceleration_std_m_s2 again. The CV
    dynamics and Joseph update are unchanged, not a new filter.
    """
    acceleration_std_m_s2: float = 3.0
    gate_chi2: float = 16.27
    max_prediction_s: float = 0.08
    max_gap_s: float = 0.25
    max_speed_m_s: float = 2.0
    max_position_std_m: float = 0.08
    reliable_std_m: float = 0.04
    recovery_confirmations: int = 4
    initial_velocity_std_m_s: float = 1.0
    recovery_gate_chi2: float = 16.27
    adaptive_quiet: bool = False
    quiet_acceleration_std: float = 0.03
    quiet_enter_s: float = 0.25
    quiet_speed_enter_m_s: float = 0.03
    quiet_speed_exit_m_s: float = 0.08
    quiet_innovation_exit_chi2: float = 7.815
    quiet_trend_window_s: float = 0.12
    quiet_motion_sigma: float = 3.0
    quiet_coherent_exit_chi2: float = 11.34
    measurement_speed_noise_allowance_m: float = 0.0
    recovery_velocity_fit: bool = False
    motion_onset_acceleration_std: Optional[float] = None

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name == 'motion_onset_acceleration_std' and value is None:
                continue
            if field.name in ('adaptive_quiet', 'recovery_velocity_fit'):
                if not isinstance(value, bool):
                    raise ValueError(f'{field.name} must be bool')
            elif field.name == 'recovery_confirmations':
                if isinstance(value, bool) or not isinstance(value, int) or value < 2:
                    raise ValueError('recovery_confirmations must be an integer >= 2')
            elif (isinstance(value, bool) or not isinstance(value, (int, float))
                  or not math.isfinite(value)
                  or (value < 0 if field.name == 'measurement_speed_noise_allowance_m'
                      else value <= 0)):
                bound = ('nonnegative' if field.name == 'measurement_speed_noise_allowance_m'
                         else 'positive')
                raise ValueError(f'{field.name} must be finite and {bound}')
        if self.max_prediction_s > self.max_gap_s:
            raise ValueError('max_prediction_s must not exceed max_gap_s')
        if self.reliable_std_m > self.max_position_std_m:
            raise ValueError('reliable_std_m must not exceed max_position_std_m')
        if self.quiet_speed_enter_m_s >= self.quiet_speed_exit_m_s:
            raise ValueError('quiet speed thresholds must have hysteresis')
        if self.adaptive_quiet and self.quiet_acceleration_std > self.acceleration_std_m_s2:
            raise ValueError('quiet PSD must not exceed motion PSD')


def _identity_key(value):
    """Snapshot value identities, including numpy alignment arrays, not aliases."""
    if isinstance(value, np.ndarray):
        return ('array', value.shape, _identity_key(value.tolist()))
    if isinstance(value, dict):
        return {key: _identity_key(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return tuple(_identity_key(item) for item in value)
    return deepcopy(value)


def _std(covariance):
    # Largest principal-axis uncertainty, not merely the diagonal entries.
    return math.sqrt(max(0.0, float(np.linalg.eigvalsh(covariance)[-1])))


def _mahalanobis(innovation, covariance):
    whitened = np.linalg.solve(np.linalg.cholesky(covariance), innovation)
    return float(whitened @ whitened)


class PoseEstimator:
    """State [x,y,z,vx,vy,vz]; all returned dictionaries are detached snapshots.

    accepted/rejected count committed Kalman measurements / refused available
    measurements since reset (including identity/timestamp resets). Missing
    position or R is unobservable, not an available measurement, and increments
    neither counter. Confirmation samples have their own counter.
    Invalid outputs never expose position_m; covariance/velocity may describe
    the internal predicted state and are diagnostics, not a valid pose.
    A refused measurement can return valid=True, tracking_state='predicting':
    rejection_reason and rejected still report the refusal. Prediction never
    updates the last accepted measurement/time or the filter from that sample.
    Reseed requires the immediately preceding published result to be invalid.
    Consecutive valid outputs (tracking OR predicting) additionally obey a
    published-position speed limit, with 1e-9 m/s numerical tolerance. This is
    independent of the latent velocity: a Kalman correction can otherwise jump
    too far with a perfectly safe velocity state. An invalid output ends the
    publication segment; the next valid output logs 'new_segment', not a speed
    across the discontinuity. Segment IDs, like counters, are since reset.
    publication_dt_s / published_speed_m_s describe only continuous valid output;
    posterior_position_* records a refused correction, not the fallback output.
    """

    def __init__(self, config=None):
        self.config = PoseEstimatorConfig() if config is None else config
        if not isinstance(self.config, PoseEstimatorConfig):
            raise ValueError('config must be PoseEstimatorConfig')
        self.reset()

    def reset(self, reason='manual'):
        """Clear clock, identity, filter, candidates and counters (e.g. recenter)."""
        self._identity = None
        self._identity_set = False
        self._timestamp = None
        self._x = self._p = None
        self._last_measurement = self._last_measurement_time = None
        self._last_measurement_covariance = None
        self._clear_quiet()
        self._candidate = None
        self._recovery_samples = []
        self._recovery_fit_diagnostics = self._seed_diagnostics = None
        self._last_valid_position_for_seed = None
        self._interrupted = False
        self._accepted = self._rejected = 0
        self._published_position = self._published_timestamp = None
        self._publication_segment = 0
        self._reset_reason = str(reason)
        self._last = self._result('lost', False)

    def diagnostics(self):
        return deepcopy(self._last)

    def _clear_quiet(self):
        self._quiet_active = False
        self._quiet_history = []
        self._quiet_since = None
        self._quiet_smoothed_trend = None
        self._trend_diagnostics = None
        self._quiet_whitened_prev = None
        self._quiet_coherent_statistic = self._quiet_coherent_cosine = None
        self._quiet_exit_reason = None
        self._process_mode = 'motion'
        self._effective_acceleration_std = self.config.acceleration_std_m_s2

    def _noise_allowance(self, r, previous_r):
        cap = self.config.measurement_speed_noise_allowance_m
        if cap == 0 or r is None or previous_r is None:
            return 0.0
        return min(cap, 3 * _std(r * 0.5 + previous_r * 0.5) * math.sqrt(2))

    def _result(self, state, valid, *, innovation=None, distance=None, r=None,
                rejection_reason=None, posterior_position_step=None,
                posterior_position_speed=None, posterior_position_dt=None,
                innovation_covariance=None, baseline_motion_distance=None,
                noise_allowance=0.0, motion_gate_covariance=None,
                motion_gate_acceleration_std=None, quiet_probe_covariance=None,
                quiet_probe_distance=None):
        _, publication_dt, published_speed = (
            self._publication_step(self._x[:3]) if valid else (None, None, None))
        continuity = 'invalid'
        if valid:
            continuity = 'continuous'
            if self._published_position is None:
                self._publication_segment += 1
                continuity = 'new_segment'
            self._published_position = self._x[:3].copy()
            self._published_timestamp = self._timestamp
            if self.config.recovery_velocity_fit:
                self._last_valid_position_for_seed = self._x[:3].copy()
        else:
            self._published_position = self._published_timestamp = None
        result = dict(valid=bool(valid), tracking_state=state,
                      position_m=self._x[:3].tolist() if valid else None,
                      velocity_m_s=None if self._x is None else self._x[3:].tolist(),
                      covariance=None if self._p is None else self._p.tolist(),
                      innovation_m=None if innovation is None else innovation.tolist(),
                      mahalanobis_sq=distance,
                      innovation_covariance=(None if innovation_covariance is None
                                             else innovation_covariance.tolist()),
                      nis_3d=distance,
                      quiet_probe_innovation_covariance=None if quiet_probe_covariance is None else quiet_probe_covariance.tolist(),
                      quiet_probe_mahalanobis_sq=quiet_probe_distance,
                      nis_optical=None,  # Caller projects innovation/S onto its optical axis.
                      baseline_motion_mahalanobis_sq=baseline_motion_distance,
                      process_mode=self._process_mode,
                      effective_acceleration_std=self._effective_acceleration_std,
                      effective_q=self._effective_acceleration_std**2,
                      quiet_active=self._quiet_active,
                      quiet_next_mode='quiet' if self._quiet_active else 'motion',
                      quiet_coherent_statistic=self._quiet_coherent_statistic,
                      quiet_coherent_cosine=self._quiet_coherent_cosine,
                      quiet_exit_reason=self._quiet_exit_reason,
                      quiet_dwell_s=(0.0 if self._quiet_since is None else
                                     max(0.0, self._timestamp - self._quiet_since)),
                      quiet_sample_count=len(self._quiet_history),
                      quiet_trend=deepcopy(self._trend_diagnostics),
                      policy_flags=dict(adaptive_quiet=self.config.adaptive_quiet,
                                        quiet_coherent_exit_chi2=self.config.quiet_coherent_exit_chi2,
                                        measurement_speed_noise_allowance_m=
                                        self.config.measurement_speed_noise_allowance_m),
                      measurement_speed_noise_allowance_m=noise_allowance,
                      measurement_covariance=None if r is None else r.tolist(),
                      accepted=self._accepted, rejected=self._rejected,
                      recovery_count=0 if self._candidate is None else self._candidate[3],
                      reset_reason=self._reset_reason, rejection_reason=rejection_reason,
                      publication_continuity=continuity,
                      publication_segment=self._publication_segment,
                      publication_dt_s=publication_dt,
                      published_speed_m_s=published_speed,
                      posterior_position_step_m=(None if posterior_position_step is None
                                                 else posterior_position_step.tolist()),
                      posterior_position_speed_m_s=posterior_position_speed,
                      posterior_position_dt_s=posterior_position_dt,
                       timestamp=self._timestamp)
        if self.config.recovery_velocity_fit:
            result.update(seed_method=None if self._seed_diagnostics is None else 'full_r_cv_gls',
                          recovery_fit=deepcopy(self._recovery_fit_diagnostics),
                          seed_fit_quality=deepcopy(self._seed_diagnostics),
                          seed_jump_m=(None if self._seed_diagnostics is None else
                                       self._seed_diagnostics['jump_m']))
        if self.config.motion_onset_acceleration_std is not None:
            # Gate proposal S is NOT necessarily the retained/updated state's S.
            # baseline_motion_mahalanobis_sq refers to this alternative gate
            # when quiet; nis_3d always refers to the actual chosen prediction.
            result.update(motion_gate_covariance=(None if motion_gate_covariance is None
                                                 else motion_gate_covariance.tolist()),
                          motion_gate_acceleration_std=motion_gate_acceleration_std)
        self._last = deepcopy(result)
        return result

    def _lose(self, reason):
        self._x = self._p = None
        self._last_measurement = self._last_measurement_time = None
        self._last_measurement_covariance = None
        self._clear_quiet()
        self._interrupted = False
        self._reset_reason = reason

    @staticmethod
    def _measurement(position, covariance):
        if position is None or covariance is None:
            return None, None
        try:
            z = np.array(position, dtype=float, copy=True)
            r = np.array(covariance, dtype=float, copy=True)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError('invalid measurement') from exc
        if z.shape != (3,) or not np.isfinite(z).all():
            raise ValueError('position must contain three finite metres')
        if (r.shape != (3, 3) or not np.isfinite(r).all()
                or not np.allclose(r, r.T, rtol=1e-10, atol=1e-14)):
            raise ValueError('measurement covariance must be finite symmetric 3x3')
        # Average without overflowing when a finite external R is very large.
        r = r * 0.5 + r.T * 0.5
        try:
            np.linalg.cholesky(r)
        except np.linalg.LinAlgError as exc:
            raise ValueError('measurement covariance must be positive definite') from exc
        return z, r

    @staticmethod
    def _prediction_from(x, p, dt, acceleration_std):
        f = np.eye(6)
        f[:3, 3:] = np.eye(3) * dt
        # Exact continuous white-acceleration discretization, not dt**4/4.
        q = acceleration_std ** 2 * np.kron(
            [[dt**3 / 3, dt**2 / 2], [dt**2 / 2, dt]], np.eye(3))
        x = f @ x
        p = f @ p @ f.T + q
        return x, (p + p.T) * 0.5

    def _predict(self, dt, *, quiet=False):
        self._process_mode = 'quiet' if quiet else 'motion'
        self._effective_acceleration_std = (self.config.quiet_acceleration_std if quiet
                                            else self.config.acceleration_std_m_s2)
        self._x, self._p = self._prediction_from(
            self._x, self._p, dt, self._effective_acceleration_std)

    @staticmethod
    def _posterior(x, p, innovation, s, r):
        k = np.linalg.solve(s, p[:, :3].T).T
        x = x + k @ innovation
        a = np.eye(6)
        a[:, :3] -= k
        p = a @ p @ a.T + k @ r @ k.T
        return x, (p + p.T) * 0.5

    @staticmethod
    def _optical_trend(samples, now):
        """Full 3-axis covariance WLS; centre time/position for conditioning.

        Measurement covariances are independent optical inputs, not filtered P.
        Solve position and velocity jointly to account for their correlation;
        return velocity and its marginal 3x3 covariance, not a Kalman reseed.
        """
        origin = samples[0][1]
        design, response = [], []
        for timestamp, z, r, _ in samples:
            h = np.c_[np.eye(3), np.eye(3) * (timestamp - now)]
            chol = np.linalg.cholesky(r)
            design.append(np.linalg.solve(chol, h))
            response.append(np.linalg.solve(chol, z - origin))
        design, response = np.vstack(design), np.concatenate(response)
        beta, _, rank, _ = np.linalg.lstsq(design, response, rcond=None)
        if rank != 6:
            return None
        covariance = np.linalg.solve(design.T @ design, np.eye(6))
        covariance = (covariance + covariance.T) * 0.5
        return beta[3:], covariance[3:, 3:]

    def _coherent_proposal(self, innovation, s):
        """Pure proposal; never queue an innovation before the final gates pass."""
        whitened = np.linalg.solve(np.linalg.cholesky(s), innovation)
        statistic = cosine = None
        previous = self._quiet_whitened_prev
        if previous is not None:
            combined = (previous + whitened) / math.sqrt(2)
            statistic = float(combined @ combined)
            norm_product = float(np.linalg.norm(previous) * np.linalg.norm(whitened))
            if norm_product > 0:
                cosine = float(np.clip((previous @ whitened) / norm_product, -1., 1.))
        trigger = (statistic is not None and cosine is not None and cosine > .8
                   and statistic > self.config.quiet_coherent_exit_chi2)
        return whitened, statistic, cosine, trigger

    def _quiet_proposal(self, z, r, now, latent_velocity, nis_3d, coherent_exit=False):
        """Pure proposal. Commit ONLY after every measurement/posterior gate.

        Fit at most 256 accepted reliable samples within the actual time window.
        Smoothing uses real dt; its covariance is NOT divided by sample count
        (overlapping trend windows are correlated). This is conservative.
        """
        c = self.config
        samples = [entry for entry in self._quiet_history
                   if now - entry[0] <= c.quiet_trend_window_s + 1e-12]
        samples = (samples + [(now, z.copy(), r.copy(), latent_velocity.copy())])[-256:]
        active, since, smoothed = self._quiet_active, self._quiet_since, self._quiet_smoothed_trend
        diagnostics = None
        ready = (len(samples) >= 4 and
                 now - samples[0][0] >= .75 * c.quiet_trend_window_s)
        fit = self._optical_trend(samples, now) if ready else None
        significant = smoothed_significant = False
        mean_latent_speed = float(np.linalg.norm(np.mean([entry[3] for entry in samples], axis=0)))
        if fit is not None:
            velocity, covariance = fit
            interval = now - self._quiet_history[-1][0] if self._quiet_history else 0.0
            alpha = -math.expm1(-interval / c.quiet_trend_window_s)
            smoothed = velocity.copy() if smoothed is None else smoothed + alpha * (velocity - smoothed)
            normalized = math.sqrt(_mahalanobis(velocity, covariance))
            smoothed_normalized = math.sqrt(_mahalanobis(smoothed, covariance))
            significant = normalized >= c.quiet_motion_sigma
            smoothed_significant = smoothed_normalized >= c.quiet_motion_sigma
            diagnostics = dict(velocity_m_s=velocity.tolist(),
                               velocity_covariance=covariance.tolist(),
                               velocity_std_m_s=_std(covariance),
                               normalized_velocity=normalized,
                               smoothed_velocity_m_s=smoothed.tolist(),
                               smoothed_normalized_velocity=smoothed_normalized,
                               mean_latent_speed_m_s=mean_latent_speed,
                               sample_time_s=now)
        # Strong optical evidence must still have passed the normal motion-Q
        # gate. A 95%-NIS excursion alone is deliberately insufficient to exit.
        exit_motion = (coherent_exit or nis_3d > 16.27 or smoothed_significant or
                       (nis_3d > c.quiet_innovation_exit_chi2 and
                        (significant or mean_latent_speed > c.quiet_speed_exit_m_s)))
        if active and exit_motion:
            active, since = False, None
        elif not active:
            slow = (fit is not None and np.linalg.norm(smoothed) < c.quiet_speed_enter_m_s
                    and mean_latent_speed <= c.quiet_speed_exit_m_s
                    and not smoothed_significant and not coherent_exit)
            since = (now if since is None else since) if slow else None
            if since is not None and now - since >= c.quiet_enter_s - 1e-12:
                active = True
        return samples, active, since, smoothed, diagnostics

    def _commit_quiet(self, proposal):
        (self._quiet_history, self._quiet_active, self._quiet_since,
         self._quiet_smoothed_trend, self._trend_diagnostics) = proposal

    def _publication_step(self, position):
        """Compare to the actual previous valid publish, never to raw z or x^-.

        Only consecutive valid publications belong to the same segment. The
        clock is capture time, so tracking after prediction uses that prediction's
        timestamp, not the older last accepted measurement timestamp.
        """
        if self._published_position is None:
            return None, None, None
        dt = self._timestamp - self._published_timestamp
        step = position - self._published_position
        return step, dt, float(np.linalg.norm(step) / dt)

    def _publication_speed_safe(self, speed):
        return speed is None or speed <= self.config.max_speed_m_s + 1e-9

    def _prediction_allowed(self, allow_prediction):
        """Common bounded prediction policy for missing AND gated measurements."""
        if self._x is None:
            return False
        age = self._timestamp - self._last_measurement_time
        if age > self.config.max_prediction_s + 1e-12:
            self._lose('prediction_expired')
            return False
        if _std(self._p[:3, :3]) > self.config.max_position_std_m:
            self._lose('position_uncertainty')
            return False
        if np.linalg.norm(self._x[3:]) > self.config.max_speed_m_s:
            self._lose('speed_limit')
            return False
        _, _, speed = self._publication_step(self._x[:3])
        if not self._publication_speed_safe(speed):
            self._lose('posterior_position_step')
            return False
        return bool(allow_prediction)

    def _confirm(self, z, r, timestamp, reliable):
        if self.config.recovery_velocity_fit:
            return self._confirm_velocity_fit(z, r, timestamp, reliable)
        if not reliable or _std(r) > self.config.reliable_std_m:
            self._candidate = None
            return False
        count = 1
        velocity = previous_r_for_velocity = previous_dt = None
        if self._candidate is not None:
            (previous, previous_r, previous_time, previous_count,
             candidate_velocity, older_r, older_dt) = self._candidate
            dt = timestamp - previous_time
            delta = z - previous
            residual = delta
            if candidate_velocity is None:
                s = r + previous_r + np.eye(3) * (
                    self.config.initial_velocity_std_m_s**2 * dt**2)
            else:
                residual = delta - candidate_velocity * dt
                ratio = dt / older_dt
                # Prediction from the two preceding candidate measurements:
                # (1+ratio)*previous - ratio*older. Account for correlation
                # rather than treating the derived velocity as independent.
                s = r + (1 + ratio)**2 * previous_r + ratio**2 * older_r
            s = s + np.eye(3) * self.config.acceleration_std_m_s2**2 * dt**3 / 3
            if (0 < dt <= self.config.max_prediction_s
                    and np.linalg.norm(delta) <= self.config.max_speed_m_s * dt
                    and _mahalanobis(residual, s) <= self.config.recovery_gate_chi2):
                count = previous_count + 1
                velocity = delta / dt
                previous_r_for_velocity, previous_dt = previous_r.copy(), dt
        self._candidate = (z.copy(), r.copy(), timestamp, count, velocity,
                           previous_r_for_velocity, previous_dt)
        if count < self.config.recovery_confirmations:
            return False
        if self._last['valid']:
            # Enough evidence to recover, but never teleport directly from a
            # published prediction to a new seed. Caller publishes invalid now;
            # a subsequent consistent reliable measurement may commit the seed.
            return False
        # Never mix rejected measurements into the old filter. Reseed only after
        # an invalid interval and a reliable temporal cluster, with zero velocity.
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

    def _recovery_fit(self, samples):
        """GLS in endpoint coordinates with exact correlated process covariance.

        For lookback a, z(t_last-a) = p_last-a*v_last + integrated acceleration.
        A diffuse endpoint prior permits this backward representation. The
        Brownian integrated-acceleration covariance is exact for this model;
        no quiet-Q reduction is allowed during recovery. SVD of the whitened
        design avoids squaring its condition number in the parameter solve.
        """
        now, origin = samples[-1][0], samples[-1][1]
        duration = now - samples[0][0]
        diagnostics = dict(accepted=False, reason=None, sample_count=len(samples),
                           duration_s=float(duration), rank=None, residual_chi2=None,
                           residual_dof=3 * len(samples) - 6, reduced_chi2=None,
                           speed_m_s=None, position_std_m=None, jump_m=None,
                           covariance_model='exact_backward_white_acceleration')
        try:
            lookback = np.array([now - entry[0] for entry in samples])
            lo = np.minimum(lookback[:, None], lookback[None, :])
            hi = np.maximum(lookback[:, None], lookback[None, :])
            kernel = self.config.acceleration_std_m_s2**2 * lo**2 * (3 * hi - lo) / 6
            observation_covariance = np.kron(kernel, np.eye(3))
            for i, (_, _, r) in enumerate(samples):
                observation_covariance[3*i:3*i+3, 3*i:3*i+3] += r
            chol = np.linalg.cholesky(observation_covariance)
            h = np.vstack([np.c_[np.eye(3), -a * np.eye(3)] for a in lookback])
            response = np.concatenate([z - origin for _, z, _ in samples])
            design = np.linalg.solve(chol, h)
            whitened = np.linalg.solve(chol, response)
            u, s, vt = np.linalg.svd(design, full_matrices=False)
            rank = int(np.count_nonzero(s > s[0] * 1e-10))
            diagnostics['rank'] = rank
            if rank != 6:
                diagnostics['reason'] = 'rank_deficient'
                return None, None, diagnostics
            beta = vt.T @ ((u.T @ whitened) / s)
            p = (vt.T / s**2) @ vt
            p = (p + p.T) * .5
            x = beta.copy()
            x[:3] += origin
            residual = whitened - design @ beta
            chi2 = float(residual @ residual)
            reduced = chi2 / diagnostics['residual_dof']
            speed = float(np.linalg.norm(x[3:]))
            worst = _std(p[:3, :3])
            diagnostics.update(residual_chi2=chi2, reduced_chi2=reduced,
                               speed_m_s=speed, position_std_m=worst)
            if not np.isfinite(x).all() or not np.isfinite(p).all():
                diagnostics['reason'] = 'numerical_error'
            elif reduced > self.config.recovery_gate_chi2 / 3:
                # Fixed reduced-residual threshold, not a claimed exact chi2
                # quantile for variable sample counts or selected clusters.
                diagnostics['reason'] = 'fit_residual'
            elif speed > self.config.max_speed_m_s:
                diagnostics['reason'] = 'fit_speed'
            elif worst > min(self.config.reliable_std_m, self.config.max_position_std_m):
                diagnostics['reason'] = 'fit_uncertainty'
            else:
                np.linalg.cholesky(p)
                diagnostics.update(accepted=True, reason='ok')
                return x, p, diagnostics
        except (np.linalg.LinAlgError, FloatingPointError):
            diagnostics['reason'] = 'numerical_error'
        return None, None, diagnostics

    def _confirm_velocity_fit(self, z, r, timestamp, reliable):
        """Separate bounded candidate cluster; no mutation of tracking on failure."""
        if not reliable or _std(r) > self.config.reliable_std_m:
            self._candidate = None
            self._recovery_samples = []
            self._recovery_fit_diagnostics = dict(accepted=False, reason='unreliable')
            return False
        count = 1
        velocity = older_r = older_dt = None
        consistent = False
        if self._candidate is not None:
            previous, previous_r, previous_time, previous_count, candidate_v, r_old, dt_old = self._candidate
            dt = timestamp - previous_time
            delta = z - previous
            if 0 < dt <= self.config.max_prediction_s:
                if candidate_v is None:
                    residual = delta
                    s = r + previous_r + np.eye(3) * (
                        self.config.initial_velocity_std_m_s**2 * dt**2)
                    process_variance = self.config.acceleration_std_m_s2**2 * dt**3 / 3
                else:
                    ratio = dt / dt_old
                    residual = delta - candidate_v * dt
                    s = r + (1 + ratio)**2 * previous_r + ratio**2 * r_old
                    process_variance = self.config.acceleration_std_m_s2**2 * dt**2 * (dt_old + dt) / 3
                s = s + np.eye(3) * process_variance
                allowance = min(.015, self._noise_allowance(r, previous_r))
                consistent = (np.linalg.norm(delta) <= self.config.max_speed_m_s * dt + allowance
                              and _mahalanobis(residual, s) <= self.config.recovery_gate_chi2)
                if consistent:
                    count = previous_count + 1
                    velocity, older_r, older_dt = delta / dt, previous_r.copy(), dt
        if not consistent:
            self._recovery_samples = []
            self._recovery_fit_diagnostics = None
        needed = max(4, self.config.recovery_confirmations)
        self._recovery_samples = (self._recovery_samples + [(timestamp, z.copy(), r.copy())])[-needed:]
        self._candidate = (z.copy(), r.copy(), timestamp, count, velocity, older_r, older_dt)
        if count < needed:
            return False
        x, p, diagnostics = self._recovery_fit(self._recovery_samples)
        self._recovery_fit_diagnostics = diagnostics
        if x is None:
            # A bad fit must not force publication invalidation as a ready
            # cluster, nor poison the next fit with its older measurements.
            self._candidate = (z.copy(), r.copy(), timestamp, 1, None, None, None)
            self._recovery_samples = [(timestamp, z.copy(), r.copy())]
            return False
        if self._last['valid']:
            return False  # Existing invalid-before-reseed contract, unchanged.
        diagnostics['jump_m'] = (None if self._last_valid_position_for_seed is None else
                                float(np.linalg.norm(x[:3] - self._last_valid_position_for_seed)))
        self._lose('confirmed_reacquisition' if self._accepted else 'confirmed_initialization')
        self._x, self._p = x, p
        self._seed_diagnostics = deepcopy(diagnostics)
        self._last_measurement, self._last_measurement_time = z.copy(), timestamp
        self._last_measurement_covariance = r.copy()
        self._candidate = None
        self._recovery_samples = []
        self._accepted += 1
        if self.config.adaptive_quiet:
            self._quiet_history = [(timestamp, z.copy(), r.copy(), x[3:].copy())]
        return True

    def update(self, position, covariance, timestamp, *, identity=None,
               allow_prediction=False, reliable=True):
        """Consume one strictly increasing capture timestamp and optional R.

        reliable only controls acquisition/reseed; False does not prevent a
        gated update of an existing filter. None position or R is unobservable.
        Missing/gated samples can publish bounded prediction only if explicitly
        authorized. A confirmed recovery cluster forces an invalid 'rejected'
        result before reseed, even if prediction would otherwise be allowed.
        A long gap, exhausted dropout budget or unsafe velocity discards the
        filter; subsequent poses require fresh reliable confirmations.
        A posterior correction exceeding the speed limit relative to the previous
        published pose is rejected as 'posterior_position_step', without changing
        the predicted x/P. Authorized bounded prediction remains possible. Invalid
        output ends publication continuity; subsequent valid output is explicitly
        logged as a new segment, including controlled recovery/reseed.
        """
        try:
            now = float(timestamp)
        except (TypeError, ValueError, OverflowError):
            now = float('nan')
        key = _identity_key(identity)
        changed = self._identity_set and key != self._identity
        # Identity changes do not bypass clock safety. Call reset explicitly if
        # a producer starts a new clock domain as well as a new session.
        if (not math.isfinite(now) or now < 0
                or (self._timestamp is not None and now <= self._timestamp)):
            self.reset('timestamp_error')
            return self._result('timestamp_error', False)
        z, r = self._measurement(position, covariance)
        if changed:
            self.reset('identity_changed')
        self._identity, self._identity_set = key, True
        dt = None if self._timestamp is None else now - self._timestamp
        self._timestamp = now
        if dt is not None and dt > self.config.max_gap_s:
            self._lose('long_gap')
            self._candidate = None
            if self.config.recovery_velocity_fit:
                self._recovery_samples = []
        if (self._x is not None and self._interrupted
                and now - self._last_measurement_time > self.config.max_prediction_s + 1e-12):
            self._lose('prediction_expired')
        prior = None
        detector_eligible = (self.config.adaptive_quiet and z is not None and reliable
                             and _std(r) <= self.config.reliable_std_m)
        if self._x is not None:
            if self.config.adaptive_quiet:
                prior = self._x, self._p
            self._predict(dt, quiet=(self.config.adaptive_quiet and self._quiet_active
                                    and (z is None or detector_eligible)))
            if np.linalg.norm(self._x[3:]) > self.config.max_speed_m_s:
                self._lose('speed_limit')
                self._candidate = None
                return self._result('lost', False, r=r, rejection_reason='speed_limit')
        if z is None:
            self._candidate = None
            if self.config.recovery_velocity_fit:
                self._recovery_samples = []
            self._interrupted = True
            self._quiet_since = None
            self._quiet_whitened_prev = None
            self._quiet_coherent_statistic = self._quiet_coherent_cosine = None
            self._quiet_exit_reason = None
            valid = self._prediction_allowed(allow_prediction)
            return self._result('predicting' if valid else 'lost', valid, r=r,
                                rejection_reason='unobservable')
        if self._x is None:
            valid = self._confirm(z, r, now, reliable)
            return self._result('tracking' if valid else 'acquiring', valid, r=r)
        innovation = z - self._x[:3]
        s = self._p[:3, :3] + r
        distance = _mahalanobis(innovation, s)
        quiet_probe_covariance = s.copy() if self._process_mode == 'quiet' else None
        quiet_probe_distance = distance if quiet_probe_covariance is not None else None
        baseline_distance = distance if self.config.adaptive_quiet else None
        motion_prediction = None
        motion_gate_root = None
        if self._process_mode == 'quiet':
            motion_gate_root = (self.config.acceleration_std_m_s2
                if self.config.motion_onset_acceleration_std is None
                else self.config.motion_onset_acceleration_std)
            motion_prediction = self._prediction_from(
                *prior, dt, motion_gate_root)
            motion_innovation = z - motion_prediction[0][:3]
            motion_s = motion_prediction[1][:3, :3] + r
            baseline_distance = _mahalanobis(motion_innovation, motion_s)
        elapsed = now - self._last_measurement_time
        implied_speed = np.linalg.norm(z - self._last_measurement) / elapsed
        noise_allowance = self._noise_allowance(r, self._last_measurement_covariance)
        reason = None
        posterior_position_step = posterior_position_speed = None
        posterior_position_dt = None
        quiet_proposal = None
        coherent_proposal = None
        original_prediction = self._x, self._p, self._process_mode, self._effective_acceleration_std
        if implied_speed > self.config.max_speed_m_s + noise_allowance / elapsed:
            reason = 'implied_speed'
        elif (distance if baseline_distance is None else baseline_distance) > self.config.gate_chi2:
            reason = 'mahalanobis_gate'
        else:
            x, p = self._posterior(self._x, self._p, innovation, s, r)
            if detector_eligible:
                coherent_proposal = self._coherent_proposal(innovation, s)
                quiet_proposal = self._quiet_proposal(z, r, now, x[3:], distance,
                                                      coherent_proposal[3])
                if self._process_mode == 'quiet' and not quiet_proposal[1]:
                    # Exit on THIS frame. Recompute from the saved prior once
                    # with motion Q, never predict the already predicted state.
                    self._x, self._p = motion_prediction
                    self._process_mode = 'motion'
                    self._effective_acceleration_std = motion_gate_root
                    innovation, s, distance = motion_innovation, motion_s, baseline_distance
                    x, p = self._posterior(self._x, self._p, innovation, s, r)
                    quiet_proposal[0][-1] = (now, z.copy(), r.copy(), x[3:].copy())
                    if quiet_proposal[4] is not None:
                        quiet_proposal[4]['mean_latent_speed_m_s'] = float(np.linalg.norm(
                            np.mean([entry[3] for entry in quiet_proposal[0]], axis=0)))
            step, step_dt, speed = self._publication_step(x[:3])
            if np.linalg.norm(x[3:]) > self.config.max_speed_m_s:
                reason = 'posterior_speed'
            elif _std(p[:3, :3]) > self.config.max_position_std_m:
                reason = 'position_uncertainty'
            elif not self._publication_speed_safe(speed):
                reason = 'posterior_position_step'
                posterior_position_step, posterior_position_speed = step, speed
                posterior_position_dt = step_dt
            else:
                self._x, self._p = x, p
                self._last_measurement = z.copy()
                self._last_measurement_time = now
                self._last_measurement_covariance = r.copy()
                self._candidate = None
                if self.config.recovery_velocity_fit:
                    self._recovery_samples = []
                self._interrupted = False
                self._accepted += 1
                if quiet_proposal is not None:
                    exited = self._quiet_active and not quiet_proposal[1]
                    self._commit_quiet(quiet_proposal)
                    self._quiet_whitened_prev = (coherent_proposal[0] if
                        self._process_mode == original_prediction[2] else
                        np.linalg.solve(np.linalg.cholesky(s), innovation))
                    self._quiet_coherent_statistic = coherent_proposal[1]
                    self._quiet_coherent_cosine = coherent_proposal[2]
                    self._quiet_exit_reason = ('coherent_innovation' if coherent_proposal[3]
                                               else 'motion_evidence') if exited else None
                elif self.config.adaptive_quiet:
                    self._quiet_since = None
                    self._quiet_whitened_prev = None
                    self._quiet_coherent_statistic = self._quiet_coherent_cosine = None
                    self._quiet_exit_reason = None
                return self._result('tracking', True, innovation=innovation,
                                    distance=distance, r=r, innovation_covariance=s,
                                    baseline_motion_distance=baseline_distance,
                                    noise_allowance=noise_allowance,
                                    motion_gate_covariance=motion_s if motion_prediction is not None else None,
                                     motion_gate_acceleration_std=motion_gate_root,
                                     quiet_probe_covariance=quiet_probe_covariance,
                                     quiet_probe_distance=quiet_probe_distance)
        # Provisional detector results (including an exit) must not learn from
        # a rejected posterior. Retain only the original prediction, not a
        # measurement correction or a second prediction with altered Q.
        provisional_exit = self._process_mode != original_prediction[2]
        self._x, self._p, self._process_mode, self._effective_acceleration_std = original_prediction
        if provisional_exit:
            # S/NIS must describe the retained prediction after rollback, not
            # a discarded tentative motion-Q update. The motion-Q gate statistic
            # remains separately logged in baseline_motion_mahalanobis_sq.
            innovation = z - self._x[:3]
            s = self._p[:3, :3] + r
            distance = _mahalanobis(innovation, s)
        self._quiet_since = None
        self._quiet_whitened_prev = None
        self._quiet_coherent_statistic = self._quiet_coherent_cosine = None
        self._quiet_exit_reason = None
        if self.config.adaptive_quiet and (self._quiet_active or self._process_mode == 'quiet'):
            # Policy-only exit for the NEXT prediction. Preserve the current
            # quiet prediction bitwise; no covariance inflation or gate retry.
            current_mode, current_acceleration = self._process_mode, self._effective_acceleration_std
            self._clear_quiet()
            self._process_mode, self._effective_acceleration_std = current_mode, current_acceleration
            self._quiet_exit_reason = 'measurement_rejected'
        self._interrupted = True
        if reason in ('posterior_speed', 'position_uncertainty'):
            self._lose(reason)
        valid = self._confirm(z, r, now, reliable)
        if valid:
            # Successful reseed: this frame is accepted by _confirm, not
            # rejected. Return without a stale rejection_reason and without
            # incrementing rejected (diagnostic-only; gates/Q/valid unchanged).
            return self._result('tracking', True,
                                innovation=innovation, distance=distance, r=r,
                                rejection_reason=None,
                                posterior_position_step=posterior_position_step,
                                posterior_position_speed=posterior_position_speed,
                                posterior_position_dt=posterior_position_dt,
                                innovation_covariance=s,
                                baseline_motion_distance=baseline_distance,
                                noise_allowance=noise_allowance,
                                motion_gate_covariance=motion_s if motion_prediction is not None else None,
                                motion_gate_acceleration_std=motion_gate_root,
                                quiet_probe_covariance=quiet_probe_covariance,
                                quiet_probe_distance=quiet_probe_distance)
        self._rejected += 1
        state = 'rejected'
        if not valid:
            recovery_ready = (self._candidate is not None and
                              self._candidate[3] >= (max(4, self.config.recovery_confirmations)
                                  if self.config.recovery_velocity_fit else self.config.recovery_confirmations))
            # Evaluate bounds even when publication is prohibited, so unsafe
            # history is discarded rather than silently kept for a later update.
            valid = self._prediction_allowed(allow_prediction and not recovery_ready)
            if valid:
                state = 'predicting'
        return self._result(state, valid,
                            innovation=innovation, distance=distance, r=r,
                            rejection_reason=reason,
                            posterior_position_step=posterior_position_step,
                            posterior_position_speed=posterior_position_speed,
                            posterior_position_dt=posterior_position_dt,
                            innovation_covariance=s,
                             baseline_motion_distance=baseline_distance,
                             noise_allowance=noise_allowance,
                             motion_gate_covariance=motion_s if motion_prediction is not None else None,
                              motion_gate_acceleration_std=motion_gate_root,
                              quiet_probe_covariance=quiet_probe_covariance,
                              quiet_probe_distance=quiet_probe_distance)
