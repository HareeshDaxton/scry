"""Tests for ``scry init`` (section 1.8).

The guarantee worth singling out is
``test_init_never_writes_into_the_target_repository``. Section 1.4 proved it for
workspace creation; this proves it still holds once a database is created too,
which is the point at which it would be easiest to accidentally break.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys

import pytest

from scry.cli.colors import strip_ansi
from scry.cli.registry import RESERVED_COMMAND_NAMES, command_names
from scry.cli.router import run
from scry.storage import reader
from scry.util.errors import ExitCode
from scry.util.logging import reset_logging
from scry.workspace import create_workspace, resolve_workspace

CORE_TABLES = frozenset(
    {"schema_version", "session_state", "agent_state", "claim_log", "claims", "merge_checkpoint"}
)


@pytest.fixture(autouse=True)
def isolated_logging():
    reset_logging()
    yield
    reset_logging()


@pytest.fixture
def home(tmp_path):
    return tmp_path / "scry_home"


@pytest.fixture
def repo(tmp_path):
    """A stand-in repository, with a .git marker and content to protect."""
    directory = tmp_path / "legacy platform"
    (directory / "src").mkdir(parents=True)
    (directory / ".git").mkdir()
    (directory / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (directory / "README.md").write_text("# demo\n", encoding="utf-8")
    return directory


class Result:
    def __init__(self, code, out, err):
        self.code = code
        self.out = strip_ansi(out)
        self.err = strip_ansi(err)
        self.raw_out = out


def invoke(argv, *, home) -> Result:
    out, err = io.StringIO(), io.StringIO()
    code = run(argv, home=home, env={}, stdout=out, stderr=err)
    return Result(code, out.getvalue(), err.getvalue())


def snapshot(directory):
    return {
        str(p.relative_to(directory)): p.read_bytes()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
def test_creates_a_workspace_with_a_database(home, repo):
    result = invoke(["init", "legacy-monolith", "--target", str(repo)], home=home)
    assert result.code == ExitCode.OK

    workspace = resolve_workspace("legacy-monolith", home=home)
    assert workspace.paths.marker.is_file()
    assert workspace.paths.database.is_file()


def test_the_database_has_every_core_table(home, repo):
    invoke(["init", "legacy-monolith", "--target", str(repo)], home=home)
    workspace = resolve_workspace("legacy-monolith", home=home)

    with reader(workspace.paths.database) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert {row[0] for row in rows} >= CORE_TABLES


def test_the_workspace_resolves_by_all_three_routes(home, repo):
    invoke(["init", "legacy-monolith", "--target", str(repo)], home=home)
    workspace = resolve_workspace("legacy-monolith", home=home)

    assert resolve_workspace(workspace.id, home=home).id == workspace.id
    assert resolve_workspace("legacy-monolith", home=home).id == workspace.id
    assert resolve_workspace(cwd=repo / "src", home=home).id == workspace.id


def test_init_never_writes_into_the_target_repository(home, repo):
    """Still true once a database is created, not only after 1.4's tree."""
    before = snapshot(repo)
    invoke(["init", "legacy-monolith", "--target", str(repo)], home=home)
    assert snapshot(repo) == before


def test_resume_reports_the_workspace_as_initialised(home, repo):
    """1.7 printed 'not initialised'; after init it should read real state."""
    invoke(["init", "legacy-monolith", "--target", str(repo)], home=home)
    result = invoke(["legacy-monolith"], home=home)
    assert "not initialised" not in result.out
    assert "status     created" in result.out


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def test_output_block_follows_the_spec_shape(home, repo):
    result = invoke(["init", "legacy-monolith", "--target", str(repo)], home=home)
    workspace = resolve_workspace("legacy-monolith", home=home)

    assert "Workspace created:" in result.out
    assert workspace.id in result.out
    assert "Mode: auto" in result.out
    assert "Target:" in result.out
    assert "Storage:" in result.out
    assert "Database:" in result.out
    assert f"scry {workspace.id}" in result.out, "the resume line is the point of the block"


def test_auto_mode_does_not_claim_a_backend(home, repo):
    """Detection happens at run time in 6.1; inventing a claim here would be a lie."""
    result = invoke(["init", "legacy-monolith", "--target", str(repo)], home=home)
    assert "backend detected at run time" in result.out


