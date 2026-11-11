# PSVR stereo diagnostic patterns (M5/M6 offline generator)

Python 3.11, standard library only. Generates three deterministic
`1920x1080` side-by-side SVG files with equal `960x1080` eye halves
(left eye x 0..959, right eye x 960..1919, whole-eye groups are plain
`translate()` so authored units are pixel units).

```powershell
python -B tools\psvr_patterns\generate_patterns.py --output-dir out\patterns
python -B tools\psvr_patterns\generate_patterns.py --output-dir out\patterns --force
```

Exit codes: 0 success, 1 refused existing target(s) or output error
(nothing written), 2 invalid arguments. `--help` writes nothing. Without
`--force` every target name is preflighted first: if any exists, all
writes are refused and no other file in the directory is touched.
`--force` overwrites only the three generated names. The output directory
is created only after preflight passes.

## Files

- `optics_grid.png` - 1024x1024 fixed flat square-line PNG reference (`generate_optics_grid.py`, stdlib-only, deterministic) shown by the optics trials. Size is fixed by design (B10-08): the generator and its CLI expose no `--width`; callers consume an existing PNG file path, never a width.
- `stereo_grid.svg` - 60 px square pixel grid (major every 300 px),
  concentric circles, red center cross, eye borders, top/left rulers with
  pixel labels, `LEFT EYE - L` / `RIGHT EYE - R`, `TOP`/`BOTTOM` and
  `960 x 1080` / `1920 x 1080` dimension text.
- `stereo_rgb.svg` - 96 px vertical stripes in order red, green, blue
  per eye, white horizontal alignment lines plus white eye-local center
  line, black label bar, distinct eye labels and orientation text.
- `stereo_geometry.svg` - concentric circles r 60..480 px step 60,
  authored as `<circle>` in pixel units with translate-only eye offsets,
  so they are truly circular only if scale/aspect are exact; off-center
  blue marks at mirrored x (180 left eye, 780 right eye) to expose
  left/right swap, and corner/edge-midpoint marks plus a red eye outline
  to expose cropping.

All files carry the visible text `DIAGNOSTIC - UNCALIBRATED` and an
SVG-image-alone disclaimer in the header comment.

## Limits

Uncalibrated diagnostic content only: nothing here is lens calibration or
a distortion profile. The tool never enumerates or opens monitors, never
automates fullscreen or compositor presentation and never launches the
monitor app; presenting a file on the PSVR panel is a later manual step.
Displaying an SVG proves nothing about compositor output or runtime frame
timing. The SVG contains no external references, no scripts and needs no
network; byte output is stable across runs, so generated files are safe
to keep as saved package/diagnostic resources.

## Tests and package integration

`tests/test_patterns.py` (namespaced XML parsing plus CLI subprocess,
temp files only, no GUI):

```powershell
python -B -m unittest discover -s tests -p test_patterns.py -v
```

CMake/docs/package wiring (e.g. generating `out/patterns` at install time
as a diagnostics resource) belongs to the parent milestone integration,
not to this tool.
