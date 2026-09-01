"""Console entry point for the ``scry`` command.

Deliberately thin. The router imports configuration, logging and the workspace
model; keeping that out of this module means ``import scry.cli.main`` stays
cheap, which is what ``tests/test_import_hygiene.py`` checks.
"""

from __future__ import annotations

from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code.

    Args:
        argv: argument vector excluding the program name. Defaults to
            ``sys.argv[1:]``. Accepted so tests never have to monkeypatch
            ``sys.argv``.

    Returns:
        An :class:`scry.util.errors.ExitCode` value.
    """
    from scry.cli.router import run

    return run(argv)
