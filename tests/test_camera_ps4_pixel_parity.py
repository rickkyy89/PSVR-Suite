"""Device-free scalar BT.601/crop oracles; NOT DirectShow converter parity.

No native DLL, capture, COM graph, preferences, calibration or driver activity.
Missing NumPy/OpenCV is an error in the junior camera environment, not a pass.
"""
import json
import math
import unittest

from tools.psvr_camera import ps4_owned_dshow as owned


def scalar_bt601_bgr(y, u, v):
    """Independent legal-range BT.601 inverse, rounded half-up then saturated.

    Derived from Kr=.299, Kb=.114 and studio swing Y=16..235, Cb/Cr=16..240.
    No OpenCV constants, functions, production crop or SIMD algorithm is used.
    This oracle deliberately makes no claim about out-of-range signal handling.
    """
    if not (16 <= y <= 235 and 16 <= u <= 240 and 16 <= v <= 240):
        raise ValueError("oracle accepts legal limited-range samples only")
    kr, kb = 0.299, 0.114
    kg = 1.0 - kr - kb
    luminance = (y - 16) * 255.0 / 219.0
    cb, cr = (u - 128) * 255.0 / 224.0, (v - 128) * 255.0 / 224.0
    red = luminance + 2.0 * (1.0 - kr) * cr
    blue = luminance + 2.0 * (1.0 - kb) * cb
    green = luminance - 2.0 * kb * (1.0 - kb) / kg * cb - 2.0 * kr * (1.0 - kr) / kg * cr
    return tuple(max(0, min(255, math.floor(value + 0.5))) for value in (blue, green, red))


# Hand-anchored known pixels, in BGR (not generated via the function under test).
KNOWN_PIXELS = (
    ((16, 128, 128), (0, 0, 0)),
    ((235, 128, 128), (255, 255, 255)),
    ((126, 128, 128), (128, 128, 128)),
    ((81, 128, 128), (76, 76, 76)),
    ((81, 90, 240), (0, 0, 254)),
    ((145, 54, 34), (1, 255, 0)),
    ((41, 240, 110), (255, 0, 0)),
)