def test_json_output_is_valid_and_alone_on_stdout(home, repo):
    result = invoke(["--json", "init", "legacy-monolith", "--target", str(repo)], home=home)
    payload = json.loads(result.raw_out)

    assert payload["name"] == "legacy-monolith"
    assert payload["mode"] == "auto"
    assert payload["schema_version"] >= 1
    assert payload["target_path"] == str(repo.resolve())


def test_warnings_never_reach_stdout(home, tmp_path):
    """A non-git target warns; a caller parsing stdout must not see it."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    result = invoke(["--json", "init", "plain-dir", "--target", str(plain)], home=home)
    json.loads(result.raw_out)  # would raise if the warning leaked
    assert "not a git repository" in result.err


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["ab", "double--hyphen", "Not_Valid", "has space"])
def test_invalid_names_are_usage_errors(home, repo, bad):
    """Not exit 3: a typo in an argument is not a missing workspace."""
    result = invoke(["init", bad, "--target", str(repo)], home=home)
    assert result.code == ExitCode.USAGE
    assert result.out == ""


def test_windows_reserved_device_names_are_refused(home, repo):
    result = invoke(["init", "nul", "--target", str(repo)], home=home)
    assert result.code == ExitCode.USAGE
    assert "reserved device name" in result.err


@pytest.mark.parametrize("reserved", ["init", "version", "doctor", "map", "hotspots"])
def test_command_names_are_refused_as_workspace_names(home, repo, reserved):
    """Including commands that do not exist yet, so a name cannot break one later."""
    result = invoke(["init", reserved, "--target", str(repo)], home=home)
    assert result.code == ExitCode.USAGE
    assert "command name" in result.err


def test_reserved_names_cover_more_than_the_registered_commands():
    """`doctor` is reserved now although 1.9 has not landed."""
    assert set(command_names()) < RESERVED_COMMAND_NAMES
    assert "doctor" in RESERVED_COMMAND_NAMES


def test_missing_target_is_a_usage_error(home, tmp_path):
    result = invoke(["init", "ghost-repo", "--target", str(tmp_path / "nope")], home=home)
    assert result.code == ExitCode.USAGE
    assert "does not exist" in result.err


def test_a_file_as_target_is_refused(home, tmp_path):
    a_file = tmp_path / "file.txt"
    a_file.write_text("x", encoding="utf-8")
    result = invoke(["init", "a-file", "--target", str(a_file)], home=home)
    assert result.code == ExitCode.USAGE
    assert "not a directory" in result.err


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("retired", ["standard", "pro"])
def test_retired_modes_explain_what_replaced_them(home, repo, retired):
    """Anyone following spec section 5 or an older README will type these."""
    result = invoke(["init", "proj", "--target", str(repo), "--mode", retired], home=home)
    assert result.code == ExitCode.USAGE
    assert "no longer used" in result.err
    assert "detected backends" in result.err


def test_unknown_mode_lists_the_valid_ones(home, repo):
    result = invoke(["init", "proj", "--target", str(repo), "--mode", "turbo"], home=home)
    assert result.code == ExitCode.USAGE
    assert "lite" in result.err and "cloud" in result.err


@pytest.mark.parametrize("mode", ["auto", "lite", "local", "cloud"])
def test_every_valid_mode_is_accepted(home, repo, mode):
    result = invoke(["init", f"proj-{mode}", "--target", str(repo), "--mode", mode], home=home)
    assert result.code == ExitCode.OK
    assert resolve_workspace(f"proj-{mode}", home=home).marker.mode == mode


# ---------------------------------------------------------------------------
# Duplicates and --force
# ---------------------------------------------------------------------------
def test_a_second_workspace_for_the_same_target_is_refused(home, repo):
    invoke(["init", "first-one", "--target", str(repo)], home=home)
    result = invoke(["init", "second-one", "--target", str(repo)], home=home)

    assert result.code == ExitCode.USAGE
    assert "already targets" in result.err
    assert resolve_workspace("first-one", home=home).id in result.err


def test_force_creates_an_additional_workspace(home, repo):
    invoke(["init", "first-one", "--target", str(repo)], home=home)
    result = invoke(["init", "second-one", "--target", str(repo), "--force"], home=home)
    assert result.code == ExitCode.OK
    assert resolve_workspace("second-one", home=home) is not None


def test_force_leaves_the_existing_workspace_untouched(home, repo):
    """--force adds; it must never overwrite completed analysis."""
    invoke(["init", "first-one", "--target", str(repo)], home=home)
    original = resolve_workspace("first-one", home=home)
    before = snapshot(original.root)

    invoke(["init", "second-one", "--target", str(repo), "--force"], home=home)

    assert snapshot(original.root) == before


def test_a_shared_name_warns_without_refusing(home, tmp_path):
    """Legitimate for two checkouts, but it costs you name-based resolution."""
    for n in (1, 2):
        directory = tmp_path / f"checkout{n}"
        directory.mkdir()
        (directory / ".git").mkdir()

    invoke(["init", "shared", "--target", str(tmp_path / "checkout1")], home=home)
    result = invoke(["init", "shared", "--target", str(tmp_path / "checkout2")], home=home)

    assert result.code == ExitCode.OK
    assert "already named" in result.err
    assert "full id" in result.err


# ---------------------------------------------------------------------------
# Git detection
# ---------------------------------------------------------------------------
def test_a_non_git_target_warns_but_succeeds(home, tmp_path):
    """Oracle, Pathologist and Semiotician all work without git."""
    plain = tmp_path / "plain"
    plain.mkdir()
    result = invoke(["init", "plain-dir", "--target", str(plain)], home=home)

    assert result.code == ExitCode.OK
    assert "not a git repository" in result.err


def test_a_target_inside_a_repository_names_the_root(home, repo):
    result = invoke(["init", "just-src", "--target", str(repo / "src")], home=home)
    assert result.code == ExitCode.OK
    assert "not a repository root" in result.err
    assert str(repo.resolve()) in result.err


def test_a_worktree_git_file_counts_as_a_repository(home, tmp_path):
    """Worktrees use a .git file rather than a directory."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: ../real/.git/worktrees/wt\n", encoding="utf-8")

    result = invoke(["init", "a-worktree", "--target", str(worktree)], home=home)
    assert result.code == ExitCode.OK
    assert "not a git repository" not in result.err


