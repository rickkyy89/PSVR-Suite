"""Command line interface for the M9 webcam-side tools.

Subcommands are independent and always require explicit selections:

* ``inventory``  list every probed camera index (never auto-selects).
* ``probe``      bounded capture of one explicit index/backend, JSON report.
* ``calibrate``  chessboard intrinsics offline (images/detections) or live.
* ``track``      live ArUco marker tracking, versioned JSON UDP output.
* ``pseye``      native PS3 Eye capture through libusb-win32 (no OpenCV).
* ``pattern``    generate a printable chessboard (SVG) or ArUco marker (PNG).
* ``protocol``   print the machine-readable position protocol.
* ``verify``     load and validate an intrinsics file.

``calibrate live`` and ``track`` accept ``--backend pseye`` to read the real
PS3 Eye through its existing libusb-win32 binding. The loaded intrinsics'
``image_size`` is checked against the live frame before any metric estimate, and
a mismatch fails closed.

Exit codes: 0 success, 1 bad arguments/inputs, 2 camera/OpenCV unavailable or
camera failure, 3 calibration/geometry error, 4 UDP transport error.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
import time
from pathlib import Path

from . import __version__
from .protocol import (
    DEFAULT_UDP_HOST,
    DEFAULT_UDP_PORT,
    protocol_document,
)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_CAMERA = 2
EXIT_CALIBRATION = 3
EXIT_TRANSPORT = 4


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return number


def _positive_float(value: str) -> float:
    number = float(value)
    if not (number > 0.0) or number != number or number in (float("inf"),):
        raise argparse.ArgumentTypeError("must be a positive number")
    return number


def _non_negative_float(value: str) -> float:
    number = float(value)
    if number < 0.0 or number != number:
        raise argparse.ArgumentTypeError("must be a non-negative number")
    return number


def _finite_float(value: str) -> float:
    import math
    number = float(value)
    if not math.isfinite(number):
        raise argparse.ArgumentTypeError("must be finite")
    return number


def _udp_port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be in 1..65535")
    return port


def _parse_endpoint(value: str):
    text = str(value).strip()
    if ":" not in text:
        raise argparse.ArgumentTypeError("endpoint must be host:port")
    host, _, port = text.rpartition(":")
    if not host:
        host = DEFAULT_UDP_HOST
    try:
        port = int(port)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be in 1..65535")
    return host, port


def _emit(obj, as_json: bool = True) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


def _expand_images(value: str) -> list:
    path = Path(value)
    if path.is_dir():
        files = []
        for pattern in ("*.png", "*.jpg", "*.jpeg", "*.bmp", "*.tif", "*.tiff"):
            files.extend(sorted(path.glob(pattern)))
        return files
    if any(ch in value for ch in "*?["):
        return sorted(Path(p) for p in glob.glob(value))
    return [path]


# --------------------------------------------------------------------------- #
# inventory / probe
# --------------------------------------------------------------------------- #

def cmd_inventory(args) -> int:
    from . import camera_backend

    if not camera_backend.cv2_available():
        _emit({
            "schema": "psvr.camera.inventory",
            "version": 1,
            "opencv": False,
            "error": "OpenCV (cv2) is not available; camera inventory disabled",
            "cameras": [],
        })
        return EXIT_CAMERA
    cameras = camera_backend.enumerate_cameras(
        max_index=args.max_index, backend=args.backend
    )
    _emit({
        "schema": "psvr.camera.inventory",
        "version": 1,
        "opencv": True,
        "backend": args.backend,
        "backends": camera_backend.available_backends(),
        "cameras": [c.to_dict() for c in cameras],
        "note": "selection is explicit: pass --index to probe or track",
    })
    return EXIT_OK


def cmd_probe(args) -> int:
    from . import camera_backend
    from .camera_source import is_pseye_backend
    from .capture import capture_frames

    if is_pseye_backend(args.backend):
        _emit({
            "schema": "psvr.camera.capture",
            "version": 1,
            "backend": "pseye",
            "opened": False,
            "errors": [
                "the native PS3 Eye backend uses 'pseye probe', not "
                "'probe --backend pseye'"
            ],
        })
        return EXIT_CAMERA
    if not camera_backend.cv2_available():
        _emit({
            "schema": "psvr.camera.capture",
            "version": 1,
            "opened": False,
            "errors": ["OpenCV (cv2) is not available; probe disabled"],
        })
        return EXIT_CAMERA
    report = capture_frames(
        index=args.index,
        backend=args.backend,
        frames=args.frames,
        max_seconds=args.max_seconds,
        warmup_frames=args.warmup,
        width=args.width,
        height=args.height,
        fps=args.fps,
    ).to_dict()
    _emit(report)
    return EXIT_OK if report["opened"] and report["frames_captured"] > 0 else EXIT_CAMERA


# --------------------------------------------------------------------------- #
# calibrate
# --------------------------------------------------------------------------- #

def _chessboard(args):
    from .calibration import ChessboardSpec

    return ChessboardSpec(
        inner_cols=args.cols,
        inner_rows=args.rows,
        square_size_m=args.square_mm / 1000.0,
    )


def cmd_calibrate_offline(args) -> int:
    from .calibration import (
        CalibrationError,
        calibrate_from_images,
        save_intrinsics,
        solve_intrinsics,
    )

    spec = _chessboard(args)
    try:
        if args.detections:
            payload = json.loads(Path(args.detections).read_text(encoding="utf-8"))
            image_size = payload["image_size"]
            views = payload["views"]
            intrinsics = solve_intrinsics(
                views, image_size, spec, prefer_opencv=not args.no_opencv,
                max_rms_px=args.max_rms_px,
            )
            report = {"source": "detections", "views": len(views)}
        else:
            if not args.images:
                raise CalibrationError("offline calibration needs --images or --detections")
            files = _expand_images(args.images)
            if not files:
                raise CalibrationError(f"no images matched {args.images!r}")
            intrinsics = calibrate_from_images(
                files, spec, prefer_opencv=not args.no_opencv,
                max_rms_px=args.max_rms_px,
            )
            report = {"source": "images", "images": len(files), "views": intrinsics.views}
    except CalibrationError as exc:
        _emit({"schema": "psvr.camera.calibrate", "version": 1, "ok": False, "error": str(exc)})
        return EXIT_CALIBRATION
    except ValueError as exc:
        # A degenerate correspondence set raises GeometryError (a ValueError);
        # report it as a calibration error rather than a traceback.
        _emit({"schema": "psvr.camera.calibrate", "version": 1, "ok": False, "error": str(exc)})
        return EXIT_CALIBRATION
    except KeyError as exc:
        _emit({
            "schema": "psvr.camera.calibrate",
            "version": 1,
            "ok": False,
            "error": f"detections file missing key {exc}",
        })
        return EXIT_CALIBRATION
    except OSError as exc:
        _emit({"schema": "psvr.camera.calibrate", "version": 1, "ok": False, "error": str(exc)})
        return EXIT_CALIBRATION
    if args.out:
        save_intrinsics(args.out, intrinsics)
    summary = intrinsics.to_dict()
    summary.update({"schema": "psvr.camera.calibrate", "ok": True, "report": report,
                    "output": args.out})
    _emit(summary)
    return EXIT_OK


def cmd_calibrate_live(args) -> int:
    from .calibration import detect_and_solve_live, save_intrinsics

    spec = _chessboard(args)
    if getattr(args, "guided", False):
        from .guided_calibration import run_guided
        return run_guided(args, spec)
    try:
        intrinsics, report = detect_and_solve_live(
            index=args.index,
            backend=args.backend,
            spec=spec,
            target_views=args.views,
            max_seconds=args.max_seconds,
            min_gap_s=args.min_gap,
            prefer_opencv=not args.no_opencv,
            width=args.width,
            height=args.height,
            fps=args.fps,
            dll_path=args.dll,
            max_read_failures=args.max_read_failures,
            max_rms_px=args.max_rms_px,
        )
    except ValueError as exc:
        _emit({"schema": "psvr.camera.calibrate", "version": 1, "ok": False, "error": str(exc)})
        return EXIT_CALIBRATION
    except Exception as exc:  # noqa: BLE001 - report camera/OpenCV failures as JSON
        _emit({
            "schema": "psvr.camera.calibrate",
            "version": 1,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        })
        return EXIT_CAMERA
    if args.out:
        save_intrinsics(args.out, intrinsics)
    summary = intrinsics.to_dict()
    summary.update({"schema": "psvr.camera.calibrate", "ok": True, "report": report,
                    "output": args.out})
    _emit(summary)
    return EXIT_OK


# --------------------------------------------------------------------------- #
# track
# --------------------------------------------------------------------------- #

def cmd_track(args) -> int:
    from .calibration import CalibrationError, load_intrinsics
    from .estimator import (
        EstimatorConfig,
        EstimatorError,
        PositionEstimator,
        PositionFilter,
        load_marker_geometry,
    )

    try:
        intrinsics = load_intrinsics(args.intrinsics)
        geometry = load_marker_geometry(args.geometry)
    except (CalibrationError, EstimatorError, OSError) as exc:
        _emit({"schema": "psvr.camera.track", "version": 1, "ok": False, "error": str(exc)})
        return EXIT_CALIBRATION
    if geometry.kind != "aruco":
        _emit({
            "schema": "psvr.camera.track",
            "version": 1,
            "ok": False,
            "error": (
                "live tracking implements the ArUco detector only; a blob/LED rig "
                "must supply correspondences through the estimator API"
            ),
        })
        return EXIT_USAGE
    try:
        config = EstimatorConfig(
            max_reprojection_px=args.max_reprojection_px,
            require_calibrated=not args.allow_uncalibrated,
            max_calibration_rms_px=args.max_calibration_rms_px,
        )
        estimator = PositionEstimator(intrinsics, geometry, config)
    except EstimatorError as exc:
        _emit({"schema": "psvr.camera.track", "version": 1, "ok": False, "error": str(exc)})
        return EXIT_CALIBRATION

    from . import camera_backend
    from .camera_backend import CameraError
    from .camera_source import open_camera_source
    from .markers import ArucoDetector
    from .tracker import TrackConfig, run_track
    from .udp_sender import SequenceAllocator, UdpPositionSender

    if not camera_backend.cv2_available():
        _emit({"schema": "psvr.camera.track", "version": 1, "ok": False,
               "error": "OpenCV (cv2) is not available; tracking disabled"})
        return EXIT_CAMERA
    dictionary = (geometry.aruco or {}).get("dictionary", "DICT_4X4_50")
    marker_id = int((geometry.aruco or {}).get("id", 0))
    try:
        detector = ArucoDetector(dictionary, [marker_id])
    except Exception as exc:  # noqa: BLE001
        _emit({"schema": "psvr.camera.track", "version": 1, "ok": False,
               "error": f"{type(exc).__name__}: {exc}"})
        return EXIT_CAMERA

    host, port = args.udp
    sender = None
    if not args.no_send:
        try:
            sender = UdpPositionSender(host, port)
        except OSError as exc:
            _emit({"schema": "psvr.camera.track", "version": 1, "ok": False,
                   "error": f"cannot open UDP socket: {exc}"})
            return EXIT_TRANSPORT
    sequences = SequenceAllocator()
    position_filter = PositionFilter(
        smoothing_time_s=args.smoothing_s, deadzone_m=args.deadzone_m,
        stale_timeout_s=args.stale_timeout_s,
    )

    expected_size = (int(intrinsics.image_size[0]), int(intrinsics.image_size[1]))
    cap = None
    last_print = 0.0
    printed = 0

    def on_sample(sample) -> None:
        nonlocal last_print, printed
        if args.json_lines:
            print(json.dumps(sample.to_packet(), sort_keys=True))
            return
        printed += 1
        now = time.monotonic()
        if not sample.valid or now - last_print >= 1.0:
            print(
                f"state={sample.state} valid={sample.valid} "
                f"pos={sample.position_m} reproj={sample.reprojection_px} "
                f"sent={printed}",
                file=sys.stderr,
            )
            last_print = now

    try:
        cap, descriptor = open_camera_source(
            args.index,
            backend=args.backend,
            width=args.width,
            height=args.height,
            fps=args.fps,
            dll_path=args.dll,
            read_timeout_s=args.read_timeout_s,
            max_consecutive_failures=args.max_read_failures,
        )
        # Required live preflight: the loaded intrinsics describe a specific
        # image size; a stream at another size would make every metre wrong.
        if descriptor.width and descriptor.height:
            live = (int(descriptor.width), int(descriptor.height))
            if live != expected_size:
                _emit({
                    "schema": "psvr.camera.track",
                    "version": 1,
                    "ok": False,
                    "error": (
                        f"live camera mode {live[0]}x{live[1]} does not match the "
                        f"calibrated intrinsics image_size "
                        f"{expected_size[0]}x{expected_size[1]}; refusing to emit "
                        "a wrong-metric pose"
                    ),
                })
                return EXIT_CALIBRATION
        if not args.json_lines:
            print(
                f"tracking index={args.index} backend={args.backend or 'any'} "
                f"mode={descriptor.width}x{descriptor.height}@{descriptor.fps} "
                f"intrinsics={expected_size[0]}x{expected_size[1]}",
                file=sys.stderr,
            )
        track_config = TrackConfig(
            frames=args.frames,
            max_seconds=args.max_seconds,
            marker_id=marker_id,
            max_read_failures=args.max_read_failures,
            expected_size=expected_size,
            source_name=geometry.source,
        )
        summary = run_track(
            cap, detector, estimator, sender, sequences, position_filter,
            track_config, on_sample=on_sample,
        )
    except CameraError as exc:
        _emit({"schema": "psvr.camera.track", "version": 1, "ok": False, "error": str(exc)})
        return EXIT_CAMERA
    finally:
        if cap is not None:
            cap.release()
        if sender is not None:
            sender.close()

    payload = {
        "schema": "psvr.camera.track",
        "version": 1,
        "endpoint": f"{host}:{port}",
        "no_send": args.no_send,
        "expected_image_size": list(expected_size),
    }
    payload.update(summary.to_dict())
    if summary.exit_reason == "transport_error":
        payload["ok"] = False
        _emit(payload)
        return EXIT_TRANSPORT
    if summary.exit_reason == "resolution_mismatch":
        payload["ok"] = False
        _emit(payload)
        return EXIT_CALIBRATION
    if summary.exit_reason == "read_failure":
        payload["ok"] = False
        _emit(payload)
        return EXIT_CAMERA
    if summary.exit_reason == "interrupted":
        return 130
    payload["ok"] = True
    _emit(payload)
    return EXIT_OK


# --------------------------------------------------------------------------- #
# pattern (printable calibration targets)
# --------------------------------------------------------------------------- #

def cmd_pattern_chessboard(args) -> int:
    from .targets import TargetError, write_chessboard

    try:
        path = write_chessboard(
            args.out,
            inner_cols=args.cols,
            inner_rows=args.rows,
            square_mm=args.square_mm,
            margin_mm=args.margin_mm,
            force=args.force,
        )
    except (TargetError, FileExistsError, OSError) as exc:
        _emit({"schema": "psvr.camera.pattern", "version": 1, "ok": False,
               "error": str(exc)})
        return EXIT_USAGE
    _emit({
        "schema": "psvr.camera.pattern",
        "version": 1,
        "ok": True,
        "kind": "chessboard",
        "out": str(path),
        "inner_cols": args.cols,
        "inner_rows": args.rows,
        "square_mm": args.square_mm,
        "note": (
            "print at 100% and verify the square size with calipers; pass the "
            "measured value to calibrate --square-mm"
        ),
    })
    return EXIT_OK


def cmd_pattern_aruco(args) -> int:
    from .targets import TargetError, write_aruco

    try:
        path = write_aruco(
            args.out,
            dictionary=args.dictionary,
            marker_id=args.id,
            pixels=args.pixels,
            border_bits=args.border_bits,
            force=args.force,
        )
    except (TargetError, FileExistsError, OSError) as exc:
        _emit({"schema": "psvr.camera.pattern", "version": 1, "ok": False,
               "error": str(exc)})
        return EXIT_USAGE
    except Exception as exc:  # noqa: BLE001 - OpenCV/cv2 failure -> camera exit
        _emit({"schema": "psvr.camera.pattern", "version": 1, "ok": False,
               "error": f"{type(exc).__name__}: {exc}"})
        return EXIT_CAMERA
    payload = {
        "schema": "psvr.camera.pattern",
        "version": 1,
        "ok": True,
        "kind": "aruco",
        "out": str(path),
        "dictionary": args.dictionary,
        "id": args.id,
        "marker_mm": args.marker_mm,
        "note": (
            "measure the printed black square side with calipers; that measured "
            "value (not marker_mm as a guess) is the marker_length_m for tracking"
        ),
    }
    if args.geometry_out:
        from .estimator import aruco_geometry

        geometry = aruco_geometry(
            args.marker_mm / 1000.0,
            dictionary=args.dictionary,
            marker_id=args.id,
            description=(
                f"ArUco {args.dictionary} id {args.id}; size is the value the "
                "operator passed, verify the print before metric use"
            ),
        )
        Path(args.geometry_out).write_text(
            json.dumps(geometry.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        payload["geometry_out"] = args.geometry_out
    _emit(payload)
    return EXIT_OK


# --------------------------------------------------------------------------- #
# protocol / verify
# --------------------------------------------------------------------------- #

def cmd_protocol(args) -> int:
    _emit(protocol_document())
    return EXIT_OK


def cmd_verify(args) -> int:
    from .calibration import CalibrationError, load_intrinsics

    try:
        expected = None
        if args.image_size:
            width, _, height = args.image_size.partition("x")
            expected = (int(width), int(height))
        intrinsics = load_intrinsics(args.intrinsics, strict=not args.lenient,
                                     expected_image_size=expected)
    except (CalibrationError, ValueError) as exc:
        _emit({"schema": "psvr.camera.intrinsics", "version": 1, "ok": False,
               "error": str(exc)})
        return EXIT_CALIBRATION
    summary = intrinsics.to_dict()
    summary.update({"ok": True})
    _emit(summary)
    return EXIT_OK


# --------------------------------------------------------------------------- #
# pseye (native PS3 Eye, libusb-win32)
# --------------------------------------------------------------------------- #

def cmd_pseye_inventory(args) -> int:
    from . import pseye_backend

    report = pseye_backend.inventory(dll_path=args.dll)
    _emit(report)
    return EXIT_OK if report.get("available") else EXIT_CAMERA


def cmd_pseye_probe(args) -> int:
    from . import pseye_backend

    try:
        report = pseye_backend.capture_frames_pseye(
            index=args.index,
            width=args.width,
            height=args.height,
            fps=args.fps,
            fmt=args.format,
            frames=args.frames,
            max_seconds=args.max_seconds,
            warmup_frames=args.warmup,
            dll_path=args.dll,
        ).to_dict()
    except Exception as exc:  # noqa: BLE001 - report, never traceback
        _emit({
            "schema": "psvr.camera.capture",
            "version": 1,
            "backend": "pseye",
            "opened": False,
            "errors": [f"{type(exc).__name__}: {exc}"],
        })
        return EXIT_CAMERA
    _emit(report)
    return EXIT_OK if report["opened"] and report["frames_captured"] > 0 else EXIT_CAMERA


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #

def cmd_led_probe(args) -> int:
    from .blue_leds import BlueLedConfig, BlueLedDetector
    from .camera_source import open_camera_source

    detector = BlueLedDetector(BlueLedConfig(
        hue_min=args.hue_min, hue_max=args.hue_max,
        saturation_min=args.saturation_min, value_min=args.value_min,
        min_area=args.min_area, max_area=args.max_area,
    ))
    cap = None
    captured = failures = 0
    counts = {}
    best_frame = None
    best_candidates = []
    started = time.monotonic()
    try:
        cap, descriptor = open_camera_source(
            args.index, backend=args.backend, width=args.width,
            height=args.height, fps=args.fps, dll_path=args.dll,
            read_timeout_s=1.0, max_consecutive_failures=10,
        )
        while captured < args.frames and time.monotonic() - started < args.max_seconds:
            ok, frame = cap.read()
            if not ok or frame is None:
                failures += 1
                if failures >= 10:
                    break
                continue
            captured += 1
            candidates = detector.detect(frame)
            count = len(candidates)
            counts[count] = counts.get(count, 0) + 1
            if best_frame is None or count > len(best_candidates):
                best_frame, best_candidates = frame.copy(), candidates
        if args.snapshot and best_frame is not None:
            path = Path(args.snapshot)
            if path.exists():
                raise ValueError(f"snapshot already exists: {path}")
            if not detector.cv2.imwrite(str(path), detector.annotate(best_frame, best_candidates)):
                raise OSError(f"cannot save snapshot: {path}")
    finally:
        if cap is not None:
            cap.release()
    _emit({
        "schema": "psvr.camera.blue_led_probe", "version": 1,
        "captured_frames": captured, "read_failures": failures,
        "duration_s": round(time.monotonic() - started, 3),
        "candidate_count_histogram": counts, "best_candidates": best_candidates,
        "snapshot": args.snapshot if best_frame is not None else None,
        "metric_pose": False,
        "note": "Unlabelled blue patches; LED identity and measured geometry are required for position.",
    })
    return EXIT_OK if captured and failures < 10 else EXIT_CAMERA


def cmd_led_track(args) -> int:
    if args.send_position and not args.driver_imu:
        raise ValueError("--send-position requires --driver-imu")
    from .led_tracking import run_led_track
    return run_led_track(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="psvr_camera",
        description="M9 webcam-side metric position tools (autonomous, no USB access).",
    )
    parser.add_argument("--version", action="version", version=f"psvr_camera {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    inventory = sub.add_parser("inventory", help="list every probed camera index")
    inventory.add_argument("--backend", default=None)
    inventory.add_argument("--max-index", type=int, default=8)
    inventory.set_defaults(func=cmd_inventory)

    probe = sub.add_parser("probe", help="bounded capture of one explicit camera")
    probe.add_argument("--index", type=int, required=True)
    probe.add_argument("--backend", default=None)
    probe.add_argument("--frames", type=_positive_int, default=120)
    probe.add_argument("--max-seconds", type=_positive_float, default=3.0)
    probe.add_argument("--warmup", type=_non_negative_int, default=2)
    probe.add_argument("--width", type=_positive_int, default=None)
    probe.add_argument("--height", type=_positive_int, default=None)
    probe.add_argument("--fps", type=_positive_float, default=None)
    probe.set_defaults(func=cmd_probe)

    leds = sub.add_parser("led-probe", help="inspect blue LED candidates; no metric pose or UDP")
    leds.add_argument("--index", type=_non_negative_int, required=True)
    leds.add_argument("--backend", default="pseye")
    leds.add_argument("--width", type=_positive_int, default=640)
    leds.add_argument("--height", type=_positive_int, default=480)
    leds.add_argument("--fps", type=_positive_int, default=75)
    leds.add_argument("--frames", type=_positive_int, default=150)
    leds.add_argument("--max-seconds", type=_positive_float, default=5.0)
    leds.add_argument("--dll", default=None)
    leds.add_argument("--snapshot", default=None, help="save best annotated frame to a new PNG")
    leds.add_argument("--hue-min", type=int, default=85)
    leds.add_argument("--hue-max", type=int, default=135)
    leds.add_argument("--saturation-min", type=int, default=80)
    leds.add_argument("--value-min", type=int, default=70)
    leds.add_argument("--min-area", type=_positive_int, default=6)
    leds.add_argument("--max-area", type=_positive_int, default=6000)
    leds.set_defaults(func=cmd_led_probe)

    led_track = sub.add_parser("led-track", help="experimental LED pose; driver UDP is opt-in")
    led_track.add_argument("--intrinsics", required=True)
    led_track.add_argument("--rig", required=True)
    led_track.add_argument("--index", type=_non_negative_int, required=True)
    led_track.add_argument("--backend", default="pseye")
    led_track.add_argument("--width", type=_positive_int, default=640)
    led_track.add_argument("--height", type=_positive_int, default=480)
    led_track.add_argument("--fps", type=_positive_int, default=75)
    led_track.add_argument("--min-area", type=_positive_int, default=6)
    led_track.add_argument("--value-min", type=int, default=60,
                           help="minimum brightness; higher rejects dim blue objects")
    led_track.add_argument("--saturation-min", type=int, default=90)
    led_track.add_argument("--opening-size", type=int, choices=(0,3,5), default=0,
                           help="remove small isolated/thin blue noise")
    led_track.add_argument("--max-reprojection-px", type=_positive_float, default=2.5)
    led_track.add_argument("--frames", type=_non_negative_int, default=0)
    led_track.add_argument("--max-seconds", type=_non_negative_float, default=0)
    led_track.add_argument("--dll", default=None)
    led_track.add_argument("--log", default=None)
    led_track.add_argument("--preview", action="store_true")
    led_track.add_argument("--exposure", type=int, default=13, help="PS Eye manual exposure 0..255")
    led_track.add_argument("--gain", type=int, default=0, help="PS Eye manual gain 0..63")
    imu_mode = led_track.add_mutually_exclusive_group()
    imu_mode.add_argument("--imu", action="store_true", help="rotation-guided partial nine-LED trial; SteamVR must be stopped")
    imu_mode.add_argument("--driver-imu", action="store_true", help="consume absolute driver IMU via loopback; never claim HID")
    led_track.add_argument("--driver-imu-port", type=_udp_port, default=4272)
    led_track.add_argument("--send-position", action="store_true", help="opt-in bound position v2; requires --driver-imu")
    led_track.add_argument("--position-port", type=_udp_port, default=4271)
    led_track.add_argument("--position-scale", type=_positive_float, default=1.0)
    led_track.add_argument("--position-filter", choices=("one_euro","kalman"), default=None,
                           help="exclusive final-position filter (default one_euro; Kalman experimental)")
    led_track.add_argument("--position-config", default=None,
                           help="JSON filter/kalman/one_euro settings; CLI filter overrides selection")
    led_track.add_argument("--position-filter-min-cutoff-hz", type=_positive_float, default=8.0,
                           help="final head-position rest cutoff; lower values suppress more stationary noise")
    led_track.add_argument("--position-filter-beta", type=_non_negative_float, default=15.0,
                           help="increase smoothing cutoff with measured head speed")
    led_track.add_argument("--head-offset-model-m", type=_finite_float, nargs=3, default=(0.,0.,0.), metavar=("X","Y","Z"))
    led_track.add_argument("--imu-rig", default="configurations/psvr_nine_leds_refined_sides_20261011.json")
    led_track.add_argument("--imu-diag", default="build/vs2022/RelWithDebInfo/psvr_diag.exe")
    led_track.add_argument("--imu-config", default="out/diag/config.json")
    led_track.set_defaults(func=cmd_led_track)

    calibrate = sub.add_parser("calibrate", help="chessboard intrinsics")
    cal_sub = calibrate.add_subparsers(dest="mode", required=True)

    def add_chessboard_args(target):
        target.add_argument("--cols", type=_positive_int, required=True,
                            help="inner corner columns (squares - 1)")
        target.add_argument("--rows", type=_positive_int, required=True,
                            help="inner corner rows (squares - 1)")
        target.add_argument("--square-mm", type=_positive_float, required=True,
                            help="measured square size in millimetres")

    offline = cal_sub.add_parser("offline", help="calibrate from images or detections")
    add_chessboard_args(offline)
    offline.add_argument("--images", default=None, help="image file, glob or directory")
    offline.add_argument("--detections", default=None,
                         help='JSON {"image_size":[w,h],"views":[[[x,y],...],...]}')
    offline.add_argument("--out", default=None)
    offline.add_argument("--max-rms-px", type=_positive_float, default=1.0,
                         help="fail closed above this reprojection RMS")
    offline.add_argument("--no-opencv", action="store_true",
                         help="force the deterministic Zhang estimate")
    offline.set_defaults(func=cmd_calibrate_offline)

    live = cal_sub.add_parser("live", help="calibrate from a live camera")
    add_chessboard_args(live)
    live.add_argument("--index", type=_non_negative_int, required=True)
    live.add_argument("--backend", default=None,
                      help="capture backend; use 'pseye' for the native PS3 Eye")
    live.add_argument("--views", type=_positive_int, default=15)
    live.add_argument("--max-seconds", type=_positive_float, default=60.0)
    live.add_argument("--min-gap", type=_non_negative_float, default=1.0)
    live.add_argument("--width", type=_positive_int, default=None)
    live.add_argument("--height", type=_positive_int, default=None)
    live.add_argument("--fps", type=_positive_float, default=None)
    live.add_argument("--dll", default=None, help="explicit psvr_pseye.dll")
    live.add_argument("--max-read-failures", type=_positive_int, default=60)
    live.add_argument("--max-rms-px", type=_positive_float, default=1.0,
                      help="fail closed above this reprojection RMS")
    live.add_argument("--out", default=None)
    live.add_argument("--no-opencv", action="store_true")
    live.set_defaults(func=cmd_calibrate_live)
    live.add_argument("--guided", action="store_true",
                      help="Tk camera preview, movement countdown and stable-frame capture")

    track = sub.add_parser("track", help="live ArUco tracking, JSON UDP output")
    track.add_argument("--intrinsics", required=True)
    track.add_argument("--geometry", required=True)
    track.add_argument("--index", type=_non_negative_int, required=True)
    track.add_argument("--backend", default=None,
                       help="capture backend; use 'pseye' for the native PS3 Eye")
    track.add_argument("--udp", type=_parse_endpoint,
                       default=(DEFAULT_UDP_HOST, DEFAULT_UDP_PORT))
    track.add_argument("--max-seconds", type=_non_negative_float, default=0.0,
                       help="0 means run until interrupted")
    track.add_argument("--frames", type=_non_negative_int, default=0,
                       help="stop after this many captured frames (0 = unlimited)")
    track.add_argument("--width", type=_positive_int, default=None)
    track.add_argument("--height", type=_positive_int, default=None)
    track.add_argument("--fps", type=_positive_float, default=None)
    track.add_argument("--dll", default=None, help="explicit psvr_pseye.dll")
    track.add_argument("--read-timeout-s", type=_positive_float, default=2.0,
                       help="bounded wait per native read")
    track.add_argument("--max-read-failures", type=_positive_int, default=30,
                       help="abort after this many consecutive read failures")
    track.add_argument("--max-reprojection-px", type=_positive_float, default=2.0)
    track.add_argument("--max-calibration-rms-px", type=_positive_float, default=1.0,
                       help="fail closed if the intrinsics RMS is worse than this")
    track.add_argument("--smoothing-s", type=_non_negative_float, default=0.05)
    track.add_argument("--deadzone-m", type=_non_negative_float, default=0.0)
    track.add_argument("--stale-timeout-s", type=_positive_float, default=0.5)
    track.add_argument("--allow-uncalibrated", action="store_true",
                       help="diagnostics only; metric claims are disabled")
    track.add_argument("--no-send", action="store_true")
    track.add_argument("--json-lines", action="store_true")
    track.set_defaults(func=cmd_track)

    protocol = sub.add_parser("protocol", help="print the position protocol")
    protocol.set_defaults(func=cmd_protocol)

    verify = sub.add_parser("verify", help="load and validate intrinsics")
    verify.add_argument("--intrinsics", required=True)
    verify.add_argument("--image-size", default=None, help="expected WxH")
    verify.add_argument("--lenient", action="store_true")
    verify.set_defaults(func=cmd_verify)

    pattern = sub.add_parser(
        "pattern", help="generate a printable chessboard or ArUco marker"
    )
    pattern_sub = pattern.add_subparsers(dest="target", required=True)

    p_chess = pattern_sub.add_parser(
        "chessboard", help="printable chessboard SVG at a measured square size"
    )
    p_chess.add_argument("--cols", type=_positive_int, required=True,
                         help="inner corner columns (squares - 1)")
    p_chess.add_argument("--rows", type=_positive_int, required=True,
                         help="inner corner rows (squares - 1)")
    p_chess.add_argument("--square-mm", type=_positive_float, required=True,
                         help="measured square size in millimetres")
    p_chess.add_argument("--margin-mm", type=_positive_float, default=None)
    p_chess.add_argument("--out", required=True)
    p_chess.add_argument("--force", action="store_true")
    p_chess.set_defaults(func=cmd_pattern_chessboard)

    p_aruco = pattern_sub.add_parser(
        "aruco", help="printable ArUco marker PNG (OpenCV contrib required)"
    )
    p_aruco.add_argument("--dictionary", default="DICT_4X4_50")
    p_aruco.add_argument("--id", type=_non_negative_int, default=0)
    p_aruco.add_argument("--marker-mm", type=_positive_float, required=True,
                         help="measured marker square size in millimetres")
    p_aruco.add_argument("--pixels", type=_positive_int, default=800)
    p_aruco.add_argument("--border-bits", type=_non_negative_int, default=1)
    p_aruco.add_argument("--out", required=True)
    p_aruco.add_argument("--geometry-out", default=None,
                         help="optional ArUco geometry JSON for --geometry")
    p_aruco.add_argument("--force", action="store_true")
    p_aruco.set_defaults(func=cmd_pattern_aruco)

    pseye = sub.add_parser(
        "pseye",
        help="native PS3 Eye capture via libusb-win32 (no OpenCV, no rebinding)",
    )
    pseye_sub = pseye.add_subparsers(dest="mode", required=True)

    pseye_inv = pseye_sub.add_parser("inventory", help="read-only DLL/device inventory")
    pseye_inv.add_argument("--dll", default=None, help="explicit psvr_pseye.dll")
    pseye_inv.set_defaults(func=cmd_pseye_inventory)

    pseye_probe = pseye_sub.add_parser(
        "probe", help="bounded native capture of one explicit index"
    )
    pseye_probe.add_argument("--index", type=_non_negative_int, required=True)
    pseye_probe.add_argument("--width", type=_positive_int, default=640)
    pseye_probe.add_argument("--height", type=_positive_int, default=480)
    pseye_probe.add_argument("--fps", type=_positive_int, default=60)
    pseye_probe.add_argument("--format", default="bgr",
                             choices=("bayer", "bgr", "rgb", "gray"))
    pseye_probe.add_argument("--frames", type=_positive_int, default=120)
    pseye_probe.add_argument("--max-seconds", type=_positive_float, default=3.0)
    pseye_probe.add_argument("--warmup", type=_non_negative_int, default=2)
    pseye_probe.add_argument("--dll", default=None, help="explicit psvr_pseye.dll")
    pseye_probe.set_defaults(func=cmd_pseye_probe)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BrokenPipeError:  # pragma: no cover
        return EXIT_OK
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    except Exception as exc:  # noqa: BLE001 - never leak a traceback as exit code
        print(json.dumps({
            "schema": "psvr.camera.error",
            "version": 1,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }, indent=2, sort_keys=True))
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
