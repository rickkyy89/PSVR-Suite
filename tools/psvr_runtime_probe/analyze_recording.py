# SPDX-License-Identifier: MIT
"""Detect candidate broad brightness pulses in a saved VR recording. Requires NumPy + ffmpeg."""
import argparse
import json
import subprocess
from pathlib import Path

import numpy as np


def brightness_change(previous, current):
    change = current.astype(np.int16) - previous.astype(np.int16)
    mean_delta = float(change.mean())
    bright_fraction = float((change >= 12).mean())
    return {
        "mean_luma_delta": mean_delta,
        "fraction_pixels_brighter_by_12": bright_fraction,
        "candidate_broad_flash": mean_delta >= 8 and bright_fraction >= 0.75,
    }


def bright_runs(means, fps):
    threshold = float(np.median(means)) + 8
    runs = []
    start = None
    for index in range(len(means) + 1):
        bright = index < len(means) and means[index] >= threshold
        if bright and start is None:
            start = index
        if not bright and start is not None:
            runs.append({"start_frame": start, "end_frame_exclusive": index,
                         "start_seconds": start / fps, "duration_seconds": (index - start) / fps,
                         "peak_mean_luma": max(means[start:index])})
            start = None
    return runs


def analyze(video, ffmpeg):
    video = Path(video).resolve(strict=True)
    ffmpeg = Path(ffmpeg).resolve(strict=True)
    ffprobe = ffmpeg.with_name("ffprobe.exe")
    details = json.loads(subprocess.check_output([
        str(ffprobe), "-v", "error", "-show_entries",
        "format=duration,size:stream=codec_name,width,height,avg_frame_rate,nb_frames",
        "-of", "json", str(video)], text=True))
    width, height, fps = 160, 90, 30
    process = subprocess.Popen([
        str(ffmpeg), "-v", "error", "-i", str(video), "-an", "-vf",
        f"fps={fps},scale={width}:{height},format=gray", "-f", "rawvideo", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    previous = None
    candidates, changes = [], []
    means = []
    try:
        while True:
            raw = process.stdout.read(width * height)
            if not raw:
                break
            if len(raw) != width * height:
                raise ValueError("incomplete decoded frame")
            frame = np.frombuffer(raw, dtype=np.uint8)
            index = len(means)
            means.append(float(frame.mean()))
            if previous is not None:
                change = brightness_change(previous, frame)
                change.update(frame=index, seconds=index / fps)
                changes.append(change)
                if change["candidate_broad_flash"]:
                    candidates.append(change)
            previous = frame
        error = process.stderr.read().decode("utf-8", "replace")
        if process.wait() != 0:
            raise RuntimeError(f"ffmpeg decode failed: {error}")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    if not means:
        raise ValueError("video has no decoded frames")
    return {
        "schema_version": 1, "video": str(video), "input_media": details,
        "analysis_fps": fps, "analysis_size": [width, height], "frames_analyzed": len(means),
        "luma_mean_min": min(means), "luma_mean_max": max(means),
        "candidate_broad_flash_count": len(candidates), "candidate_broad_flashes": candidates,
        "bright_intervals_above_median_plus_8": bright_runs(means, fps),
        "largest_positive_mean_changes": sorted(changes, key=lambda x: x["mean_luma_delta"], reverse=True)[:6],
        "flash_cause_verified": False, "physical_panel_verified": False,
        "limitations": "Heuristic on downsampled 30fps software-window video. Can miss very brief, "
                       "localized or subtle flashes; does not identify their cause or HDMI/OLED behavior. "
                       "A candidate requires mean luma rise >=8/255 and >=75% of pixels rising >=12/255.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--ffmpeg", type=Path, default=Path(r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"))
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.out and (args.out.exists() or not args.out.parent.is_dir()):
        parser.error("--out requires a new file in an existing directory")
    try:
        report = analyze(args.video, args.ffmpeg)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(2, f"Recording analysis failed: {error}\n")
    text = json.dumps(report, indent=2)
    if args.out:
        with args.out.open("x", encoding="utf-8") as output:
            output.write(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
