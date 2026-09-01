"""Enables ``python -m scry`` as an alternative to the ``scry`` command.

This is not decoration. When a virtual environment's ``Scripts/`` directory is
not on PATH — a common and confusing situation on Windows — the ``scry``
console script is unavailable while the package itself is perfectly importable.
``python -m scry`` always works in that case, for users and for our own tests.
"""

from scry.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
