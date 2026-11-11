"""M9 webcam-side metric position tools for PSVR-PC.

This package is autonomous and additive: it never touches the PSVR driver and
never modifies the OS camera stack or the USB device binding. It only *reads* a
camera the user explicitly selects and emits a versioned JSON position stream
over UDP for the driver agent to consume.

Design rules enforced by this package:

* OpenCV is optional. ``camera_backend``/``capture`` import it lazily and fail
  with a clear message instead of installing anything. Geometry, calibration
  math and the UDP protocol only need NumPy (protocol needs the stdlib alone).
* A camera is never selected implicitly. Inventory lists every probed index;
  probing and tracking require an explicit index/backend.
* The optional native PS3 Eye backend (``pseye_backend``) talks to the camera's
  *existing* libusb-win32 binding through ``tools/psvr_pseye``. It never
  installs, replaces, rebinds or reflashes a driver and needs no administrator
  rights. The OpenCV paths are untouched.
* Position is metric only when explicit calibrated intrinsics and a measured
  marker geometry are supplied. The PSVR built-in LED ring geometry is *not*
  calibrated here and is explicitly refused as a geometry source.
"""

from __future__ import annotations

__all__ = ["__version__"]

# Tool version, independent from the on-wire protocol version (protocol.py).
__version__ = "0.1.0"
