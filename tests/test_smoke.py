"""Smoke tests: the package installs, imports, and its entry points run.

These are the tests that prove the *installation* works, not that any feature
does. Because the project uses a ``src/`` layout, ``import scry`` only succeeds
when the package is genuinely installed — so a passing import here means we are
testing the artifact we would actually ship, not a directory that happens to be
sitting in the current working directory.
"""

from __future__ import annotations

import re
import subprocess
import sys

import scry
from scry.cli.main import main

VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+([-.+].*)?$")


def test_package_exposes_a_sane_version() -> None:
    assert VERSION_PATTERN.match(scry.__version__), (
        f"__version__ is not a recognisable version string: {scry.__version__!r}"
    )


def test_main_returns_success_exit_code(capsys) -> None:
    assert main([]) == 0
    assert "scry" in capsys.readouterr().out.lower()


def test_main_accepts_an_explicit_argv() -> None:
    # main() must never require sys.argv monkeypatching to be testable.
    # Section 1.1's stub ignored every argument; since 1.7 the router parses
    # them, so this asserts the argv is genuinely honoured rather than dropped.
    assert main(["--version"]) == 0
    assert main(["--not-an-option"]) == 2  # ExitCode.USAGE


def test_python_dash_m_entry_point_works() -> None:
    """``python -m scry`` is the fallback when Scripts/ is not on PATH."""
    result = subprocess.run(
        [sys.executable, "-m", "scry"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "scry" in result.stdout.lower()
