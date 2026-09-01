"""Terminal colour, per spec section 22.

Colour is a courtesy, never a requirement: every message must read correctly
with the escapes stripped, because they will be, whenever output is piped.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import TextIO

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"


def rgb(hex_colour: str) -> str:
    """Truecolor escape for a ``#rrggbb`` value."""
    value = hex_colour.lstrip("#")
    red, green, blue = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    return f"\x1b[38;2;{red};{green};{blue}m"


# Spec section 22.2. Timekeeper is deferred to v1.5 but keeps its colour, so the
# palette stays complete and the section does not have to touch this file.
AGENT_HEX: Mapping[str, str] = {
    "cartographer": "#58a6ff",
    "hematologist": "#f85149",
    "archivist": "#d29922",
    "pathologist": "#bc8cff",
    "semiotician": "#3fb950",
    "skeptic": "#e3b341",
    "timekeeper": "#79c0ff",
    "oracle": "#39c5cf",
    "scribe": "#f0f6fc",
    "conductor": "#8b949e",
}

SEMANTIC_HEX: Mapping[str, str] = {
    "success": "#238636",
    "warning": "#d29922",
    "error": "#da3633",
    "info": "#58a6ff",
    "highlight": "#79c0ff",
    "muted": "#8b949e",
}

AGENT_COLORS: Mapping[str, str] = {name: rgb(hex_) for name, hex_ in AGENT_HEX.items()}
SEMANTIC_COLORS: Mapping[str, str] = {name: rgb(hex_) for name, hex_ in SEMANTIC_HEX.items()}


def enable_windows_vt() -> bool:
    """Turn on ANSI escape processing for the Windows console.

    Escapes only work once ``ENABLE_VIRTUAL_TERMINAL_PROCESSING`` is set on the
    console handle. Windows Terminal sets it; the older ``conhost.exe`` does not,
    and without it every escape prints as literal garbage.

    Done with stdlib ``ctypes`` rather than by importing colorama. Colorama is
    present in this environment only as a transitive dependency of pytest, and
    building on a package we never asked for is how a dependency vanishes from
    under you at the worst moment.
    """
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def supports_color(
    stream: TextIO,
    *,
    env: Mapping[str, str] | None = None,
    force_off: bool = False,
) -> bool:
    """Decide whether to emit escapes on ``stream``.

    ``NO_COLOR`` is honoured alongside ``SCRY_NO_COLOR``. Spec section 18.2 lists
    only the latter, but the former is the cross-tool convention and a user who
    has set it once expects it to work everywhere.
    """
    environ = os.environ if env is None else env

    if force_off:
        return False
    if environ.get("NO_COLOR"):
        return False
    if environ.get("SCRY_NO_COLOR"):
        return False
    if environ.get("TERM") == "dumb":
        return False
    if not hasattr(stream, "isatty") or not stream.isatty():
        return False
    return enable_windows_vt()


def strip_ansi(text: str) -> str:
    """Remove escape sequences. Used by tests and by non-TTY rendering."""
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def default_stream() -> TextIO:
    return sys.stdout
