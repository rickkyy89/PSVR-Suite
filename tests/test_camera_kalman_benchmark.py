"""Fast contracts for the Kalman tuning benchmark; synthetic data only."""
import json
import unittest

import numpy as np

from tools.psvr_camera import kalman_optimization_benchmark as kob
from tools.psvr_camera.geometry import rodrigues
from tools.psvr_camera.pose_uncertainty import inflate_optical_covariance


class BenchmarkTests(unittest.TestCase):
    def test_base_replicates_original_generator(self):
        frames, seed = 24, 6130
        data = kob.make_scenarios(frames=frames, seed=seed)
        self.assertEqual(sorted(n for n in data if n in kob.BASE_SCENARIOS), sorted(kob.BASE_SCENARIOS))
        rng = np.random.default_rng(seed)
        for name in kob.BASE_SCENARIOS:
            for i in range(frames):
                now = 1 + i / 75
                elapsed = i / 75
                position = np.array([0.0, 0.0, 1.0])
                if name == "linear":
                    position[0] += 0.1 * elapsed
                if name in ("rapid", "depth"):
                    phase = elapsed % 2
                    displacement = 0.1 * np.clip((phase - 0.5) / 0.1, 0, 1)
                    displacement -= 0.1 * np.clip((phase - 1.5) / 0.1, 0, 1)
                    position[2 if name == "depth" else 0] += displacement
                raw = position + rng.normal(0, [0.0015, 0.0015, 0.005])
                if name == "outlier" and i in (200, 400, 600):
                    raw += [0.5, 0, 0.4]
                valid = not (name == "dropout" and (200 <= i < 204 or 400 <= i < 420))
                entry = data[name]["inputs"][i]
                self.assertEqual(entry["monotonic_s"], now)
                self.assertEqual(entry["packet"]["valid"], valid)
                if valid:
                    np.testing.assert_allclose(entry["packet"]["position_m"], raw.tolist())
                else:
                    self.assertIsNone(entry["packet"]["position_m"])
                np.testing.assert_allclose(np.array(entry["covariance"]),
                                           np.diag(np.square([0.0015, 0.0015, 0.005])))
                self.assertEqual(entry["quality"]["reliable"], True)
                np.testing.assert_allclose(data[name]["reference"][i]["position_m"], position.tolist())
                self.assertEqual(data[name]["reference"][i]["monotonic_s"], entry["monotonic_s"])

    def test_repeated_output_identical(self):
        first = kob.make_scenarios(frames=60, seed=6130)
        second = kob.make_scenarios(frames=60, seed=6130)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        rows_a = kob.run_variant(first["stationary"], kalman={"acceleration_std_m_s2": 1.0})
        rows_b = kob.run_variant(second["stationary"], kalman={"acceleration_std_m_s2": 1.0})
        self.assertEqual(json.dumps(rows_a, sort_keys=True), json.dumps(rows_b, sort_keys=True))
        again = kob.compare({"q1": {"kalman": {"acceleration_std_m_s2": 1.0}}},
                            frames=60, scenarios=["stationary"])
        twice = kob.compare({"q1": {"kalman": {"acceleration_std_m_s2": 1.0}}},
                            frames=60, scenarios=["stationary"])
        self.assertEqual(json.dumps(again, sort_keys=True), json.dumps(twice, sort_keys=True))

    def test_pipeline_inflation_single_legacy_helper_delegates(self):
        raw = np.diag([0.0015 ** 2, 0.0015 ** 2, 0.005 ** 2])
        np.testing.assert_allclose(kob._effective_covariance(raw, np.eye(3), 2.0),
                                   inflate_optical_covariance(raw, np.eye(3), 2.0))
        data = kob.make_scenarios(frames=48, seed=11)
        scenario = data["stationary"]
        rows = kob.run_variant(scenario, kalman={}, uncertainty={"depth_factor": 2.0})
        for row, entry in zip(rows, scenario["inputs"]):
            np.testing.assert_allclose(np.array(row["raw_covariance"]), np.array(entry["covariance"]))
            np.testing.assert_allclose(np.array(row["effective_covariance"]),
                                       inflate_optical_covariance(np.array(entry["covariance"]),
                                                                  np.array(entry["alignment"]), 2.0))
        rows_alias = kob.run_variant(scenario, kalman={},
                                     uncertainty={"depth_variance_factor": 2.0})
        self.assertEqual(json.dumps(rows, sort_keys=True), json.dumps(rows_alias, sort_keys=True))

    def test_prediction_output_driver_hold_holds_predictions(self):
        data = kob.make_scenarios(frames=450, seed=6130)
        scenario = data["dropout"]
        camera = kob.run_variant(scenario, kalman={}, prediction_output="camera")
        hold = kob.run_variant(scenario, kalman={}, prediction_output="driver_hold")
        cam_wire = sum(r["wire_valid"] for r in camera)
        hold_wire = sum(r["wire_valid"] for r in hold)
        cam_est = sum(r["estimator_valid"] for r in camera)
        hold_est = sum(r["estimator_valid"] for r in hold)
        self.assertEqual(cam_est, hold_est)
        self.assertGreaterEqual(cam_wire, hold_wire)
        self.assertTrue(any(r["estimator_valid"] and not r["wire_valid"] for r in hold))

    def test_tilted_moves_optical_axis(self):
        data = kob.make_scenarios(frames=48, seed=7)
        scenario = data["tilted_depth"]
        a = rodrigues(np.array([0.3, 0.7, 0.0]))
        u = (a.T @ np.array([0.0, 0.0, 1.0])).reshape(3)
        np.testing.assert_allclose(np.array(scenario["inputs"][0]["alignment"]), a)
        refs = np.array([r["position_m"] for r in scenario["reference"]])
        delta = refs - np.array([0.0, 0.0, 1.0])
        cross = np.linalg.norm(np.cross(delta, np.broadcast_to(u, delta.shape)), axis=1)
        self.assertTrue(np.all(cross < 1e-12))
        rows = kob.run_variant(scenario, kalman={})
        summary = kob.summarize(rows, scenario["reference"], scenario["classification"], start_s=1.0)
        self.assertIsNotNone(summary["optical_rmse_m"])
        self.assertFalse(summary["genuinely_static"])

    def test_noisy_fast_depth_bounded_z(self):
        data = kob.make_scenarios(frames=150, seed=9)
        scenario = data["noisy_fast_depth"]
        refs = np.array([r["position_m"] for r in scenario["reference"]])
        self.assertTrue(np.all(refs[:, 0] == 0.0))
        self.assertLessEqual(float(np.ptp(refs[:, 2])), 0.5 + 1e-9)
        self.assertIn("fast_direction_change", scenario["classification"])
        self.assertFalse(scenario["static"])

    def test_partial_blocks_seed_then_loss(self):
        data = kob.make_scenarios(frames=100, seed=13)
        scenario = data["partial_leds"]
        self.assertTrue(all(c == "clean_5led" for c in scenario["classification"][:20]))
        self.assertTrue(all(c == "partial_2led" for c in scenario["classification"][20:30]))
        self.assertTrue(all(c == "partial_3led" for c in scenario["classification"][30:60]))
        self.assertTrue(all(c == "partial_4led" for c in scenario["classification"][60:90]))
        self.assertIsNone(scenario["inputs"][20]["covariance"])
        self.assertEqual(scenario["inputs"][20]["led_count"], 2)
        self.assertFalse(scenario["inputs"][30]["quality"]["reliable"])
        rows = kob.run_variant(scenario, kalman={})
        summary = kob.summarize(rows, scenario["reference"], scenario["classification"], start_s=1.0)
        self.assertTrue(summary["genuinely_static"])
        fresh_inputs = []
        for k, e in enumerate(scenario["inputs"][30:60]):
            fresh_inputs.append({"event": "pose", "monotonic_s": 1.0 + k / 75.0,
                                 "packet": dict(e["packet"]), "alignment": e["alignment"],
                                 "covariance": e["covariance"], "quality": dict(e["quality"]),
                                 "context_valid": True, "prediction_allowed": False,
                                 "led_count": e["led_count"], "tag": e["tag"]})
        lost_rows = kob.run_variant({"inputs": fresh_inputs}, kalman={})
        self.assertFalse(any(r["packet"]["valid"] for r in lost_rows))

    def test_optical_nis_uses_s_not_r(self):
        data = kob.make_scenarios(frames=60, seed=21)
        scenario = data["stationary"]
        rows = kob.run_variant(scenario, kalman={"acceleration_std_m_s2": 1.5})
        summary = kob.summarize(rows, scenario["reference"], scenario["classification"], start_s=1.0)
        manual = []
        for r in rows:
            diag, axis = r["diagnostics"], r["optical_axis_world_m"]
            if diag.get("innovation_m") is not None and diag.get("innovation_covariance") is not None:
                u = np.asarray(axis, float)
                s = np.asarray(diag["innovation_covariance"], float)
                manual.append(float((float(u @ np.asarray(diag["innovation_m"], float)) ** 2) / float(u @ s @ u)))
        if manual:
            self.assertAlmostEqual(summary["optical_nis_mean"], float(np.mean(manual)), places=12)
        self.assertIn("innov_autocorr_all", summary)
        self.assertIn("innov_autocorr_accepted", summary)

    def test_false_reject_split_and_overshoot_static(self):
        data = kob.make_scenarios(frames=60, seed=6130)
        scenario = data["stationary"]
        rows = kob.run_variant(scenario, kalman={})
        summary = kob.summarize(rows, scenario["reference"], scenario["classification"], start_s=1.0)
        self.assertEqual(summary["false_reject_clean"], summary["clean_invalid"])
        self.assertEqual(summary["false_reject_rate"], summary["clean_invalid_rate"])
        self.assertGreaterEqual(summary["clean_invalid"], summary["clean_gate_rejected"])
        self.assertTrue(summary["genuinely_static"])
        self.assertIn("wire_valid_frames", summary)
        self.assertIn("estimator_valid_frames", summary)
        self.assertIn("initial_acquisition_ms", summary)
        self.assertIn("quiet_active_fraction", summary)

    def test_onset_exit_latency_and_t50(self):
        data = kob.make_scenarios(frames=400, seed=6130)
        scenario = data["onset"]
        self.assertIn("motion_start_s", scenario)
        rows = kob.run_variant(scenario, kalman={"acceleration_std_m_s2": 1.5})
        summary = kob.summarize(rows, scenario["reference"], scenario["classification"],
                                start_s=1.0, motion_start_s=scenario["motion_start_s"])
        self.assertIsNone(summary["onset_exit_latency_s"])  # This variant never enters quiet.
        self.assertIsNotNone(summary["onset_response_10mm_s"])
        self.assertGreaterEqual(summary["onset_response_10mm_s"], 0.0)
        self.assertIsNotNone(summary["onset_t50_s"])
        self.assertGreaterEqual(summary["onset_t90_s"], summary["onset_t50_s"])

    def test_default_variants_profile(self):
        variants = kob.default_variants()
        self.assertIn("one_euro_2.5_8", variants)
        self.assertIn("initial_Q3_camera", variants)
        self.assertIn("optimized_driver_hold", variants)
        self.assertIn("optimized_camera", variants)
        self.assertIn("static_Q1.5_f1", variants)
        self.assertIn("adaptive_Q5_f1", variants)
        self.assertEqual(variants["initial_Q3_camera"]["kalman"]["acceleration_std_m_s2"], 3.0)
        self.assertEqual(variants["optimized_driver_hold"]["prediction_output"], "driver_hold")
        self.assertEqual(variants["optimized_camera"]["prediction_output"], "camera")
        self.assertEqual(variants["optimized_camera"]["kalman"], variants["optimized_driver_hold"]["kalman"])
        self.assertNotEqual(sorted(variants)[0], "static_Q1.5_f1")
        report = kob.compare({"one_euro_2.5_8": variants["one_euro_2.5_8"],
                              "initial_Q3_camera": variants["initial_Q3_camera"]},
                             frames=48, scenarios=["stationary", "onset"])
        self.assertEqual(sorted(report), ["onset", "stationary"])
        for summary in report["onset"].values():
            self.assertIn("onset_exit_latency_s", summary)
            self.assertIsNone(summary["timing_us_mean"])


if __name__ == "__main__":
    unittest.main()