# ---------------------------------------------------------------------------
# Help and dispatch
# ---------------------------------------------------------------------------
def test_init_appears_in_the_command_list(home):
    assert "init" in invoke([], home=home).out


def test_init_help_describes_its_options(home):
    result = invoke(["init", "--help"], home=home)
    assert result.code == ExitCode.OK
    for flag in ("--target", "--mode", "--force"):
        assert flag in result.out


def test_init_without_a_name_is_a_usage_error(home):
    """argparse's own errors must return a code, not kill the process."""
    result = invoke(["init"], home=home)
    assert result.code == ExitCode.USAGE
    assert "required" in result.err
    assert result.out == ""


def test_argparse_errors_go_through_the_console(home):
    """Otherwise they bypass --no-color and cannot be captured."""
    result = invoke(["init", "--target"], home=home)  # missing the flag's value
    assert result.code == ExitCode.USAGE
    assert result.err != "", "argparse's message was written to the real stderr"


def test_a_name_starting_with_a_hyphen_needs_the_standard_separator(home, repo):
    """`--` is the conventional escape; the name is then rejected on its merits."""
    result = invoke(["init", "--target", str(repo), "--", "-leading"], home=home)
    assert result.code == ExitCode.USAGE
    assert "invalid workspace name" in result.err


def test_init_does_not_import_leaf_dependencies(tmp_path):
    code = (
        "import sys, tempfile, pathlib\n"
        "from scry.cli.router import run\n"
        "tmp = pathlib.Path(tempfile.mkdtemp())\n"
        "(tmp / 'repo').mkdir()\n"
        "run(['--json', 'init', 'probe-ws', '--target', str(tmp / 'repo')],"
        " home=tmp / 'home', env={})\n"
        "heavy = [m for m in ('jinja2', 'networkx', 'textual') if m in sys.modules]\n"
        "sys.exit(1 if heavy else 0)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_workspace_created_by_the_api_is_visible_to_the_cli(home, repo):
    """1.4's create_workspace and 1.8's init must agree on the same layout."""
    workspace = create_workspace("api-made", repo, home=home)
    assert workspace.id in invoke([workspace.id], home=home).out
