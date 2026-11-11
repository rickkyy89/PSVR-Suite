"""Shared deterministic datasets for Kalman tuning, not hardware validation.

The first six scenarios replicate :mod:`benchmark_position_filters` exactly
(same names, same single-RNG order, same noise/outlier/dropout patterns) so
baseline results are identical. Extra scenarios isolate depth-axis behaviour.
R tuning is applied exactly once, at runtime, by ``PositionPipeline`` via
:func:`pose_uncertainty.inflate_optical_covariance`; this module never
pre-inflates. ``_effective_covariance`` remains as a legacy helper that
delegates to the canonical function, for tests only.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np

from .geometry import rodrigues
from .pose_uncertainty import inflate_optical_covariance
from .position_pipeline import PositionPipeline
from .replay_position_log import metrics

BASE_SCENARIOS = ("stationary", "linear", "rapid", "depth", "outlier", "dropout")
EXTRA_SCENARIOS = ("slow_depth", "tilted_depth", "partial_leds",
                   "noisy_fast_depth", "correlated_depth")
ALL_SCENARIOS = BASE_SCENARIOS + EXTRA_SCENARIOS + ("onset",)
BASE_ONE_EURO = {"min_cutoff_hz": 2.5, "beta": 8.0}
INITIAL_Q3_KALMAN = {"acceleration_std_m_s2": 3.0, "adaptive_quiet": False,
                     "measurement_speed_noise_allowance_m": 0.0}
BASE_NOISE_STD = np.array([0.0015, 0.0015, 0.005])
BASE_R = np.diag(np.square(BASE_NOISE_STD))
CHI2_3_P05 = 0.352
CHI2_3_P95 = 7.815
OPTIMIZED_PROFILE = "configurations/position_kalman_optimized_20261013.json"


def _truth_position(name, elapsed):
    position = np.array([0.0, 0.0, 1.0])
    if name == "linear":
        position[0] += 0.1 * elapsed
    if name in ("rapid", "depth"):
        phase = elapsed % 2
        displacement = 0.1 * np.clip((phase - 0.5) / 0.1, 0, 1)
        displacement -= 0.1 * np.clip((phase - 1.5) / 0.1, 0, 1)
        position[2 if name == "depth" else 0] += displacement
    return position


def _packet(session, epoch, seq, now, valid, position):
    return {
        "session_id": session,
        "recenter_epoch": epoch,
        "seq": seq,
        "monotonic_s": now,
        "valid": bool(valid),
        "state": "tracking" if valid else "lost",
        "position_m": None if not valid or position is None else [float(v) for v in position],
    }


def _base_frame(rng, name, i, now, elapsed):
    """One frame identical to benchmark_position_filters.benchmark()."""
    position = _truth_position(name, elapsed)
    raw = position + rng.normal(0, [0.0015, 0.0015, 0.005])
    if name == "outlier" and i in (200, 400, 600):
        raw = raw + np.array([0.5, 0.0, 0.4])
    valid = not (name == "dropout" and (200 <= i < 204 or 400 <= i < 420))
    packet = {
        "session_id": "benchmark",
        "recenter_epoch": 0,
        "valid": valid,
        "state": "tracking" if valid else "lost",
        "position_m": raw.tolist() if valid else None,
    }
    tag = "clean"
    if name == "outlier" and i in (200, 400, 600):
        tag = "outlier"
    if name == "dropout" and (200 <= i < 204 or 400 <= i < 420):
        tag = "dropout"
    entry = {
        "event": "pose",
        "monotonic_s": now,
        "packet": packet,
        "alignment": np.eye(3).tolist(),
        "covariance": np.diag(np.square([0.0015, 0.0015, 0.005])).tolist(),
        "quality": {"reliable": True, "accepted": valid,
                    "reason": "ok" if valid else "optical_dropout"},
        "context_valid": True,
        "prediction_allowed": True,
        "led_count": 5,
        "tag": tag,
    }
    truth = {"monotonic_s": now, "position_m": position.tolist()}
    return entry, truth, tag


def make_scenarios(frames=900, seed=6130):
    """Build shared deterministic datasets.

    Returns ``{name: {"inputs": [...], "reference": [...], "classification": [...],
    "static": bool}}``. The six base scenarios consume ``seed`` RNG first, in
    benchmark order, so their packets match ``benchmark_position_filters``.
    Every variant filters the same inputs per scenario.
    """
    frames = int(frames)
    if frames < 1:
        raise ValueError("frames must be positive")
    if frames > 100_000:
        raise ValueError("frames above 100000 would exhaust memory; split the run")
    rng = np.random.default_rng(seed)
    out = {}
    for name in BASE_SCENARIOS:
        inputs, reference, classification = [], [], []
        for i in range(frames):
            now = 1.0 + i / 75.0
            elapsed = i / 75.0
            entry, truth, tag = _base_frame(rng, name, i, now, elapsed)
            inputs.append(entry)
            reference.append(truth)
            classification.append(tag)
        out[name] = {"inputs": inputs, "reference": reference,
                     "classification": classification, "static": True}
    out["slow_depth"] = _slow_depth(rng, frames)
    out["tilted_depth"] = _tilted_depth(rng, frames)
    out["partial_leds"] = _partial_leds(rng, frames)
    out["noisy_fast_depth"] = _noisy_fast_depth(rng, frames)
    out["correlated_depth"] = _correlated_depth(rng, frames)
    out["onset"] = _onset(rng, frames)
    return out


def _slow_depth(rng, frames):
    """Gentle ramp plus sinusoid, speed always <= 0.05 m/s."""
    inputs, reference, classification = [], [], []
    for i in range(frames):
        now = 1.0 + i / 75.0
        elapsed = i / 75.0
        position = np.array([0.0, 0.0, 1.0 + 0.02 * elapsed + 0.05 * math.sin(2 * math.pi * elapsed / 12.0)])
        raw = position + rng.normal(0, [0.0015, 0.0015, 0.005])
        packet = _packet("benchmark", 0, i, now, True, raw)
        inputs.append({
            "event": "pose", "monotonic_s": now, "packet": packet,
            "alignment": np.eye(3).tolist(), "covariance": BASE_R.tolist(),
            "quality": {"reliable": True, "accepted": True, "reason": "ok"},
            "context_valid": True, "prediction_allowed": True,
            "led_count": 5, "tag": "clean",
        })
        reference.append({"monotonic_s": now, "position_m": position.tolist()})
        classification.append("clean")
    return {"inputs": inputs, "reference": reference, "classification": classification, "static": False}


TILTED_A = rodrigues(np.array([0.3, 0.7, 0.0]))
TILTED_U = (TILTED_A.T @ np.array([0.0, 0.0, 1.0])).reshape(3)


def _tilted_depth(rng, frames):
    """Depth motion along the true optical axis ``u = A.T @ eZ``.

    Noise is drawn in the camera frame and rotated to world with ``A.T @ n``.
    """
    inputs, reference, classification = [], [], []
    r_cam = np.diag(np.square(np.array([0.0015, 0.0015, 0.005])))
    base = np.array([0.0, 0.0, 1.0])
    for i in range(frames):
        now = 1.0 + i / 75.0
        elapsed = i / 75.0
        phase = elapsed % 2
        displacement = 0.1 * np.clip((phase - 0.5) / 0.1, 0, 1)
        displacement -= 0.1 * np.clip((phase - 1.5) / 0.1, 0, 1)
        position = base + TILTED_U * displacement
        noise_cam = rng.normal(0, [0.0015, 0.0015, 0.005])
        raw = position + TILTED_A.T @ noise_cam
        r_world = TILTED_A.T @ r_cam @ TILTED_A
        packet = _packet("benchmark", 0, i, now, True, raw)
        inputs.append({
            "event": "pose", "monotonic_s": now, "packet": packet,
            "alignment": TILTED_A.tolist(), "covariance": r_world.tolist(),
            "quality": {"reliable": True, "accepted": True, "reason": "ok"},
            "context_valid": True, "prediction_allowed": True,
            "led_count": 5, "tag": "clean",
        })
        reference.append({"monotonic_s": now, "position_m": position.tolist()})
        classification.append("clean")
    return {"inputs": inputs, "reference": reference, "classification": classification, "static": False}


def _partial_leds(rng, frames):
    """Seed 20x5LED, then 10x2LED loss, 30x3LED (cannot reinit), 30x4LED, rest 5LED."""
    inputs, reference, classification = [], [], []
    for i in range(frames):
        now = 1.0 + i / 75.0
        position = np.array([0.0, 0.0, 1.0])
        raw = position + rng.normal(0, [0.0015, 0.0015, 0.005])
        if i < 20:
            leds = 5
        elif i < 30:
            leds = 2
        elif i < 60:
            leds = 3
        elif i < 90:
            leds = 4
        else:
            leds = 5
        if leds == 2:
            covariance, quality, tag = None, {"reliable": False, "accepted": False,
                                              "reason": "insufficient_leds"}, "partial_2led"
        elif leds == 3:
            covariance, quality, tag = BASE_R.tolist(), {"reliable": False, "accepted": True,
                                                         "reason": "unreliable_3led"}, "partial_3led"
        elif leds == 4:
            covariance, quality, tag = BASE_R.tolist(), {"reliable": True, "accepted": True,
                                                         "reason": "ok"}, "partial_4led"
        else:
            covariance, quality, tag = BASE_R.tolist(), {"reliable": True, "accepted": True,
                                                         "reason": "ok"}, "clean_5led"
        packet = _packet("benchmark", 0, i, now, True, raw)
        inputs.append({
            "event": "pose", "monotonic_s": now, "packet": packet,
            "alignment": np.eye(3).tolist(), "covariance": covariance,
            "quality": dict(quality), "context_valid": True, "prediction_allowed": True,
            "led_count": leds, "tag": tag,
        })
        reference.append({"monotonic_s": now, "position_m": position.tolist()})
        classification.append(tag)
    return {"inputs": inputs, "reference": reference, "classification": classification, "static": True}


def _noisy_fast_depth(rng, frames):
    """1.7 m/s triangular Z motion, 0.5 m range, 5 mm isotropic noise.

    Direction reversals are tagged ``fast_direction_change`` (slope reset).
    """
    inputs, reference, classification = [], [], []
    r = np.diag([0.005 ** 2] * 3)
    speed, span = 1.7, 0.5
    half = span / speed
    period = 2 * half
    for i in range(frames):
        now = 1.0 + i / 75.0
        elapsed = i / 75.0
        cycle = elapsed % period
        if cycle < half:
            depth = speed * cycle - span / 2.0
            turn = cycle < 1.0 / 75.0 or (half - cycle) < 1.0 / 75.0
        else:
            depth = span / 2.0 - speed * (cycle - half)
            turn = (cycle - half) < 1.0 / 75.0 or (period - cycle) < 1.0 / 75.0
        position = np.array([0.0, 0.0, 1.0 + depth])
        raw = position + rng.normal(0, 0.005, 3)
        tag = "fast_direction_change" if turn else "clean"
        packet = _packet("benchmark", 0, i, now, True, raw)
        inputs.append({
            "event": "pose", "monotonic_s": now, "packet": packet,
            "alignment": np.eye(3).tolist(), "covariance": r.tolist(),
            "quality": {"reliable": True, "accepted": True, "reason": "ok"},
            "context_valid": True, "prediction_allowed": True,
            "led_count": 5, "tag": tag,
        })
        reference.append({"monotonic_s": now, "position_m": position.tolist()})
        classification.append(tag)
    return {"inputs": inputs, "reference": reference, "classification": classification, "static": False}


def _correlated_depth(rng, frames):
    """Stationary truth with AR(1) correlated noise (rho=0.9)."""
    inputs, reference, classification = [], [], []
    rho = 0.9
    scale = math.sqrt(1 - rho ** 2)
    state = np.zeros(3)
    std = np.array([0.0015, 0.0015, 0.005])
    for i in range(frames):
        now = 1.0 + i / 75.0
        position = np.array([0.0, 0.0, 1.0])
        state = rho * state + scale * rng.normal(0, std)
        raw = position + state
        packet = _packet("benchmark", 0, i, now, True, raw)
        inputs.append({
            "event": "pose", "monotonic_s": now, "packet": packet,
            "alignment": np.eye(3).tolist(), "covariance": BASE_R.tolist(),
            "quality": {"reliable": True, "accepted": True, "reason": "ok"},
            "context_valid": True, "prediction_allowed": True,
            "led_count": 5, "tag": "clean",
        })
        reference.append({"monotonic_s": now, "position_m": position.tolist()})
        classification.append("clean")
    return {"inputs": inputs, "reference": reference, "classification": classification, "static": True}


def _onset(rng, frames):
    """Quiet 1.0 s, then a strong (0.1 m/s) and a slow (0.02 m/s) X ramp.

    ``motion_start_s`` declares the first GT start statistically, so exit
    latency and t50/t90 are measured against known truth, never noisy output.
    """
    inputs, reference, classification = [], [], []
    strong_v, slow_v = 0.1, 0.02
    strong_len, quiet_gap = 225, 75
    start_idx = 75
    motion_start_s = 1.0 + start_idx / 75.0
    for i in range(frames):
        now = 1.0 + i / 75.0
        position = np.array([0.0, 0.0, 1.0])
        if start_idx <= i < start_idx + strong_len:
            position[0] += strong_v * (i - start_idx) / 75.0
            tag = "onset_start" if i == start_idx else "ramp_strong"
        elif start_idx + strong_len <= i < start_idx + strong_len + quiet_gap:
            position[0] += strong_v * strong_len / 75.0
            tag = "clean"
        elif start_idx + strong_len + quiet_gap <= i < start_idx + 2 * strong_len + quiet_gap:
            position[0] += strong_v * strong_len / 75.0 + slow_v * (
                i - start_idx - strong_len - quiet_gap) / 75.0
            tag = "ramp_slow"
        elif i >= start_idx + 2 * strong_len + quiet_gap:
            position[0] += (strong_v * strong_len + slow_v * strong_len) / 75.0
            tag = "clean"
        else:
            tag = "quiet"
        raw = position + rng.normal(0, [0.0015, 0.0015, 0.005])
        packet = _packet("benchmark", 0, i, now, True, raw)
        inputs.append({
            "event": "pose", "monotonic_s": now, "packet": packet,
            "alignment": np.eye(3).tolist(), "covariance": BASE_R.tolist(),
            "quality": {"reliable": True, "accepted": True, "reason": "ok"},
            "context_valid": True, "prediction_allowed": True,
            "led_count": 5, "tag": tag,
        })
        reference.append({"monotonic_s": now, "position_m": position.tolist()})
        classification.append(tag)
    return {"inputs": inputs, "reference": reference, "classification": classification,
            "static": False, "motion_start_s": motion_start_s}


def _normalize_uncertainty(uncertainty):
    """Map ``None``/factor/``depth_factor`` alias to canonical pipeline kwargs."""
    if uncertainty is None:
        return {}
    if isinstance(uncertainty, (int, float)):
        return {"depth_variance_factor": float(uncertainty)}
    if isinstance(uncertainty, dict):
        out = {}
        if "depth_variance_factor" in uncertainty:
            out["depth_variance_factor"] = float(uncertainty["depth_variance_factor"])
        elif "depth_factor" in uncertainty:
            out["depth_variance_factor"] = float(uncertainty["depth_factor"])
        if "depth_extra_std_m" in uncertainty:
            out["depth_extra_std_m"] = float(uncertainty["depth_extra_std_m"])
        return out
    raise ValueError("uncertainty must be None, a factor, or a dict")


def _effective_covariance(raw, alignment, factor=1.0, extra_std_m=0.0):
    """Legacy helper: delegates to the canonical runtime function (tests only).

    Production filtering must use ``PositionPipeline(uncertainty=...)`` so R is
    tuned exactly once; this wrapper exists so older assertions keep passing.
    """
    return inflate_optical_covariance(raw, alignment, variance_factor=float(factor),
                                      extra_std_m=float(extra_std_m))


def run_variant(dataset, kalman=None, one_euro=False, uncertainty=None,
                prediction_output="camera", measure_timing=False):
    """Filter one shared scenario with a single variant.

    Raw ``covariance``/``quality``/``led_count`` are passed through untouched;
    the pipeline recalibrates ``reliable`` itself. Rows log ``raw``/``effective``
    covariances from diagnostics, plus wire (published) vs estimator validity.
    """
    inputs = dataset["inputs"]
    canonical = _normalize_uncertainty(uncertainty)
    if one_euro is not False and one_euro is not None:
        params = dict(BASE_ONE_EURO) if one_euro is True else dict(one_euro)
        pipe = PositionPipeline("one_euro", one_euro=params)
    else:
        pipe = PositionPipeline("kalman", kalman=dict(kalman or {}),
                                uncertainty=canonical, prediction_output=prediction_output)
    rows = []
    for entry in inputs:
        incoming = entry["packet"]
        pkt = dict(incoming)
        if pkt.get("position_m") is not None:
            pkt["position_m"] = list(pkt["position_m"])
        quality = dict(entry["quality"])
        quality["led_count"] = int(entry.get("led_count", 5))
        started = time.perf_counter() if measure_timing else None
        out = pipe.filter_packet(
            pkt, entry["alignment"], entry["monotonic_s"],
            covariance=entry["covariance"],
            quality=quality,
            prediction_allowed=bool(entry["prediction_allowed"]),
            context_valid=entry.get("context_valid"),
        )
        elapsed_us = None if not measure_timing else (time.perf_counter() - started) * 1e6
        diag = pipe.diagnostics()
        alignment = None if entry["alignment"] is None else np.asarray(entry["alignment"], float)
        rows.append({
            "monotonic_s": entry["monotonic_s"],
            "packet": out,
            "diagnostics": diag,
            "optical_axis_world_m": None if alignment is None else (alignment.T @ [0.0, 0.0, 1.0]).tolist(),
            "raw_covariance": diag.get("raw_measurement_covariance", entry["covariance"]),
            "effective_covariance": diag.get("measurement_covariance", entry["covariance"]),
            "wire_valid": bool(out["valid"]) if out is not None else False,
            "estimator_valid": bool(diag.get("estimator_valid", out is not None and out['valid'])),
            "input_valid": bool(incoming["valid"]) if incoming is not None else False,
            "tag": entry.get("tag", "clean"),
            "led_count": entry.get("led_count", 5),
            "compute_us": elapsed_us,
        })
    return rows


def _rmse(a, b):
    a = np.asarray(a, float)
    b = np.asarray(b, float)
    return float(np.sqrt(np.mean(np.square(a - b)))) if len(a) else None


GATE_REASONS = frozenset({"mahalanobis_gate", "implied_speed", "posterior_speed",
                          "posterior_position_step", "position_uncertainty"})


def _pearson(x, y):
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    if len(x) < 2 or float(np.std(x)) == 0.0 or float(np.std(y)) == 0.0:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def _autocorr_block(vectors):
    """Lag-1 uses adjacent ORIGINAL frame indices; missing/gaps are not joined."""
    if len(vectors) < 2:
        return {"count": len(vectors), "dot_mean": None, "signed_mean": [None, None, None],
                "pearson": [None, None, None]}
    indices = np.asarray([v[0] for v in vectors])
    arr = np.asarray([v[1] for v in vectors], float)
    adjacent = np.diff(indices) == 1
    left, right = arr[:-1][adjacent], arr[1:][adjacent]
    prods = right * left
    return {
        "count": len(vectors),
        "adjacent_pairs": int(adjacent.sum()),
        "dot_mean": float(np.mean(np.sum(prods, axis=1))) if len(prods) else None,
        "signed_mean": np.mean(prods, axis=0).tolist() if len(prods) else [None]*3,
        "pearson": [_pearson(left[:, k], right[:, k]) for k in range(3)] if len(left) else [None]*3,
        "signed_innovation_mean_m": arr.mean(axis=0).tolist(),
    }


def _innovation_partitions(rows):
    all_v, accepted_v = [], []
    by_led, by_mode, after_pred, after_recovery = {}, {}, [], []
    prev_predicting, prev_invalid = False, True
    for i, r in enumerate(rows):
        diag = r["diagnostics"]
        innov = diag.get("innovation_m")
        vec = np.asarray(innov, float).tolist() if innov is not None else None
        if vec is not None:
            indexed = (i, vec)
            all_v.append(indexed)
            if diag.get("rejection_reason") is None and diag.get("tracking_state") == "tracking":
                accepted_v.append(indexed)
            by_led.setdefault(int(r.get("led_count", 5)), []).append(indexed)
            by_mode.setdefault(str(diag.get("process_mode", "motion")), []).append(indexed)
            if prev_predicting or prev_invalid:
                after_recovery.append(indexed) if prev_invalid and i > 0 else after_pred.append(indexed)
        prev_predicting = diag.get("tracking_state") == "predicting"
        pkt = r["packet"]
        prev_invalid = pkt is None or not pkt["valid"]
    return {"all": all_v, "accepted": accepted_v, "by_led": by_led,
            "by_mode": by_mode, "after_pred": after_pred, "after_recovery": after_recovery}


def summarize(rows, reference, classification, start_s=2.0, include_timing=False,
              allowance_m=0.015, motion_start_s=None):
    """Combine replay metrics with tuning extras (deterministic by default)."""
    base = metrics(rows, start_s=start_s, reference=reference)
    valid_idx = [i for i, r in enumerate(rows)
                 if r["monotonic_s"] >= start_s and r["packet"] is not None and r["packet"]["valid"]]
    errors, optical_errors = [], []
    for i in valid_idx:
        est = np.asarray(rows[i]["packet"]["position_m"], float)
        truth = np.asarray(reference[i]["position_m"], float)
        errors.append(est - truth)
        axis = rows[i].get("optical_axis_world_m")
        optical_errors.append(float(np.dot(est, axis) - np.dot(truth, axis)) if axis is not None else None)
    out = dict(base)
    out["xyz_rmse_m"] = (np.sqrt(np.mean(np.square(errors), axis=0)).tolist() if errors else [None, None, None])
    out["xyz_rmse_mm"] = [None if v is None else v * 1000.0 for v in out["xyz_rmse_m"]]
    present_optical = [e for e in optical_errors if e is not None]
    out["optical_rmse_m"] = (_rmse(present_optical, np.zeros(len(present_optical))) if present_optical else None)
    out["max_error_m"] = (float(np.max(np.linalg.norm(errors, axis=1))) if errors else None)
    ref_arr = np.asarray([r["position_m"] for r in reference], float)
    est_by_idx = {i: np.asarray(rows[i]["packet"]["position_m"], float) for i in valid_idx}
    if valid_idx:
        over_xyz = []
        for k in range(3):
            lo, hi = float(ref_arr[:, k].min()), float(ref_arr[:, k].max())
            over_xyz.append(max([float(est_by_idx[i][k] - hi) for i in valid_idx] + [0.0] +
                                [float(lo - est_by_idx[i][k]) for i in valid_idx]))
        out["overshoot_xyz_m"] = over_xyz
        if present_optical:
            axes = [np.asarray(rows[i]["optical_axis_world_m"], float) for i in valid_idx]
            ref_opt = np.array([float(np.dot(ref_arr[i], axes[j])) for j, i in enumerate(valid_idx)])
            est_opt = np.array([float(np.dot(est_by_idx[i], axes[j])) for j, i in enumerate(valid_idx)])
            out["overshoot_optical_m"] = float(max(0.0, float(est_opt.max() - ref_opt.max()),
                                                  float(ref_opt.min() - est_opt.min())))
        else:
            out["overshoot_optical_m"] = None
        out["overshoot_max_m"] = float(max([out["overshoot_optical_m"] or 0.0] + out["overshoot_xyz_m"]))
    else:
        out.update(overshoot_xyz_m=[None, None, None], overshoot_optical_m=None, overshoot_max_m=None)
    out["overshoot_m"] = out["overshoot_max_m"]
    out["available_frames"] = len(rows)
    out["invalid_frames"] = sum(1 for r in rows if r["packet"] is None or not r["packet"]["valid"])
    out["wire_valid_frames"] = sum(1 for r in rows if r.get("wire_valid"))
    out["estimator_valid_frames"] = sum(1 for r in rows if r.get("estimator_valid"))
    out["predicted_frames"] = sum(1 for r in rows if r["diagnostics"].get("tracking_state") == "predicting")
    out["internal_predicted_frames"] = out["predicted_frames"]
    clean_idx = [i for i, c in enumerate(classification) if c == "clean"]
    gated = sum(1 for i in clean_idx if rows[i]["diagnostics"].get("rejection_reason") in GATE_REASONS)
    invalid = sum(1 for i in clean_idx if rows[i]["packet"] is None or not rows[i]["packet"]["valid"])
    out["clean_gate_rejected"] = gated
    out['quality_rejected_measurements'] = sum(bool(r['diagnostics'].get('quality_rejected_measurement')) for r in rows)
    out['clean_quality_rejected'] = sum(bool(rows[i]['diagnostics'].get('quality_rejected_measurement')) for i in clean_idx)
    out['total_gate_or_quality_rejected'] = out['rejected_measurements'] + out['quality_rejected_measurements']
    out["clean_invalid"] = invalid
    out["clean_gate_rejected_rate"] = (gated / len(clean_idx)) if clean_idx else None
    out["clean_invalid_rate"] = (invalid / len(clean_idx)) if clean_idx else None
    out["false_reject_clean"] = invalid
    out["false_reject_rate"] = out["clean_invalid_rate"]
    nis = [r["diagnostics"].get("mahalanobis_sq") for r in rows
           if r["diagnostics"].get("mahalanobis_sq") is not None]
    accepted_nis = [r["diagnostics"].get("mahalanobis_sq") for r in rows
                    if r["diagnostics"].get("mahalanobis_sq") is not None
                    and r["diagnostics"].get("rejection_reason") is None
                    and r["diagnostics"].get("tracking_state") == "tracking"]
    if nis:
        arr = np.asarray(nis, float)
        out["nis_mean_all"] = float(arr.mean())
        out["nis_p50"] = float(np.percentile(arr, 50))
        out["nis_p95"] = float(np.percentile(arr, 95))
        out["nis_frac_below_p05"] = float(np.mean(arr < CHI2_3_P05))
        out["nis_frac_above_p95"] = float(np.mean(arr > CHI2_3_P95))
    else:
        out.update(nis_mean_all=None, nis_p50=None, nis_p95=None,
                   nis_frac_below_p05=None, nis_frac_above_p95=None)
    out["nis_mean_accepted"] = (float(np.mean(accepted_nis)) if accepted_nis else None)
    optical_nis = []
    for r in rows:
        diag = r["diagnostics"]
        innov = diag.get("innovation_m")
        s_cov = diag.get("innovation_covariance")
        axis = r.get("optical_axis_world_m")
        if innov is not None and s_cov is not None and axis is not None:
            try:
                innov_v = np.asarray(innov, float)
                s_m = np.asarray(s_cov, float)
                u = np.asarray(axis, float)
                denom = float(u @ s_m @ u)
                if denom > 0 and np.isfinite(denom):
                    optical_nis.append(float((float(u @ innov_v) ** 2) / denom))
            except np.linalg.LinAlgError:
                continue
    out["optical_nis_mean"] = (float(np.mean(optical_nis)) if optical_nis else None)
    out["optical_nis_p95"] = (float(np.percentile(optical_nis, 95)) if optical_nis else None)
    parts = _innovation_partitions(rows)
    out["innov_autocorr_all"] = _autocorr_block(parts["all"])
    out["innov_autocorr_accepted"] = _autocorr_block(parts["accepted"])
    out["innov_autocorr_by_led"] = {str(k): _autocorr_block(v) for k, v in sorted(parts["by_led"].items())}
    out["innov_autocorr_by_mode"] = {str(k): _autocorr_block(v) for k, v in sorted(parts["by_mode"].items())}
    out["innov_autocorr_after_pred"] = _autocorr_block(parts["after_pred"])
    out["innov_autocorr_after_recovery"] = _autocorr_block(parts["after_recovery"])
    first_valid = next((r["monotonic_s"] for r in rows if r["packet"] is not None and r["packet"]["valid"]), None)
    out["acquisition_s"] = (None if first_valid is None else float(first_valid - rows[0]["monotonic_s"]))
    out["initial_acquisition_ms"] = (None if out["acquisition_s"] is None else out["acquisition_s"] * 1000.0)
    gaps, jumps = [], []
    last_good, lost = None, True
    for r in rows:
        pkt = r["packet"]
        if pkt is None or not pkt["valid"]:
            lost = True
            continue
        point = np.asarray(pkt["position_m"], float)
        if lost and last_good is not None:
            gaps.append(float(r["monotonic_s"] - last_good[0]))
            jumps.append(float(np.linalg.norm(point - last_good[1])))
        last_good, lost = (r["monotonic_s"], point), False
    out["reacquisition_count"] = len(jumps)
    out["reacquisition_gaps_s"] = gaps
    out["recovery_gap_durations_s"] = list(gaps)
    out["reacquisition_jumps_m"] = jumps
    out["reacquisition_jump_max_m"] = (max(jumps) if jumps else None)
    out["dropout_jump_max_m"] = out["reacquisition_jump_max_m"]
    quiet_flags = [bool(r["diagnostics"].get("quiet_active")) for r in rows
                   if r["diagnostics"].get("quiet_active") is not None]
    out["quiet_active_fraction"] = (float(np.mean(quiet_flags)) if quiet_flags else 0.0)
    eff_q = [r["diagnostics"].get("effective_q") for r in rows if r["diagnostics"].get("effective_q") is not None]
    if eff_q:
        arr = np.asarray(eff_q, float)
        out["effective_q_mean"] = float(arr.mean())
        out["effective_q_min"] = float(arr.min())
        out["effective_q_max"] = float(arr.max())
    else:
        out.update(effective_q_mean=None, effective_q_min=None, effective_q_max=None)
    vels = [np.linalg.norm(np.asarray(r["diagnostics"]["velocity_m_s"], float)) for r in rows
            if r["diagnostics"].get("velocity_m_s") is not None]
    out["velocity_max_m_s"] = (float(max(vels)) if vels else None)
    out["genuinely_static"] = bool(float(np.max(np.ptp(ref_arr, axis=0))) <= 1e-12)
    out['jitter_interpretation'] = ('synthetic stationary ground truth' if out['genuinely_static']
        else 'synthetic moving ground truth; XYZ dispersion is NOT static jitter')
    if motion_start_s is not None:
        out.update(_onset_metrics(rows, reference, motion_start_s))
    else:
        out.update(onset_exit_latency_s=None, onset_response_10mm_s=None, onset_t50_s=None, onset_t90_s=None)
    out["allowance_m"] = float(allowance_m)
    out["score_within_allowance"] = (all(v is not None and v <= allowance_m for v in out["xyz_rmse_m"])
                                     if out["xyz_rmse_m"] else None)
    if include_timing and any(r.get("compute_us") is not None for r in rows):
        vals = np.asarray([r["compute_us"] for r in rows if r.get("compute_us") is not None], float)
        out["timing_us_mean"] = float(vals.mean())
        out["timing_us_p95"] = float(np.percentile(vals, 95))
    else:
        out["timing_us_mean"] = None
        out["timing_us_p95"] = None
    return out


def _onset_metrics(rows, reference, motion_start_s):
    """Exit latency and t50/t90 against declared GT start (never noisy output)."""
    times = np.array([r["monotonic_s"] for r in rows])
    ref_x = np.array([r["position_m"][0] for r in reference])
    baseline = float(ref_x[times < motion_start_s].mean()) if np.any(times < motion_start_s) else 0.0
    est = np.array([np.asarray(r["packet"]["position_m"], float)[0]
                    if r["packet"] is not None and r["packet"]["valid"] else np.nan for r in rows])
    post = times >= motion_start_s
    response = next((float(t - motion_start_s) for t, e in zip(times[post], est[post])
                    if np.isfinite(e) and abs(e - baseline) >= 0.01), None)
    before = [r for r in rows if r['monotonic_s'] < motion_start_s]
    was_quiet = bool(before and before[-1]['diagnostics'].get('quiet_active'))
    exited = next((float(r['monotonic_s']-motion_start_s) for r in rows
        if r['monotonic_s'] >= motion_start_s and not r['diagnostics'].get('quiet_active')),
        None) if was_quiet else None
    strong = [i for i, r in enumerate(rows) if r.get("tag") == "ramp_strong"]
    t50 = t90 = None
    if strong:
        t50 = next((float(times[i] - motion_start_s) for i in strong
                    if np.isfinite(est[i]) and est[i] - baseline >= 0.05), None)
        t90 = next((float(times[i] - motion_start_s) for i in strong
                    if np.isfinite(est[i]) and est[i] - baseline >= 0.09), None)
    gt50 = next((float(times[i]-motion_start_s) for i in strong if ref_x[i]-baseline >= .05), None)
    gt90 = next((float(times[i]-motion_start_s) for i in strong if ref_x[i]-baseline >= .09), None)
    return {"onset_exit_latency_s": exited, "onset_response_10mm_s":response,
            "onset_t50_s": t50, "onset_t90_s": t90,
            "onset_t50_additional_delay_s":None if t50 is None or gt50 is None else t50-gt50,
            "onset_t90_additional_delay_s":None if t90 is None or gt90 is None else t90-gt90}


def _optimized_config():
    root = Path(__file__).resolve().parents[2]
    data = json.loads((root / OPTIMIZED_PROFILE).read_text(encoding="utf-8"))
    return {"kalman": dict(data.get("kalman", {})),
            "uncertainty": dict(data.get("uncertainty", {})),
            "prediction_output": data.get("prediction_output", "driver_hold")}


def default_variants():
    """Curated comparison set; Q1.5 is a candidate, never the initial baseline."""
    variants = {
        "one_euro_2.5_8": {"one_euro": dict(BASE_ONE_EURO)},
        "initial_Q3_camera": {"kalman": dict(INITIAL_Q3_KALMAN),
                              "uncertainty": {"depth_variance_factor": 1.0},
                              "prediction_output": "camera"},
    }
    opt = _optimized_config()
    variants["optimized_driver_hold"] = {"kalman": dict(opt["kalman"]),
                                         "uncertainty": dict(opt["uncertainty"]),
                                         "prediction_output": opt["prediction_output"]}
    variants["optimized_camera"] = {"kalman": dict(opt["kalman"]),
                                     "uncertainty": dict(opt["uncertainty"]),
                                     "prediction_output": "camera"}
    without_pulse = dict(opt['kalman'])
    without_pulse['motion_onset_acceleration_std'] = None
    variants['optimized_no_pulse_camera'] = {'kalman':without_pulse,
        'uncertainty':dict(opt['uncertainty']), 'prediction_output':'camera'}
    variants["static_Q1.5_f1"] = {"kalman": {"acceleration_std_m_s2": 1.5},
                                  "uncertainty": {"depth_variance_factor": 1.0},
                                  "prediction_output": "camera"}
    variants["adaptive_Q5_f1"] = {"kalman": {"acceleration_std_m_s2": 5.0, "adaptive_quiet": True,
                                             "quiet_acceleration_std": 0.03,
                                             "measurement_speed_noise_allowance_m": 0.015},
                                  "uncertainty": {"depth_variance_factor": 1.0},
                                  "prediction_output": "camera"}
    return variants


def compare(variants, frames=900, seed=6130, scenarios=None, allowance_m=0.015):
    """Run every variant on the same shared inputs; fully deterministic."""
    names = list(ALL_SCENARIOS) if scenarios is None else list(scenarios)
    data = make_scenarios(frames=frames, seed=seed)
    for name in names:
        ref, inp = data[name]["reference"], data[name]["inputs"]
        assert len(ref) == len(inp) and all(
            r["monotonic_s"] == e["monotonic_s"] for r, e in zip(ref, inp)), name
    report = {}
    for name in names:
        scenario = data[name]
        per_variant = {}
        for variant in sorted(variants):
            cfg = variants[variant] or {}
            rows = run_variant(scenario, kalman=cfg.get("kalman"),
                               one_euro=cfg.get("one_euro", False),
                               uncertainty=cfg.get("uncertainty"),
                               prediction_output=cfg.get("prediction_output", "camera"))
            per_variant[variant] = summarize(rows, scenario["reference"], scenario["classification"],
                                             allowance_m=allowance_m,
                                             motion_start_s=scenario.get("motion_start_s"))
        report[name] = per_variant
    return report


def _table(results):
    lines = ["variant | scenario | wire | est | pred | xyz_mm | optical_mm | lag | maxjump_mm | nisAll/Acc | quiet"]
    for scenario in sorted(results):
        for variant in sorted(results[scenario]):
            m = results[scenario][variant]
            xyz = m.get("xyz_rmse_mm") or [None, None, None]
            xyz_s = ",".join("?" if v is None else f"{v:.2f}" for v in xyz)
            optical = m.get("optical_rmse_m")
            optical_s = "?" if optical is None else f"{optical * 1000.0:.2f}"
            lag = m.get("response_delay_s")
            lag_s = "?" if lag is None else f"{lag:.3f}"
            jump = m.get("reacquisition_jump_max_m")
            jump_s = "?" if jump is None else f"{jump * 1000.0:.1f}"
            nis = (m.get("nis_mean_all"), m.get("nis_mean_accepted"))
            nis_s = "/".join("?" if v is None else f"{v:.2f}" for v in nis)
            qf = m.get("quiet_active_fraction")
            qf_s = "?" if qf is None else f"{qf:.2f}"
            lines.append(f"{variant} | {scenario} | {m.get('wire_valid_frames')} | "
                         f"{m.get('estimator_valid_frames')} | {m.get('internal_predicted_frames')} | "
                         f"{xyz_s} | {optical_s} | {lag_s} | {jump_s} | {nis_s} | {qf_s}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sweep-q", type=float, nargs="*", default=[0.05, 0.1, 0.2, 0.5, 1.0, 1.5, 2.0, 3.0])
    parser.add_argument("--depth-factors", type=float, nargs="*", default=[1.0, 2.0, 4.0])
    parser.add_argument("--frames", type=int, default=900)
    parser.add_argument("--seed", type=int, default=6130)
    parser.add_argument("--seeds", type=int, nargs='+', help='repeat shared comparisons over explicit noise seeds')
    parser.add_argument("--scenarios", nargs='+', choices=ALL_SCENARIOS)
    parser.add_argument("--allowance", type=float, default=0.015)
    parser.add_argument("--compare-only", action="store_true")
    parser.add_argument("--include-onset", action="store_true")
    parser.add_argument("--measure-timing", action="store_true")
    parser.add_argument("--out", default=None)
    parser.add_argument("--variants-config", default=None)
    args = parser.parse_args(argv)
    if args.variants_config:
        variants = {k: dict(v) for k, v in json.loads(Path(args.variants_config).read_text(encoding="utf-8")).items()}
        names = list(ALL_SCENARIOS) if args.include_onset else [n for n in ALL_SCENARIOS if n != "onset"]
    elif args.compare_only:
        variants = default_variants()
        names = list(ALL_SCENARIOS)
    else:
        variants = {}
        for q in args.sweep_q:
            for factor in args.depth_factors:
                variants[f"Q{q}_d{factor}"] = {"kalman": {"acceleration_std_m_s2": float(q)},
                                               "uncertainty": {"depth_variance_factor": float(factor)},
                                               "prediction_output": "camera"}
        names = list(BASE_SCENARIOS if not args.include_onset else BASE_SCENARIOS + ("onset",))
    names = args.scenarios or names
    seeds = args.seeds or [args.seed]
    repeated = {str(seed):compare(variants, frames=args.frames, seed=seed,
                                 scenarios=names, allowance_m=args.allowance) for seed in seeds}
    results = repeated[str(seeds[0])]
    for seed in seeds:
        print('seed=' + str(seed))
        print(_table(repeated[str(seed)]))
    if args.measure_timing:
        data = make_scenarios(frames=args.frames, seed=args.seed)
        profile = {}
        for variant in sorted(variants):
            cfg = variants[variant] or {}
            rows = run_variant(data["stationary"], kalman=cfg.get("kalman"),
                               one_euro=cfg.get("one_euro", False),
                               uncertainty=cfg.get("uncertainty"),
                               prediction_output=cfg.get("prediction_output", "camera"),
                               measure_timing=True)
            vals = sorted(r["compute_us"] for r in rows if r.get("compute_us") is not None)
            profile[variant] = {"median_us": float(vals[len(vals) // 2]),
                                "p99_us": float(vals[int(0.99 * (len(vals) - 1))])}
        print("timing(root stationary profile, NOT in report): " + json.dumps(profile, sort_keys=True))
    if args.out:
        payload = {"schema": "psvr.position.kalman_sweep", "seed": args.seed,
                   "frames": args.frames, "allowance_m": args.allowance,
                    "variants": sorted(variants), "variant_configs": variants, "results": results,
                    "seeds":seeds, "repeated_results":repeated if len(seeds)>1 else None,
                    "hardware_accepted":False, "ground_truth":"synthetic",
                    "static_certification":"synthetic truth only; no certified real static interval"}
        with Path(args.out).open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
