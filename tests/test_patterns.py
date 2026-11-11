"""Tests for the offline stereo diagnostic pattern generator (M5/M6).

Software-only: SVGs are parsed from temporary directories with namespaced
ElementTree and the CLI runs in a subprocess. No windows, no monitors, no
fullscreen automation. The agent generates temp test files only; package
artifacts are the parent build's job.
"""
from __future__ import annotations

import ast
import contextlib
from io import StringIO
import importlib.util
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "tools/psvr_patterns/generate_patterns.py"
SPEC = importlib.util.spec_from_file_location("generate_patterns", GENERATOR)
patterns = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(patterns)

SVG_NS = "http://www.w3.org/2000/svg"
TRANSLATE_RE = re.compile(
    r"^translate\(\s*(-?\d+(?:\.\d+)?)[ ,]\s*(-?\d+(?:\.\d+)?)\s*\)$"
)
XMLNS_DECL = 'xmlns="http://www.w3.org/2000/svg"'
FORBIDDEN_RAW = (
    "<script", "href", "url(", "xlink", "<image", "<style",
    "foreignobject", "@import", "<!entity", "javascript",
)


def S(tag):
    return f"{{{SVG_NS}}}{tag}"


def run_main(argv):
    out, err = StringIO(), StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = patterns.main(argv)
    return code, out.getvalue(), err.getvalue()


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-B", str(GENERATOR), *args],
        capture_output=True, text=True, timeout=60,
    )


def node_int(node, name):
    return int(float(node.get(name)))


