"""Exclusive final-position filter selection; wire binding remains camera-owned."""
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import math
import numpy as np

from . import position_filter
from .pose_estimator import PoseEstimator, PoseEstimatorConfig
from .pose_uncertainty import inflate_optical_covariance


# Sentinel distinguishing "packet key absent" (contract error -> ValueError)
# from "key present with None value" (legitimate lost-packet payload).
_MISSING = object()


@dataclass(frozen=True)
class UncertaintyConfig:
    depth_variance_factor: float = 1.0
    depth_extra_std_m: float = 0.0

    def __post_init__(self):
        if (isinstance(self.depth_variance_factor, bool) or isinstance(self.depth_extra_std_m, bool)
                or not math.isfinite(self.depth_variance_factor) or self.depth_variance_factor < 1
                or not math.isfinite(self.depth_extra_std_m) or self.depth_extra_std_m < 0):
            raise ValueError('invalid covariance inflation')


def load_position_config(path=None):
    if path is None:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) - {"filter", "kalman", "one_euro", "uncertainty", "prediction_output"}:
        raise ValueError("unknown position config fields")
    if data.get("filter", "one_euro") not in ("one_euro", "kalman"):
        raise ValueError("unknown position filter")
    # B6-01: non-dict sections and unknown/retired keys are caller errors.
    # The suite loader only catches (OSError, ValueError), so TypeError must
    # never leak: report every unusable config as ValueError.
    for section in ("kalman", "one_euro", "uncertainty"):
        if section in data and not isinstance(data[section], dict):
            raise ValueError(f"invalid position config section {section!r}: must be an object")
    try:
        PoseEstimatorConfig(**data.get("kalman", {}))
    except TypeError as exc:
        raise ValueError(f"invalid kalman position config: {exc}") from exc
    try:
        UncertaintyConfig(**data.get("uncertainty", {}))
    except TypeError as exc:
        raise ValueError(f"invalid uncertainty position config: {exc}") from exc
    if data.get("prediction_output", "camera") not in ("camera", "driver_hold"):
        raise ValueError('unknown prediction publication policy')
    try:
        position_filter.HeadPositionFilter(**data.get("one_euro", {}))
    except TypeError as exc:
        raise ValueError(f"invalid one_euro position config: {exc}") from exc
    return data


