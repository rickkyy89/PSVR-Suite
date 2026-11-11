import copy
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from tools.psvr_camera.replay_position_log import compare_log, load_inputs, metrics, replay, compare_variants_log


def observation(i, position=(0., 0., 1.), *, valid=True, covariance=True, prediction=True):
    stamp = 1. + i / 75.
    return {"event": "pose", "monotonic_s": stamp,
        "packet": {"valid": valid, "position_m": list(position) if valid else None,
            "state": "tracking" if valid else "lost", "session_id": "test", "recenter_epoch": 0,
            "imu_seq": i, "imu_sample_time_s": stamp, "seq": i, "monotonic_s": stamp},
        "alignment": np.eye(3).tolist(),
        "covariance": (np.eye(3) * .003**2).tolist() if covariance else None,
        "quality": {"accepted": covariance, "reliable": covariance, "led_count": 5,
                    "reason": "ok" if covariance else "insufficient_leds"},
        "prediction_allowed": prediction}


class ReplayTests(unittest.TestCase):
    def test_exact_inputs_timestamps_and_determinism_no_mutation(self):
        inputs = [observation(i) for i in range(12)]
        before = copy.deepcopy(inputs)
        first = replay(inputs)
        second = replay(inputs)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(inputs, before)
        for mode in first:
            self.assertEqual([r["monotonic_s"] for r in first[mode]], [r["monotonic_s"] for r in inputs])
            for original, output in zip(inputs, first[mode]):
                self.assertEqual(output["packet"]["imu_seq"], original["packet"]["imu_seq"])
                self.assertEqual(output["packet"]["imu_sample_time_s"], original["monotonic_s"])

    def test_outlier_dropout_recovery_and_reset(self):
        inputs = [observation(i) for i in range(8)]
        inputs.append(observation(8, (1., 0., 1.)))
        inputs.extend(observation(i, valid=False, covariance=False) for i in range(9, 16))
        inputs.extend(observation(i) for i in range(16, 23))
        result = replay(inputs)
        self.assertEqual(result["kalman"][8]["diagnostics"]["tracking_state"], "predicting")
        self.assertFalse(result["kalman"][15]["packet"]["valid"])
        self.assertTrue(result["kalman"][-1]["packet"]["valid"])
        m = metrics(result["kalman"])
        self.assertEqual(m["rejected_measurements"], 1)
        self.assertGreater(m["predicted_frames"], 0)
        self.assertLess(m["step_max_m"], .01)
        self.assertGreater(metrics(result["one_euro"])["step_max_m"], .5)
        inputs.insert(5, {"event": "position_reset", "reason": "realign"})
        result = replay(inputs)
        self.assertFalse(result["kalman"][5]["packet"]["valid"])

    def test_new_log_roundtrip_and_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.jsonl"
            rows = [{"event": "session", "position_config": {"one_euro": {"min_cutoff_hz": 2.5, "beta": 8}}}]
            for i in range(20):
                obs = observation(i)
                rows.append({"event": "pose", "monotonic_s": obs.pop("monotonic_s"),
                             "estimator_input": {k: v for k, v in obs.items() if k != "event"}})
            path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            inputs, provenance = load_inputs(path)
            self.assertEqual(provenance, {"exact_recorded_inputs": 20})
            report, outputs = compare_log(path)
            self.assertFalse(report["hardware_accepted"])
            self.assertIn("NOT stationary jitter", report["metrics"]["kalman"]["jitter_interpretation"])
            self.assertEqual(outputs["one_euro"][0]["diagnostics"]["settings"]["min_cutoff_hz"], 2.5)
            self.assertEqual(compare_log(path)[0], report)
            variants = {'baseline':{'one_euro':{'min_cutoff_hz':2.5,'beta':8}},
                        'initial':{'kalman':{}},
                        'optimized':{'kalman':{'adaptive_quiet':True},
                                     'uncertainty':{'depth_variance_factor':2},
                                     'prediction_output':'driver_hold'}}
            abc = compare_variants_log(path, variants)
            self.assertEqual(compare_variants_log(path, variants), abc)
            self.assertEqual(set(abc['metrics']), set(variants))
            self.assertIsNone(abc['ground_truth'])
            self.assertFalse(abc['hardware_accepted'])

    def test_legacy_no_geometry_does_not_invent_measurement_confidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.jsonl"
            rows = [{"event": "session"}]
            for i in range(8):
                obs = observation(i)
                rows.append({"event": "pose", "monotonic_s": obs["monotonic_s"], "valid": True,
                    "imu": {"session_id": "test", "recenter_epoch": 0, "valid": True,
                        "monotonic_s": obs["monotonic_s"]},
                    "position_filter": {"raw_world_position_m": [0., 0., 1.]}})
            path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            inputs, provenance = load_inputs(path)
            self.assertEqual(provenance["legacy_reconstructed_inputs"], 8)
            self.assertTrue(all(r["covariance"] is None for r in inputs if r["event"] == "pose"))
            self.assertTrue(all(not r["packet"]["valid"] for r in replay(inputs)["kalman"]))

    def test_known_motion_delay_and_static_jitter(self):
        rows, reference = [], []
        for i in range(200):
            t = 1 + i * .01
            p = [np.sin((t - .07) * 7) * .1, 0, 1]
            rows.append({"monotonic_s": t, "packet": {"valid": True, "position_m": p}, "diagnostics": {}})
            reference.append({"monotonic_s": t, "position_m": [np.sin(t * 7) * .1, 0, 1]})
        self.assertAlmostEqual(metrics(rows, reference=reference)["response_delay_s"], .07, places=2)
        self.assertIsNone(metrics(rows)["response_delay_s"])
        static = replay([observation(i) for i in range(100)])["kalman"]
        measured = metrics(static, start_s=1.2, end_s=2.)
        np.testing.assert_allclose(measured["xyz_std_m"], 0, atol=1e-14)
        self.assertAlmostEqual(measured["depth_drift_m_s"], 0, places=12)

    def test_ambiguous_optical_frame_invalidates_publication_without_destroying_context(self):
        inputs = [observation(i) for i in range(10)]
        inputs[8].update(context_valid=True, prediction_allowed=False)
        inputs[8]["packet"].update(valid=False, position_m=None, state="lost")
        inputs[8]["covariance"] = None
        output = replay(inputs)["kalman"]
        self.assertFalse(output[8]["packet"]["valid"])
        self.assertTrue(output[9]["packet"]["valid"])
        self.assertNotEqual(output[9]["diagnostics"]["reset_reason"], "invalid_context")


if __name__ == "__main__":
    unittest.main()
