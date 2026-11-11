# SPDX-License-Identifier: MIT
"""Load DLL and call only the factory. Never Init/Activate, SteamVR or USB."""
import ctypes
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

root = Path(sys.argv[1])
manifest = json.loads((root / "driver.vrdrivermanifest").read_text(encoding="utf-8"))
settings = json.loads((root / "resources/settings/default.vrsettings").read_text(encoding="utf-8"))
assert manifest["name"] == "psvrpc" and manifest["alwaysActivate"] is False
assert manifest["hmd_presence"] == ["054C.09AF"]
assert settings["driver_psvrpc"]["enable"] is False
assert settings["driver_psvrpc"]["recenterMode"] == "full"
assert settings["driver_psvrpc"]["displayEnabled"] is False
assert settings["driver_psvrpc"]["displayMonitorPath"] == ""
assert settings["driver_psvrpc"]["nativeDirectMode"] is False and settings["driver_psvrpc"]["virtualDisplayEnabled"] is False and settings["driver_psvrpc"]["positionEnabled"] is False
assert settings["driver_psvrpc"]["imuExportEnabled"] is False and settings["driver_psvrpc"]["imuExportPort"] == 4272 and settings["driver_psvrpc"]["imuExportHz"] == 100
assert "redirectsDisplay" not in manifest and json.loads((Path(__file__).resolve().parents[1] / "driver" / "virtual_package" / "driver.vrdrivermanifest").read_text(encoding="utf-8")).get("redirectsDisplay") is True
assert "projectionLeftLeft" not in settings["driver_psvrpc"]
assert settings["driver_psvrpc"]["distortionMode"] == "identity"
assert settings["driver_psvrpc"]["distortionProfilePath"] == ""
profile = json.loads((root / "resources/profiles/identity_unverified.json").read_text(encoding="utf-8"))
assert profile["schema_version"] == 1 and "unverified" in profile["name"]
assert profile["mapping"] == "display-to-render"
for name in ("stereo_grid.svg", "stereo_rgb.svg", "stereo_geometry.svg"):
    svg = ET.parse(root / "resources/diagnostics" / name).getroot()
    assert svg.attrib["width"] == "1920" and svg.attrib["height"] == "1080"
for name in ("openvr-LICENSE.txt", "LICENSE", "BSL-1.0.txt", "LICENSE-bsd.txt", "json-MIT.txt"):
    assert (root / "licenses" / name).is_file(), name
dll = ctypes.CDLL(str(root / "bin/win64/driver_psvrpc.dll"))
factory = dll.HmdDriverFactory
factory.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
factory.restype = ctypes.c_void_p
code = ctypes.c_int(-1)
provider = factory(b"IServerTrackedDeviceProvider_004", ctypes.byref(code))
assert provider and code.value == 0
assert factory(b"IServerTrackedDeviceProvider_004", None) == provider
assert not factory(b"unsupported", ctypes.byref(code)) and code.value != 0
assert not factory(None, ctypes.byref(code)) and code.value != 0
print("PASS package, Windows x64 DLL load and factory (no hardware/runtime initialized)")
