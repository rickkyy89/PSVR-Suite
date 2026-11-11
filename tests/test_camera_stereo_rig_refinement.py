"""Synthetic-only stereo rig fitting; no device imports or real captures required."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from tools.psvr_camera.refine_stereo_rig import (
    best_alignment, fit_stereo_rig, read_views, sample_views, symmetric_outline,
    refine,
)


REFERENCE = symmetric_outline([.08, .053, .078, .051, .025, .025])


def rotation(a, b, c):
    ca, sa = np.cos(a), np.sin(a)
    cb, sb = np.cos(b), np.sin(b)
    cc, sc = np.cos(c), np.sin(c)
    return (np.array([[1, 0, 0], [0, ca, -sa], [0, sa, ca]])
            @ np.array([[cb, 0, sb], [0, 1, 0], [-sb, 0, cb]])
            @ np.array([[cc, -sc, 0], [sc, cc, 0], [0, 0, 1]]))


def views_for(points, count=36, noise=.0002, outliers=False):
    rng = np.random.default_rng(42)
    views = []
    for i in range(count):
        r = rotation(.2*np.sin(i), .25*np.cos(i), .1*np.sin(i/2))
        p = points @ r.T + [.04*np.sin(i), .02*np.cos(i), .55+i*.008]
        p += rng.normal(0, noise, p.shape)
        if outliers and i % 9 == 0:
            p[rng.integers(5)] += [.12, -.08, .17]
        views.append(p[rng.permutation(5)])
    return np.array(views)


class StereoRigRefinementTests(unittest.TestCase):
    def test_similarity_proper_rotation_scale_and_permutation(self):
        r = rotation(.1, -.2, .04)
        target = .87*REFERENCE @ r.T + [.1, -.03, .8]
        perm = [3, 0, 4, 2, 1]
        fitted_r, t, scale, rms, mapping = best_alignment(REFERENCE, target[perm], similarity=True)
        np.testing.assert_allclose(fitted_r, r, atol=1e-12)
        np.testing.assert_allclose(t, [.1, -.03, .8], atol=1e-12)
        self.assertAlmostEqual(scale, .87, places=12)
        self.assertAlmostEqual(np.linalg.det(fitted_r), 1, places=12)
        self.assertLess(rms, 1e-12)
        np.testing.assert_allclose(target[perm][mapping], target)

    def test_global_scale_with_whole_constellation_rejection_and_immutability(self):
        truth = REFERENCE*.88
        views = views_for(truth, outliers=True)
        saved, ref = views.copy(), REFERENCE.copy()
        fitted, report = fit_stereo_rig(views, REFERENCE, model="similarity")
        np.testing.assert_allclose(fitted, truth, atol=.0003)
        self.assertEqual(report["training_outlier_indices"], [0, 9, 18, 27])
        self.assertFalse(report["physical_scale_validated"])
        np.testing.assert_array_equal(views, saved)
        np.testing.assert_array_equal(REFERENCE, ref)

    def test_outline_recovers_shape_up_to_rigid_frame_gauge(self):
        truth = symmetric_outline([.071, .041, .068, .049, .028, .021])
        views = views_for(truth, noise=.00015, outliers=True)
        fitted, report = fit_stereo_rig(views, REFERENCE)
        self.assertLess(best_alignment(fitted, truth)[3], .0005)
        self.assertLess(np.median(report["training_rms_3d_m"]), .0005)
        self.assertEqual(len(report["training_outlier_indices"]), 4)
        np.testing.assert_array_equal(fitted[2], [0, 0, 0])
        self.assertGreater(np.linalg.matrix_rank(fitted-fitted.mean(0)), 2)
        # Improvement generalizes to independent poses/noise.
        heldout = views_for(truth, count=12, noise=.0003)
        before = np.median([best_alignment(REFERENCE, p)[3] for p in heldout])
        after = np.median([best_alignment(fitted, p)[3] for p in heldout])
        self.assertLess(after, .1*before)

    def test_rejects_bad_shape_nonfinite_degenerate_and_outlier_majority(self):
        views = views_for(REFERENCE, count=12)
        for bad in (views[:7], views[:, :, :2], np.full(views.shape, np.nan)):
            with self.assertRaises(ValueError):
                fit_stereo_rig(bad, REFERENCE)
        for kwargs in ({"model": "free"}, {"iterations": 0}, {"iterations": True}):
            with self.assertRaises(ValueError):
                fit_stereo_rig(views, REFERENCE, **kwargs)
        with self.assertRaises(ValueError):
            best_alignment(np.zeros((5, 3)), views[0])
        with self.assertRaises(ValueError):
            fit_stereo_rig(views*10, REFERENCE)
        bad = views.copy()
        bad[:9, 0] += [.5, -.4, .6]
        with self.assertRaises(ValueError):
            fit_stereo_rig(bad, REFERENCE)

    def test_sampler_is_bounded_diverse_and_not_adjacent_60hz(self):
        views = [{"seq": i, "t": i/60., "points": REFERENCE+[0, 0, .5+i*.0002]}
                 for i in range(2400)]
        selected = sample_views(views)
        self.assertEqual(len(selected), 60)
        self.assertGreaterEqual(np.min(np.diff([v["t"] for v in selected])), .3-1e-12)
        self.assertGreater(np.ptp([v["points"].mean(0)[2] for v in selected]), .4)
        self.assertEqual([v["seq"] for v in selected], [v["seq"] for v in sample_views(views)])
        for kwargs in ({"max_views": 61}, {"max_views": 5}, {"min_time_gap_s": -1}):
            with self.assertRaises(ValueError):
                sample_views(views, **kwargs)

    def test_stream_reader_hashes_rejects_malformed_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"synthetic.jsonl"
            row = {"seq": 7, "t": 1., "matches": [{"p": p.tolist()} for p in REFERENCE+[0, 0, .8]],
                   "left": np.ones((5, 2)).tolist(), "right": np.ones((5, 2)).tolist()}
            raw = (json.dumps(row)+"\nnot json\n"+json.dumps({**row, "matches": []})+"\n").encode()
            path.write_bytes(raw)
            views, source = read_views(path)
            self.assertEqual(len(views), 1)
            self.assertEqual(source["total_rows"], 3)
            self.assertEqual(source["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(path.read_bytes(), raw)
            with self.assertRaises(ValueError):
                refine(path, "unused", "unused", path)
            self.assertEqual(path.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
