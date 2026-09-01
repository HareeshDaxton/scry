"""What every command handler is given.

Handlers take ``(args, ctx)``. Passing one object rather than a growing argument
list means adding something a command needs later does not touch every command
that does not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from scry.cli.output import Console
from scry.config import Config


@dataclass(frozen=True)
class Context:
    config: Config
    logger: logging.Logger
    console: Console
    home: Path | None = None
    # Where configuration was actually loaded from, honouring --config. Carried
    # here so `scry doctor` can report on the file the run really used rather
    # than guessing at the default location.
    config_path: Path | None = None
    # Set when configuration could not be loaded and defaults were substituted.
    # Only `scry doctor` is allowed to proceed in that state; see the router.
    config_error: str | None = None
    json_output: bool = False
    verbose: bool = False
