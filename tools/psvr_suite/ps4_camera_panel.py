"""Isolated PS4 stereo acquisition panel; never starts or alters tracking."""
from __future__ import annotations

from pathlib import Path
import json
import queue
import subprocess
import sys
import threading
from datetime import datetime, timezone

from .i18n import t


def summarize_output(stdout):
    """Keep capture failures and LED counts visible without dumping all blobs."""
    try:
        report = json.loads(stdout)
        probe = report.get("led_probe")
        if probe:
            capture = report["capture"]
            summary = {
                "state": probe["state"], "pairs_analyzed": probe["pairs_analyzed"],
                "stream_pair_fps": capture.get("ps4_stereo", {}).get("stream_pair_fps"),
                "errors": capture["errors"], "cleanup_pending": capture["cleanup_pending"],
                "signal": probe["signal"], "tracking_6dof": False,
                "camera_controls_modified": False,
                "profiles": {name: {
                    "pairs_with_candidates_both_eyes": profile["pairs_with_candidates_both_eyes"],
                    **{eye: {"counts": profile[eye]["candidate_count_histogram"],
                             "static_reference_motion": profile[eye]["static_reference_motion"]}
                       for eye in ("left", "right")}}
                    for name, profile in probe["profiles"].items()},
                "report_file": report.get("report_file"), "snapshot": report.get("snapshot"),
            }
            return json.dumps(summary, indent=2)
        stereo = report.get("stereo_probe")
        if isinstance(stereo, dict):
            capture = report.get("capture") if isinstance(report.get("capture"), dict) else {}
            raw_errors = report.get("errors") or []
            errors = list(raw_errors) if isinstance(raw_errors, list) else [raw_errors]
            capture_error = capture.get("error")
            if capture_error and capture_error not in errors:
                errors.append(capture_error)
            summary = {
                "state_counts": stereo.get("state_counts"),
                "pairs_analyzed": stereo.get("pairs_analyzed"),
                "point_observations": stereo.get("point_observations"),
                "point_count_histogram": stereo.get("point_count_histogram"),
                "stream_pair_fps": stereo.get("stream_pair_fps"),
                "point_reprojection_rms_px": stereo.get("point_reprojection_rms_px"),
                "matched_patch_depth_range_m": stereo.get("matched_patch_depth_range_m"),
                "errors": errors, "cleanup_pending": bool(capture.get("cleanup_pending")),
                "tracking_6dof": False, "camera_controls_modified": False,
                "report_file": report.get("report_file"), "snapshot": report.get("snapshot"),
            }
            # Whitelist gia' esclude i blob (last_result); tronca per garanzia bounded.
            return json.dumps(summary, indent=2)[:16000]
        return stdout[-16000:]
    except (ValueError, KeyError, TypeError, AttributeError):
        return stdout[-16000:]


