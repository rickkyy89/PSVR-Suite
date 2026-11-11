"""Deterministic comparison at the final-position boundary, never hardware approval.

New logs record exact estimator inputs. Legacy logs preserve raw world positions
but not alignment/offset/reset events: covariance reconstruction is approximate
and explicitly reported. No measurements/timestamps are resampled for filtering.

Operational memory limit (B13-02): inputs are loaded whole-file
(`read_text().splitlines()` plus one record per line), so peak memory is
roughly the file size plus parsed structures; variants are evaluated
sequentially to bound the rest. For very long optical logs (75 Hz x hours),
prefer a time window (`--start-s`/`--end-s`) or split the log first; a
streaming refactor is future work, the format is unchanged.
"""
import argparse
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

import numpy as np

from .calibration import load_intrinsics
from .geometry import rodrigues
from .imu_led_pose import MODEL_TO_IMU, rotation_from_quaternion
from .led_pose import LedPose
from .pose_uncertainty import estimate_pose_covariance
from .position_pipeline import PositionPipeline


def _resolve(path, log):
    path = Path(path)
    if path.is_file():
        return path
    root = Path(__file__).resolve().parents[2]
    for candidate in (log.parent / path, root / path, root / "configurations" / path.name):
        if candidate.is_file():
            return candidate
    raise ValueError("missing replay intrinsics: " + str(path))


def load_inputs(path, intrinsics_path=None):
    """Load once; both algorithms receive these same detached observations."""
    path = Path(path)
    inputs, session, intrinsics, alignment, identity = [], {}, None, None, None
    provenance = Counter()
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        event = row.get("event")
        if event == "session":
            session = row
            intrinsics = alignment = identity = None
            inputs.append({"event": "session", "config": row.get("position_config", {})})
            continue
        if event == "position_reset":
            inputs.append(row)
            if row.get("reason") == "realign":
                alignment = None
            continue
        if event != "pose":
            continue
        now = row["monotonic_s"]
        if "estimator_input" in row:
            inp = deepcopy(row["estimator_input"])
            provenance["exact_recorded_inputs"] += 1
        else:
            provenance["legacy_reconstructed_inputs"] += 1
            imu = row.get("imu") or {}
            sid = imu.get("session_id")
            if sid != identity:
                alignment = None
                identity = sid
            rvec, q = row.get("rvec"), imu.get("quaternion_sensor_wxyz")
            if alignment is None and rvec is not None and q is not None:
                alignment = rodrigues(rvec) @ MODEL_TO_IMU.T @ rotation_from_quaternion(q).T
            raw = (row.get("position_filter") or {}).get("raw_world_position_m")
            context = (sid is not None and imu.get("valid") is True
                       and imu.get("monotonic_s") is not None
                       and 0 <= now - imu["monotonic_s"] <= .15 and alignment is not None)
            optical = bool(row.get("valid"))
            packet = None if sid is None else {
                "protocol": "psvr.position", "version": 2, "frame": "psvr_imu_unrecentered",
                "units": "m", "session_id": sid, "recenter_epoch": imu.get("recenter_epoch", 0),
                "imu_seq": imu.get("seq", 0), "imu_sample_time_s": imu.get("sample_time_s", 0),
                "seq": len(inputs), "monotonic_s": now, "host_time_ns": 0,
                "valid": bool(context and optical and raw is not None), "source": "blob_led",
                "state": "tracking" if context and optical and raw is not None else "lost",
                "position_m": raw if context and optical else None}
            covariance, quality = None, {"reliable": False, "reason": "legacy_missing_geometry"}
            rig = session.get("rig", {})
            if optical and alignment is not None and rig.get("object_points_m") is not None:
                if intrinsics is None:
                    intrinsics = load_intrinsics(_resolve(intrinsics_path or session["intrinsics"], path))
                pose = LedPose(True, row.get("reason", ""),
                    image_points=np.asarray(row.get("image_points"), float),
                    rvec=np.asarray(rvec, float), tvec=np.asarray(row.get("position_camera_m"), float),
                    led_labels=row.get("led_labels"))
                covariance, quality = estimate_pose_covariance(pose, intrinsics,
                    rig["object_points_m"], rig["labels"], alignment,
                    guided=row.get("reason") in ("imu_guided_pattern", "imu_guided_front_reacquired"))
                quality["legacy_assumption"] = "alignment inferred; scale=1 offset=0; resets unavailable"
            reason = row.get("reason", "")
            prediction = context and (optical or reason.startswith("Pattern parziale:")
                                      or reason.startswith("Pattern non associato:"))
            inp = dict(packet=packet, alignment=None if alignment is None else alignment.tolist(),
                       covariance=None if covariance is None else covariance.tolist(),
                       quality=quality, prediction_allowed=bool(prediction),context_valid=bool(context))
            # Use actual historic One Euro parameters, including trial overrides.
            inp["legacy_one_euro_settings"] = (row.get("position_filter") or {}).get("settings")
        saved_filter = row.get("position_filter") or {}
        inputs.append({"event": "pose", "monotonic_s": now, **inp,
            "recorded_baseline_position_m": saved_filter.get("filtered_world_position_m")
                if saved_filter.get("type", "one_euro") == "one_euro" else None})
    return inputs, dict(provenance)


