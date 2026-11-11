"""Reproducible synthetic metrics, explicitly NOT physical validation."""
import json

import numpy as np

from .replay_position_log import metrics, replay


def benchmark():
    rng = np.random.default_rng(6130)
    report = {"schema": "psvr.position.synthetic_benchmark", "seed": 6130,
              "hardware_accepted": False, "one_euro": {"min_cutoff_hz": 2.5, "beta": 8.0},
              "scenarios": {}}
    for name in ("stationary", "linear", "rapid", "depth", "outlier", "dropout"):
        inputs, truth = [], []
        for i in range(900):
            now = 1 + i / 75
            elapsed = i / 75
            position = np.array([0., 0., 1.])
            if name == "linear":
                position[0] += .1 * elapsed
            if name in ("rapid", "depth"):
                # Known finite-speed displacement, not an impossible step.
                phase = elapsed % 2
                displacement = .1 * np.clip((phase - .5) / .1, 0, 1)
                displacement -= .1 * np.clip((phase - 1.5) / .1, 0, 1)
                position[2 if name == "depth" else 0] += displacement
            raw = position + rng.normal(0, [.0015, .0015, .005])
            if name == "outlier" and i in (200, 400, 600):
                raw += [.5, 0, .4]
            valid = not (name == "dropout" and (200 <= i < 204 or 400 <= i < 420))
            packet = {"session_id": "benchmark", "recenter_epoch": 0, "valid": valid,
                      "state": "tracking" if valid else "lost", "position_m": raw.tolist() if valid else None}
            inputs.append({"event": "pose", "monotonic_s": now, "packet": packet,
                "alignment": np.eye(3).tolist(), "covariance": np.diag(np.square([.0015, .0015, .005])).tolist(),
                "quality": {"reliable": True, "accepted": valid, "reason": "ok" if valid else "optical_dropout"},
                "context_valid": True, "prediction_allowed": True})
            truth.append({"monotonic_s": now, "position_m": position.tolist()})
        outputs = replay(inputs, one_euro=report["one_euro"])
        result = {}
        for mode, rows in outputs.items():
            m = metrics(rows, start_s=2., reference=truth)
            m["jitter_interpretation"] = "known stationary synthetic target" if name in (
                "stationary", "outlier", "dropout") else "known moving synthetic target; dispersion not jitter"
            errors = [np.asarray(row["packet"]["position_m"]) - target["position_m"]
                      for row, target in zip(rows, truth) if row["monotonic_s"] >= 2
                      and row["packet"] is not None and row["packet"]["valid"]]
            m["xyz_error_rmse_m"] = np.sqrt(np.mean(np.square(errors), axis=0)).tolist()
            result[mode] = m
        report["scenarios"][name] = result
    return report


if __name__ == "__main__":
    print(json.dumps(benchmark(), indent=2, sort_keys=True, allow_nan=False))
