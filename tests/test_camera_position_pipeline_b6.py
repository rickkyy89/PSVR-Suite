"""Regression tests for review block 6 items B6-01/02/04/05/07/08 (pipeline scope).

Synthetic packets only; no hardware, no profile or production-config changes.
Covers: ValueError (never TypeError/KeyError) on unusable configs and malformed
packets, the explicit-context boundary of the One Euro branch, the non-circular
context default, refreshed One Euro invalid diagnostics, missing-covariance
quality and raw-measurement isolation.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from tools.psvr_camera.position_filter import HeadPositionFilter
from tools.psvr_camera.position_pipeline import PositionPipeline, load_position_config

R_SMALL = np.eye(3) * 0.005 ** 2
RELIABLE = {"reliable": True, "reason": "ok", "accepted": True}


def packet(position=(0., 0., 1.), **changes):
    row = dict(session_id="activation", recenter_epoch=1, valid=True,
               state="tracking", position_m=list(position), seq=1)
    row.update(changes)
    return row


def lost_packet(**changes):
    row = dict(session_id="activation", recenter_epoch=1, valid=False,
               state="lost", position_m=None, seq=1)
    row.update(changes)
    return row


def acquire_kalman(pipe, alignment, start=1.0, count=4, position=(0., 0., 1.)):
    out = None
    for i in range(count):
        out = pipe.filter_packet(packet(position), alignment, start + i * 0.01,
                                 covariance=R_SMALL, quality=dict(RELIABLE),
                                 prediction_allowed=False, context_valid=True)
    return out


class ConfigValidationTests(unittest.TestCase):
    def test_retired_kalman_key_is_value_error(self):
        pipe_kwargs = {"kalman": {"retired_option": 1.0}}
        with self.assertRaises(ValueError):
            PositionPipeline("kalman", **pipe_kwargs)
        with self.assertRaises(ValueError):
            PositionPipeline("one_euro", one_euro={"retired_option": 1.0})
        with self.assertRaises(ValueError):
            PositionPipeline("kalman", uncertainty={"retired_option": 1.0})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "position.json"
            path.write_text(json.dumps({"filter": "kalman",
                                        "kalman": {"retired_option": 1.0}}),
                            encoding="utf-8")
            with self.assertRaises(ValueError):
                load_position_config(str(path))
            # The suite loader only catches (OSError, ValueError): prove no
            # TypeError leaks through the tolerant path either.
            try:
                load_position_config(str(path))
            except TypeError:
                self.fail("TypeError leaked from load_position_config")
            except ValueError:
                pass

    def test_non_dict_section_is_value_error(self):
        for section in ("kalman", "one_euro", "uncertainty"):
            with self.subTest(section=section):
                kwargs = {section: ["not", "a", "dict"]}
                with self.assertRaises(ValueError):
                    PositionPipeline("kalman", **kwargs)
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "position.json"
                    path.write_text(json.dumps({section: ["not", "a", "dict"]}),
                                    encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_position_config(str(path))

    def test_valid_configs_still_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "position.json"
            path.write_text(json.dumps({"filter": "kalman",
                                        "kalman": {"acceleration_std_m_s2": 2.0},
                                        "uncertainty": {"depth_variance_factor": 1.5},
                                        "prediction_output": "driver_hold"}),
                            encoding="utf-8")
            data = load_position_config(str(path))
            self.assertEqual(data["prediction_output"], "driver_hold")
        pipe = PositionPipeline("kalman",
                                kalman={"acceleration_std_m_s2": 2.0},
                                uncertainty={"depth_variance_factor": 1.5},
                                prediction_output="driver_hold")
        self.assertEqual(pipe.prediction_output, "driver_hold")


class OneEuroContextBoundaryTests(unittest.TestCase):
    def test_explicit_false_context_invalidates(self):
        pipe = PositionPipeline("one_euro")
        out = pipe.filter_packet(packet((0., 0., 1.)), np.eye(3), 1.0,
                                 covariance=R_SMALL, quality=dict(RELIABLE),
                                 prediction_allowed=True, context_valid=True)
        self.assertTrue(out["valid"])
        out = pipe.filter_packet(packet((0.5, 0., 1.)), np.eye(3), 1.02,
                                 covariance=R_SMALL, quality=dict(RELIABLE),
                                 prediction_allowed=True, context_valid=False)
        self.assertFalse(out["valid"])
        self.assertEqual(out["state"], "lost")
        self.assertIsNone(out["position_m"])
        self.assertEqual(out["session_id"], "activation")
        diag = pipe.diagnostics()
        self.assertEqual(diag["type"], "one_euro")
        self.assertEqual(diag["quality"], RELIABLE)
        self.assertIsNone(diag["filtered_world_position_m"])
        self.assertIsNone(diag["raw_world_position_m"])
        # The owned filter really held: same elapsed-time behaviour as invalid.
        self.assertIsNone(pipe.filter.filtered_world_position_m)

    def test_explicit_false_context_with_none_packet(self):
        pipe = PositionPipeline("one_euro")
        self.assertIsNone(pipe.filter_packet(None, np.eye(3), 1.0,
                                             context_valid=False))
        self.assertEqual(pipe.diagnostics()["type"], "one_euro")

    def test_omitted_context_keeps_passthrough(self):
        pipe = PositionPipeline("one_euro")
        out = pipe.filter_packet(packet((0.01, 0., 1.)), np.eye(3), 1.0,
                                 covariance=R_SMALL, quality=dict(RELIABLE),
                                 prediction_allowed=True)
        self.assertTrue(out["valid"])
        np.testing.assert_allclose(out["position_m"], [0.01, 0., 1.])


class ContextDefaultTests(unittest.TestCase):
    def test_default_context_without_raw_never_predicts(self):
        pipe = PositionPipeline("kalman")
        acquire_kalman(pipe, np.eye(3))
        # Lost optical frame, prediction nominally allowed, context omitted:
        # the old circular default (raw or prediction_allowed) published a
        # prediction without context; the fixed default must stay lost.
        out = pipe.filter_packet(copy.deepcopy(lost_packet()), np.eye(3), 1.06,
                                 covariance=None, quality=dict(RELIABLE),
                                 prediction_allowed=True)
        self.assertFalse(out["valid"])
        self.assertIsNone(out["position_m"])
        self.assertEqual(pipe.diagnostics()["reset_reason"], "invalid_context")

    def test_default_context_with_raw_still_tracks(self):
        pipe = PositionPipeline("kalman")
        out = None
        for i in range(4):
            out = pipe.filter_packet(packet((0., 0., 1.)), np.eye(3), 1.0 + i * 0.01,
                                     covariance=R_SMALL, quality=dict(RELIABLE),
                                     prediction_allowed=False)
        self.assertTrue(out["valid"])

    def test_none_packet_without_context_stays_none(self):
        pipe = PositionPipeline("kalman")
        self.assertIsNone(pipe.filter_packet(None, np.eye(3), 1.0,
                                             covariance=R_SMALL,
                                             quality=dict(RELIABLE),
                                             prediction_allowed=True))


class OneEuroInvalidDiagnosticsTests(unittest.TestCase):
    def test_last_refreshed_after_invalid(self):
        pipe = PositionPipeline("one_euro")
        pipe.filter_packet(packet((0., 0., 1.)), np.eye(3), 1.0)
        before = pipe.diagnostics()
        self.assertIsNotNone(before["filtered_world_position_m"])
        pipe.invalid(1.05)
        after = pipe.diagnostics()
        self.assertEqual(after["type"], "one_euro")
        self.assertEqual(after["reset_reason"], "camera_read_failure")
        self.assertEqual(after["invalid_at"], 1.05)
        self.assertFalse(after["quality"]["accepted"])
        self.assertFalse(after["quality"]["reliable"])
        self.assertIsNone(after["filtered_world_position_m"])
        self.assertIsNone(after["raw_world_position_m"])
        # Owned filter state agrees: brief hold, no synthetic position.
        self.assertIsNone(pipe.filter.filtered_world_position_m)


class MissingCovarianceQualityTests(unittest.TestCase):
    def test_covariance_none_marks_quality_unreliable(self):
        pipe = PositionPipeline("kalman")
        out = pipe.filter_packet(packet((0., 0., 1.)), np.eye(3), 1.0,
                                 covariance=None, quality=dict(RELIABLE),
                                 prediction_allowed=False, context_valid=True)
        self.assertFalse(out["valid"])
        quality = pipe.diagnostics()["quality"]
        self.assertFalse(quality["accepted"])
        self.assertFalse(quality["reliable"])
        self.assertEqual(quality["reason"], "missing_covariance")

    def test_raw_measurement_isolated_from_caller_alias(self):
        pipe = PositionPipeline("kalman")
        pkt = packet((0., 0., 1.))
        pipe.filter_packet(pkt, np.eye(3), 1.0,
                           covariance=R_SMALL, quality=dict(RELIABLE),
                           prediction_allowed=False, context_valid=True)
        pkt["position_m"][0] = 50.0
        diag = pipe.diagnostics()
        self.assertEqual(diag["raw_world_position_m"], [0., 0., 1.])


class MalformedPacketTests(unittest.TestCase):
    def test_pipeline_missing_valid_is_value_error(self):
        for mode in ("one_euro", "kalman"):
            with self.subTest(mode=mode):
                pipe = PositionPipeline(mode)
                pkt = packet()
                del pkt["valid"]
                with self.assertRaises(ValueError):
                    pipe.filter_packet(pkt, np.eye(3), 1.0,
                                       covariance=R_SMALL, quality=dict(RELIABLE),
                                       prediction_allowed=False, context_valid=True)

    def test_pipeline_missing_position_and_session_are_value_error(self):
        pipe = PositionPipeline("kalman")
        pkt = packet()
        del pkt["position_m"]
        with self.assertRaises(ValueError):
            pipe.filter_packet(pkt, np.eye(3), 1.0,
                               covariance=R_SMALL, quality=dict(RELIABLE),
                               prediction_allowed=False, context_valid=True)
        pkt = packet()
        del pkt["session_id"]
        with self.assertRaises(ValueError):
            pipe.filter_packet(pkt, np.eye(3), 1.0,
                               covariance=R_SMALL, quality=dict(RELIABLE),
                               prediction_allowed=False, context_valid=True)

    def test_filter_missing_valid_or_position_is_value_error(self):
        filt = HeadPositionFilter()
        pkt = packet()
        del pkt["valid"]
        with self.assertRaises(ValueError):
            filt.filter_packet(pkt, np.eye(3), 1.0)
        pkt = packet()
        del pkt["position_m"]
        with self.assertRaises(ValueError):
            filt.filter_packet(pkt, np.eye(3), 1.0)


if __name__ == "__main__":
    unittest.main()
