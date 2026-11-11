# SPDX-License-Identifier: MIT
"""Command line interface for the read-only readiness inventory."""

from __future__ import annotations

import argparse
import datetime as dt
import json

from . import __version__
from .inventory import build_report

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_STRICT = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="psvr_app_readiness",
        description=(
            "Read-only inventory of M7 video tools, M8 Assetto Corsa and M10 "
            "MSFS prerequisites. It never launches a game, installs a package, "
            "writes a file or changes a registry value."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"psvr_app_readiness {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    inventory = sub.add_parser("inventory", help="emit the readiness inventory as JSON")
    inventory.add_argument(
        "--steam-root",
        default=None,
        help="explicit Steam root (default: registry SteamPath, then standard path)",
    )
    inventory.add_argument(
        "--library",
        action="append",
        default=[],
        metavar="PATH",
        help="extra Steam library root to include (repeatable)",
    )
    inventory.add_argument(
        "--library-folders",
        default=None,
        metavar="VDF",
        help="explicit libraryfolders.vdf path",
    )
    inventory.add_argument(
        "--bin-dir",
        action="append",
        default=[],
        metavar="PATH",
        help="directory searched for tools before PATH (repeatable)",
    )
    inventory.add_argument(
        "--require",
        action="append",
        default=[],
        metavar="TOOL",
        help="tool id that must be present (repeatable); unknown ids are reported, never assumed",
    )
    inventory.add_argument(
        "--local-appdata",
        default=None,
        metavar="PATH",
        help="override %%LOCALAPPDATA%% for Microsoft Store package probing",
    )
    inventory.add_argument(
        "--generated-utc",
        default=None,
        help="override the report timestamp (deterministic output)",
    )
    inventory.add_argument(
        "--strict",
        action="store_true",
        help="exit 3 when a required tool is missing or unknown",
    )
    inventory.add_argument("--json", action="store_true", help="JSON output (default)")
    inventory.set_defaults(func=cmd_inventory)
    return parser


def cmd_inventory(args) -> int:
    raw = args.generated_utc
    if raw is not None:
        text = str(raw).strip()
        candidate = text
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        try:
            dt.datetime.fromisoformat(candidate)
        except ValueError:
            build_parser().error(
                f"invalid --generated-utc: {raw!r} (expected ISO 8601)"
            )
    report = build_report(
        steam_root=args.steam_root,
        extra_libraries=args.library,
        bin_dirs=args.bin_dir,
        required=args.require,
        library_folders=args.library_folders,
        local_appdata=args.local_appdata,
        generated_utc=args.generated_utc,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.strict and report["readiness"]["strict_failures"]:
        return EXIT_STRICT
    return EXIT_OK


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    except Exception as exc:  # noqa: BLE001 - report, never leak a traceback
        print(
            json.dumps(
                {
                    "schema": "psvr.app_readiness.error",
                    "version": __version__,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                indent=2,
                sort_keys=True,
            )
        )
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())
