# SPDX-License-Identifier: MIT
"""Read-only Steam library discovery and appmanifest inventory.

Libraries are located via ``steamapps/libraryfolders.vdf``. Only the small
``steamapps/*.acf`` manifests are read; the ``common`` game directories are
never listed or traversed.
"""

from __future__ import annotations

from pathlib import Path

from . import registry
from . import vdf

DEFAULT_STEAM_ROOTS = (
    r"C:\Program Files (x86)\Steam",
    r"C:\Program Files\Steam",
)


def resolve_steam_root(explicit=None):
    """Resolve the Steam root and where it came from, without writing anything."""
    if isinstance(explicit, str) and not explicit.strip():
        explicit = None
    if explicit:
        return Path(explicit), "explicit"
    value, source = registry.read_steam_install_path()
    if value:
        return Path(value), f"registry:{source}"
    for candidate in DEFAULT_STEAM_ROOTS:
        path = Path(candidate)
        try:
            if path.is_dir():
                return path, "default"
        except OSError:
            continue
    return None, "not_found"


def _lookup_ci(mapping, key):
    wanted = key.lower()
    for existing, value in mapping.items():
        if str(existing).lower() == wanted:
            return value
    return None


def load_library(path, source):
    """Inventory one library root: its manifest files and parse errors."""
    path = Path(path)
    steamapps = path / "steamapps"
    record = {
        "path": str(path),
        "source": source,
        "exists": path.is_dir(),
        "steamapps": str(steamapps),
        "steamapps_exists": steamapps.is_dir(),
        "appmanifests": [],
        "errors": [],
    }
    if not record["steamapps_exists"]:
        return record
    try:
        manifests = sorted(steamapps.glob("*.acf"))
    except OSError as exc:
        record["errors"].append({"file": str(steamapps), "error": str(exc)})
        return record
    for manifest in manifests:
        try:
            data = vdf.loads_file(manifest)
        except vdf.VdfError as exc:
            record["errors"].append({"file": str(manifest), "error": str(exc)})
            continue
        app = data.get("AppState", data)
        if not isinstance(app, dict):
            record["errors"].append(
                {"file": str(manifest), "error": "missing AppState block"}
            )
            continue
        record["appmanifests"].append(
            {
                "file": str(manifest),
                "appid": _lookup_ci(app, "appid"),
                "name": _lookup_ci(app, "name"),
                "installdir": _lookup_ci(app, "installdir"),
                "buildid": _lookup_ci(app, "buildid"),
                "stateflags": _lookup_ci(app, "stateflags"),
                "size_on_disk": _lookup_ci(app, "SizeOnDisk"),
            }
        )
    return record


def parse_library_folders(path):
    """Return the library paths declared in a ``libraryfolders.vdf`` file."""
    data = vdf.loads_file(path)
    folders = data.get("libraryfolders", data)
    if not isinstance(folders, dict):
        raise vdf.VdfError("libraryfolders.vdf has no libraryfolders block")
    result = []
    for entry in folders.values():
        if isinstance(entry, dict):
            value = _lookup_ci(entry, "path")
            if value and str(value).strip():
                result.append(str(value))
    return result


def library_folders_path(steam_root, override=None):
    if override and str(override).strip():
        return Path(override)
    if steam_root:
        return Path(steam_root) / "steamapps" / "libraryfolders.vdf"
    return None
