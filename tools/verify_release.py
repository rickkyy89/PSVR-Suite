"""Verify a source ZIP and test an isolated extraction, without live hardware."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import zipfile


def verify_release(archive, temp_parent=None, run_tests=True, cmake=None, dependency_cache=None, build=False,
                   generator="Visual Studio 17 2022"):
    archive = Path(archive).resolve()
    with tempfile.TemporaryDirectory(prefix="psvr-release-", dir=temp_parent) as temporary:
        work = Path(temporary).resolve()
        with zipfile.ZipFile(archive) as package:
            entries = package.infolist()
            if sum(item.file_size for item in entries) > 64 * 1024 * 1024:
                raise ValueError("archive exceeds bounded source size")
            names = [item.filename for item in entries]
            if len(names) != len(set(names)):
                raise ValueError("duplicate archive member")
            for item in entries:
                target = (work / item.filename).resolve()
                if work not in target.parents or stat.S_ISLNK(item.external_attr >> 16):
                    raise ValueError("unsafe archive member")
                if item.file_size > 2 * 1024 * 1024:
                    raise ValueError("source member exceeds bounded size")
            manifests = [name for name in names if name.endswith("/RELEASE_MANIFEST.json")]
            if len(manifests) != 1:
                raise ValueError("missing or ambiguous release manifest")
            manifest = json.loads(package.read(manifests[0]))
            prefix = manifests[0].removesuffix("RELEASE_MANIFEST.json")
            expected = {prefix + record["path"] for record in manifest["files"]}
            if expected | {manifests[0]} != set(names):
                raise ValueError("manifest does not describe the complete archive")
            for record in manifest["files"]:
                data = package.read(prefix + record["path"])
                if len(data) != record["bytes"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
                    raise ValueError("source member checksum mismatch: " + record["path"])
            package.extractall(work)  # Every destination and member type validated above.
        checkout = work / prefix
        environment = dict(os.environ, PYTHONPATH=str(checkout), PYTHONDONTWRITEBYTECODE="1")
        commands = [("license_check", [sys.executable, "-B", "tools/collect_licenses.py", "--check"]),
                    ("headless_check", [sys.executable, "-B", "-m", "tools.psvr_suite", "--headless-check"])]
        if run_tests:
            commands.append(("python_tests", [sys.executable, "-B", "-m", "pytest", "tests", "-q"]))
        if cmake is not None:
            if dependency_cache is None:
                raise ValueError("isolated configure requires a pinned dependency cache directory")
            configure = [str(cmake), "-S", str(checkout), "-B", str(work / "build"),
                         "-G", generator, "-A", "x64",
                         "-DBUILD_TESTING=ON", "-DFETCHCONTENT_FULLY_DISCONNECTED=ON",
                         "-DPython3_EXECUTABLE=" + sys.executable,
                         "-DPSVR_DRIVER_PACKAGE=" + str(work / "driver-stage/psvrpc")]
            for key in ("hidapi", "nlohmann_json", "openvr_sdk"):
                source = Path(dependency_cache).resolve() / (key + "-src")
                if not source.is_dir():
                    raise ValueError("missing pinned source checkout: " + key)
                configure.append("-DFETCHCONTENT_SOURCE_DIR_" + key.upper() + "=" + str(source))
            commands.append(("isolated_cmake_configure", configure))
            if build:
                commands.append(("isolated_native_build", [str(cmake), "--build", str(work / "build"),
                                                           "--config", "RelWithDebInfo", "--parallel"]))
                commands.append(("isolated_ctest", [str(Path(cmake).with_name("ctest.exe")),
                                                    "--test-dir", str(work / "build"),
                                                    "-C", "RelWithDebInfo", "--output-on-failure"]))
        checks = []
        for label, argv in commands:
            result = subprocess.run(argv, cwd=checkout, env=environment, capture_output=True,
                                    text=True, errors="replace", timeout=240,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            summary = "\n".join((result.stdout + result.stderr).splitlines()[-8:])
            if result.returncode:
                failure_lines = [line for line in (result.stdout + result.stderr).splitlines()
                                 if "FAIL " in line or "error " in line.lower() or "***Failed" in line]
                summary = "\n".join(failure_lines[:20]) + "\n" + summary
            checks.append({"check": label, "exit_code": result.returncode, "summary": summary})
            if result.returncode:
                return {"ok": False, "version": manifest["version"], "checks": checks}
        return {"ok": True, "version": manifest["version"], "files": len(manifest["files"]),
                "checks": checks, "isolated_source_checkout": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--temp-parent", type=Path)
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--cmake", type=Path, help="optional isolated configure (default generator VS2022, see --generator)")
    parser.add_argument("--generator", default="Visual Studio 17 2022",
                        help="CMake generator for the isolated configure (default: %(default)s)")
    parser.add_argument("--dependency-cache", type=Path, help="existing pinned build/_deps checkouts")
    parser.add_argument("--build", action="store_true", help="also build and run CTest in the isolated extraction")
    args = parser.parse_args(argv)
    if args.build and args.cmake is None:
        parser.error("--build requires --cmake and --dependency-cache")
    result = verify_release(args.archive, args.temp_parent, not args.skip_tests, args.cmake, args.dependency_cache, args.build,
                          args.generator)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