def replay(inputs, *, kalman=None, one_euro=None, uncertainty=None, prediction_output=None,
           modes=("one_euro", "kalman")):
    filters = {mode: PositionPipeline(mode, kalman=kalman, one_euro=one_euro,
               uncertainty=uncertainty, prediction_output=prediction_output or "camera") for mode in modes}
    output = {mode: [] for mode in filters}
    for row in inputs:
        event = row["event"]
        if event == "session":
            cfg = row.get("config", {})
            filters = {mode: PositionPipeline(mode, kalman=kalman if kalman is not None else cfg.get("kalman"),
                one_euro=one_euro if one_euro is not None else cfg.get("one_euro"),
                uncertainty=uncertainty if uncertainty is not None else cfg.get("uncertainty"),
                prediction_output=prediction_output or cfg.get("prediction_output", "camera")) for mode in filters}
            continue
        if event == "position_reset":
            reason = row.get("reason", "manual")
            for mode, filt in filters.items():
                if reason == "camera_read_failure":
                    if mode == "kalman":
                        filt.reset(reason)
                    elif row.get("monotonic_s") is not None:
                        filt.invalid(row["monotonic_s"])
                elif mode == "kalman" or reason not in ("camera_controls", "detector_settings"):
                    filt.reset(reason)
            continue
        historic = row.get("legacy_one_euro_settings")
        if historic and one_euro is None and "one_euro" in filters and filters["one_euro"].filter.settings != historic:
            filters["one_euro"] = PositionPipeline("one_euro", one_euro=historic)
        for mode, filt in filters.items():
            packet = filt.filter_packet(deepcopy(row["packet"]), row["alignment"], row["monotonic_s"],
                covariance=row["covariance"], quality=row["quality"],
                prediction_allowed=row["prediction_allowed"], context_valid=row.get("context_valid"))
            output[mode].append({"monotonic_s": row["monotonic_s"], "packet": packet,
                                 "optical_axis_world_m": None if row["alignment"] is None else
                                     (np.asarray(row["alignment"]).T @ [0., 0., 1.]).tolist(),
                                 "diagnostics": filt.diagnostics()})
    return output


def compare_variants_log(path, variants, *, intrinsics_path=None, start_s=None, end_s=None):
    """A/B/C on one shared input sequence; no invented reference or static labels.

    Variants are evaluated sequentially to bound memory on long optical logs.
    Defaults/session profile cannot silently replace explicitly supplied baselines.
    """
    inputs, provenance = load_inputs(path, intrinsics_path)
    results = {}
    for name, cfg in variants.items():
        mode = 'one_euro' if cfg.get('one_euro') is not None else 'kalman'
        rows = replay(inputs, modes=(mode,), kalman=cfg.get('kalman', {}),
            one_euro=cfg.get('one_euro'), uncertainty=cfg.get('uncertainty', {}),
            prediction_output=cfg.get('prediction_output', 'camera'))[mode]
        selected = [r for r in rows if (start_s is None or r['monotonic_s'] >= start_s)
                    and (end_s is None or r['monotonic_s'] <= end_s)]
        summary = metrics(selected)
        # No certification is inferred from selecting a time window.
        summary['jitter_interpretation'] = 'uncertified real tracking interval; NOT measured static jitter'
        summary['invalid_frames'] = len(selected) - summary['valid_frames']
        summary['estimator_valid_frames'] = sum(bool(r['diagnostics'].get('estimator_valid',
            r['packet'] is not None and r['packet']['valid'])) for r in selected)
        summary['quiet_active_fraction'] = float(np.mean([bool(r['diagnostics'].get('quiet_active'))
            for r in selected])) if selected else None
        summary['quality_rejected_measurements'] = sum(bool(r['diagnostics'].get('quality_rejected_measurement')) for r in selected)
        summary['total_gate_or_quality_rejected'] = summary['rejected_measurements'] + summary['quality_rejected_measurements']
        summary['nis'] = innovation_statistics(selected)
        results[name] = summary
    return {'schema':'psvr.position.optimization_real_replay', 'hardware_accepted':False,
            'ground_truth':None, 'certified_static_intervals':[], 'input_provenance':provenance,
            'timestamp_policy':'shared original capture inputs, no resampling',
            'variants':deepcopy(variants), 'metrics':results}