class PositionPipeline:
    def __init__(self, mode="one_euro", *, kalman=None, one_euro=None,
                 uncertainty=None, prediction_output="camera"):
        if mode not in ("one_euro", "kalman"):
            raise ValueError("unknown position filter")
        self.mode = mode
        # B6-01: same contract as load_position_config — sections must be
        # dicts and unknown keys report ValueError, never TypeError. None
        # still normalizes to {} exactly as before for every caller.
        for name, section in (("kalman", kalman), ("one_euro", one_euro),
                              ("uncertainty", uncertainty)):
            if section is not None and not isinstance(section, dict):
                raise ValueError(f"invalid position config section {name!r}: must be an object")
        try:
            self.uncertainty = UncertaintyConfig(**(uncertainty or {}))
        except TypeError as exc:
            raise ValueError(f"invalid uncertainty position config: {exc}") from exc
        if prediction_output not in ("camera", "driver_hold"):
            raise ValueError('unknown prediction publication policy')
        self.prediction_output = prediction_output
        try:
            self.filter = (position_filter.HeadPositionFilter(**(one_euro or {})) if mode == "one_euro"
                           else PoseEstimator(PoseEstimatorConfig(**(kalman or {}))))
        except TypeError as exc:
            raise ValueError(f"invalid {mode} position config: {exc}") from exc
        self._last = {}

    def reset(self, reason="manual"):
        if self.mode == "kalman":
            self.filter.reset(reason)
        else:
            self.filter.reset()
        self._last = {"type": self.mode, "reset_reason": reason}

    def invalid(self, now):
        """Mark the current frame unusable without fresh optical/IMU context.

        The Kalman and One Euro branches stay intentionally asymmetric, both
        fail-closed (B6-04): Kalman performs a full reset — clock, identity
        and acquisition candidates are discarded, so further confirmations are
        needed before publishing again (~80 ms of invalid) — while One Euro
        keeps its brief-loss hold (~100 ms) and only refreshes the last-known
        diagnostics below. Do not unify them: predicting from a stale binding
        would be worse than a short additional dropout.
        """
        # No frame/fresh IMU: never publish a prediction with old binding data.
        if self.mode == "kalman":
            self.reset("camera_read_failure")
        else:
            self.filter.invalid(now)
            self._last = {**self.filter.diagnostics(), "type": self.mode,
                          "quality": {"accepted": False, "reliable": False,
                                      "reason": "camera_read_failure"},
                          "reset_reason": "camera_read_failure", "invalid_at": now}

    def filter_packet(self, packet, alignment, now, *, covariance=None,
                      quality=None, prediction_allowed=False, context_valid=None):
        quality = quality or {}
        # B6-08: malformed packets report ValueError like every other input
        # validation here, never a bare KeyError.
        if packet is None:
            raw = None
        else:
            valid = packet.get("valid", _MISSING)
            if valid is _MISSING:
                raise ValueError("position packet missing 'valid'")
            if not valid:
                raw = None
            else:
                raw = packet.get("position_m", _MISSING)
                if raw is _MISSING:
                    raise ValueError("position packet missing 'position_m'")
        if self.mode == "one_euro":
            # B6-02: an explicit invalid context is a hard boundary here too:
            # hold the filter, refresh diagnostics and publish invalid. An
            # omitted context (None) keeps the historic pass-through; only an
            # explicit False triggers this branch.
            if context_valid is False:
                self.filter.invalid(now)
                self._last = {**self.filter.diagnostics(), "type": self.mode, "quality": quality}
                if packet is None:
                    return None
                return {**packet, "valid": False, "state": "lost", "position_m": None}
            output = self.filter.filter_packet(packet, alignment, now)
            self._last = {**self.filter.diagnostics(), "type": self.mode, "quality": quality}
            return output
        if packet is None:
            identity = None
        else:
            session_id = packet.get("session_id", _MISSING)
            if session_id is _MISSING:
                raise ValueError("position packet missing 'session_id'")
            identity = (session_id, packet["recenter_epoch"], alignment)
        a = None if alignment is None else np.asarray(alignment, float)
        alignment_valid = (a is not None and a.shape == (3, 3) and np.isfinite(a).all()
                           and np.allclose(a.T @ a, np.eye(3), atol=1e-6)
                           and np.isclose(np.linalg.det(a), 1., atol=1e-6))
        # A bad IMU/context is a hard boundary, not merely an optical dropout.
        # B6-05: an omitted context means "no fresh context observed" (raw
        # only); permission to predict must never imply validity. Production
        # callers always pass an explicit boolean, so only callers omitting
        # the parameter change behaviour.
        if context_valid is None:
            context_valid = raw is not None
        if not alignment_valid:
            context_valid = False
            quality = {**quality, 'accepted':False, 'reliable':False, 'reason':'invalid_alignment'}
        if not context_valid:
            self.filter.reset("invalid_context")
            raw = covariance = None
            prediction_allowed = False
        raw_covariance = covariance
        covariance = inflate_optical_covariance(covariance, alignment,
            self.uncertainty.depth_variance_factor, self.uncertainty.depth_extra_std_m)
        quality = dict(quality)
        # B6-07: a discarded input covariance leaves the measurement
        # unobservable, so the producer's accepted/reliable flags must not
        # survive in diagnostics. Only the never-provided case is relabelled
        # here; the inflation gate below keeps its own reason.
        if raw is not None and covariance is None and raw_covariance is None:
            quality.update(accepted=False, reliable=False, reason='missing_covariance')
        if covariance is not None and (self.uncertainty.depth_variance_factor != 1
                                      or self.uncertainty.depth_extra_std_m != 0):
            worst = float(np.sqrt(np.linalg.eigvalsh(covariance)[-1]))
            quality.update(raw_worst_std_m=quality.get('worst_std_m'), worst_std_m=worst)
            quality['reliable'] = bool(quality.get('reliable', False) and
                                       worst <= self.filter.config.reliable_std_m)
            if worst > self.filter.config.max_position_std_m:
                covariance = None
                quality.update(accepted=False, reliable=False, reason='inflated_uncertainty')
        result = self.filter.update(raw, covariance, now, identity=identity,
                                    allow_prediction=prediction_allowed,
                                    reliable=bool(quality.get("reliable", False)))
        estimator_valid = result['valid']
        publish_valid = estimator_valid and (self.prediction_output == 'camera' or
                                             result['tracking_state'] != 'predicting')
        nis_optical = None
        if result.get('innovation_covariance') is not None and alignment_valid:
            u = np.asarray(alignment, float).T @ [0., 0., 1.]
            innovation = np.asarray(result['innovation_m'], float)
            s = np.asarray(result['innovation_covariance'], float)
            nis_optical = float((u @ innovation)**2 / (u @ s @ u))
        self._last = {**result, "valid":publish_valid, "estimator_valid":estimator_valid,
                      "type": self.mode, "quality": quality, "nis_optical":nis_optical,
                      "prediction_output":self.prediction_output,
                      "quality_rejected_measurement":bool(raw is not None and covariance is None),
                      "uncertainty_settings":asdict(self.uncertainty),
                      "raw_measurement_covariance":None if raw_covariance is None else np.asarray(raw_covariance).tolist(),
                      "settings": asdict(self.filter.config),
                      # B6-07: snapshot the raw measurement; never alias the
                      # caller's list inside diagnostics state.
                      "raw_world_position_m": None if raw is None else list(raw),
                      "filtered_world_position_m": result["position_m"]}
        if packet is None:
            return None
        return {**packet, "valid": publish_valid,
                "state": "tracking" if publish_valid else "lost",
                "position_m": result["position_m"] if publish_valid else None}

    def diagnostics(self):
        from copy import deepcopy
        return deepcopy(self._last)