class SvgFixture(unittest.TestCase):
    """Shared namespaced parsing of the three generated files."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.outdir = Path(cls.tmp.name) / "patterns"
        patterns.generate(cls.outdir)
        cls.raw = {
            name: (cls.outdir / name).read_text(encoding="utf-8")
            for name in patterns.GENERATED_FILE_NAMES
        }
        cls.root = {name: ET.fromstring(text) for name, text in cls.raw.items()}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def eye_groups(self, name):
        groups = {
            g.get("id"): g
            for g in self.root[name].iter(S("g"))
            if g.get("id") in ("eye_left", "eye_right")
        }
        self.assertEqual(set(groups), {"eye_left", "eye_right"}, name)
        return groups

    def eye_texts(self, group):
        return [t.text or "" for t in group.iter(S("text"))]

    def groups_by_prefix(self, node, prefix):
        return [g for g in node.iter(S("g")) if (g.get("id") or "").startswith(prefix)]


class StructureTest(SvgFixture):
    def test_xml_declaration_and_namespace(self):
        for name, text in self.raw.items():
            self.assertTrue(text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n'))
            self.assertIn(f"<svg {XMLNS_DECL}", text)
            self.assertEqual(text.count(XMLNS_DECL), 1, name)
            self.assertEqual(self.root[name].tag, S("svg"))

    def test_root_dimensions_and_pixel_aspect(self):
        for name in patterns.GENERATED_FILE_NAMES:
            root = self.root[name]
            self.assertEqual(node_int(root, "width"), 1920, name)
            self.assertEqual(node_int(root, "height"), 1080, name)
            self.assertEqual(root.get("viewBox"), "0 0 1920 1080", name)
            # An overridden preserveAspectRatio would distort circles.
            self.assertIsNone(root.get("preserveAspectRatio"), name)

    def test_eye_grouping_equal_sbs_halves(self):
        for name in patterns.GENERATED_FILE_NAMES:
            eyes = self.eye_groups(name)
            left = TRANSLATE_RE.fullmatch(eyes["eye_left"].get("transform") or "")
            right = TRANSLATE_RE.fullmatch(eyes["eye_right"].get("transform") or "")
            self.assertIsNotNone(left, name)
            self.assertIsNotNone(right, name)
            self.assertEqual((float(left.group(1)), float(left.group(2))), (0.0, 0.0))
            self.assertEqual((float(right.group(1)), float(right.group(2))), (960.0, 0.0))
            self.assertEqual(960, 1920 // 2)  # Equal-width side-by-side halves.

    def test_only_eye_offsets_have_transforms(self):
        # translate() only: no scale/skew/rotate anywhere, so authored pixel
        # units map 1:1 to device pixels and circles stay circular.
        for name in patterns.GENERATED_FILE_NAMES:
            for el in self.root[name].iter():
                transform = el.get("transform")
                if transform is not None:
                    self.assertEqual(el.tag, S("g"), name)
                    self.assertIn(el.get("id"), ("eye_left", "eye_right"), name)
                    self.assertIsNotNone(TRANSLATE_RE.fullmatch(transform), transform)

    def test_no_ellipses_and_positive_circle_radii(self):
        for name in patterns.GENERATED_FILE_NAMES:
            self.assertEqual(len(list(self.root[name].iter(S("ellipse")))), 0, name)
            circles = list(self.root[name].iter(S("circle")))
            for circle in circles:
                self.assertGreater(float(circle.get("r")), 0.0)
            if name in ("stereo_grid.svg", "stereo_geometry.svg"):
                self.assertGreaterEqual(len(circles), 8, name)

    def test_banner_unique_and_frame_scoped(self):
        parent = {c: p for root in self.root.values() for p in root.iter() for c in p}
        for name in patterns.GENERATED_FILE_NAMES:
            banners = [
                el for el in self.root[name].iter(S("text"))
                if (el.text or "").strip() == patterns.DIAGNOSTIC_BANNER
            ]
            self.assertEqual(len(banners), 1, name)
            self.assertEqual(parent[banners[0]].get("id"), "frame", name)

    def test_eye_labels_distinct(self):
        for name in patterns.GENERATED_FILE_NAMES:
            eyes = self.eye_groups(name)
            left_blob = "\n".join(self.eye_texts(eyes["eye_left"]))
            right_blob = "\n".join(self.eye_texts(eyes["eye_right"]))
            self.assertIn("LEFT", left_blob)
            self.assertIn("RIGHT", right_blob)
            self.assertNotIn("RIGHT", left_blob, name)
            self.assertNotIn("LEFT", right_blob, name)
            self.assertIn("LEFT EYE - L", left_blob, name)
            self.assertIn("RIGHT EYE - R", right_blob, name)

    def test_orientation_and_dimension_text(self):
        for name in patterns.GENERATED_FILE_NAMES:
            eyes = self.eye_groups(name)
            for side, group in eyes.items():
                blob = "\n".join(self.eye_texts(group))
                self.assertIn("TOP", blob, f"{name}/{side}")
                self.assertIn("BOTTOM", blob, f"{name}/{side}")
                self.assertIn("960 x 1080", blob, f"{name}/{side}")
            frame = "\n".join(
                t.text or "" for t in self.root[name].iter(S("text"))
            )
            self.assertIn("1920 x 1080", frame, name)

    def test_ids_are_unique_per_document(self):
        for name in patterns.GENERATED_FILE_NAMES:
            ids = [el.get("id") for el in self.root[name].iter() if el.get("id")]
            self.assertEqual(len(ids), len(set(ids)), name)

    def test_no_external_references_or_active_scripts(self):
        for name, text in self.raw.items():
            stripped = text.replace(XMLNS_DECL, "")
            lowered = stripped.lower()
            for token in FORBIDDEN_RAW:
                self.assertNotIn(token, lowered, f"{name}: {token}")
            for el in self.root[name].iter():
                if el.tag.rsplit("}", 1)[-1] == "script":
                    self.fail(f"{name} contains a script element")
                for key, value in el.attrib.items():
                    local = key.rsplit("}", 1)[-1]
                    self.assertFalse(local.startswith("on"), f"{name}: {key}")
                    self.assertNotIn("http", value, f"{name}: {key}")
                    self.assertNotIn("url(", value, f"{name}: {key}")

    def test_not_a_proof_disclaimer_in_header(self):
        for name, text in self.raw.items():
            self.assertIn("not compositor", text, name)
            self.assertIn("runtime-timing proof", text, name)


class GridPatternTest(SvgFixture):
    def test_grid_cells_are_square_in_pixel_units(self):
        for eye_id, side in (("eye_left", "left"), ("eye_right", "right")):
            group = next(g for g in self.groups_by_prefix(
                self.eye_groups("stereo_grid.svg")[eye_id], f"grid-{side}"))
            verticals, horizontals = [], []
            for line in group.iter(S("line")):
                if line.get("x1") == line.get("x2"):
                    verticals.append(node_int(line, "x1"))
                elif line.get("y1") == line.get("y2"):
                    horizontals.append(node_int(line, "y1"))
            for xs, label in ((verticals, "x"), (horizontals, "y")):
                xs = sorted(set(xs))
                self.assertGreaterEqual(len(xs), 12, f"vertical {label}")
                steps = {b - a for a, b in zip(xs, xs[1:])}
                self.assertEqual(steps, {60}, f"{eye_id} {label}-spacing")
            self.assertEqual(
                sorted({b - a for a, b in zip(sorted(set(verticals)), sorted(set(verticals))[1:])}),
                sorted({b - a for a, b in zip(sorted(set(horizontals)), sorted(set(horizontals))[1:])}),
            )

    def test_center_cross_and_border(self):
        eyes = self.eye_groups("stereo_grid.svg")
        for eye_id, side in (("eye_left", "left"), ("eye_right", "right")):
            cross = self.groups_by_prefix(eyes[eye_id], f"center-cross-{side}")
            self.assertEqual(len(cross), 1, eye_id)
            self.assertIn(S("circle"), [c.tag for c in cross[0]])
            borders = self.groups_by_prefix(eyes[eye_id], f"border-{side}")
            rect = next(borders[0].iter(S("rect")))
            self.assertEqual(
                (node_int(rect, "x"), node_int(rect, "y"),
                 node_int(rect, "width"), node_int(rect, "height")),
                (2, 2, 956, 1076), eye_id,
            )

    def test_rulers_have_ticks_and_px_labels(self):
        eyes = self.eye_groups("stereo_grid.svg")
        for eye_id, side in (("eye_left", "left"), ("eye_right", "right")):
            ruler = self.groups_by_prefix(eyes[eye_id], f"ruler-{side}")[0]
            top_ticks = [l for l in ruler.iter(S("line"))
                         if l.get("x1") == l.get("x2") and node_int(l, "y1") == 0]
            left_ticks = [l for l in ruler.iter(S("line"))
                          if l.get("y1") == l.get("y2") and node_int(l, "x1") == 0]
            self.assertEqual(len(top_ticks), 17, eye_id)  # 0..960 step 60.
            self.assertEqual(len(left_ticks), 19, eye_id)  # 0..1080 step 60.
            labels = set(self.eye_texts(ruler))
            self.assertLessEqual({"0", "300", "600", "900", "960", "1080"}, labels, eye_id)

    def test_grid_circles_concentric(self):
        eyes = self.eye_groups("stereo_grid.svg")
        for eye_id, side in (("eye_left", "left"), ("eye_right", "right")):
            circles = self.groups_by_prefix(eyes[eye_id], f"circles-{side}")[0]
            radii = []
            for c in circles.iter(S("circle")):
                self.assertEqual((node_int(c, "cx"), node_int(c, "cy")), (480, 540), eye_id)
                radii.append(float(c.get("r")))
            self.assertEqual(radii, sorted(radii))
            self.assertGreater(len(radii), 3, eye_id)


class RgbPatternTest(SvgFixture):
    def test_rgb_stripe_order_and_geometry_per_eye(self):
        eyes = self.eye_groups("stereo_rgb.svg")
        for eye_id, side in (("eye_left", "left"), ("eye_right", "right")):
            stripes = self.groups_by_prefix(eyes[eye_id], f"rgb-stripes-{side}")[0]
            rects = sorted(stripes.iter(S("rect")), key=lambda r: node_int(r, "x"))
            self.assertEqual(len(rects), 10, eye_id)
            self.assertEqual([node_int(r, "x") for r in rects], list(range(0, 960, 96)))
            self.assertEqual({node_int(r, "width") for r in rects}, {96})
            cycle = [patterns.RED, patterns.GREEN, patterns.BLUE]
            self.assertEqual([r.get("fill") for r in rects],
                             [cycle[i % 3] for i in range(10)], eye_id)
            self.assertEqual(rects[0].get("fill"), "#FF0000")

    def test_white_alignment_lines(self):
        eyes = self.eye_groups("stereo_rgb.svg")
        for eye_id, side in (("eye_left", "left"), ("eye_right", "right")):
            align = self.groups_by_prefix(eyes[eye_id], f"alignment-{side}")[0]
            white_h = [l for l in align.iter(S("line"))
                       if l.get("stroke") == "#FFFFFF" and l.get("y1") == l.get("y2")]
            white_v = [l for l in align.iter(S("line"))
                       if l.get("stroke") == "#FFFFFF" and l.get("x1") == l.get("x2")]
            self.assertGreaterEqual(len(white_h), 5, eye_id)
            self.assertGreaterEqual(len(white_v), 1, eye_id)

    def test_label_bar_and_stripe_dimension(self):
        eyes = self.eye_groups("stereo_rgb.svg")
        for eye_id, side in (("eye_left", "left"), ("eye_right", "right")):
            blob = "\n".join(self.eye_texts(eyes[eye_id]))
            self.assertIn("STRIPE 96 PX", blob, eye_id)
            bars = self.groups_by_prefix(eyes[eye_id], f"labels-{side}")[0]
            black_bar = next(
                r for r in bars.iter(S("rect"))
                if r.get("fill") == "#000000" and node_int(r, "y") == 900
            )
            self.assertEqual(node_int(black_bar, "height"), 180, eye_id)


class GeometryPatternTest(SvgFixture):
    def test_concentric_circles_truly_circular_pixel_units(self):
        eyes = self.eye_groups("stereo_geometry.svg")
        expected = list(range(60, 481, 60))
        for eye_id, side in (("eye_left", "left"), ("eye_right", "right")):
            circles = self.groups_by_prefix(eyes[eye_id], f"concentric-circles-{side}")[0]
            radii = []
            for c in circles.iter(S("circle")):
                self.assertEqual((node_int(c, "cx"), node_int(c, "cy")), (480, 540), eye_id)
                self.assertIsNone(c.get("rx"))  # circle, never a squashed ellipse.
                radii.append(node_int(c, "r"))
            self.assertEqual(radii, expected, eye_id)
            self.assertLessEqual(max(expected), 480)  # Fits the 960-wide eye.

    def test_offcenter_marks_differ_per_eye(self):
        eyes = self.eye_groups("stereo_geometry.svg")
        positions = {}
        for eye_id, side in (("eye_left", "left"), ("eye_right", "right")):
            group = self.groups_by_prefix(eyes[eye_id], f"offcenter-{side}")[0]
            circle = next(group.iter(S("circle")))
            positions[eye_id] = (node_int(circle, "cx"), node_int(circle, "cy"))
            self.assertNotEqual(positions[eye_id], (480, 540), eye_id)
            blob = "\n".join(self.eye_texts(group))
            self.assertIn(f"OFFCENTER X {positions[eye_id][0]} Y {positions[eye_id][1]}", blob)
        self.assertNotEqual(positions["eye_left"], positions["eye_right"])

    def test_edge_marks_reach_every_eye_boundary(self):
        eyes = self.eye_groups("stereo_geometry.svg")
        for eye_id, side in (("eye_left", "left"), ("eye_right", "right")):
            marks = self.groups_by_prefix(eyes[eye_id], f"edge-marks-{side}")[0]
            rects = list(marks.iter(S("rect")))
            corners = {(node_int(r, "x"), node_int(r, "y")) for r in rects
                       if node_int(r, "width") == 16 and node_int(r, "height") == 16}
            self.assertEqual(corners, {(1, 1), (943, 1), (1, 1063), (943, 1063)}, eye_id)
            self.assertTrue(any(node_int(r, "y") == 1 for r in rects), f"{eye_id} top")
            self.assertTrue(any(node_int(r, "y") + node_int(r, "height") == 1079
                                for r in rects), f"{eye_id} bottom")
            self.assertTrue(any(node_int(r, "x") == 1 for r in rects), f"{eye_id} left")
            self.assertTrue(any(node_int(r, "x") + node_int(r, "width") == 959
                                for r in rects), f"{eye_id} right")
            outlines = self.groups_by_prefix(eyes[eye_id], f"outline-{side}")[0]
            self.assertEqual(next(outlines.iter(S("rect"))).get("stroke"), "#FF0000")


class DeterminismTest(unittest.TestCase):
    def test_build_document_deterministic_strings(self):
        for name in patterns.GENERATED_FILE_NAMES:
            self.assertEqual(patterns.build_document(name), patterns.build_document(name))
        docs = {name: patterns.build_document(name) for name in patterns.GENERATED_FILE_NAMES}
        self.assertEqual(len(set(docs.values())), 3)

    def test_generated_files_byte_identical_across_runs(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            first = patterns.generate(Path(a) / "deep/nested")
            second = patterns.generate(Path(b))
            self.assertEqual([p.name for p in first], list(patterns.GENERATED_FILE_NAMES))
            self.assertTrue((Path(a) / "deep/nested").is_dir())
            for name in patterns.GENERATED_FILE_NAMES:
                left = (Path(a) / "deep/nested" / name).read_bytes()
                right = (Path(b) / name).read_bytes()
                self.assertEqual(left, right, name)
                self.assertEqual(left.decode("utf-8"), patterns.build_document(name))

    def test_unknown_pattern_rejected(self):
        with self.assertRaises(KeyError):
            patterns.build_document("stereo_missing.svg")


class PreflightTest(unittest.TestCase):
    def test_existing_targets_refused_without_partial_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "stereo_grid.svg").write_text("sentinel-a", encoding="utf-8")
            code, _, err = run_main(["--output-dir", str(out)])
            self.assertEqual(code, 1)
            self.assertIn("--force", err)
            self.assertIn("stereo_grid.svg", err)
            # Nothing was written at all: neither overwritten nor created.
            self.assertEqual((out / "stereo_grid.svg").read_text(encoding="utf-8"),
                             "sentinel-a")
            for name in ("stereo_rgb.svg", "stereo_geometry.svg"):
                self.assertFalse((out / name).exists(), name)

    def test_refusal_keeps_existing_output_dir_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            for name in patterns.GENERATED_FILE_NAMES:
                (out / name).write_text("sentinel", encoding="utf-8")
            code, _, _ = run_main(["--output-dir", str(out)])
            self.assertEqual(code, 1)
            for name in patterns.GENERATED_FILE_NAMES:
                self.assertEqual((out / name).read_text(encoding="utf-8"), "sentinel")

    def test_output_dir_pointing_at_a_file_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "blocker"
            blocker.write_text("x", encoding="utf-8")
            code, _, err = run_main(["--output-dir", str(blocker)])
            self.assertEqual(code, 1)
            self.assertIn("not a directory", err)


class SourceHygieneTest(unittest.TestCase):
    def test_imports_are_stdlib_and_benign(self):
        tree = ast.parse(GENERATOR.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        self.assertLessEqual(
            imported, {"__future__", "argparse", "pathlib", "sys", "xml.sax.saxutils"}
        )

    def test_no_gui_network_or_exec_calls(self):
        source = GENERATOR.read_text(encoding="utf-8")
        for token in ("tkinter", "webbrowser", "ctypes", "urllib", "socket",
                      "subprocess", "os.system", "eval(", "exec(", "datetime"):
            self.assertNotIn(token, source, token)


class CliSubprocessTest(unittest.TestCase):
    def test_help_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out"
            proc = run_cli("--help")
            self.assertEqual(proc.returncode, 0)
            self.assertIn("--output-dir", proc.stdout)
            self.assertIn("--force", proc.stdout)
            self.assertFalse(target.exists())

    def test_missing_output_dir_is_cli_error(self):
        proc = run_cli()
        self.assertEqual(proc.returncode, 2)

    def test_empty_output_dir_is_cli_error(self):
        proc = run_cli("--output-dir", "")
        self.assertEqual(proc.returncode, 2)

    def test_run_writes_three_named_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "patterns"
            proc = run_cli("--output-dir", str(target))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(sorted(p.name for p in target.iterdir()),
                             sorted(patterns.GENERATED_FILE_NAMES))

    def test_force_only_touches_generated_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "patterns"
            target.mkdir()
            (target / "stereo_grid.svg").write_text("sentinel", encoding="utf-8")
            (target / "keep.txt").write_text("keep", encoding="utf-8")
            (target / "unrelated.svg").write_text("unrelated", encoding="utf-8")
            refused = run_cli("--output-dir", str(target))
            self.assertEqual(refused.returncode, 1)
            self.assertIn("--force", refused.stderr)
            forced = run_cli("--output-dir", str(target), "--force")
            self.assertEqual(forced.returncode, 0, forced.stderr)
            self.assertEqual((target / "keep.txt").read_text(encoding="utf-8"), "keep")
            self.assertEqual((target / "unrelated.svg").read_text(encoding="utf-8"),
                             "unrelated")
            regenerated = (target / "stereo_grid.svg").read_text(encoding="utf-8")
            self.assertEqual(regenerated, patterns.build_document("stereo_grid.svg"))


if __name__ == "__main__":
    unittest.main()
