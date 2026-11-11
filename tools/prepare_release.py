"""Export a bounded source-only release from the consolidated working tree.

No git staging, commits, tags, network, hardware, or installed runtime changes.
Binary assets are copied byte-for-byte through ZIP, never edited as text.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = {".github", "configurations", "docs", "driver", "scripts", "tests", "third_party", "tools"}
EXCLUDED_DIRS = {".git", ".opencode", ".vs", ".pytest_cache", "__pycache__", ".deps",
                 "build", "out", "dist", "logs", "cache", "node_modules",
                 ".tmp.driveupload", ".tmp.drivedownload"}
EXTENSIONS = {".py", ".cpp", ".c", ".hpp", ".h", ".ps1", ".cmake", ".json", ".md",
              ".txt", ".yml", ".yaml", ".toml", ".svg", ".patch", ".terms", ".cfg",
              ".vrsettings", ".vrdrivermanifest", ".def", ".rc", ".in", ".cmd", ".bat"}
ROOT_FILES = {".gitignore", "CMakeLists.txt", "CMakePresets.json", "VERSION", "LICENSE", "config.json"}
MAX_FILE = 2 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024


def source_files(root: Path):
    root = root.resolve()
    for current, folders, files in os.walk(root, topdown=True, followlinks=False):
        folder = Path(current)
        folders[:] = sorted(name for name in folders if name not in EXCLUDED_DIRS
                            and not name.startswith(".venv")
                            and not (folder / name).is_symlink()
                            and (folder != root or name in SOURCE_DIRS))
        for name in sorted(files):
            path = folder / name
            relative = path.relative_to(root)
            if path.is_symlink() or name.startswith(("psvr_suite_user.json", "psvr_suite_ui.json", ".env")):
                continue
            notice = name.startswith(("LICENSE", "COPYING", "NOTICE")) or name == "CMakeLists.txt"
            if folder == root and name not in ROOT_FILES and path.suffix not in {".md", ".ps1", ".txt"} and not notice:
                continue
            asset = relative.parts[:3] == ("tools", "psvr_suite", "assets") and path.suffix in {".png", ".ico"}
            if path.suffix not in EXTENSIONS and name not in ROOT_FILES and not notice and not asset:
                continue
            if path.stat().st_size > MAX_FILE:
                raise ValueError("source file exceeds bounded export size: " + relative.as_posix())
            yield path, relative.as_posix()


def digest(path: Path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            result.update(chunk)
    return result.hexdigest()


def build_source_package(root=ROOT, output=None):
    root = Path(root).resolve()
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?", version):
        raise ValueError("invalid release version")
    output = Path(output) if output is not None else root / "dist"
    if not output.parent.is_dir():
        raise ValueError("release output parent must exist")
    output.mkdir(exist_ok=True)
    stem = "psvr-suite-" + version + "-source"
    archive = output / (stem + ".zip")
    checksum = output / (stem + ".sha256")
    if archive.exists() or checksum.exists():
        raise FileExistsError("release already exists; choose a fresh output directory")
    files = sorted(source_files(root), key=lambda item: item[1])
    if sum(path.stat().st_size for path, _ in files) > MAX_TOTAL:
        raise ValueError("aggregate source export exceeds 64 MiB")
    manifest = {"version": version, "type": "source-only", "source": "working-tree export",
                "published": False, "files": []}
    try:
        with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as package:
            for path, relative in files:
                package.write(path, stem + "/" + relative)
                manifest["files"].append({"path": relative, "bytes": path.stat().st_size,
                                          "sha256": digest(path)})
            package.writestr(stem + "/RELEASE_MANIFEST.json", json.dumps(manifest, indent=2) + "\n")
        with zipfile.ZipFile(archive) as package:
            bad = package.testzip()
            if bad:
                raise ValueError("archive integrity check failed: " + bad)
        sha256 = digest(archive)
        with checksum.open("x", encoding="ascii") as stream:
            stream.write(sha256 + "  " + archive.name + "\n")
    except Exception:
        archive.unlink(missing_ok=True)
        checksum.unlink(missing_ok=True)
        raise
    return {"archive": str(archive), "sha256": sha256, "checksum": str(checksum),
            "files": len(files), "bytes": archive.stat().st_size, "version": version}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(build_source_package(args.root, args.output_dir), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
