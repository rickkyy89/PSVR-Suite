# SPDX-License-Identifier: MIT
"""Read-only PSVR1 audio diagnostics for the suite PU cinema/VR modes.

Run with ``python -m tools.psvr_audio_diag`` (see ``cli.py``). The package
never writes: it only uses the existing passive ``psvr_diag --status``
observation plus read-only PowerShell inventory queries. No power/mode/LED
commands, no SteamVR settings changes, no driver-binding changes. Unknown
results stay unknown; nothing is inferred from silence.

Context and hardware limits: ``docs/AUDIO_TRACKING_CAMERA_20261015.md``.
"""

__version__ = "0.1.0"
