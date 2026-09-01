"""Console entry point for the ``scry`` command.

In section 1.1 this is deliberately a stub: it exists so the console script
declared in ``pyproject.toml`` resolves and the package is provably installed
and runnable. Section 1.7 replaces the body with the real argument parser and
the self-registering subcommand router.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from scry import __version__

_BANNER = f"""\
scry {__version__}

Terminal-native software archaeology.
Maps unknown codebases before you know what to ask.

No commands are wired up yet — this is the section 1.1 scaffold.
The parser and subcommand router arrive in section 1.7.
"""


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code.

    Args:
        argv: Argument vector excluding the program name. Defaults to
            ``sys.argv[1:]``. Accepted now so tests never have to monkeypatch
            ``sys.argv``, and so 1.7 can extend this without changing callers.

    Returns:
        A process exit code. See section 1.7 for the full taxonomy;
        for now, always ``0``.
    """
    _argv = sys.argv[1:] if argv is None else list(argv)
    sys.stdout.write(_BANNER)
    return 0
