# SPDX-License-Identifier: MIT
"""Known-tool and known-game catalog for the readiness inventory.

The catalog is intentionally conservative. A tool being listed means "we know
which executable to look for", not "this tool is verified for PSVR". Unknown
names are never promoted to a supported status.
"""

from __future__ import annotations

PREREQUISITES = (
    {
        "id": "M5",
        "status": "open",
        "detail": (
            "Display/compositor gate open: a recurring fullscreen -202 error and "
            "IsFullscreen=false were observed; Direct Mode is not established."
        ),
    },
    {
        "id": "M6",
        "status": "open",
        "detail": (
            "Optical acceptance open: the identity profile is diagnostic only; "
            "lens distortion has not been measured or accepted."
        ),
    },
)

TOOLS = (
    {
        "id": "whirligig",
        "name": "Whirligig",
        "kind": "video_player",
        "executables": ("Whirligig.exe",),
        "note": "M7 candidate; presence does not verify 180/360 mono/stereo SBS/TB.",
    },
    {
        "id": "gizmovr",
        "name": "GizmoVR",
        "kind": "video_player",
        "executables": ("GizmoVR.exe",),
        "note": "M7 candidate; not verified for PSVR.",
    },
    {
        "id": "deovr",
        "name": "DeoVR",
        "kind": "video_player",
        "executables": ("DeoVR.exe",),
        "note": "M7 candidate; not verified for PSVR.",
    },
    {
        "id": "vlc",
        "name": "VLC",
        "kind": "video_player",
        "executables": ("vlc.exe",),
        "note": "Desktop player; not a VR projection verifier on its own.",
    },
    {
        "id": "mpv",
        "name": "mpv",
        "kind": "video_player",
        "executables": ("mpv.exe",),
        "note": "Desktop player; not a VR projection verifier on its own.",
    },
    {
        "id": "yt-dlp",
        "name": "yt-dlp",
        "kind": "tool",
        "executables": ("yt-dlp.exe", "yt-dlp"),
        "note": "YouTube fetch candidate; URL expiry and separate streams stay open.",
    },
    {
        "id": "ffmpeg",
        "name": "FFmpeg",
        "kind": "tool",
        "executables": ("ffmpeg.exe", "ffmpeg"),
        "note": "Mux/transcode candidate; not part of a verified M7 flow yet.",
    },
)

# Products are grouped by milestone component. ``known_entries`` are relative to
# ``steamapps/common/<installdir>`` and are checked individually (file/dir
# existence only) so no game directory is ever traversed.
GAME_PRODUCTS = (
    {
        "component": "m8_assetto_corsa",
        "appid": "244210",
        "name": "Assetto Corsa",
        "store_packages": (),
        "known_entries": (
            ("acs.exe", "file"),
            ("AssettoCorsa.exe", "file"),
            ("content", "dir"),
            ("system", "dir"),
        ),
        "notes": (
            "Steam appid 244210. Presence does not verify the OpenVR mode, "
            "scale, recenter, latency or frametime required by M8.",
        ),
    },
    {
        "component": "m10_msfs",
        "appid": "1250410",
        "name": "Microsoft Flight Simulator (2020)",
        "store_packages": ("Microsoft.FlightSimulator_8wekyb3d8bbwe",),
        "known_entries": (("FlightSimulator.exe", "file"),),
        "notes": (
            "Steam appid 1250410; the Store package is also probed read-only. "
            "SteamVR must still be selected as the active OpenXR runtime by the "
            "operator.",
        ),
    },
    {
        "component": "m10_msfs",
        "appid": "2537590",
        "name": "Microsoft Flight Simulator 2024",
        "store_packages": ("Microsoft.Limitless_8wekyb3d8bbwe",),
        "known_entries": (("FlightSimulator2024.exe", "file"),),
        "notes": (
            "Steam appid 2537590; the launcher is FlightSimulator2024.exe. The "
            "Store package is also probed read-only. Document the exact "
            "2020/2024 build during M10."
        ),
    },
)