def innovation_statistics(rows):
    """Pre-gate and selected accepted NIS; missing observations are not zeros."""
    groups = {}
    for row in rows:
        diag = row['diagnostics']
        nis = diag.get('mahalanobis_sq')
        if nis is None:
            continue
        accepted = diag.get('tracking_state') == 'tracking' and diag.get('rejection_reason') is None
        labels = ['all_pre_gate', 'accepted' if accepted else 'rejected',
                  'led_'+str(diag.get('quality', {}).get('led_count', 'unknown')),
                  'mode_'+str(diag.get('process_mode', 'unknown'))]
        for label in labels:
            groups.setdefault(label, []).append((nis, diag.get('nis_optical'), diag.get('innovation_m')))
    result = {}
    for name, samples in groups.items():
        nis = np.asarray([s[0] for s in samples])
        optical = np.asarray([s[1] for s in samples if s[1] is not None])
        innovation = np.asarray([s[2] for s in samples if s[2] is not None])
        result[name] = dict(count=len(samples), mean=float(nis.mean()), median=float(np.median(nis)),
            p95=float(np.percentile(nis, 95)), fraction_above_7_815=float(np.mean(nis > 7.815)),
            optical_mean=float(optical.mean()) if len(optical) else None,
            optical_p95=float(np.percentile(optical, 95)) if len(optical) else None,
            signed_innovation_mean_m=innovation.mean(axis=0).tolist() if len(innovation) else None)
    return result


def metrics(rows, start_s=None, end_s=None, reference=None):
    selected = [r for r in rows if (start_s is None or r["monotonic_s"] >= start_s)
                and (end_s is None or r["monotonic_s"] <= end_s)]
    valid = [r for r in selected if r["packet"] is not None and r["packet"]["valid"]]
    result = {"frames": len(selected), "valid_frames": len(valid),
              "predicted_frames": sum(r["diagnostics"].get("tracking_state") == "predicting" for r in selected),
              "rejected_measurements": sum(r["diagnostics"].get("rejection_reason") in
                  ("mahalanobis_gate", "implied_speed", "posterior_speed", "posterior_position_step", "position_uncertainty") for r in selected),
              "quality_rejections": dict(Counter(r["diagnostics"].get("quality", {}).get("reason")
                  for r in selected if not r["diagnostics"].get("quality", {}).get("accepted", False))),
              "jitter_interpretation": "stationary-window proxy; verify headset was stationary" if start_s is not None
                  or end_s is not None else "whole-trial dispersion; NOT stationary jitter",
              "response_delay_s": None}
    if not valid:
        return result
    times = np.asarray([r["monotonic_s"] for r in valid])
    positions = np.asarray([r["packet"]["position_m"] for r in valid])
    centered_time = times - times.mean()
    design = np.column_stack((centered_time, np.ones(len(times))))
    trend = np.linalg.lstsq(design, positions, rcond=None)[0]
    result.update(xyz_std_m=positions.std(axis=0).tolist(),
                  detrended_xyz_std_m=(positions - design @ trend).std(axis=0).tolist(),
                  depth_drift_m_s=float(trend[0, 2]))
    if all(r.get("optical_axis_world_m") is not None for r in valid):
        depth = np.asarray([np.dot(r["packet"]["position_m"], r["optical_axis_world_m"]) for r in valid])
        depth_trend = np.linalg.lstsq(design, depth, rcond=None)[0]
        result.update(camera_depth_std_m=float(depth.std()),
                      camera_depth_detrended_std_m=float((depth - design @ depth_trend).std()),
                      camera_depth_drift_m_s=float(depth_trend[0]))
    steps, speeds, reattachments, prediction_steps, velocity = [], [], [], [], []
    last_good = previous = None
    lost = False
    for row in selected:
        packet = row["packet"]
        if packet is None or not packet["valid"]:
            lost, previous = True, None
            continue
        point = np.asarray(packet["position_m"])
        if lost and last_good is not None:
            reattachments.append(float(np.linalg.norm(point - last_good)))
        if previous is not None:
            dt = row["monotonic_s"] - previous[0]
            step = float(np.linalg.norm(point - previous[1]))
            if dt > 0:
                steps.append(step)
                speeds.append(step / dt)
                if row["diagnostics"].get("tracking_state") == "predicting":
                    prediction_steps.append(step)
        v = row["diagnostics"].get("velocity_m_s")
        if v is not None:
            velocity.append(float(np.linalg.norm(v)))
        last_good, previous, lost = point, (row["monotonic_s"], point), False
    result.update(step_p95_m=float(np.percentile(steps, 95)) if steps else None,
                  step_p99_m=float(np.percentile(steps, 99)) if steps else None,
                  step_max_m=max(steps, default=None), speed_max_m_s=max(speeds, default=None),
                  estimated_velocity_max_m_s=max(velocity, default=None),
                  reacquisitions=len(reattachments), reacquisition_jump_max_m=max(reattachments, default=None),
                  predicted_step_max_m=max(prediction_steps, default=None))
    if reference and len(valid) > 10:
        rt = np.asarray([r["monotonic_s"] for r in reference])
        rp = np.asarray([r["position_m"] for r in reference])
        if len(rt) < 2 or np.any(np.diff(rt) <= 0):
            raise ValueError("reference timestamps must strictly increase")
        # A known moving reference is necessary: no delay inference from optical noise.
        if np.max(np.ptp(rp, axis=0)) > .02:
            common = (times >= rt[0] + .25) & (times <= rt[-1])
            if common.sum() > 10:
                lags = np.linspace(0, .25, 251)
                errors = []
                for lag in lags:
                    truth = np.column_stack([np.interp(times[common] - lag, rt, rp[:, axis]) for axis in range(3)])
                    errors.append(float(np.mean((positions[common] - truth) ** 2)))
                result["response_delay_s"] = float(lags[int(np.argmin(errors))])
                result["reference_rmse_m"] = float(np.sqrt(errors[0]))
    return result


