"""Filesystem locations shared across Scry.

``scry_home()`` lives here rather than in ``util/logging`` — which needed it
first, back in section 1.3 — so that ``workspace/`` does not have to import from
the logging module just to find a directory.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

HOME_ENV_VAR = "SCRY_HOME"
DEFAULT_HOME_DIRNAME = ".scry"


def scry_home(env: Mapping[str, str] | None = None) -> Path:
    """Return Scry's data directory: ``$SCRY_HOME`` or ``~/.scry``.

    Everything Scry writes lives under here — never inside the repository being
    analysed. Tests pass an explicit ``env`` so they never touch a real home
    directory.
    """
    environ = env if env is not None else os.environ
    raw = environ.get(HOME_ENV_VAR)
    return Path(raw).expanduser() if raw else Path.home() / DEFAULT_HOME_DIRNAME
