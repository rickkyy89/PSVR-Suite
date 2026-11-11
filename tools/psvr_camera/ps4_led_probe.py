"""Finite stereo LED diagnostics, with no pose, calibration or camera controls."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import json
from pathlib import Path

import numpy as np

from .blue_leds import BlueLedConfig, BlueLedDetector
from .camera_backend import CameraError
from .ps4_camera import MODES, capture_stereo


PROFILES = {
    "suite_dark_blue": BlueLedConfig(value_min=60, saturation_min=90,
                                     include_white_cores=True),
    # Comparison only: suppress small fragments seen in the current static rig.
    # Do not promote to tracking defaults without distance/motion validation.
    "static_min_area10": BlueLedConfig(value_min=60, saturation_min=90,
                                       min_area=10, include_white_cores=True),
    "static_min_area20": BlueLedConfig(value_min=60, saturation_min=90,
                                       min_area=20, include_white_cores=True),
    "default_blue": BlueLedConfig(),
    "sensitive_blue": BlueLedConfig(value_min=30, saturation_min=60,
                                    min_area=3, include_white_cores=True),
}


class _EyeStats:
    def __init__(self):
        self.counts = Counter()
        self.last = []
        self.reference = None
        self.matched_frames = 0
        self.squared_motion = 0.0
        self.max_motion = 0.0

    def add(self, candidates):
        self.counts[len(candidates)] += 1
        self.last = candidates[:64]
        # Local temporal association only, never L/R correspondence or LED ID.
        # Reject merges/dropouts, nonunique nearest neighbours and movement
        # outside the gate instead of silently relabelling blobs by x/y order.
        if len(candidates) != 5:
            return
        centers = np.asarray([c["center_px"] for c in candidates], dtype=float)
        if self.reference is None:
            self.reference = centers.copy()
        distances = np.linalg.norm(self.reference[:, None] - centers[None, :], axis=2)
        nearest = distances.argmin(axis=1)
        selected = distances[np.arange(5), nearest]
        if len(set(nearest.tolist())) != 5 or np.any(selected > 4):
            return
        # Ties/near ties are ambiguous, even when the minimum happens to be unique.
        ordered = np.sort(distances, axis=1)
        if np.any(ordered[:, 1] - ordered[:, 0] < 1):
            return
        self.matched_frames += 1
        self.squared_motion += float(np.sum(selected ** 2))
        self.max_motion = max(self.max_motion, float(selected.max()))

    def to_dict(self):
        total = sum(self.counts.values())
        return {
            "frames": total,
            "candidate_count_histogram": dict(sorted(self.counts.items())),
            "frames_with_candidates": total - self.counts[0],
            "frames_with_five_candidates": self.counts[5],
            "last_candidates": self.last,
            "static_reference_motion": {
                "meaning": "unlabelled_temporal_patches_not_led_ids_or_pose",
                "gate_px": 4, "matched_frames": self.matched_frames,
                "rms_px": (float(np.sqrt(self.squared_motion / (5 * self.matched_frames)))
                           if self.matched_frames else None),
                "max_px": self.max_motion if self.matched_frames else None,
            },
        }


class StaticLedProbe:
    """Bounded-size aggregate statistics; no per-frame image/history storage."""
    def __init__(self, profiles=None):
        self.profiles = dict(PROFILES if profiles is None else profiles)
        self.detectors = {name: BlueLedDetector(config) for name, config in self.profiles.items()}
        self.stats = {name: {eye: _EyeStats() for eye in ("left", "right")}
                      for name in self.profiles}
        self.both = Counter()
        self.frames = 0
        self.signal = {eye: {"all_zero_frames": 0, "max_value": 0, "mean_sum": 0.0}
                       for eye in ("left", "right")}
        self.last_pair = None
        self.last_candidates = None

    def add_pair(self, left, right):
        for image in (left, right):
            if (not isinstance(image, np.ndarray) or image.dtype != np.uint8
                    or image.ndim != 3 or image.shape[2] != 3 or not image.size):
                raise ValueError("LED probe requires nonempty uint8 BGR images")
        if left.shape != right.shape:
            raise ValueError("stereo eye dimensions must match")
        self.frames += 1
        for eye, image in (("left", left), ("right", right)):
            maximum = int(image.max())
            signal = self.signal[eye]
            signal["all_zero_frames"] += int(maximum == 0)
            signal["max_value"] = max(signal["max_value"], maximum)
            signal["mean_sum"] += float(image.mean())
        results = {}
        for name, detector in self.detectors.items():
            l, r = detector.detect(left), detector.detect(right)
            self.stats[name]["left"].add(l)
            self.stats[name]["right"].add(r)
            self.both[name] += int(bool(l) and bool(r))
            results[name] = (l, r)
        # One last pair only, for an explicitly requested diagnostic snapshot.
        self.last_pair = (left, right)
        self.last_candidates = results
        return True

    def to_dict(self):
        all_zero = self.frames > 0 and all(
            value["all_zero_frames"] == self.frames for value in self.signal.values())
        visible = any(self.both.values())
        state = ("no_frames" if not self.frames else "all_zero_images" if all_zero
                 else "candidates_visible_both_eyes" if visible else "no_bilateral_candidates")
        return {
            "schema": "psvr.camera.ps4.led_probe", "version": 1,
            "state": state, "pairs_analyzed": self.frames,
            "camera_controls_modified": False, "calibrated": False,
            "tracking_6dof": False, "stereo_correspondences_assigned": False,
            "signal": {eye: {"all_zero_frames": stats["all_zero_frames"],
                       "max_value": stats["max_value"],
                       "mean_value": stats["mean_sum"] / self.frames if self.frames else None}
                       for eye, stats in self.signal.items()},
            "profiles": {name: {"config": asdict(self.profiles[name]),
                         "pairs_with_candidates_both_eyes": self.both[name],
                         **{eye: stats.to_dict() for eye, stats in eyes.items()}}
                         for name, eyes in self.stats.items()},
        }

    def save_snapshot(self, path):
        if self.last_pair is None:
            return False
        name = next(iter(self.detectors), None)
        if name is None:
            return False
        detector = self.detectors[name]
        images = [detector.annotate(im, cs) for im, cs in
                  zip(self.last_pair, self.last_candidates[name])]
        return bool(detector.cv2.imwrite(str(path), np.concatenate(images, axis=1)))


def run_probe(index, backend="dshow", mode="640x400", fps=60, seconds=10):
    probe = StaticLedProbe()
    capture = capture_stereo(index, backend, mode, fps, seconds, on_pair=probe.add_pair)
    return {"capture": capture, "led_probe": probe.to_dict()}, probe


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=int, required=True)
    parser.add_argument("--backend", choices=("dshow", "msmf"), default="dshow")
    parser.add_argument("--mode", choices=tuple(MODES), default="640x400")
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--output-dir", type=Path,
                        help="Optional explicit folder for JSON and one annotated JPEG")
    args = parser.parse_args(argv)
    if args.index < 0:
        parser.error("index must be non-negative")
    try:
        report, probe = run_probe(args.index, args.backend, args.mode, args.fps, args.seconds)
        if args.output_dir is not None:
            from datetime import datetime, timezone
            args.output_dir.mkdir(parents=True, exist_ok=True)
            stem = "ps4-led-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            snapshot = args.output_dir / (stem + ".jpg")
            report["snapshot"] = str(snapshot) if probe.save_snapshot(snapshot) else None
            report["report_file"] = str(args.output_dir / (stem + ".json"))
            Path(report["report_file"]).write_text(
                json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        if report["capture"]["errors"] or report["capture"]["cleanup_pending"]:
            return 2
        return 0 if report["led_probe"]["state"] == "candidates_visible_both_eyes" else 3
    except (CameraError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
