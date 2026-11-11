"""Real DLL loader must reject a legacy upstream BEFORE calling its init."""
import ctypes
import sys
from pathlib import Path

shim_path, legacy_path = map(Path, sys.argv[1:3])
legacy = ctypes.WinDLL(str(legacy_path))
shim = ctypes.WinDLL(str(shim_path))
shim.psvr_pseye_load.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
shim.psvr_pseye_load.restype = ctypes.c_int
reason = ctypes.create_string_buffer(512)
assert legacy.fixture_initializations() == 0
assert shim.psvr_pseye_load(str(legacy_path.parent).encode("utf-8"), reason, len(reason)) == -2
assert b"patch set" in reason.value
assert legacy.fixture_initializations() == 0, "legacy init must never be called"
assert shim.psvr_pseye_count() == -1
print("PASS legacy upstream rejected before init; no device enumeration")
