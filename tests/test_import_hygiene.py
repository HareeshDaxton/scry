"""Enforces the lazy-import rule for leaf dependencies.

Jinja2, NetworkX and textual are *leaf* dependencies: they must be imported
inside the functions that need them, never at module scope. Fast commands such
as ``scry why`` and ``scry owners`` promise sub-second answers and must not pay
their import cost.

Why guard this from the very first commit: import-time creep is invisible and
cumulative. Nobody adds 400 ms in one change. Someone adds a convenient
top-level ``import networkx`` while building the Salience Engine, someone else
adds ``import textual`` while building the TUI, and by the LLM phase a command
that prints six lines takes a second to start. This test fails the moment it
happens and names the offending module, which is far cheaper than untangling
dozens of imports later.

Each check runs in a *fresh subprocess*. Asserting against ``sys.modules``
inside the pytest process would be meaningless, since other tests in the same
session will already have imported these packages.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

# Declared as runtime dependencies, but only legitimately imported deep inside
# the phases that use them: Jinja2 (brief rendering), NetworkX (graph
# algorithms), textual (TUI).
LEAF_DEPENDENCIES = ("jinja2", "networkx", "textual")


@pytest.mark.parametrize("leaf", LEAF_DEPENDENCIES)
def test_importing_the_cli_does_not_pull_in(leaf: str) -> None:
    code = f"import sys\nimport scry.cli.main\nsys.exit(1 if {leaf!r} in sys.modules else 0)\n"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"importing scry.cli.main pulled in {leaf!r}. Move that import inside "
        f"the function that needs it."
    )


def test_importing_the_package_root_is_cheap() -> None:
    """``import scry`` must not drag in the CLI or anything heavier."""
    code = (
        "import sys\n"
        "import scry\n"
        "heavy = [m for m in ('jinja2', 'networkx', 'textual') if m in sys.modules]\n"
        "sys.exit(1 if heavy else 0)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, "`import scry` pulled in a leaf dependency"


@pytest.mark.parametrize("leaf", LEAF_DEPENDENCIES)
def test_importing_the_logging_utilities_does_not_pull_in(leaf: str) -> None:
    """Logging is configured on every command, before anything else runs."""
    code = f"import sys\nimport scry.util.logging\nsys.exit(1 if {leaf!r} in sys.modules else 0)\n"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"importing scry.util.logging pulled in {leaf!r}. Move that import inside "
        f"the function that needs it."
    )


@pytest.mark.parametrize("leaf", LEAF_DEPENDENCIES)
def test_importing_the_runtime_does_not_pull_in(leaf: str) -> None:
    """The runtime harness is stdlib only.

    Every worker process re-imports it at spawn time, so an import added here is
    paid once per agent per run rather than once per process.
    """
    code = f"import sys\nimport scry.runtime\nsys.exit(1 if {leaf!r} in sys.modules else 0)\n"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"importing scry.runtime pulled in {leaf!r}. Move that import inside "
        f"the function that needs it."
    )


def test_configuring_logging_does_not_import_multiprocessing() -> None:
    """``multiprocessing`` is stdlib but not free, and every command pays for
    ``util.logging``. Only ``start_log_listener`` needs it, and it imports it
    itself."""
    code = (
        "import sys\n"
        "import scry.util.logging\n"
        "sys.exit(1 if 'multiprocessing' in sys.modules else 0)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "importing scry.util.logging pulled in multiprocessing; keep that import "
        "inside start_log_listener()"
    )


@pytest.mark.parametrize("leaf", LEAF_DEPENDENCIES)
def test_importing_the_config_package_does_not_pull_in(leaf: str) -> None:
    """Config loads on every command, including the sub-second fast paths."""
    code = f"import sys\nimport scry.config\nsys.exit(1 if {leaf!r} in sys.modules else 0)\n"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"importing scry.config pulled in {leaf!r}. Move that import inside "
        f"the function that needs it."
    )