def open_panel(parent, language="it"):
    import tkinter as tk
    from tkinter import ttk

    window = tk.Toplevel(parent)
    window.title("PS4 Camera · Stereo UVC")
    window.geometry("780x600")
    window.columnconfigure(0, weight=1)
    window.rowconfigure(3, weight=1)
    ttk.Label(window, text=t(language, "ps4.guide"), wraplength=740,
              justify="left").grid(row=0, column=0, sticky="ew", padx=12, pady=12)
    options = ttk.Frame(window)
    options.grid(row=1, column=0, sticky="ew", padx=12)
    values = []
    for column, (label, default, choices) in enumerate((
            ("UVC index", "0", None),
            ("Backend", "dshow", ("dshow", "msmf")),
            ("Mode / eye", "640x400", ("1280x800", "640x400", "320x192")),
            ("FPS", "60", ("60", "30", "15", "8", "120", "240")))):
        box = ttk.Frame(options)
        box.grid(row=0, column=column, padx=(0, 12))
        ttk.Label(box, text=label).pack(anchor="w")
        value = tk.StringVar(value=default)
        values.append(value)
        if choices:
            ttk.Combobox(box, textvariable=value, values=choices,
                         state="readonly", width=14).pack()
        else:
            ttk.Entry(box, textvariable=value, width=8).pack()
    buttons = ttk.Frame(window)
    buttons.grid(row=2, column=0, sticky="ew", padx=12, pady=12)
    output = tk.Text(window, wrap="word", height=20, state="disabled")
    output.grid(row=3, column=0, sticky="nsew", padx=12, pady=(0, 12))
    events = queue.Queue(maxsize=2)
    process = None
    closed = False
    active = False
    timer = None
    calibration_stop_file = None
    points_stop_file = None

    def show(message):
        output.configure(state="normal")
        output.delete("1.0", "end")
        output.insert("end", message)
        output.configure(state="disabled")

    def start(preview=False, led_check=False, calibration=False, points=False):
        nonlocal process, active, calibration_stop_file, points_stop_file
        if active:
            return
        # The suite is normally started with pythonw; use python.exe for the
        # captured JSON streams, while CREATE_NO_WINDOW keeps the console hidden.
        python = Path(sys.executable)
        if python.name.lower() == "pythonw.exe":
            python = python.with_name("python.exe")
        command = [str(python), "-m", "tools.psvr_camera.ps4_guided_calibration" if calibration
                   else "tools.psvr_camera.ps4_stereo_probe" if points
                   else "tools.psvr_camera.ps4_led_probe" if led_check
                   else "tools.psvr_camera.ps4_camera"]
        calibration_stop_file = None
        points_stop_file = None
        if preview or led_check or calibration or points:
            try:
                index = int(values[0].get())
                if index < 0:
                    raise ValueError()
            except ValueError:
                show(t(language, "ps4.index_error"))
                return
            command += ["--index", str(index), "--mode", values[2].get(), "--fps", values[3].get()]
            if calibration:
                if values[1].get() != "dshow":
                    show(t(language, "ps4.calibration_dshow"))
                    return
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                folder = Path(__file__).resolve().parents[2] / "logs" / "ps4_camera" / "calibration"
                folder.mkdir(parents=True, exist_ok=True)
                calibration_stop_file = folder / f"ps4_stereo_{stamp}.stop"
                command += ["--out", str(folder / f"ps4_stereo_{stamp}.json"),
                            "--stop-file", str(calibration_stop_file),
                            "--gain", "8", "--brightness", "8"]
            elif points:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                folder = Path(__file__).resolve().parents[2] / "logs" / "ps4_camera"
                folder.mkdir(parents=True, exist_ok=True)
                points_stop_file = folder / f"ps4_stereo_points_{stamp}.stop"
                command += ["--seconds", "30", "--brightness", "0",
                            "--output-dir", str(folder),
                            "--stop-file", str(points_stop_file)]
            else:
                command += ["--backend", values[1].get(), "--seconds", "10" if led_check else "60"]
            if led_check:
                command += ["--output-dir", str(Path(__file__).resolve().parents[2]
                                               / "logs" / "ps4_camera")]
            elif preview:
                command += ["--preview"]
        else:
            command += ["--inventory"]
        try:
            process = subprocess.Popen(command,
                cwd=str(Path(__file__).resolve().parents[2]),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            show(str(exc))
            return
        active = True
        show(t(language, "ps4.running"))
        child = process
        stop_marker = calibration_stop_file if calibration else points_stop_file

        def wait():
            try:
                timeout = 1320 if calibration else 60 if points else 75
                stdout, stderr = child.communicate(timeout=timeout)
                message = summarize_output(stdout) + ("\n" + stderr[-2000:] if stderr else "")
            except subprocess.TimeoutExpired:
                if calibration or points:
                    # Never terminate a process that still needs to restore
                    # temporary camera controls. Ask it to close safely.
                    stop_marker.write_text("stop\n", encoding="utf-8")
                    events.put((t(language, "ps4.restoring"), False))
                    stdout, stderr = child.communicate()
                    message = summarize_output(stdout) + ("\n" + stderr[-2000:] if stderr else "")
                else:
                    child.kill()
                    child.communicate()
                    message = "PS4 Camera: process timeout (75 s)"
            events.put((message, True))

        threading.Thread(target=wait, daemon=True, name="ps4-camera-ui").start()

    def stop():
        if process is not None and process.poll() is None:
            cooperative = calibration_stop_file if calibration_stop_file is not None else points_stop_file
            if cooperative is not None:
                cooperative.write_text("stop\n", encoding="utf-8")
                if not closed:
                    show(t(language, "ps4.restoring"))
            else:
                process.terminate()

    def poll():
        nonlocal timer, active
        if closed:
            return
        try:
            message, finished = events.get_nowait()
            show(message)
            active = not finished
        except queue.Empty:
            pass
        timer = window.after(100, poll)

    def destroy(event):
        nonlocal closed
        if event.widget is not window:
            return
        closed = True
        if timer is not None:
            window.after_cancel(timer)
        stop()

    for i, (key, callback) in enumerate((("ps4.inventory", start),
                           ("ps4.preview", lambda: start(True)),
                           ("ps4.led_check", lambda: start(led_check=True)),
                           ("ps4.points", lambda: start(points=True)),
                           ("ps4.calibrate", lambda: start(calibration=True)),
                           ("ps4.stop", stop))):
        ttk.Button(buttons, text=t(language, key), command=callback).grid(
            row=i // 3, column=i % 3, sticky="ew", padx=(0, 8), pady=3)
    window.bind("<Destroy>", destroy, add="+")
    show(t(language, "ps4.guide"))
    poll()
    return window
