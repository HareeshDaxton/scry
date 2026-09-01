"""The command table, and lazy loading of handlers.

By phase 8 this dispatches around twenty-one commands. ``argparse``'s
``add_subparsers`` is the obvious tool and the wrong one here: building
subparsers requires every command's argument definitions up front, which means
importing every command module at startup. ``scry map`` imports textual and
``scry hotspots`` imports NetworkX, so ``scry why`` — which promises a
sub-second answer — would pay the import cost of the entire product before
printing a line.

So the registry holds *metadata* only. The module is a string; nothing is
imported until a command is actually selected. ``scry --help`` still lists
everything, because names and summaries live here rather than in the modules.

Commands appear here as their sections land, so ``--help`` always describes what
works rather than advertising twenty commands that print "not implemented".
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import import_module
from types import ModuleType

from scry.util.errors import ScryError


@dataclass(frozen=True)
class Command:
    name: str
    summary: str
    module: str
    aliases: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


COMMANDS: tuple[Command, ...] = (
    Command(
        name="version",
        summary="Show version information",
        module="scry.cli.commands.version",
    ),
)

# Handles `scry <name>-<uid>` from spec section 5. Not in COMMANDS because it has
# no name of its own — it is what a bare workspace token dispatches to.
RESUME_MODULE = "scry.cli.commands.resume"


def find_command(name: str, commands: Sequence[Command] = COMMANDS) -> Command | None:
    return next((c for c in commands if name in c.names), None)


def command_names(commands: Sequence[Command] = COMMANDS) -> tuple[str, ...]:
    return tuple(name for command in commands for name in command.names)


def suggest(name: str, commands: Sequence[Command] = COMMANDS) -> tuple[str, ...]:
    """Close command names, for a 'did you mean' line on a typo."""
    return tuple(difflib.get_close_matches(name, command_names(commands), n=3, cutoff=0.6))


def load_module(dotted: str) -> ModuleType:
    """Import a command module and check it honours the handler contract."""
    try:
        module = import_module(dotted)
    except ImportError as exc:  # pragma: no cover - a packaging fault, not a user one
        raise ScryError(f"command module {dotted!r} could not be imported: {exc}") from exc

    if not hasattr(module, "run"):
        raise ScryError(f"command module {dotted!r} does not define run()")
    return module
