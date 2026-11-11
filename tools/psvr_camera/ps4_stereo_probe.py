"""Diagnostic stereo LED points only: no anatomical IDs, head pose or UDP output."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import queue
import threading
import time

import numpy as np

from .blue_leds import BlueLedConfig, BlueLedDetector
from .camera_backend import CameraError
from .ps4_camera import MODES
from .stereo_calibration import load_stereo


class StereoPointProbe:
    """Fixed-size aggregate statistics and one most recent pair; never a pose."""
    def __init__(self, calibration, detector_config=None, matching_config=None):
        from .stereo_leds import StereoLedConfig
        self.calibration = calibration
        self.detector = BlueLedDetector(detector_config or BlueLedConfig(
            value_min=160, saturation_min=90, min_area=20, include_white_cores=True))
        self.matching_config = matching_config or StereoLedConfig()
        self.frames = 0
        self.states, self.point_counts = Counter(), Counter()
        self.candidate_counts = {eye: Counter() for eye in ("left", "right")}
        self.last_pair = self.last_candidates = self.last_result = None
        self.first_time = self.last_time = None
        self.depth_min = self.depth_max = None
        self.reprojection_squared = 0.0
        self.point_observations = 0

    def add_pair(self, left, right, stamp=None):
        from .stereo_leds import triangulate_candidates
        expected = (self.calibration.image_size[1], self.calibration.image_size[0], 3)
        for image in (left, right):
            if (not isinstance(image, np.ndarray) or image.dtype != np.uint8
                    or image.shape != expected):
                raise CameraError(f"stereo calibration requires BGR uint8 {expected}")
        stamp = time.monotonic() if stamp is None else float(stamp)
        # Windows/Python monotonic clocks can quantize distinct frame reads to
        # the same timestamp. Sequence freshness is owned by StereoSession;
        # equal host observations are valid, backwards time is not.
        if not math.isfinite(stamp) or stamp < 0 or (self.last_time is not None and stamp < self.last_time):
            raise CameraError("stereo pair timestamp must be finite and nondecreasing")
        candidates = (self.detector.detect(left), self.detector.detect(right))
        result = triangulate_candidates(self.calibration, *candidates, config=self.matching_config)
        self.frames += 1
        self.first_time = stamp if self.first_time is None else self.first_time
        self.last_time = stamp
        self.states[result["state"]] += 1
        self.point_counts[len(result["matches"])] += 1
        for eye, patches in zip(("left", "right"), candidates):
            self.candidate_counts[eye][len(patches)] += 1
        for match in result["matches"]:
            depth = float(match["point_left_camera_m"][2])
            self.depth_min = depth if self.depth_min is None else min(self.depth_min, depth)
            self.depth_max = depth if self.depth_max is None else max(self.depth_max, depth)
            self.reprojection_squared += float(match["reprojection_px"]) ** 2
            self.point_observations += 1
        self.last_pair, self.last_candidates, self.last_result = (left, right), candidates, result
        return result

    def to_dict(self):
        return {
            "schema": "psvr.camera.ps4.stereo_probe", "version": 1,
            "pairs_analyzed": self.frames, "state_counts": dict(self.states),
            "point_count_histogram": dict(sorted(self.point_counts.items())),
            "candidate_count_histograms": {eye: dict(sorted(hist.items()))
                                           for eye, hist in self.candidate_counts.items()},
            "stream_pair_fps": ((self.frames - 1) / (self.last_time - self.first_time)
                                if self.frames > 1 and self.last_time > self.first_time else None),
            "point_observations": self.point_observations,
            "point_reprojection_rms_px": (math.sqrt(self.reprojection_squared / self.point_observations)
                                           if self.point_observations else None),
            "matched_patch_depth_range_m": ([self.depth_min, self.depth_max]
                                            if self.point_observations else None),
            "last_result": self.last_result,
            "detector": asdict(self.detector.config),
            "matching": asdict(self.matching_config),
            "coordinate_frame": "left_camera_stream_not_head_or_world",
            "timing_source": "host_read_completion_not_exposure",
            "identities_assigned": False, "head_pose_estimated": False,
            "position_published": False, "tracking_6dof": False,
            "camera_controls_modified_by_analysis": False,
        }

    def render(self, candidate_background=False):
        """Visual mask is optional and never changes detection or camera controls."""
        if self.last_pair is None:
            return None
        cv2 = self.detector.cv2
        images = []
        for image, patches in zip(self.last_pair, self.last_candidates):
            shown = image
            if not candidate_background:
                shown = np.zeros_like(image)
                for patch in patches:
                    x, y, w, h = patch["bbox_px"]
                    shown[y:y+h, x:x+w] = image[y:y+h, x:x+w]
            images.append(self.detector.annotate(shown, patches))
        combined = np.concatenate(images, axis=1)
        offset = self.calibration.image_size[0]
        palette = ((255, 255, 0), (0, 255, 255), (255, 0, 255),
                   (80, 255, 80), (255, 160, 40), (180, 180, 255))
        for n, match in enumerate(self.last_result["matches"]):
            lp = self.last_candidates[0][match["left_index"]]["center_px"]
            rp = self.last_candidates[1][match["right_index"]]["center_px"]
            a = tuple(int(round(v)) for v in lp)
            b = (int(round(rp[0] + offset)), int(round(rp[1])))
            color = palette[n % len(palette)]
            cv2.line(combined, a, b, color, 1)
            cv2.putText(combined, f"{match['point_left_camera_m'][2]:.2f} m",
                        (a[0] + 4, max(14, a[1] - 7)), cv2.FONT_HERSHEY_SIMPLEX,
                        .4, color, 1)
        if combined.shape[1] > 1280:
            combined = cv2.resize(combined,
                (1280, round(combined.shape[0] * 1280 / combined.shape[1])))
        return combined


def run_probe(index, calibration_path, mode="640x400", fps=60, seconds=10,
              preview=False, detector_config=None, stop_file=None, module=None,
              candidate_background=False, brightness=0):
    from .ps4_stereo_session import StereoSession
    if type(index) is not int or index < 0 or mode not in MODES:
        raise CameraError("explicit non-negative index and known PS4 mode required")
    if not math.isfinite(seconds) or not 0 < seconds <= 60:
        raise CameraError("stereo probe duration must be in (0, 60] seconds")
    spec = MODES[mode]
    calibration = load_stereo(calibration_path,
        expected_image_size=(spec.eye_width, spec.eye_height), expected_mode=mode)
    if not calibration.validation or calibration.validation.get("accepted") is not True:
        raise CameraError("stereo probe requires an accepted held-out calibration")
    probe = StereoPointProbe(calibration, detector_config)
    session = StereoSession(index, mode=mode, fps=fps, gain=None, brightness=brightness,
                            exposure=None, module=module)
    stop, done = threading.Event(), threading.Event()
    images = queue.Queue(maxsize=1)
    report = {"errors": [], "calibration_file": str(calibration_path),
              "calibration_sha256": hashlib.sha256(Path(calibration_path).read_bytes()).hexdigest(),
              "camera_controls_requested": {"exposure": None, "gain": None, "brightness": brightness}}
    root = None
    closing = False

    def marker_stopped():
        return stop_file is not None and Path(stop_file).exists()

    def worker():
        started = time.monotonic()
        try:
            session.start()
            while not stop.is_set() and not marker_stopped() and time.monotonic() - started < seconds:
                sample = session.next_pair(timeout_s=.5)
                if sample is None:
                    error = session.report().get("error")
                    if error:
                        raise CameraError(error)
                    continue
                result = probe.add_pair(sample["left"], sample["right"], sample["host_monotonic"])
                if preview:
                    image = probe.render(candidate_background)
                    h, w = image.shape[:2]
                    ppm = f"P6\n{w} {h}\n255\n".encode() + image[:, :, ::-1].tobytes()
                    detail = (f"L: {len(probe.last_candidates[0])} candidati | "
                              f"R: {len(probe.last_candidates[1])} | "
                              f"Punti 3D: {len(result['matches'])} | Stato: {result['state']}")
                    try:
                        images.get_nowait()
                    except queue.Empty:
                        pass
                    images.put_nowait((ppm, detail))
        except Exception as exc:
            report["errors"].append(f"{type(exc).__name__}: {exc}")
        finally:
            session.stop(timeout_s=2)
            capture = session.report()
            while capture.get("cleanup_pending"):
                time.sleep(.1)
                capture = session.report()
            if capture.get("error"):
                report["errors"].append(capture["error"])
            if capture.get("controls", {}).get("errors"):
                report["errors"].extend(capture["controls"]["errors"])
            report.update(capture=capture, stereo_probe=probe.to_dict(),
                          duration_s=time.monotonic() - started)
            done.set()

    if preview:
        import tkinter as tk
        root = tk.Tk()
        root.title("PS4 Camera - LED 3D diagnostica")
        root.geometry("1300x570")
        tk.Label(root, text="Triangolazione diagnostica: punti LED, NON posa della testa e NON tracking SteamVR.",
                 font=("Segoe UI", 13, "bold")).pack(pady=6)
        status = tk.StringVar(value="Apertura camera... mostra i LED accesi alla camera")
        tk.Label(root, textvariable=status, font=("Segoe UI", 11)).pack()
        label = tk.Label(root, bg="black")
        label.pack(pady=6)
        tk.Label(root, text="Le linee indicano coppie locali, non identita dei LED. "
                 "Le distanze sono profondita dei singoli punti nel primo occhio, non della testa.").pack()

        def close(*_):
            nonlocal closing
            closing = True
            stop.set()
            status.set("Chiusura e rilascio camera...")

        root.protocol("WM_DELETE_WINDOW", close)
        root.bind("<Escape>", close)
        tk.Button(root, text="CHIUDI", command=close).pack(pady=5)
        thread = threading.Thread(target=worker, daemon=True, name="ps4-stereo-points")
        thread.start()

        def tick():
            if marker_stopped() and not closing:
                close()
            try:
                ppm, detail = images.get_nowait()
                photo = tk.PhotoImage(data=ppm, format="PPM", master=root)
                label.configure(image=photo)
                label.image = photo
                if not closing:
                    status.set(detail)
            except queue.Empty:
                pass
            if done.is_set():
                root.destroy()
                return
            root.after(40, tick)

        root.after(40, tick)
        root.mainloop()
        thread.join()
    else:
        worker()
    return report, probe


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=int, required=True)
    parser.add_argument("--calibration", type=Path,
                        default=Path("configurations/ps4_stereo_640x400_calibrated.json"))
    parser.add_argument("--mode", choices=tuple(MODES), default="640x400")
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--value-min", type=int, default=160)
    parser.add_argument("--min-area", type=int, default=20)
    parser.add_argument("--brightness", type=float, default=0,
                        help="Temporary dark LED brightness; previous value restored on close")
    parser.add_argument("--keep-brightness", action="store_true",
                        help="Leave the camera brightness control untouched")
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--original-background", action="store_true",
                        help="Show the original image instead of the display-only candidate mask")
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        config = BlueLedConfig(value_min=args.value_min, saturation_min=90,
                               min_area=args.min_area, include_white_cores=True)
        report, probe = run_probe(args.index, args.calibration, args.mode, args.fps,
            args.seconds, args.preview, config, args.stop_file,
            candidate_background=args.original_background,
            brightness=None if args.keep_brightness else args.brightness)
        if args.output_dir is not None:
            args.output_dir.mkdir(parents=True, exist_ok=True)
            stem = "ps4-stereo-points-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            image = probe.render(args.original_background)
            if image is not None:
                image_path = args.output_dir / (stem + ".jpg")
                if not probe.detector.cv2.imwrite(str(image_path), image):
                    raise OSError("could not save diagnostic image")
                report["snapshot"] = str(image_path)
            output = args.output_dir / (stem + ".json")
            report["report_file"] = str(output)
            with output.open("x", encoding="utf-8") as stream:
                json.dump(report, stream, indent=2, allow_nan=False)
        print(json.dumps(report, indent=2, allow_nan=False))
        if report["errors"]:
            return 2
        return 0 if probe.point_observations else 3
    except (ValueError, OSError, TypeError) as exc:
        print(json.dumps({"errors": [str(exc)]}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
