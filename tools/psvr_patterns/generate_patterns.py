# SPDX-License-Identifier: MIT
"""Offline stereo diagnostic pattern generator (M5 display / M6 optics).

Generates 1920x1080 side-by-side SVG patterns with equal 960x1080 eye
halves (left eye x 0..959, right eye x 960..1919). Deterministic: the same
command produces byte-identical files on every run. Standard library only;
no external references, scripts or network access.

The tool never touches displays, never automates fullscreen or compositor
presentation and never launches a monitor app. An SVG image alone is not
proof of compositor output or runtime frame timing; it is uncalibrated
diagnostic content for later manual checks (see docs/MILESTONE_5_REPORT.md
and docs/MILESTONE_6_SOFTWARE.md).
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
from xml.sax.saxutils import escape

CANVAS_WIDTH = 1920
CANVAS_HEIGHT = 1080
EYE_WIDTH = 960
EYE_HEIGHT = 1080
CENTER_X = EYE_WIDTH // 2  # Eye-local center: 480
CENTER_Y = EYE_HEIGHT // 2  # Eye-local center: 540

DIAGNOSTIC_BANNER = "DIAGNOSTIC - UNCALIBRATED"
FRAME_DIMENSIONS = "FULL 1920 x 1080 PX - EACH EYE 960 x 1080 PX"
GENERATOR_TAG = "psvr_patterns v1"

BLACK = "#000000"
WHITE = "#FFFFFF"
RED = "#FF0000"
GREEN = "#00FF00"
BLUE = "#0000FF"
MINOR_GRAY = "#505050"

GRID_STEP = 60
GRID_MAJOR = 300
GRID_CIRCLE_RADII = (100, 200, 300, 400)

RGB_STRIPE_WIDTH = 96
RGB_BAND_HEIGHT = 900
RGB_CYCLE = (RED, GREEN, BLUE)

GEOMETRY_RADII = tuple(range(60, 481, 60))  # 60..480 px, circular in pixel units.

GENERATED_FILE_NAMES = (
    "stereo_grid.svg",
    "stereo_rgb.svg",
    "stereo_geometry.svg",
)


def _eyes():
    """Per-eye authored content specs, in eye-local 960x1080 coordinates."""
    return (
        {
            "side": "left",
            "offset": 0,
            "label": "LEFT EYE - L",
            "offcenter": (CENTER_X - 300, CENTER_Y - 270),
        },
        {
            "side": "right",
            "offset": EYE_WIDTH,
            "label": "RIGHT EYE - R",
            "offcenter": (CENTER_X + 300, CENTER_Y - 270),
        },
    )


def _attrs(pairs):
    return " ".join(f'{key}="{value}"' for key, value in pairs)


def _rect(x, y, w, h, fill=None, stroke=None, stroke_width=None):
    pairs = [("x", x), ("y", y), ("width", w), ("height", h)]
    if fill is not None:
        pairs.append(("fill", fill))
    if stroke is not None:
        pairs.extend([("stroke", stroke), ("stroke-width", stroke_width)])
    return f"<rect {_attrs(pairs)}/>"


def _line(x1, y1, x2, y2, stroke, stroke_width):
    return (
        f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
        f'stroke="{stroke}" stroke-width="{stroke_width}"/>'
    )


def _circle(cx, cy, r, stroke, stroke_width, fill="none"):
    return (
        f'<circle cx="{cx}" cy="{cy}" r="{r}" stroke="{stroke}" '
        f'stroke-width="{stroke_width}" fill="{fill}"/>'
    )


def _text(x, y, size, anchor, content, fill=BLACK):
    return (
        f'<text x="{x}" y="{y}" font-family="monospace" font-size="{size}" '
        f'text-anchor="{anchor}" fill="{fill}">{escape(str(content))}</text>'
    )


def _group(gid, children):
    return [f'<g id="{gid}">', *children, "</g>"]


def _build_grid_eye(eye):
    side = eye["side"]
    grid = []
    for pos in range(GRID_STEP, EYE_WIDTH, GRID_STEP):
        major = pos % GRID_MAJOR == 0
        grid.append(_line(pos, 0, pos, EYE_HEIGHT, WHITE if major else MINOR_GRAY,
                          2 if major else 1))
    for pos in range(GRID_STEP, EYE_HEIGHT, GRID_STEP):
        major = pos % GRID_MAJOR == 0
        grid.append(_line(0, pos, EYE_WIDTH, pos, WHITE if major else MINOR_GRAY,
                          2 if major else 1))
    ruler = []
    for pos in range(0, EYE_WIDTH + 1, GRID_STEP):
        major = pos % GRID_MAJOR == 0 or pos == EYE_WIDTH
        ruler.append(_line(pos, 0, pos, 36 if major else 12, WHITE, 2))
    for pos in range(0, EYE_HEIGHT + 1, GRID_STEP):
        major = pos % GRID_MAJOR == 0 or pos == EYE_HEIGHT
        ruler.append(_line(0, pos, 36 if major else 12, pos, WHITE, 2))
    for pos in (GRID_MAJOR, 2 * GRID_MAJOR, 3 * GRID_MAJOR):
        ruler.append(_text(pos, 60, 22, "middle", str(pos), WHITE))
        ruler.append(_text(44, pos + 10, 22, "start", str(pos), WHITE))
    ruler.append(_text(0, 60, 22, "middle", "0", WHITE))
    ruler.append(_text(EYE_WIDTH, 60, 22, "middle", str(EYE_WIDTH), WHITE))
    ruler.append(_text(44, EYE_HEIGHT - 10, 22, "start", str(EYE_HEIGHT), WHITE))
    return [
        _rect(0, 0, EYE_WIDTH, EYE_HEIGHT, fill=BLACK),
        *_group(f"grid-{side}", grid),
        *_group(f"circles-{side}", [
            _circle(CENTER_X, CENTER_Y, r, GREEN, 2) for r in GRID_CIRCLE_RADII
        ]),
        *_group(f"center-cross-{side}", [
            _line(CENTER_X - 60, CENTER_Y, CENTER_X + 60, CENTER_Y, RED, 3),
            _line(CENTER_X, CENTER_Y - 60, CENTER_X, CENTER_Y + 60, RED, 3),
            _circle(CENTER_X, CENTER_Y, 10, RED, 2, fill=RED),
        ]),
        *_group(f"ruler-{side}", ruler),
        *_group(f"border-{side}", [
            _rect(2, 2, EYE_WIDTH - 4, EYE_HEIGHT - 4, fill="none",
                  stroke=WHITE, stroke_width=4)
        ]),
        *_group(f"labels-{side}", [
            _text(CENTER_X, 88, 32, "middle", "TOP", WHITE),
            _text(CENTER_X, 140, 44, "middle", eye["label"], WHITE),
            _text(CENTER_X, 952, 24, "middle", "EYE 960 x 1080 PX", WHITE),
            _text(CENTER_X, 996, 32, "middle", "BOTTOM", WHITE),
        ]),
    ]


def _build_rgb_eye(eye):
    side = eye["side"]
    stripes = [
        _rect(i * RGB_STRIPE_WIDTH, 0, RGB_STRIPE_WIDTH, RGB_BAND_HEIGHT,
              fill=RGB_CYCLE[i % len(RGB_CYCLE)])
        for i in range(EYE_WIDTH // RGB_STRIPE_WIDTH)
    ]
    alignment = [_line(0, y, EYE_WIDTH, y, WHITE, 4) for y in (150, 300, 450, 600, 750)]
    alignment.append(_line(CENTER_X, 0, CENTER_X, RGB_BAND_HEIGHT, WHITE, 2))
    for tx, ty in ((0, 0), (CENTER_X - 8, 0), (EYE_WIDTH - 16, 0),
                   (0, RGB_BAND_HEIGHT - 8), (CENTER_X - 8, RGB_BAND_HEIGHT - 8),
                   (EYE_WIDTH - 16, RGB_BAND_HEIGHT - 8)):
        alignment.append(_rect(tx, ty, 16, 8, fill=BLACK))
    return [
        _rect(0, 0, EYE_WIDTH, EYE_HEIGHT, fill=WHITE),
        *_group(f"rgb-stripes-{side}", stripes),
        *_group(f"alignment-{side}", alignment),
        *_group(f"labels-{side}", [
            _rect(0, RGB_BAND_HEIGHT, EYE_WIDTH, EYE_HEIGHT - RGB_BAND_HEIGHT,
                  fill=BLACK),
            _text(CENTER_X, 952, 44, "middle", eye["label"], WHITE),
            _text(CENTER_X, 1000, 26, "middle",
                  "TOP = UPPER EDGE / BOTTOM = LOWER EDGE", WHITE),
            _text(CENTER_X, 1046, 26, "middle",
                  "STRIPE 96 PX - EYE 960 x 1080 PX - ORDER R G B L-TO-R", WHITE),
        ]),
        *_group(f"border-{side}", [
            _rect(1, 1, EYE_WIDTH - 2, EYE_HEIGHT - 2, fill="none",
                  stroke=WHITE, stroke_width=2)
        ]),
    ]


def _build_geometry_eye(eye):
    side = eye["side"]
    dx, dy = eye["offcenter"]
    corners = [_rect(x, y, 16, 16, fill=BLACK) for x, y in
               ((1, 1), (EYE_WIDTH - 17, 1), (1, EYE_HEIGHT - 17),
                (EYE_WIDTH - 17, EYE_HEIGHT - 17))]
    edge_ticks = [
        _rect(CENTER_X - 8, 1, 16, 12, fill=BLACK),
        _rect(CENTER_X - 8, EYE_HEIGHT - 13, 16, 12, fill=BLACK),
        _rect(1, CENTER_Y - 8, 12, 16, fill=BLACK),
        _rect(EYE_WIDTH - 13, CENTER_Y - 8, 12, 16, fill=BLACK),
    ]
    return [
        _rect(0, 0, EYE_WIDTH, EYE_HEIGHT, fill=WHITE),
        *_group(f"concentric-circles-{side}", [
            _circle(CENTER_X, CENTER_Y, r, BLACK, 3) for r in GEOMETRY_RADII
        ]),
        *_group(f"center-cross-{side}", [
            _line(CENTER_X - 60, CENTER_Y, CENTER_X + 60, CENTER_Y, RED, 3),
            _line(CENTER_X, CENTER_Y - 60, CENTER_X, CENTER_Y + 60, RED, 3),
            _circle(CENTER_X, CENTER_Y, 8, RED, 2, fill=RED),
        ]),
        *_group(f"offcenter-{side}", [
            _circle(dx, dy, 30, BLUE, 4),
            _line(dx - 46, dy, dx + 46, dy, BLUE, 3),
            _line(dx, dy - 46, dx, dy + 46, BLUE, 3),
            _text(dx, dy + 66, 22, "middle", f"OFFCENTER X {dx} Y {dy}"),
        ]),
        *_group(f"edge-marks-{side}", corners + edge_ticks),
        *_group(f"outline-{side}", [
            _rect(1, 1, EYE_WIDTH - 2, EYE_HEIGHT - 2, fill="none",
                  stroke=RED, stroke_width=2)
        ]),
        *_group(f"labels-{side}", [
            _rect(320, 108, 320, 44, fill=WHITE, stroke=BLACK, stroke_width=2),
            _text(CENTER_X, 140, 28, "middle", eye["label"]),
            _text(CENTER_X, 190, 26, "middle", "TOP"),
            _text(CENTER_X, 890, 26, "middle", "BOTTOM"),
            _text(CENTER_X, 930, 20, "middle", "EYE 960 x 1080 PX"),
            _text(24, 40, 20, "start", "CIRCLES R 60..480 PX STEP 60"),
        ]),
    ]


PATTERN_BUILDERS = {
    "stereo_grid.svg": _build_grid_eye,
    "stereo_rgb.svg": _build_rgb_eye,
    "stereo_geometry.svg": _build_geometry_eye,
}


def _frame_children():
    return [
        _rect(540, 8, 840, 44, fill=WHITE, stroke=BLACK, stroke_width=2),
        _text(CANVAS_WIDTH // 2, 40, 32, "middle", DIAGNOSTIC_BANNER),
        _text(CANVAS_WIDTH // 2, 78, 18, "middle", FRAME_DIMENSIONS),
        _line(CANVAS_WIDTH // 2, 0, CANVAS_WIDTH // 2, CANVAS_HEIGHT, BLACK, 4),
    ]


def build_document(name):
    """Return the full deterministic SVG document text for one pattern."""
    try:
        builder = PATTERN_BUILDERS[name]
    except KeyError:
        raise KeyError(f"unknown pattern: {name}") from None
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f"<!-- {GENERATOR_TAG} {name}: offline deterministic SVG; no external "
        "references, scripts or network. The file alone is not compositor "
        "or runtime-timing proof. -->",
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{CANVAS_WIDTH}" '
        f'height="{CANVAS_HEIGHT}" viewBox="0 0 {CANVAS_WIDTH} {CANVAS_HEIGHT}">',
        f'  <rect id="background" x="0" y="0" width="{CANVAS_WIDTH}" '
        f'height="{CANVAS_HEIGHT}" fill="{WHITE}"/>',
    ]
    for eye in _eyes():
        lines.append(
            f'  <g id="eye_{eye["side"]}" transform="translate({eye["offset"]},0)">'
        )
        for child in builder(eye):
            for physical_line in child.splitlines():
                lines.append("    " + physical_line)
        lines.append("  </g>")
    lines.append('  <g id="frame">')
    for child in _frame_children():
        for physical_line in child.splitlines():
            lines.append("    " + physical_line)
    lines.append("  </g>")
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="generate_patterns",
        description=(
            "Generate offline PSVR stereo diagnostic SVG patterns (M5/M6). "
            "Writes files only; never opens monitors, never automates "
            "fullscreen or compositor presentation."
        ),
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help=(
            "Directory for the generated SVG files (created if missing, but "
            "only after every target passes preflight). Required."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite existing files, but only the generated names in "
            "GENERATED_FILE_NAMES. Other files are never touched."
        ),
    )
    args = parser.parse_args(argv)
    if not args.output_dir.strip():
        parser.error("--output-dir must not be empty")
    return args


def generate(output_dir, force=False):
    """Preflight all targets, then write every generated SVG. Returns paths.

    Raises FileExistsError (nothing written) when a target exists and
    force is false. Raises NotADirectoryError when output_dir is a file.
    """
    # B10-09: duplica lo strip() della CLI anche per chiamate dirette a generate().
    if isinstance(output_dir, str):
        output_dir = output_dir.strip()
    if not str(output_dir).strip():
        raise ValueError("--output-dir must not be empty")
    output_dir = Path(output_dir)
    if output_dir.exists() and not output_dir.is_dir():
        raise NotADirectoryError(f"--output-dir is not a directory: {output_dir}")
    targets = [(output_dir / name, build_document(name)) for name in GENERATED_FILE_NAMES]
    blocked = [path.name for path, _ in targets if path.exists()]
    if blocked and not force:
        raise FileExistsError(
            "refusing to overwrite existing file(s): "
            + ", ".join(blocked)
            + " (pass --force to overwrite generated names only)"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for path, text in targets:
        path.write_bytes(text.encode("utf-8"))
        written.append(path)
    return written


def main(argv=None):
    args = parse_args(argv)
    try:
        written = generate(args.output_dir, force=args.force)
    except (FileExistsError, NotADirectoryError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
