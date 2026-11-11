"""PS4 OV580 UVC acquisition, deliberately independent of pose/UDP tracking.

No firmware, installers or upstream code are bundled. The user prepares UVC
with OrbisEyeCam externally. Indices are explicit and never auto-selected.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
import subprocess
import sys
import time

from .camera_backend import CameraError
from .capture import capture_frames


@dataclass(frozen=True)
class StereoMode:
    width: int
    height: int
    eye_width: int
    eye_height: int
    rates: tuple


# OV580 transport includes 96 bytes (48 YUY2 pixels) before the images
# on EACH row, interleaved auxiliary data after them, and 8 trailing rows.
# Source: PS4EYECam README + driver/src/ps4eye.cpp getLastVideo*Pointer.
MODES = {
    "1280x800": StereoMode(3448, 808, 1280, 800, (60, 30, 15, 8)),
    "640x400": StereoMode(1748, 408, 640, 400, (120, 60, 30, 15, 8)),
    "320x192": StereoMode(898, 200, 320, 192, (240, 120, 60, 30)),
}


def split_stereo(frame, mode="640x400"):
    """Return independent BGR left/right arrays from ONE decoded UVC frame.

    Reject scaled, mono, raw YUY2 or unknown frames rather than guessing a
    half-frame split. L/R naming follows upstream; physical order is unverified.
    """
    spec = MODES[mode]
    if (getattr(frame, "shape", None) != (spec.height, spec.width, 3)
            or str(getattr(frame, "dtype", "")) != "uint8"):
        raise CameraError(
            f"PS4 UVC requires BGR uint8 {spec.width}x{spec.height}; "
            f"received {getattr(frame, 'shape', None)}. "
            "Check selected index, firmware and negotiated mode; no fallback.")
    x, w, h = 48, spec.eye_width, spec.eye_height
    return frame[:h, x:x+w].copy(), frame[:h, x+w:x+2*w].copy()


def classify_devices(devices):
    """Classify only known VID/PIDs, never the PSVR headset or PS3 Eye."""
    found = []
    for device in devices:
        identity = str(device.get("InstanceId", "")).upper()
        for pid, state in (("0580", "boot"), ("058A", "uvc"), ("058B", "uvc")):
            if f"VID_05A9&PID_{pid}" in identity:
                found.append({**device, "camera_state": state})
                break
    if any(d["camera_state"] == "uvc" and d.get("Status") == "OK" for d in found):
        state = "uvc_present"
    elif any(d["camera_state"] == "boot" for d in found):
        state = "firmware_required"
    else:
        state = "device_error" if found else "not_detected"
    return {"schema": "psvr.camera.ps4.inventory", "version": 1,
            "state": state, "devices": found,
            "uvc_index": None, "tracking_6dof": False}


def inventory():
    """Read-only Windows PnP query; no camera opening or driver modifications."""
    if sys.platform != "win32":
        raise CameraError("PS4 PnP inventory requires Windows")
    command = ("$ErrorActionPreference='Stop'; "
               "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
               "$d=@(Get-PnpDevice -PresentOnly | "
               "Where-Object { $_.InstanceId -match 'VID_05A9&PID_058[0AB]' } | "
               "Select-Object Status,Class,FriendlyName,InstanceId); "
               "ConvertTo-Json -InputObject $d -Compress")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise CameraError("Windows PnP inventory failed: " + result.stderr[:500])
    devices = json.loads(result.stdout.lstrip("\ufeff").strip() or "[]")
    return classify_devices(devices if isinstance(devices, list) else [devices])


def capture_stereo(index, backend="dshow", mode="640x400", fps=60,
                   seconds=5.0, preview=False, on_pair=None, module=None):
    """Bounded capture; each callback gets an atomic pair, never a metric pose.

    Host callback time is not a camera exposure timestamp. Preview owns a Tk
    window in the suite's separate process; OpenCV HighGUI is not required.
    """
    if mode not in MODES:
        raise CameraError("unknown PS4 mode")
    spec = MODES[mode]
    if fps not in spec.rates:
        raise CameraError(f"unsupported fps for {mode}: {spec.rates}")
    if backend not in ("msmf", "dshow"):
        raise CameraError("PS4 UVC backend must be msmf or dshow")
    if not math.isfinite(seconds) or not 0 < seconds <= 60:
        raise CameraError("capture duration must be in (0, 60] seconds")
    pairs = 0
    first_pair_at = last_pair_at = None
    deadline = None
    cv2 = None
    preview_window = preview_label = None
    preview_closed = False
    if preview:
        from .camera_backend import import_cv2
        cv2 = import_cv2(module)
        # The Suite environment may use opencv-python-headless. Tk is already
        # the Suite UI dependency; do not require OpenCV's optional HighGUI.
        import tkinter as tk
        preview_window = tk.Tk()
        preview_window.title("PS4 Camera | L / R | ESC")
        preview_label = tk.Label(preview_window, bg="black")
        preview_label.pack()

        def close_preview(*_):
            nonlocal preview_closed
            preview_closed = True

        preview_window.protocol("WM_DELETE_WINDOW", close_preview)
        preview_window.bind("<Escape>", close_preview)

    def accept(frame):
        nonlocal pairs, first_pair_at, last_pair_at
        left, right = split_stereo(frame, mode)
        last_pair_at = time.perf_counter()
        if first_pair_at is None:
            first_pair_at = last_pair_at
        pairs += 1
        if on_pair is not None and on_pair(left, right) is False:
            return False
        if cv2 is not None:
            import numpy as np
            image = np.concatenate((left, right), axis=1)
            scale = min(1.0, 1280 / image.shape[1])
            if scale < 1:
                image = cv2.resize(image, (1280, round(image.shape[0] * scale)))
            h, w = image.shape[:2]
            ppm = f"P6\n{w} {h}\n255\n".encode() + image[:, :, ::-1].tobytes()
            photo = tk.PhotoImage(data=ppm, format="PPM", master=preview_window)
            preview_label.configure(image=photo)
            preview_label.image = photo
            preview_window.update_idletasks()
            preview_window.update()
            if preview_closed:
                return False
        # Stop cooperatively on a delivered pair before scheduling a read
        # with less than a frame interval left. Otherwise a healthy stream
        # is reported as timed out at the intentional end of every probe.
        return time.perf_counter() < deadline - min(seconds * 0.2, 2 / fps + 0.05)

    try:
        deadline = time.perf_counter() + seconds
        report = capture_frames(index, backend=backend,
            frames=max(1, math.ceil(seconds * fps)), max_seconds=seconds,
            width=spec.width, height=spec.height, fps=fps,
            on_frame=accept, module=module, cleanup_timeout_s=0.5).to_dict()
    finally:
        if preview_window is not None:
            preview_window.destroy()
    report["ps4_stereo"] = {
        "mode": mode, "pairs_captured": pairs,
        "stream_pair_fps": (round((pairs - 1) / (last_pair_at - first_pair_at), 3)
                            if pairs > 1 and last_pair_at > first_pair_at else None),
        "timing_source": "host_callback_not_exposure",
        "eye_size": [spec.eye_width, spec.eye_height],
        "pair_source": "same_uvc_frame", "physical_eye_order_verified": False,
        "calibrated": False, "tracking_6dof": False,
    }
    # capture_frames counts failed reads but currently records deadline
    # exceptions only in errors. Include those in this diagnostic's count too.
    report["timeouts"] += sum(
        error.startswith(("camera open timed out;", "camera read timed out;"))
        for error in report["errors"])
    if not pairs and not report["errors"]:
        report["errors"].append("no PS4 stereo pair delivered")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", action="store_true")
    parser.add_argument("--index", type=int)
    parser.add_argument("--backend", choices=("dshow", "msmf"), default="dshow")
    parser.add_argument("--mode", choices=tuple(MODES), default="640x400")
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--seconds", type=float, default=5)
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args(argv)
    if not args.inventory and (args.index is None or args.index < 0):
        parser.error("capture requires an explicit non-negative --index")
    try:
        report = inventory() if args.inventory else capture_stereo(
            args.index, args.backend, args.mode, args.fps, args.seconds, args.preview)
        print(json.dumps(report, indent=2))
        if args.inventory:
            return 0 if report["state"] == "uvc_present" else 2
        return 2 if report["errors"] or report["cleanup_pending"] else 0
    except (CameraError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
