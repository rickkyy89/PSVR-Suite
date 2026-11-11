"""Entry point: ``python -m tools.psvr_audio_diag``."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
