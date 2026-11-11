# SPDX-License-Identifier: MIT
"""Regression tests for B5-05 (Rodrigues at pi) and B5-06 (white-core cost).

B5-05: ``rotation_to_rodrigues`` must round-trip exact pi rotations on generic
axes with pure NumPy (no OpenCV needed).
B5-06: the white-core detector must keep exact masks/candidates while using a
constant number of full-frame operations (one union dilation) instead of one
full-frame dilation per selected component. Thresholds and caps are unchanged;
equivalence is checked against a frozen copy of the pre-fix algorithm.
"""
from __future__ import annotations

from pathlib import Path
import sys
import math
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from psvr_camera import geometry as G  # noqa: E402
from psvr_camera.blue_leds import BlueLedConfig, BlueLedDetector  # noqa: E402

try:
    import cv2  # noqa: F401

    HAS_CV2 = True
except ImportError:  # pragma: no cover - environment dependent
    HAS_CV2 = False


# ---------------------------------------------------------------------------
# B5-05: Rodrigues at pi.
# ---------------------------------------------------------------------------

def _normalized(vector) -> np.ndarray:
    axis = np.asarray(vector, dtype=np.float64)
    return axis / float(np.linalg.norm(axis))


class RodriguesPiTest(unittest.TestCase):
    def test_exact_pi_review_repro_roundtrip(self):
        # Review repro: axis (1, 2, 3), R = 2aa^T - I returned a null vector
        # pre-fix and rebuilt a rotation 2.828427125 away (Frobenius).
        axis = _normalized([1.0, 2.0, 3.0])
        rotation = G.rodrigues(math.pi * axis)
        np.testing.assert_allclose(
            rotation, 2.0 * np.outer(axis, axis) - np.eye(3), atol=1e-12
        )
        recovered = G.rotation_to_rodrigues(rotation)
        self.assertTrue(bool(np.all(np.isfinite(recovered))))
        rebuilt = G.rodrigues(recovered)
        distance = float(np.linalg.norm(rebuilt - rotation))
        self.assertLess(distance, 1e-6)

    def test_exact_pi_generic_axes_roundtrip(self):
        # Float64 trace noise alone shifts the recovered angle by ~1e-8 near
        # pi, so the round-trip bound is 1e-6: six orders of magnitude below
        # the pre-fix error of 2.83, and at the information limit of the
        # input matrix itself.
        axes = [
            [1.0, 2.0, 3.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
            [-0.4, 0.7, 0.2],
            [0.1, -0.9, 0.3],
            [1.0, 1.0, 1.0],
        ]
        for raw in axes:
            with self.subTest(axis=raw):
                axis = _normalized(raw)
                rotation = G.rodrigues(math.pi * axis)
                recovered = G.rotation_to_rodrigues(rotation)
                self.assertTrue(bool(np.all(np.isfinite(recovered))))
                self.assertAlmostEqual(float(np.linalg.norm(recovered)), math.pi, places=6)
                np.testing.assert_allclose(
                    G.rodrigues(recovered), rotation, atol=1e-6
                )

    def test_near_pi_roundtrip(self):
        # Near pi the trace-to-angle conditioning amplifies input rounding by
        # ~1/eps, so the bound scales with eps. The sign check below guards
        # the actual fix: pi + eps must round-trip through -(pi - eps)*axis.
        rng = np.random.default_rng(20261016)
        for eps in (1e-9, 1e-7, 1e-5, 1e-4):
            tolerance = max(1e-9, 5e-16 / eps)
            for _ in range(20):
                axis = _normalized(rng.normal(size=3))
                cases = ((math.pi - eps, math.pi - eps, axis),
                         (math.pi + eps, math.pi - eps, -axis))
                for theta, expected_angle, expected_axis in cases:
                    with self.subTest(eps=eps, theta=theta):
                        rotation = G.rodrigues(theta * axis)
                        recovered = G.rotation_to_rodrigues(rotation)
                        self.assertTrue(bool(np.all(np.isfinite(recovered))))
                        np.testing.assert_allclose(
                            G.rodrigues(recovered), rotation, atol=tolerance
                        )
                        # Same rotation as the equivalent acute vector.
                        expected = expected_angle * expected_axis
                        cosine = float(
                            np.dot(recovered, expected)
                            / (np.linalg.norm(recovered) * np.linalg.norm(expected))
                        )
                        self.assertGreater(cosine, 1.0 - 1e-9)

    def test_randomized_roundtrip_sweep(self):
        rng = np.random.default_rng(77)
        for trial in range(200):
            axis = _normalized(rng.normal(size=3))
            theta = float(rng.uniform(0.0, math.pi))
            with self.subTest(trial=trial, theta=theta):
                rotation = G.rodrigues(theta * axis)
                recovered = G.rotation_to_rodrigues(rotation)
                self.assertTrue(bool(np.all(np.isfinite(recovered))))
                # Trace-to-angle conditioning near 0/pi scales with 1/gap.
                gap = min(theta, math.pi - theta)
                tolerance = max(1e-9, 4e-16 / max(gap, 1e-12))
                np.testing.assert_allclose(
                    G.rodrigues(recovered), rotation, atol=tolerance
                )

    def test_small_angles_unchanged(self):
        for rvec in ([0.0, 0.0, 0.0], [1e-9, 0.0, 0.0], [0.1, -0.2, 0.3]):
            rotation = G.rodrigues(rvec)
            recovered = G.rotation_to_rodrigues(rotation)
            np.testing.assert_allclose(G.rodrigues(recovered), rotation, atol=1e-9)


# ---------------------------------------------------------------------------
# B5-06: white-core performance with exact masks.
# ---------------------------------------------------------------------------

def _reference_old_detect(image, config, cv2mod):
    """Frozen copy of the pre-fix detect(): one full-frame dilate per core."""
    cv2, c = cv2mod, config
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([c.hue_min, c.saturation_min, c.value_min], np.uint8),
                       np.array([c.hue_max, 255, 255], np.uint8))
    if c.include_white_cores:
        halo = cv2.inRange(hsv, np.array([c.hue_min, 40, 70], np.uint8),
                           np.array([c.hue_max, 255, 255], np.uint8))
        near_blue = cv2.dilate(halo, np.ones((7, 7), np.uint8))
        white = cv2.inRange(hsv, np.array([0, 0, max(200, c.value_min)], np.uint8),
                            np.array([179, 90, 255], np.uint8))
        count, components, stats, _ = cv2.connectedComponentsWithStats(white, 8)
        for label in range(1, count):
            x, y, width, height, area = (int(v) for v in stats[label])
            if area > c.max_area:
                continue
            local = components[y:y + height, x:x + width] == label
            if np.count_nonzero(near_blue[y:y + height, x:x + width][local]) >= 2:
                mask[y:y + height, x:x + width][local] = 255
                core = np.zeros_like(mask)
                core[y:y + height, x:x + width][local] = 255
                attached = cv2.bitwise_and(halo, cv2.dilate(core, np.ones((5, 5), np.uint8)))
                mask = cv2.bitwise_or(mask, attached)
    if c.opening_size:
        kernel = np.ones((c.opening_size, c.opening_size), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
    candidates = []
    for label in range(1, count):
        x, y, width, height, area = (int(v) for v in stats[label])
        if not c.min_area <= area <= c.max_area:
            continue
        # A clipped patch has a biased centre and cannot supply a correspondence.
        if x == 0 or y == 0 or x + width == image.shape[1] or y + height == image.shape[0]:
            continue
        candidates.append({
            "center_px": [float(v) for v in centroids[label]],
            "area_px": area,
            "bbox_px": [x, y, width, height],
        })
    return candidates


class CountingCv2:
    """Wraps the real cv2 module and counts full-frame operation calls."""

    def __init__(self, real):
        self._real = real
        self.calls: dict = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        target = getattr(self._real, name)
        if not callable(target):
            return target

        def wrapper(*args, **kwargs):
            self.calls[name] = self.calls.get(name, 0) + 1
            return target(*args, **kwargs)

        return wrapper


_BLUE = (255, 80, 0)  # BGR blue, as in the existing LED tests.
_WHITE = (255, 255, 255)


def _many_cores_frame():
    """640x480 frame with exactly 500 selectable white cores plus clutter."""
    frame = np.zeros((480, 640, 3), np.uint8)
    for gy in range(20):
        for gx in range(25):
            x = 10 + gx * 24
            y = 10 + gy * 22
            frame[y:y + 3, x:x + 3] = _WHITE
            frame[y + 1, x + 3] = _BLUE
    # White-only distractors: no blue nearby, never selected.
    for i in range(10):
        frame[445:448, 10 + i * 20:13 + i * 20] = _WHITE
    # Oversize white blob with a blue fringe: skipped by max_area.
    frame[435:480, 595:640] = _WHITE
    frame[435:480, 594] = _BLUE
    # Blue-only lamps far from every core: direct blue candidates.
    for i in range(6):
        frame[10 + i * 30:16 + i * 30, 600:606] = _BLUE
    return frame


def _random_frame(seed, width=320, height=240, shapes=120):
    rng = np.random.default_rng(seed)
    frame = np.zeros((height, width, 3), np.uint8)
    for _ in range(shapes):
        kind = int(rng.integers(0, 4))
        w = int(rng.integers(1, 9))
        h = int(rng.integers(1, 9))
        x = int(rng.integers(0, width - w))
        y = int(rng.integers(0, height - h))
        if kind == 0:
            color = _WHITE
        elif kind == 1:
            color = _BLUE
        elif kind == 2:
            color = (0, 0, 255)  # red noise, never blue/white.
        else:
            color = (0, 255, 0)  # green noise.
        frame[y:y + h, x:x + w] = color
    # A few oversize white patches: skipped by max_area even near blue.
    for _ in range(3):
        w = int(rng.integers(50, 80))
        h = int(rng.integers(50, 80))
        x = int(rng.integers(0, width - w))
        y = int(rng.integers(0, height - h))
        frame[y:y + h, x:x + w] = _WHITE
        frame[y:y + h, max(0, x - 1)] = _BLUE
    return frame


def _normalized_candidates(candidates):
    ordered = sorted(candidates, key=lambda c: (c["bbox_px"], c["area_px"], c["center_px"]))
    return (
        [tuple(c["bbox_px"]) for c in ordered],
        [c["area_px"] for c in ordered],
        [tuple(c["center_px"]) for c in ordered],
    )


def _assert_same_candidates(test, new, old):
    new_boxes, new_areas, new_centers = _normalized_candidates(new)
    old_boxes, old_areas, old_centers = _normalized_candidates(old)
    test.assertEqual(new_boxes, old_boxes)
    test.assertEqual(new_areas, old_areas)
    test.assertEqual(len(new_centers), len(old_centers))
    for left, right in zip(new_centers, old_centers):
        np.testing.assert_allclose(left, right, atol=1e-9)


@unittest.skipUnless(HAS_CV2, "OpenCV unavailable")
class WhiteCoreEquivalenceTest(unittest.TestCase):
    def test_many_cores_match_reference(self):
        import cv2 as real

        config = BlueLedConfig(min_area=6, max_area=2000, include_white_cores=True)
        frame = _many_cores_frame()
        new = BlueLedDetector(config).detect(frame)
        old = _reference_old_detect(frame, config, real)
        self.assertGreater(len(new), 500)
        _assert_same_candidates(self, new, old)

    def test_randomized_frames_match_reference(self):
        import cv2 as real

        configs = [
            BlueLedConfig(min_area=6, max_area=2000, include_white_cores=True),
            BlueLedConfig(min_area=6, max_area=2000, include_white_cores=True,
                          opening_size=3),
            BlueLedConfig(min_area=3, max_area=6000, include_white_cores=True),
        ]
        for seed in (11, 12, 13):
            frame = _random_frame(seed)
            for config in configs:
                with self.subTest(seed=seed, config=config):
                    new = BlueLedDetector(config).detect(frame)
                    old = _reference_old_detect(frame, config, real)
                    _assert_same_candidates(self, new, old)

    def test_clipped_core_and_thresholds_match_reference(self):
        import cv2 as real

        config = BlueLedConfig(min_area=6, max_area=2000, include_white_cores=True)
        # White core touching the frame border: clipped, rejected by both.
        frame = np.zeros((60, 80, 3), np.uint8)
        frame[0:4, 0:5] = _WHITE
        frame[0:4, 5] = _BLUE
        _assert_same_candidates(
            self, BlueLedDetector(config).detect(frame),
            _reference_old_detect(frame, config, real),
        )
        self.assertEqual(BlueLedDetector(config).detect(frame), [])
        # Valid lamp survives identically in both.
        frame = np.zeros((60, 80, 3), np.uint8)
        frame[20:24, 30:35] = _WHITE
        frame[20:24, 35] = _BLUE
        new = BlueLedDetector(config).detect(frame)
        old = _reference_old_detect(frame, config, real)
        self.assertEqual(len(new), 1)
        _assert_same_candidates(self, new, old)


@unittest.skipUnless(HAS_CV2, "OpenCV unavailable")
class WhiteCoreOperationCountTest(unittest.TestCase):
    def test_full_frame_dilates_are_constant(self):
        import cv2 as real

        config = BlueLedConfig(min_area=6, max_area=2000, include_white_cores=True)
        big = _many_cores_frame()
        small = np.zeros((120, 160, 3), np.uint8)
        for i in range(5):
            x, y = 10 + i * 24, 10 + i * 18
            small[y:y + 3, x:x + 3] = _WHITE
            small[y + 1, x + 3] = _BLUE

        proxy_big = CountingCv2(real)
        BlueLedDetector(config, module=proxy_big).detect(big)
        proxy_small = CountingCv2(real)
        BlueLedDetector(config, module=proxy_small).detect(small)

        # One dilate for the blue halo plus one for the union of all cores,
        # no matter how many cores were selected.
        self.assertEqual(proxy_big.calls.get("dilate", 0), 2)
        self.assertEqual(proxy_small.calls.get("dilate", 0), 2)
        self.assertEqual(proxy_big.calls.get("bitwise_and", 0), 1)
        self.assertEqual(proxy_big.calls.get("bitwise_or", 0), 2)

    def test_reference_scales_with_components(self):
        import cv2 as real

        config = BlueLedConfig(min_area=6, max_area=2000, include_white_cores=True)
        proxy = CountingCv2(real)
        _reference_old_detect(_many_cores_frame(), config, proxy)
        # 500 selected cores plus the halo dilation: the cost the fix removes.
        self.assertEqual(proxy.calls.get("dilate", 0), 501)


if __name__ == "__main__":
    unittest.main()
