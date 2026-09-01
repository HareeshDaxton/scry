"""``scry version`` — what is installed, and what it is running on."""

from __future__ import annotations

import argparse
import platform
import sys

from scry import __version__
from scry.cli.context import Context


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """No options of its own."""


def run(args: argparse.Namespace, ctx: Context) -> int:
    if ctx.json_output:
        ctx.console.json(
            {
                "name": "scry",
                "version": __version__,
                "python": platform.python_version(),
                "platform": f"{platform.system()} {platform.release()}",
                "executable": sys.executable,
            }
        )
        return 0

    ctx.console.out(f"scry {__version__}")
    ctx.console.out(
        f"python {platform.python_version()} on {platform.system()} {platform.release()}"
    )
    return 0
