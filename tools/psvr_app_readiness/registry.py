# SPDX-License-Identifier: MIT
"""Read-only Windows registry probes used by the readiness inventory.

Every access uses ``winreg.OpenKey(..., KEY_READ)``. This module never calls
``CreateKey``/``SetValue`` and never touches the launch/install configuration.
Off Windows it returns explicit "unavailable" results instead of raising.
"""

from __future__ import annotations

import sys


def _winreg():
    import winreg  # imported locally: absent off Windows

    return winreg


def read_openxr_runtime() -> dict:
    """Return the active OpenXR runtime, read-only.

    Reads ``ActiveRuntime`` from both the 64-bit and the WOW6432Node
    ``HKLM\\SOFTWARE\\Khronos\\OpenXR\\1`` keys. The value is reported as
    found; it is never changed.
    """
    result = {
        "available": False,
        "active_runtime": None,
        "source": None,
        "steamvr": None,
        "read_only": True,
        "error": None,
    }
    if sys.platform != "win32":
        result["error"] = "registry probe requires Windows"
        return result
    winreg = _winreg()
    candidates = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Khronos\OpenXR\1"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Khronos\OpenXR\1"),
    )
    for root, subkey in candidates:
        try:
            with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ) as key:
                value, _ = winreg.QueryValueEx(key, "ActiveRuntime")
        except OSError:
            continue
        text = str(value)
        result.update(
            available=True,
            active_runtime=text,
            source=rf"HKLM\{subkey}\ActiveRuntime",
            steamvr="steamvr" in text.lower(),
        )
        return result
    result["error"] = "ActiveRuntime value not present"
    return result


def read_steam_install_path():
    """Return ``(path, source)`` for Steam from the registry, read-only."""
    if sys.platform != "win32":
        return None, None
    winreg = _winreg()
    candidates = (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    )
    for root, subkey, name in candidates:
        try:
            with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ) as key:
                value, _ = winreg.QueryValueEx(key, name)
        except OSError:
            continue
        if value:
            hive = "HKCU" if root == winreg.HKEY_CURRENT_USER else "HKLM"
            return str(value), rf"{hive}\{subkey}\{name}"
    return None, None
