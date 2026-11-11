# SPDX-License-Identifier: MIT
"""Minimal, strict Valve KeyValues (VDF/ACF) reader.

Implements only what Steam's ``libraryfolders.vdf`` and
``appmanifest_*.acf`` need: quoted keys/values, nested ``{ }`` blocks,
``//`` line comments, a UTF-8 BOM and Windows paths stored with escaped
backslashes (``"D:\\\\SteamLibrary"``). Nothing is ever written back.
"""

from __future__ import annotations

import re
from pathlib import Path


class VdfError(ValueError):
    """A KeyValues document is missing, unreadable or malformed."""


#: Maximum accepted VDF file size (1 MiB). Real manifests are a few KiB,
#: so anything larger is treated as crafted input, not a real manifest.
MAX_VDF_FILE_BYTES = 1024 * 1024

#: Maximum accepted ``{ }`` nesting depth. Real manifests nest 2-3 levels.
MAX_VDF_DEPTH = 64


_ESCAPES = {"\\": "\\", '"': '"', "n": "\n", "t": "\t", "r": "\r"}

_TOKEN = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<comment>//[^\n]*)
    | (?P<lbrace>\{)
    | (?P<rbrace>\})
    | (?P<qstring>"(?:\\.|[^"\\])*")
    | (?P<bare>[^\s{}"]+)
    """,
    re.VERBOSE,
)


def _decode(raw: str) -> str:
    out = []
    index = 0
    while index < len(raw):
        char = raw[index]
        if char == "\\" and index + 1 < len(raw):
            out.append(_ESCAPES.get(raw[index + 1], raw[index + 1]))
            index += 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _tokenize(text: str):
    if text.startswith("\ufeff"):
        text = text[1:]
    tokens = []
    position = 0
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None:
            raise VdfError(
                f"unexpected character at offset {position}: {text[position]!r}"
            )
        position = match.end()
        kind = match.lastgroup
        if kind in ("ws", "comment"):
            continue
        if kind == "qstring":
            tokens.append(("string", _decode(match.group()[1:-1])))
        elif kind == "bare":
            tokens.append(("string", match.group()))
        else:
            tokens.append((kind, match.group()))
    return tokens


def _parse_block(tokens, index, root=False, _depth=0):
    if _depth > MAX_VDF_DEPTH:
        raise VdfError("nesting too deep")
    result = {}
    while True:
        if index >= len(tokens):
            if root:
                return result, index
            raise VdfError("unexpected end of input: unclosed block")
        kind, value = tokens[index]
        if kind == "rbrace":
            return result, index + 1
        if kind != "string":
            raise VdfError(f"expected a key, found {kind!r}")
        key = value
        index += 1
        if index >= len(tokens):
            raise VdfError(f"missing value for key {key!r}")
        next_kind, next_value = tokens[index]
        if next_kind == "lbrace":
            block, index = _parse_block(tokens, index + 1, _depth=_depth + 1)
            result[key] = block
        elif next_kind == "string":
            result[key] = next_value
            index += 1
        else:
            raise VdfError(f"expected a value or block for key {key!r}")


def loads(text: str) -> dict:
    """Parse a KeyValues document into nested dictionaries."""
    tokens = _tokenize(text)
    if not tokens:
        raise VdfError("empty KeyValues document")
    try:
        result, index = _parse_block(tokens, 0, root=True)
    except RecursionError as exc:
        # Safety net if the interpreter limit is hit before MAX_VDF_DEPTH
        # (e.g. a platform with a smaller stack): callers only catch VdfError.
        raise VdfError("nesting too deep") from exc
    if index != len(tokens):
        raise VdfError("unbalanced braces or trailing tokens")
    return result


def loads_file(path) -> dict:
    """Read and parse a KeyValues file, mapping every failure to ``VdfError``."""
    try:
        size = Path(path).stat().st_size
    except OSError:
        size = None
    if size is not None and size > MAX_VDF_FILE_BYTES:
        raise VdfError(f"file too large: {path}")
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise VdfError(f"file not found: {path}") from exc
    except PermissionError as exc:
        raise VdfError(f"cannot read (permission denied): {path}") from exc
    except IsADirectoryError as exc:
        raise VdfError(f"not a file: {path}") from exc
    except UnicodeDecodeError as exc:
        raise VdfError(f"not UTF-8 text: {path}") from exc
    except OSError as exc:
        raise VdfError(f"cannot read {path}: {exc}") from exc
    if len(text) > MAX_VDF_FILE_BYTES:
        # File grew between stat() and read(): still refuse to parse it.
        raise VdfError(f"file too large: {path}")
    return loads(text)