def coordinate_fixture():
    """Packed Y0 U Y1 V, 408 rows of exactly 3496 bytes; scalar BGR oracle.

    A small scalar-converted palette keeps the full-frame oracle inexpensive.
    The coordinate field is asymmetric across rows, columns and both eyes.
    Marker pairs straddle x47/48, 687/688, 1327/1328; y399/400 differ too.
    """
    import numpy as np

    palette = []
    for p in range(251):
        y0, y1 = 16 + p * 37 % 220, 16 + (p * 53 + 79) % 220
        u, v = 16 + (p * 29 + 23) % 225, 16 + (p * 47 + 101) % 225
        if y1 == y0:
            y1 = 16 + (y1 - 16 + 1) % 220
        if u == v:
            v = 16 + (v - 16 + 1) % 225
        palette.append((y0, u, y1, v))
    rows = np.arange(408, dtype=np.int32)[:, None]
    pairs = np.arange(874, dtype=np.int32)[None, :]
    ids = (rows * 37 + pairs * 19 + (rows // 7) * 11 + (pairs // 13) * 17) % 251
    for row, shift in ((0, 13), (1, 29), (399, 61), (400, 109), (407, 157)):
        ids[row] = (ids[row] + shift) % 251
    for marker, pair in enumerate((23, 24, 343, 344, 663, 664)):
        ids[:, pair] = (np.arange(408) * 43 + marker * 31 + 17) % 251
    packed = np.asarray(palette, dtype=np.uint8)[ids].reshape(408, 1748, 2)
    scalar_palette = np.asarray([
        (scalar_bt601_bgr(y0, u, v), scalar_bt601_bgr(y1, u, v))
        for y0, u, y1, v in palette
    ], dtype=np.uint8)
    expected = scalar_palette[ids].reshape(408, 1748, 3)
    return packed.tobytes(), expected


def pixel_metrics(actual, expected):
    import numpy as np

    delta = np.abs(actual.astype(np.int16) - expected.astype(np.int16))
    return {"max_channel_lsb": int(delta.max()),
            "different_channels": int(np.count_nonzero(delta)),
             "different_pixels": int(np.count_nonzero(np.any(delta != 0, axis=2)))}


def synthetic_patch_fixture(patches):
    """Duplicate eye-local, pair-aligned patches into legal limited-range YUY2.

    Each entry is (x, y, width, height, (Y0, U, Y1, V)). Reference pixels
    come only from the scalar oracle; neither decoder nor cvtColor builds them.
    """
    import numpy as np

    packed = np.empty((408, 874, 4), dtype=np.uint8)
    packed[:] = (16, 128, 16, 128)
    reference = np.zeros((408, 1748, 3), dtype=np.uint8)
    for x, y, width, height, (y0, u, y1, v) in patches:
        if x % 2 or width % 2 or not (0 <= x < x + width <= 640 and 0 <= y < y + height <= 400):
            raise ValueError("patch must be pair-aligned and contained in an eye")
        colors = (scalar_bt601_bgr(y0, u, v), scalar_bt601_bgr(y1, u, v))
        for start in (48, 688):
            first = start + x
            packed[y:y + height, first // 2:(first + width) // 2] = (y0, u, y1, v)
            reference[y:y + height, first:first + width:2] = colors[0]
            reference[y:y + height, first + 1:first + width:2] = colors[1]
    return packed.tobytes(), reference


def suite_fixture_config(include_white_cores=True):
    """Explicit Suite operating point, NOT BlueLedConfig's library defaults.

    No persisted Suite settings, calibration or user preferences are loaded.
    """
    from tools.psvr_camera.blue_leds import BlueLedConfig
    return BlueLedConfig(value_min=160, saturation_min=90, min_area=20,
                         include_white_cores=include_white_cores)


class _MaskRecorder:
    """Forward real detector operations; observe its final component mask only."""
    def __init__(self, cv2):
        self.cv2 = cv2
        self.mask = None

    def __getattr__(self, name):
        return getattr(self.cv2, name)

    def connectedComponentsWithStats(self, mask, *args, **kwargs):
        self.mask = mask.copy()
        return self.cv2.connectedComponentsWithStats(mask, *args, **kwargs)


class PixelParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import cv2
        import numpy as np
        cls.cv2, cls.np = cv2, np
        cls.raw, cls.reference = coordinate_fixture()

    def assert_pixels(self, actual, expected, label, tolerance=1):
        self.assertEqual(actual.shape, expected.shape)
        self.assertEqual(actual.dtype, self.np.uint8)
        metrics = pixel_metrics(actual, expected)
        print(json.dumps({"parity_fixture": label, **metrics}, sort_keys=True))
        # <=1 only for independently rounded BT.601 color arithmetic. No
        # geometric tolerance, interpolation or % mismatching-pixel allowance.
        self.assertLessEqual(metrics["max_channel_lsb"], tolerance, label)

    def assert_candidates(self, actual, expected):
        # Component-label ordering is not part of the detector contract.
        # Sort only for comparison; retain duplicates and compare every field.
        def bbox(candidate):
            return tuple(candidate["bbox_px"])
        self.assertEqual(sorted(actual, key=bbox), sorted(expected, key=bbox))

    def test_scalar_reference_has_hand_anchored_limited_range_pixels(self):
        for sample, expected in KNOWN_PIXELS:
            with self.subTest(sample=sample):
                self.assertEqual(scalar_bt601_bgr(*sample), expected)
        for sample in ((15, 128, 128), (236, 128, 128), (100, 15, 128), (100, 128, 241)):
            with self.assertRaises(ValueError):
                scalar_bt601_bgr(*sample)

    def test_known_unequal_lumas_shared_chroma_and_bgr_channel_mapping(self):
        np = self.np
        for (y0, u, v), known in KNOWN_PIXELS:
            y1 = 235 if y0 == 16 else 16
            raw = bytes((y0, u, y1, v)) * (owned.TRANSPORT_BYTES // 4)
            expected = np.empty((400, 640, 3), dtype=np.uint8)
            expected[:, 0::2] = known
            expected[:, 1::2] = scalar_bt601_bgr(y1, u, v)
            for eye, actual in enumerate(owned.decode_pair(raw)):
                self.assert_pixels(actual, expected, f"known_{y0}_{u}_{v}_eye{eye}",
                                   tolerance=0 if u == v == 128 else 1)
            # Explicit byte-level YUYV and BGR anchors: red != blue; second
            # pixel shares U/V but has its own Y, not duplicated pair luma.
            self.assertNotEqual(tuple(expected[0, 0]), tuple(expected[0, 1]))

    def test_coordinate_markers_full_eyes_are_top_down_without_flips_or_swaps(self):
        left, right = owned.decode_pair(self.raw)
        for eye, actual, start in (("left", left, 48), ("right", right, 688)):
            expected = self.reference[:400, start:start + 640]
            self.assert_pixels(actual, expected, eye + "_coordinate_oracle")
            for row in (0, 1, 198, 399):
                for column in (0, 1, 638, 639):
                    difference = self.np.abs(actual[row, column].astype(self.np.int16) -
                                             self.reference[row, start + column].astype(self.np.int16))
                    self.assertLessEqual(int(difference.max()), 1, (eye, row, start + column))

    def test_prefix_tail_and_y400_padding_markers_are_excluded(self):
        np = self.np
        original = np.frombuffer(self.raw, dtype=np.uint8).reshape(408, 1748, 2)
        mutated = original.copy()
        # Whole pairs so legal packed YUY2 remains intact. Includes x47 and
        # x1328 plus every excluded row, but never x48..1327/y0..399.
        for region in (mutated[:, :48], mutated[:, 1328:], mutated[400:]):
            region[:, 0::2, 0], region[:, 0::2, 1] = 235, 16
            region[:, 1::2, 0], region[:, 1::2, 1] = 16, 240
        self.assertFalse(np.array_equal(mutated[:, :48], original[:, :48]))
        self.assertFalse(np.array_equal(mutated[:, 1328:], original[:, 1328:]))
        self.assertFalse(np.array_equal(mutated[400:], original[400:]))
        np.testing.assert_array_equal(mutated[:400, 48:1328], original[:400, 48:1328])
        left, right = owned.decode_pair(mutated.tobytes())
        self.assert_pixels(left, self.reference[:400, 48:688], "excluded_markers_left")
        self.assert_pixels(right, self.reference[:400, 688:1328], "excluded_markers_right")

    def test_coordinate_oracle_is_sensitive_to_mapping_and_packing_errors(self):
        np = self.np
        expected = self.reference[:400, 48:688]
        wrong = {
            "vertical_flip": expected[::-1], "horizontal_flip": expected[:, ::-1],
            "eye_swap": self.reference[:400, 688:1328],
            "prefix_off_by_one": self.reference[:400, 47:687],
            "end_off_by_one": self.reference[:400, 49:689],
            "skip_first_row": self.reference[1:401, 48:688],
            "include_padding_row": self.reference[8:408, 48:688],
            "rgb_instead_of_bgr": expected[:, :, ::-1],
        }
        for label, other in wrong.items():
            with self.subTest(label=label):
                self.assertGreater(pixel_metrics(other, expected)["different_pixels"], 1000)
                self.assertGreater(pixel_metrics(other, expected)["max_channel_lsb"], 1)
        # Independent scalar counterexamples for packed Y0/Y1 and U/V swaps.
        y0, u, y1, v = (59, 73, 187, 201)
        correct = (scalar_bt601_bgr(y0, u, v), scalar_bt601_bgr(y1, u, v))
        self.assertNotEqual(correct, correct[::-1])
        self.assertNotEqual(correct, (scalar_bt601_bgr(y0, v, u), scalar_bt601_bgr(y1, v, u)))
        self.assertFalse(np.array_equal(expected, wrong["eye_swap"]))

    def test_returned_eyes_are_detached_writable_and_do_not_alias(self):
        np = self.np
        left, right = owned.decode_pair(self.raw)
        self.assertFalse(np.shares_memory(left, right))
        self.assertTrue(left.flags.owndata and right.flags.owndata)
        self.assertTrue(left.flags.writeable and right.flags.writeable)
        untouched_right = right.copy()
        left[:] = 0
        np.testing.assert_array_equal(right, untouched_right)
        # A fresh decode must still match the independent coordinate oracle;
        # comparing two aliases of immutable input bytes would prove nothing.
        fresh_left, _ = owned.decode_pair(self.raw)
        self.assert_pixels(fresh_left, self.reference[:400, 48:688], "detached_after_mutation")

    def test_unchanged_detector_masks_on_scalar_known_color_patches(self):
        from tools.psvr_camera.blue_leds import BlueLedConfig, BlueLedDetector
        np = self.np
        packed = np.empty((408, 874, 4), dtype=np.uint8)
        packed[:] = (16, 128, 16, 128)
        reference = np.zeros((408, 1748, 3), dtype=np.uint8)
        for x, y in ((80, 40), (720, 140)):
            packed[y:y + 20, x // 2:(x + 20) // 2] = (41, 240, 41, 110)
            reference[y:y + 20, x:x + 20] = scalar_bt601_bgr(41, 240, 110)
            packed[y + 8:y + 12, (x + 8) // 2:(x + 12) // 2] = (235, 128, 235, 128)
            reference[y + 8:y + 12, x + 8:x + 12] = scalar_bt601_bgr(235, 128, 128)
        packed[100:120, 150:160] = (81, 90, 81, 240)  # non-blue red control
        reference[100:120, 300:320] = scalar_bt601_bgr(81, 90, 240)
        actual = owned.decode_pair(packed.tobytes())
        # Same production detector/bounds on both inputs, with its existing
        # white-core branch both off and on. No user preferences are read/written.
        for white_cores in (False, True):
            config = BlueLedConfig(include_white_cores=white_cores)
            for eye, start in enumerate((48, 688)):
                scalar_eye = reference[:400, start:start + 640].copy()
                scalar_cv, owned_cv = _MaskRecorder(self.cv2), _MaskRecorder(self.cv2)
                scalar_candidates = BlueLedDetector(config, module=scalar_cv).detect(scalar_eye)
                owned_candidates = BlueLedDetector(config, module=owned_cv).detect(actual[eye])
                differences = int(np.count_nonzero(scalar_cv.mask != owned_cv.mask))
                print(json.dumps({"detector_fixture": "known_color_patches", "eye": eye,
                                  "white_cores": white_cores, "mask_pixel_differences": differences}))
                self.assertEqual(differences, 0)
                self.assert_candidates(owned_candidates, scalar_candidates)
                self.assertEqual(len(owned_candidates), 1)  # not vacuous empty-mask parity
                self.assertEqual(int(np.count_nonzero(owned_cv.mask)), 400 if white_cores else 384)

    def test_legal_luma_chroma_ramps_clipping_and_color_transitions(self):
        np = self.np
        # All 220 legal Y values, unequal pair lumas, all 225 legal U/V
        # values, extreme chroma clipping, then abrupt pair-boundary colors.
        palettes = [
            [(16 + i % 220, 128, 235 - i % 220, 128) for i in range(320)],
            [(16 + i % 220, 16 + i % 225, 235 - i % 220, 240 - i % 225)
             for i in range(320)],
            [(y0, u, y1, v) for y0, y1 in ((16, 235), (17, 234), (100, 101))
             for u in (16, 17, 128, 239, 240) for v in (16, 17, 128, 239, 240)],
            [(16, 128, 235, 128), (81, 90, 82, 240), (145, 54, 146, 34),
             (41, 240, 42, 110), (235, 128, 16, 128)],
        ]
        patches = []
        for band, palette in enumerate(palettes):
            for pair in range(320):
                patches.append((pair * 2, band * 100, 2, 100, palette[pair % len(palette)]))
        raw, reference = synthetic_patch_fixture(patches)
        for eye, actual in enumerate(owned.decode_pair(raw)):
            start = (48, 688)[eye]
            expected = reference[:400, start:start + 640]
            self.assert_pixels(actual, expected, f"legal_ramps_clipping_transitions_eye{eye}")
            self.assertTrue(np.any(expected[200:300] == 0))
            self.assertTrue(np.any(expected[200:300] == 255))
            # No spatial tolerance: every pixel immediately around an abrupt
            # chroma step must also be within one channel LSB of its own oracle.
            self.assert_pixels(actual[300:, :10], expected[300:, :10],
                               f"adjacent_color_steps_eye{eye}")

    def _detect_recorded(self, image, config):
        from tools.psvr_camera.blue_leds import BlueLedDetector
        recorder = _MaskRecorder(self.cv2)
        candidates = BlueLedDetector(config, module=recorder).detect(image)
        self.assertIsNotNone(recorder.mask)
        return candidates, recorder.mask

    def test_suite_value_and_saturation_thresholds_with_explicit_branch_expectations(self):
        # Scalar HSV anchors selected independently of decode_pair. Hue is
        # comfortably inside 85..135; only the named inclusive bound is crossed.
        samples = (
            ((77, 171, 121), (120, 158, 158), False),
            ((76, 172, 123), (122, 164, 159), False),
            ((79, 171, 106), (111, 194, 160), True),
            ((78, 172, 111), (114, 184, 161), True),
            ((79, 172, 112), (114, 179, 162), True),
            ((139, 157, 128), (125, 88, 202), False),
            ((139, 158, 126), (123, 89, 204), False),
            ((139, 152, 116), (111, 90, 192), True),
            ((139, 151, 115), (110, 91, 190), True),
            ((138, 158, 128), (125, 92, 203), True),
        )
        patches = [(20 + i * 40, 60, 20, 20, (y, u, y, v))
                   for i, ((y, u, v), _, _) in enumerate(samples)]
        raw, reference = synthetic_patch_fixture(patches)
        for eye, image in enumerate(owned.decode_pair(raw)):
            start = (48, 688)[eye]
            scalar_eye = reference[:400, start:start + 640]
            self.assert_pixels(image, scalar_eye, f"suite_thresholds_eye{eye}")
            decoded_hsv = []
            for i, (_, hsv, _) in enumerate(samples):
                scalar_hsv = self.cv2.cvtColor(scalar_eye[60:61, 20 + i * 40:21 + i * 40],
                                              self.cv2.COLOR_BGR2HSV)[0, 0]
                self.assertEqual(tuple(int(v) for v in scalar_hsv), hsv)
                decoded_hsv.append(tuple(int(v) for v in self.cv2.cvtColor(
                    image[60:61, 20 + i * 40:21 + i * 40], self.cv2.COLOR_BGR2HSV)[0, 0]))
            for white_cores in (False, True):
                config = suite_fixture_config(white_cores)
                # Bright S88/S89 blue patches also satisfy the white-core
                # S<=90/V>=200 and halo S>=40 gates. Their own blue halo
                # supplies proximity, so the enabled branch reintroduces
                # these components despite failing the primary S90 gate.
                expected_candidates = [
                    {"center_px": [29.5 + i * 40, 69.5], "area_px": 400,
                     "bbox_px": [20 + i * 40, 60, 20, 20]}
                    for i, (_, _, accepted) in enumerate(samples)
                    if accepted or (white_cores and i in (5, 6))]
                scalar_candidates, scalar_mask = self._detect_recorded(scalar_eye, config)
                candidates, mask = self._detect_recorded(image, config)
                self.assert_candidates(scalar_candidates, expected_candidates)
                # Rounding by <=1 BGR LSB may legitimately cross S/V bounds.
                # Derive decoded expectations from its measured HSV, not from
                # an assumption that masks must equal the scalar masks.
                # Isolated, uniform rectangles make white-core proximity
                # equivalent to self-overlap with their own halo here.
                decoded_expected = []
                for i, (hue, saturation, value) in enumerate(decoded_hsv):
                    primary = 85 <= hue <= 135 and saturation >= 90 and value >= 160
                    self_core = (white_cores and 85 <= hue <= 135 and
                                 40 <= saturation <= 90 and value >= 200)
                    if primary or self_core:
                        decoded_expected.append({"center_px": [29.5 + i * 40, 69.5],
                                                 "area_px": 400,
                                                 "bbox_px": [20 + i * 40, 60, 20, 20]})
                self.assert_candidates(candidates, decoded_expected)
                for observed, expected_components in ((scalar_mask, expected_candidates),
                                                       (mask, decoded_expected)):
                    expected_mask = self.np.zeros((400, 640), dtype=self.np.uint8)
                    for candidate in expected_components:
                        x, y, width, height = candidate["bbox_px"]
                        expected_mask[y:y + height, x:x + width] = 255
                    self.np.testing.assert_array_equal(observed, expected_mask)
                print(json.dumps({"detector_fixture": "suite_threshold_branches", "eye": eye,
                                  "white_cores": white_cores,
                                  "mask_pixel_differences": int(self.np.count_nonzero(mask != scalar_mask)),
                                  "scalar_candidate_count": len(scalar_candidates),
                                  "decoded_candidate_count": len(candidates)}))

    def test_suite_one_lsb_threshold_sensitivity_is_not_mask_parity(self):
        # These legal samples straddle S90 or V160 under one B-channel LSB.
        # Different valid converter rounding can change entire components.
        # Do not require that a particular OpenCV build exhibit a difference.
        cases = (((114, 151, 129), (161, 104, 116), (126, 90, 161)),
                 ((127, 143, 105), (160, 142, 93), (98, 107, 160)))
        for sample, bgr, hsv in cases:
            with self.subTest(sample=sample):
                self.assertEqual(scalar_bt601_bgr(*sample), bgr)
                color = self.np.array([[bgr]], dtype=self.np.uint8)
                self.assertEqual(tuple(self.cv2.cvtColor(color, self.cv2.COLOR_BGR2HSV)[0, 0]), hsv)
                perturbed = color.copy()
                perturbed[0, 0, 0] -= 1
                perturbed_hsv = self.cv2.cvtColor(perturbed, self.cv2.COLOR_BGR2HSV)[0, 0]
                self.assertFalse(85 <= perturbed_hsv[0] <= 135 and
                                 perturbed_hsv[1] >= 90 and perturbed_hsv[2] >= 160)
                y, u, v = sample
                raw, reference = synthetic_patch_fixture([(40, 70, 20, 20, (y, u, y, v))])
                for eye, image in enumerate(owned.decode_pair(raw)):
                    start = (48, 688)[eye]
                    scalar_eye = reference[:400, start:start + 640]
                    # Synthetic BGR perturbation, not a second YUY2 decode:
                    # force the same one-LSB threshold crossing on every build
                    # and verify its effect through the real detector below.
                    perturbed_eye = scalar_eye.copy()
                    perturbed_eye[70:90, 40:60] = perturbed[0, 0]
                    self.assertEqual(pixel_metrics(perturbed_eye, scalar_eye),
                                     {"max_channel_lsb": 1, "different_channels": 400,
                                      "different_pixels": 400})
                    self.assert_pixels(image, scalar_eye, f"sensitive_{sample}_eye{eye}")
                    actual_hsv = self.cv2.cvtColor(image[70:71, 40:41], self.cv2.COLOR_BGR2HSV)[0, 0]
                    accepted = bool(85 <= actual_hsv[0] <= 135 and
                                    actual_hsv[1] >= 90 and actual_hsv[2] >= 160)
                    for white_cores in (False, True):
                        config = suite_fixture_config(white_cores)
                        scalar_candidates, scalar_mask = self._detect_recorded(scalar_eye, config)
                        candidates, mask = self._detect_recorded(image, config)
                        perturbed_candidates, perturbed_mask = self._detect_recorded(perturbed_eye, config)
                        expected = [{"center_px": [49.5, 79.5], "area_px": 400,
                                     "bbox_px": [40, 70, 20, 20]}]
                        self.assert_candidates(scalar_candidates, expected)
                        self.assert_candidates(candidates, expected if accepted else [])
                        self.assert_candidates(perturbed_candidates, [])
                        self.assertEqual(int(self.np.count_nonzero(perturbed_mask)), 0)
                        self.assertEqual(int(self.np.count_nonzero(scalar_mask)), 400)
                        self.assertEqual(int(self.np.count_nonzero(perturbed_mask != scalar_mask)), 400)
                        self.assertEqual(int(self.np.count_nonzero(mask)), 400 if accepted else 0)
                        differences = int(self.np.count_nonzero(mask != scalar_mask))
                        self.assertEqual(differences, 0 if accepted else 400)
                        print(json.dumps({"detector_fixture": "suite_one_lsb_sensitivity",
                                          "sample_yuv": sample, "eye": eye,
                                          "white_cores": white_cores, "scalar_hsv": hsv,
                                          "decoded_hsv": [int(v) for v in actual_hsv],
                                          "perturbed_mask_pixel_differences": 400,
                                          "mask_pixel_differences": differences}))

    def test_suite_inclusive_minimum_area_and_stable_centroids(self):
        # Shared U/V but unequal lumas: one bright pixel per pair. Its dark
        # partner remains below V160, giving exact areas 19, 20 and 21 pixels.
        patches = [(40 + i * 80, 80, 2, area, (100, 180, 16, 115))
                   for i, area in enumerate((19, 20, 21))]
        raw, reference = synthetic_patch_fixture(patches)
        expected = [{"center_px": [120.0, 89.5], "area_px": 20, "bbox_px": [120, 80, 1, 20]},
                    {"center_px": [200.0, 90.0], "area_px": 21, "bbox_px": [200, 80, 1, 21]}]
        for eye, image in enumerate(owned.decode_pair(raw)):
            start = (48, 688)[eye]
            scalar_eye = reference[:400, start:start + 640]
            self.assert_pixels(image, scalar_eye, f"suite_min_area_eye{eye}")
            for white_cores in (False, True):
                config = suite_fixture_config(white_cores)
                scalar_candidates, scalar_mask = self._detect_recorded(scalar_eye, config)
                candidates, mask = self._detect_recorded(image, config)
                self.assert_candidates(scalar_candidates, expected)
                self.assert_candidates(candidates, expected)
                self.np.testing.assert_array_equal(mask, scalar_mask)
                # The 19-pixel component exists in the mask but is filtered
                # from candidates, rather than silently disappearing in HSV.
                self.assertEqual(int(self.np.count_nonzero(mask)), 19 + 20 + 21)

    def test_suite_white_core_value_boundary_and_detached_white_control(self):
        patches = []
        for i, y_white in enumerate((187, 188, 189)):  # gray V199 / V200 / V201
            x = 40 + i * 80
            patches.extend(((x, 100, 20, 20, (41, 240, 41, 110)),
                            (x + 8, 108, 4, 4, (y_white, 128, y_white, 128))))
        patches.append((400, 200, 20, 20, (235, 128, 235, 128)))
        raw, reference = synthetic_patch_fixture(patches)
        for eye, image in enumerate(owned.decode_pair(raw)):
            start = (48, 688)[eye]
            scalar_eye = reference[:400, start:start + 640]
            self.assert_pixels(image, scalar_eye, f"suite_white_core_boundary_eye{eye}")
            decoded_core_values = []
            for i, value in enumerate((199, 200, 201)):
                x = 48 + i * 80
                scalar_hsv = self.cv2.cvtColor(scalar_eye[108:109, x:x + 1],
                                              self.cv2.COLOR_BGR2HSV)[0, 0]
                self.assertEqual(tuple(int(v) for v in scalar_hsv), (0, 0, value))
                decoded_hsv = self.cv2.cvtColor(image[108:109, x:x + 1],
                                               self.cv2.COLOR_BGR2HSV)[0, 0]
                self.assertEqual(int(decoded_hsv[1]), 0)
                decoded_core_values.append(int(decoded_hsv[2]))
            for white_cores in (False, True):
                config = suite_fixture_config(white_cores)
                scalar_candidates, scalar_mask = self._detect_recorded(scalar_eye, config)
                candidates, mask = self._detect_recorded(image, config)
                expected = [{"center_px": [49.5 + i * 80, 109.5],
                             "area_px": 400 if white_cores and i > 0 else 384,
                             "bbox_px": [40 + i * 80, 100, 20, 20]} for i in range(3)]
                self.assert_candidates(scalar_candidates, expected)
                decoded_expected = [dict(candidate, area_px=400 if white_cores and value >= 200 else 384)
                                    for candidate, value in zip(expected, decoded_core_values)]
                self.assert_candidates(candidates, decoded_expected)
                for observed, expected_components in ((scalar_mask, expected), (mask, decoded_expected)):
                    expected_mask = self.np.zeros((400, 640), dtype=self.np.uint8)
                    for candidate in expected_components:
                        x, y, width, height = candidate["bbox_px"]
                        expected_mask[y:y + height, x:x + width] = 255
                        if candidate["area_px"] == 384:
                            expected_mask[y + 8:y + 12, x + 8:x + 12] = 0
                    self.np.testing.assert_array_equal(observed, expected_mask)
                self.assertEqual(int(self.np.count_nonzero(mask[200:220, 400:420])), 0)
                print(json.dumps({"detector_fixture": "suite_white_core_boundary", "eye": eye,
                                  "white_cores": white_cores, "decoded_core_values": decoded_core_values,
                                  "mask_pixel_differences": int(self.np.count_nonzero(mask != scalar_mask))}))


if __name__ == "__main__":
    unittest.main()
