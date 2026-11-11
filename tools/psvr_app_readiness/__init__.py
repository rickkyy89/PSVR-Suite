"""Read-only application readiness inventory for PSVR-PC (M7/M8/M10).

This package answers one narrow, offline question: *which of the tools and
installed games that M7 (VR video), M8 (Assetto Corsa) and M10 (MSFS) depend on
are present on this machine?* It is deliberately not a compatibility claim.

Hard rules enforced here:

* **Read-only.** No process is launched, no file is written, no registry value
  is created or changed, no package is installed or downloaded.
* **No disk walking.** Steam libraries are found through ``libraryfolders.vdf``
  and only small ``steamapps/*.acf`` manifests plus a fixed list of known
  relative paths are inspected; game directories are never traversed.
* **Prerequisites are honoured.** M5 (display/compositor) and M6 (optical
  acceptance) are open, so the report never returns an end-to-end "ready".
* **Unknown tools are not claimed.** A required tool outside the catalog is
  reported as ``unknown``, never as present or verified.
"""

from __future__ import annotations

__all__ = ["__version__"]

# Inventory schema version, independent from the report content.
__version__ = "0.1.0"
