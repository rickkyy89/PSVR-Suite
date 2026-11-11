"""Avvio suite: ``.venv-camera\\Scripts\\python.exe -m tools.psvr_suite``."""

from __future__ import annotations

import sys

from .app import main

if __name__ == "__main__":
    sys.exit(main())
