"""Shared fixtures.

Before this existed, ``test_cli``, ``test_init`` and ``test_doctor`` each carried
their own copy of the logging-isolation fixture, the temporary home, a target
repository and a CLI invocation helper. Three copies that had to be kept in step,
and would eventually not be.
"""

from __future__ import annotations

import pytest

from scry.storage import initialise_database
from scry.util.logging import reset_logging
from scry.workspace import create_workspace
from tests.fixtures.gitrepo import build_repo, git_is_available


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="rewrite golden files instead of comparing against them",
    )


@pytest.fixture
def update_golden(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--update-golden"))


@pytest.fixture(autouse=True)
def isolated_logging():
    """Logger objects are process-global, so configuration leaks between tests.

    Autouse because forgetting it produces failures in a *later* test than the
    one at fault, which is a miserable thing to debug.
    """
    reset_logging()
    yield
    reset_logging()


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------
@pytest.fixture
def home(tmp_path):
    """An isolated SCRY_HOME. Nothing in the suite touches a real one."""
    return tmp_path / "scry_home"


@pytest.fixture
def repo(tmp_path):
    """A stand-in for the repository under analysis.

    Deliberately contains a space in its name and a ``.git`` marker: the first
    catches quoting bugs, the second stops `scry init` warning in every test that
    does not care about git detection.
    """
    directory = tmp_path / "legacy platform"
    (directory / "src").mkdir(parents=True)
    (directory / ".git").mkdir()
    (directory / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (directory / "README.md").write_text("# demo\n", encoding="utf-8")
    return directory


@pytest.fixture
def workspace(home, repo):
    """A created workspace, without a database — the state 1.4 leaves behind."""
    return create_workspace("demo-project", repo, home=home)


@pytest.fixture
def initialised_workspace(workspace):
    """A workspace with its database, as ``scry init`` produces."""
    initialise_database(workspace.paths.database)
    return workspace


# ---------------------------------------------------------------------------
# Synthetic git repositories
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def git_required():
    """Skip rather than fail where git is unavailable.

    Git is a hard requirement of the product, but a unit test that does not need
    it should not fail on a machine that lacks it.
    """
    if not git_is_available():
        pytest.skip("git is not installed")


@pytest.fixture
def make_repo(tmp_path, git_required):
    """Build a synthetic repository with an exactly-known history."""

    def _make(commits, *, name="synthetic", **kwargs):
        return build_repo(tmp_path / name, commits, **kwargs)

    return _make
