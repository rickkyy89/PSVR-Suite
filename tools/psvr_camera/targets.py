"""Printable calibration-target preparation (chessboard and ArUco).

Two independent artifacts, both driven by a *measured* size the operator
supplies, so nothing here invents a metric value:

* a **chessboard** as a deterministic, standard-library SVG whose physical
  ``width``/``height`` are in millimetres, so a 100% print is dimensionally
  correct and the printed square size can be verified with calipers;
* an **ArUco** marker as a PNG, generated with the optional OpenCV contrib
  module (``cv2.aruco``). Without OpenCV this command fails closed with a clear
  message; it never fabricates a marker.

The chessboard needs no OpenCV at all. No file is overwritten unless ``force``
is set, matching ``tools/psvr_patterns``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np

from .camera_backend import CameraError, OpenCvUnavailable, import_cv2


class TargetError(ValueError):
    pass


def _positive_number(value, name: str) -> float:
    if not np.isfinite(value) or value <= 0:
        raise TargetError(f"{name} must be a positive number")
    return float(value)


def chessboard_svg(
    inner_cols: int,
    inner_rows: int,
    square_mm: float,
    margin_mm: Optional[float] = None,
) -> str:
    """Return a deterministic, dimensionally-true chessboard SVG.

    ``inner_cols``/``inner_rows`` are the inner corner counts (squares minus
    one), matching ``calibrate --cols/--rows``. The board is drawn in millimetre
    user units so it prints at the measured square size.
    """
    if type(inner_cols) is not int or inner_cols < 1:
        raise TargetError("inner_cols must be a positive integer")
    if type(inner_rows) is not int or inner_rows < 1:
        raise TargetError("inner_rows must be a positive integer")
    square = _positive_number(square_mm, "square_mm")
    margin = square if margin_mm is None else _positive_number(margin_mm, "margin_mm")
    squares_x = inner_cols + 1
    squares_y = inner_rows + 1
    board_w = squares_x * square
    board_h = squares_y * square
    total_w = board_w + 2.0 * margin
    total_h = board_h + 2.0 * margin

    def fmt(value: float) -> str:
        text = f"{value:.4f}".rstrip("0").rstrip(".")
        return text or "0"

    rects = []
    for j in range(squares_y):
        for i in range(squares_x):
            if (i + j) % 2 == 0:
                x = margin + i * square
                y = margin + j * square
                rects.append(
                    f'  <rect x="{fmt(x)}" y="{fmt(y)}" width="{fmt(square)}" '
                    f'height="{fmt(square)}" fill="#000000"/>'
                )
    comment = (
        f"<!-- psvr_camera chessboard: {inner_cols}x{inner_rows} inner corners, "
        f"{fmt(square)} mm squares, {fmt(margin)} mm margin. Print at 100% and "
        "verify the square size with calipers; the size is a measurement, not a "
        "constant. -->"
    )
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        comment,
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{fmt(total_w)}mm" '
        f'height="{fmt(total_h)}mm" viewBox="0 0 {fmt(total_w)} {fmt(total_h)}">',
        f'  <rect x="0" y="0" width="{fmt(total_w)}" height="{fmt(total_h)}" '
        'fill="#FFFFFF"/>',
        *rects,
        "</svg>",
    ]
    return "\n".join(lines) + "\n"


def write_chessboard(
    path,
    inner_cols: int,
    inner_rows: int,
    square_mm: float,
    margin_mm: Optional[float] = None,
    force: bool = False,
) -> Path:
    target = Path(path)
    if target.exists() and not force:
        raise FileExistsError(f"refusing to overwrite {target} (pass --force)")
    text = chessboard_svg(inner_cols, inner_rows, square_mm, margin_mm=margin_mm)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def aruco_marker_png(
    dictionary: str,
    marker_id: int,
    pixels: int = 800,
    border_bits: int = 1,
) -> bytes:
    """Return PNG bytes for one ArUco marker (requires OpenCV contrib)."""
    if type(marker_id) is not int or marker_id < 0:
        raise TargetError("marker id must be a non-negative integer")
    if type(pixels) is not int or pixels < 16:
        raise TargetError("pixels must be an integer >= 16")
    try:
        cv2 = import_cv2()
    except OpenCvUnavailable as exc:
        raise CameraError(str(exc)) from exc
    aruco = getattr(cv2, "aruco", None)
    if aruco is None:
        raise CameraError(
            "ArUco generation needs opencv-contrib-python[-headless]; this "
            "OpenCV build has no aruco module"
        )
    dictionary_id = getattr(aruco, dictionary, None)
    if dictionary_id is None:
        raise CameraError(f"unknown ArUco dictionary {dictionary!r}")
    getter = getattr(aruco, "getPredefinedDictionary", None) or getattr(
        aruco, "Dictionary_get", None
    )
    if getter is None:
        raise CameraError("no ArUco dictionary accessor in this OpenCV build")
    dictionary_obj = getter(dictionary_id)
    image = None
    try:
        image = aruco.generateImageMarker(dictionary_obj, marker_id, pixels)
    except TypeError:
        image = None
    if image is None:
        image = np.zeros((pixels, pixels), dtype=np.uint8)
        aruco.generateImageMarker(dictionary_obj, marker_id, pixels, image, border_bits)
    ok, buffer = cv2.imencode(".png", image)
    if not ok:
        raise CameraError("could not encode the ArUco marker as PNG")
    return buffer.tobytes()


def write_aruco(
    path,
    dictionary: str,
    marker_id: int,
    pixels: int = 800,
    border_bits: int = 1,
    force: bool = False,
) -> Path:
    target = Path(path)
    if target.exists() and not force:
        raise FileExistsError(f"refusing to overwrite {target} (pass --force)")
    data = aruco_marker_png(
        dictionary, marker_id, pixels=pixels, border_bits=border_bits
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


__all__ = [
    "TargetError",
    "chessboard_svg",
    "write_chessboard",
    "aruco_marker_png",
    "write_aruco",
]
