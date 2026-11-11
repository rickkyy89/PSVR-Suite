# SPDX-License-Identifier: MIT
"""Builds the read-only application readiness report.

The report is a statement of *presence*, not of compatibility. It always lists
the open M5/M6 prerequisites and always reports ``end_to_end_ready`` as false.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import sys
from pathlib import Path

from . import __version__
from . import catalog
from . import registry
from . import steam
from . import vdf


def search_dirs(bin_dirs):
    """Explicit bin dirs first, then the process ``PATH``."""
    directories = [
        str(item) for item in bin_dirs if item and str(item).strip()
    ]
    for part in os.environ.get("PATH", "").split(os.pathsep):
        if part:
            directories.append(part)
    return directories


def find_executable(names, directories):
    for name in names:
        candidate = Path(name)
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    joined = os.pathsep.join(directories) if directories else None
    for name in names:
        found = shutil.which(name, path=joined)
        if found:
            return str(Path(found))
    for directory in directories:
        for name in names:
            candidate = Path(directory) / name
            try:
                if candidate.is_file():
                    return str(candidate)
            except OSError:
                continue
    return None


def inventory_tools(bin_dirs):
    directories = search_dirs(bin_dirs)
    entries = []
    for spec in catalog.TOOLS:
        location = find_executable(spec["executables"], directories)
        entries.append(
            {
                "id": spec["id"],
                "name": spec["name"],
                "kind": spec["kind"],
                "executables": list(spec["executables"]),
                "found": location is not None,
                "location": location,
                "status": "present" if location else "missing",
                "note": spec["note"],
            }
        )
    return entries


def _component_status(flags):
    if flags and all(flags):
        return "present"
    if any(flags):
        return "partial"
    return "missing"


def _blocked_by():
    return [item["id"] for item in catalog.PREREQUISITES if item["status"] != "closed"]


def build_tool_component(component, tools, required):
    found = {tool["id"]: tool["found"] for tool in tools}
    known = set(found)
    required = sorted(set(required))
    unknown = sorted(name for name in required if name not in known)
    missing = sorted(name for name in required if name in known and not found[name])
    return {
        "component": component,
        "inventory_status": _component_status(list(found.values())),
        "tools": tools,
        "required": required,
        "required_unknown": unknown,
        "required_missing": missing,
        "readiness": "not_ready",
        "blocked_by": _blocked_by(),
    }


def _store_packages(spec, local_appdata):
    results = []
    if local_appdata is not None and not str(local_appdata).strip():
        local_appdata = None
    for package in spec.get("store_packages", ()):
        if not package or not str(package).strip():
            continue
        path = (Path(local_appdata) / "Packages" / str(package)) if local_appdata else None
        results.append(
            {
                "package": package,
                "path": str(path) if path is not None else None,
                "present": bool(path is not None and path.is_dir()),
            }
        )
    return results


def _installdir_is_unsafe(library_path, installdir):
    """Return True when ``installdir`` must not be probed under the library.

    Rejects non-string values, any ``..`` substring, absolute paths
    (POSIX, UNC and Windows drive forms) and values whose normalized
    location escapes ``steamapps/common``.
    """
    if not isinstance(installdir, str):
        return True
    if ".." in installdir:
        return True
    text = installdir.strip()
    if not text:
        return False
    if os.path.isabs(text) or Path(text).is_absolute():
        return True
    if text.startswith("\\\\") or text.startswith("//"):
        return True
    if len(text) >= 2 and text[1] == ":":
        return True
    try:
        common = Path(library_path) / "steamapps" / "common"
        candidate = common / installdir
        common_norm = os.path.normcase(os.path.normpath(str(common)))
        candidate_norm = os.path.normcase(os.path.normpath(str(candidate)))
        if candidate_norm != common_norm and not candidate_norm.startswith(
            common_norm + os.sep
        ):
            return True
    except OSError:
        return True
    return False


def _installed_product(library, manifest, spec, local_appdata):
    installdir = manifest.get("installdir")
    base = None
    rejected = False
    if installdir:
        if _installdir_is_unsafe(library["path"], installdir):
            rejected = True
        else:
            base = Path(library["path"]) / "steamapps" / "common" / str(installdir)
    entries = []
    if not rejected:
        for relative, kind in spec["known_entries"]:
            target = (base / relative) if base is not None else None
            present = False
            if target is not None:
                try:
                    present = target.is_file() if kind == "file" else target.is_dir()
                except OSError:
                    present = False
            entries.append(
                {
                    "path": str(target) if target is not None else None,
                    "kind": kind,
                    "present": present,
                }
            )
    store_packages = _store_packages(spec, local_appdata)
    store_installed = any(entry["present"] for entry in store_packages)
    executable_flags = [entry["present"] for entry in entries if entry["kind"] == "file"]
    fallback = executable_flags or [entry["present"] for entry in entries]
    steam_status = _component_status(fallback)
    if steam_status == "present":
        status, source = "present", "steam"
    elif steam_status == "partial":
        status, source = "partial", "steam"
    elif store_installed:
        status, source = "present", "store"
    else:
        status, source = "missing", None
    notes = list(spec["notes"])
    if rejected:
        notes.append(
            "installdir value rejected as unsafe; steam entries not probed."
        )
    return {
        "component": spec["component"],
        "appid": spec["appid"],
        "name": spec["name"],
        "found": True,
        "inventory_status": status,
        "install_source": source,
        "store_installed": store_installed,
        "library": library["path"],
        "manifest": manifest["file"],
        "installdir": installdir,
        "install_path": str(base) if base is not None else None,
        "known_entries": entries,
        "store_packages": store_packages,
        "notes": notes,
    }


def inspect_product(libraries, spec, local_appdata):
    for library in libraries:
        for manifest in library["appmanifests"]:
            if str(manifest.get("appid")) == str(spec["appid"]):
                return _installed_product(library, manifest, spec, local_appdata)
    store_packages = _store_packages(spec, local_appdata)
    store_installed = any(entry["present"] for entry in store_packages)
    return {
        "component": spec["component"],
        "appid": spec["appid"],
        "name": spec["name"],
        "found": False,
        "inventory_status": "present" if store_installed else "missing",
        "install_source": "store" if store_installed else None,
        "store_installed": store_installed,
        "library": None,
        "manifest": None,
        "installdir": None,
        "install_path": None,
        "known_entries": [],
        "store_packages": store_packages,
        "notes": list(spec["notes"]),
    }


def build_game_component(component, products, libraries, local_appdata):
    records = [inspect_product(libraries, spec, local_appdata) for spec in products]
    statuses = {record["inventory_status"] for record in records}
    if statuses == {"present"}:
        status = "present"
    elif statuses == {"missing"}:
        status = "missing"
    else:
        status = "partial"
    return {
        "component": component,
        "inventory_status": status,
        "products": records,
        "readiness": "not_ready",
        "blocked_by": _blocked_by(),
    }


def build_steam_report(steam_root, extra_libraries, library_folders_override):
    if isinstance(steam_root, str) and not steam_root.strip():
        steam_root = None
    if isinstance(library_folders_override, str) and not library_folders_override.strip():
        library_folders_override = None
    root, root_source = steam.resolve_steam_root(steam_root)
    errors = []
    libraries = []
    seen = set()

    def add(path, source):
        key = os.path.normcase(os.path.abspath(str(path)))
        if key in seen:
            return
        seen.add(key)
        libraries.append(steam.load_library(path, source))

    if root is not None:
        add(root, root_source)

    folders_file = steam.library_folders_path(root, library_folders_override)
    if folders_file is not None and folders_file.is_file():
        try:
            for folder in steam.parse_library_folders(folders_file):
                if not folder or not str(folder).strip():
                    continue
                add(folder, "libraryfolders.vdf")
        except vdf.VdfError as exc:
            errors.append({"file": str(folders_file), "error": str(exc)})
    elif folders_file is not None and library_folders_override:
        errors.append({"file": str(folders_file), "error": "libraryfolders.vdf not found"})

    for extra in (extra_libraries or ()):
        if not extra or not str(extra).strip():
            continue
        add(extra, "explicit")

    return {
        "root": str(root) if root is not None else None,
        "root_source": root_source,
        "library_folders": str(folders_file) if folders_file is not None else None,
        "errors": errors,
        "libraries": libraries,
    }


def build_report(
    steam_root=None,
    extra_libraries=(),
    bin_dirs=(),
    required=(),
    library_folders=None,
    local_appdata=None,
    generated_utc=None,
):
    if local_appdata is None:
        local_appdata = os.environ.get("LOCALAPPDATA")
    tools = inventory_tools(bin_dirs)
    steam_report = build_steam_report(steam_root, extra_libraries, library_folders)
    libraries = steam_report["libraries"]

    components = {
        "m7_video": build_tool_component("m7_video", tools, required),
    }
    for component in ("m8_assetto_corsa", "m10_msfs"):
        products = [
            spec for spec in catalog.GAME_PRODUCTS if spec["component"] == component
        ]
        components[component] = build_game_component(
            component, products, libraries, local_appdata
        )

    strict_failures = []
    for component in components.values():
        strict_failures.extend(component.get("required_unknown", []))
        strict_failures.extend(component.get("required_missing", []))

    return {
        "schema": "psvr.app_readiness",
        "version": __version__,
        "generated_utc": generated_utc
        or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "read_only": True,
        "host": {"platform": sys.platform, "python": sys.version.split()[0]},
        "prerequisites": [dict(item) for item in catalog.PREREQUISITES],
        "steam": steam_report,
        "openxr": registry.read_openxr_runtime(),
        "components": components,
        "readiness": {
            "end_to_end_ready": False,
            "blocked_by": _blocked_by(),
            "strict_failures": sorted(strict_failures),
            "summary": (
                "Read-only inventory only. The M5 display and M6 optical gates "
                "are open, so no component is end-to-end ready; a present tool or "
                "game is not a verified M7/M8/M10 result."
            ),
        },
    }