def compare_log(path, *, intrinsics_path=None, start_s=None, end_s=None, reference=None):
    inputs, provenance = load_inputs(path, intrinsics_path)
    outputs = replay(inputs)
    errors = []
    for inp, out in zip((r for r in inputs if r["event"] == "pose"), outputs["one_euro"]):
        expected = inp.get("recorded_baseline_position_m")
        if expected is not None and out["packet"] is not None and out["packet"]["valid"]:
            errors.append(float(np.linalg.norm(np.asarray(expected) - out["packet"]["position_m"])))
    return {"schema": "psvr.position.filter_comparison", "experimental": True,
            "hardware_accepted": False, "input_provenance": provenance,
            "recorded_baseline_comparison": {"compared_frames": len(errors),
                "max_difference_m": max(errors, default=None)},
            "timestamp_policy": "original capture timestamps, shared inputs; no resampling",
            "metrics": {mode: metrics(rows, start_s, end_s, reference) for mode, rows in outputs.items()}}, outputs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log")
    parser.add_argument("--out")
    parser.add_argument("--intrinsics")
    parser.add_argument("--start-s", type=float)
    parser.add_argument("--end-s", type=float)
    parser.add_argument("--reference", help="JSONL known-motion monotonic_s/position_m in the same frame")
    parser.add_argument("--variants-config", help="JSON named variants for real A/B/C; no ground truth inferred")
    args = parser.parse_args(argv)
    reference = None if args.reference is None else [json.loads(line) for line in
        Path(args.reference).read_text(encoding="utf-8").splitlines()]
    if args.variants_config:
        variants = json.loads(Path(args.variants_config).read_text(encoding='utf-8'))
        if args.reference:
            parser.error('--reference currently applies to the two-filter replay only')
        report = compare_variants_log(args.log, variants, intrinsics_path=args.intrinsics,
                                      start_s=args.start_s, end_s=args.end_s)
    else:
        report, _ = compare_log(args.log, intrinsics_path=args.intrinsics,
                               start_s=args.start_s, end_s=args.end_s, reference=reference)
    text = json.dumps(report, indent=2, sort_keys=True, allow_nan=False)
    if args.out:
        with Path(args.out).open("x", encoding="utf-8") as stream:
            stream.write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
