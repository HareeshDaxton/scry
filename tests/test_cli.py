"""Tests for the CLI parser and router (section 1.7).

Two invariants get the most attention. Nothing but a command's result may reach
stdout, because a single stray character there breaks every caller that pipes
us. And selecting one command must not import any other, because that is what
keeps ``scry why`` fast once twenty commands exist.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys

import pytest

from scry import __version__
from scry.cli.colors import strip_ansi, supports_color
from scry.cli.registry import COMMANDS, find_command, suggest
from scry.cli.router import EXIT_INTERRUPTED
from scry.util.errors import ExitCode
from scry.workspace import create_workspace
from tests.fixtures.cli import invoke

# `home`, `repo` and the autouse logging isolation come from conftest.


# ---------------------------------------------------------------------------
# Top level
# ---------------------------------------------------------------------------
def test_bare_invocation_prints_help_on_stdout(home):
    """For a tool whose whole point is discovery, the first run shows the way in."""
    result = invoke([], home=home)
    assert result.code == ExitCode.OK
    assert "Usage: scry" in result.out
    assert "Commands:" in result.out


def test_help_lists_every_registered_command(home):
    result = invoke([], home=home)
    for command in COMMANDS:
        assert command.name in result.out
        assert command.summary in result.out


def test_version_flag(home):
    result = invoke(["--version"], home=home)
    assert result.code == ExitCode.OK
    assert __version__ in result.out


def test_version_command(home):
    result = invoke(["version"], home=home)
    assert result.code == ExitCode.OK
    assert __version__ in result.out


def test_short_v_is_verbose_not_version(home):
    """Deviates from spec section 5 deliberately: -v means verbose everywhere else."""
    result = invoke(["-v", "version"], home=home)
    assert result.code == ExitCode.OK
    assert __version__ in result.out

    assert __version__ in invoke(["-V"], home=home).out


def test_command_help(home):
    result = invoke(["version", "--help"], home=home)
    assert result.code == ExitCode.OK
    assert "scry version" in result.out


# ---------------------------------------------------------------------------
# Errors and exit codes
# ---------------------------------------------------------------------------
def test_unknown_command_is_a_usage_error(home):
    result = invoke(["nonsense"], home=home)
    assert result.code == ExitCode.USAGE
    assert "unknown command" in result.err


def test_unknown_command_keeps_stdout_empty(home):
    """A caller piping us must never receive an error as though it were a result."""
    assert invoke(["nonsense"], home=home).out == ""


def test_unknown_command_suggests_a_close_match(home):
    result = invoke(["verison"], home=home)
    assert "Did you mean" in result.err
    assert "version" in result.err


def test_unrecognised_option_is_a_usage_error(home):
    result = invoke(["--nope"], home=home)
    assert result.code == ExitCode.USAGE
    assert result.out == ""


def test_suggestions_come_from_the_registry():
    assert "version" in suggest("verison")
    assert suggest("zzzzzzzz") == ()


def test_registry_lookup():
    assert find_command("version") is not None
    assert find_command("nope") is None


# ---------------------------------------------------------------------------
# Workspace tokens
# ---------------------------------------------------------------------------
def test_workspace_id_resumes(home, repo):
    workspace = create_workspace("legacy-monolith", repo, home=home)
    result = invoke([workspace.id], home=home)
    assert result.code == ExitCode.OK
    assert workspace.id in result.out
    assert str(repo.resolve()) in result.out


def test_workspace_name_resumes_when_unambiguous(home, repo):
    workspace = create_workspace("legacy-monolith", repo, home=home)
    assert invoke(["legacy-monolith"], home=home).code == ExitCode.OK
    assert workspace.id in invoke(["legacy-monolith"], home=home).out


def test_missing_workspace_id_uses_the_workspace_exit_code(home):
    """Id-shaped means the user clearly meant a workspace, so say so specifically."""
    result = invoke(["absent-aaaa2222"], home=home)
    assert result.code == ExitCode.WORKSPACE_NOT_FOUND
    assert result.out == ""


def test_a_typo_is_an_unknown_command_not_a_missing_workspace(home):
    """Reporting a mistyped command as a missing workspace would explain nothing."""
    assert invoke(["verison"], home=home).code == ExitCode.USAGE


def test_commands_win_a_name_collision(home, repo):
    """A command shadowed by data is more surprising than the reverse."""
    workspace = create_workspace("version", repo, home=home)
    result = invoke(["version"], home=home)
    assert __version__ in result.out
    assert workspace.id not in result.out, "the workspace shadowed the command"


def test_the_full_id_is_the_escape_hatch_for_a_collision(home, repo):
    workspace = create_workspace("version", repo, home=home)
    result = invoke([workspace.id], home=home)
    assert workspace.id in result.out


def test_resume_reports_an_uninitialised_workspace(home, repo):
    """A workspace exists from 1.4; only `scry init` creates its database."""
    workspace = create_workspace("fresh-one", repo, home=home)
    result = invoke([workspace.id], home=home)
    assert result.code == ExitCode.OK
    assert "not initialised" in result.out


def test_resume_reports_session_state_once_the_database_exists(home, repo):
    from scry.storage import initialise_database

    workspace = create_workspace("with-db", repo, home=home)
    initialise_database(workspace.paths.database)

    result = invoke([workspace.id], home=home)
    assert "created" in result.out
    assert "llm calls  0" in result.out


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------
def test_json_output_is_valid_and_alone_on_stdout(home):
    result = invoke(["--json", "version"], home=home)
    payload = json.loads(result.raw_out)
    assert payload["version"] == __version__


def test_json_resume_carries_the_workspace(home, repo):
    workspace = create_workspace("legacy-monolith", repo, home=home)
    payload = json.loads(invoke(["--json", workspace.id], home=home).raw_out)
    assert payload["id"] == workspace.id
    assert payload["session"] is None


def test_json_output_is_never_coloured(home):
    """Escapes would make the output unparseable."""
    assert "\x1b[" not in invoke(["--json", "version"], home=home).raw_out


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------
def test_colour_is_off_when_output_is_piped():
    assert supports_color(io.StringIO(), env={}) is False


def test_no_color_env_disables_colour():
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    assert supports_color(Tty(), env={"NO_COLOR": "1"}) is False
    assert supports_color(Tty(), env={"SCRY_NO_COLOR": "1"}) is False
    assert supports_color(Tty(), env={"TERM": "dumb"}) is False


def test_no_color_flag_disables_colour(home):
    assert "\x1b[" not in invoke(["--no-color", "nonsense"], home=home).err


def test_strip_ansi_round_trips():
    assert strip_ansi("\x1b[38;2;1;2;3mtext\x1b[0m") == "text"


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------
def test_keyboard_interrupt_uses_the_conventional_code(home, monkeypatch):
    import scry.cli.router as router

    def explode(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(router, "_dispatch", explode)
    assert invoke(["version"], home=home).code == EXIT_INTERRUPTED


def test_unexpected_errors_do_not_dump_a_traceback(home, monkeypatch):
    """A wall of traceback tells the target user nothing actionable."""
    import scry.cli.router as router

    def explode(*_args, **_kwargs):
        raise RuntimeError("something internal broke")

    monkeypatch.setattr(router, "_dispatch", explode)
    result = invoke(["version"], home=home)

    assert result.code == ExitCode.ERROR
    assert "Traceback" not in result.err
    assert "unexpected error" in result.err
    assert "--verbose" in result.err


def test_verbose_re_raises_so_developers_get_the_traceback(home, monkeypatch):
    import scry.cli.router as router

    def explode(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(router, "_dispatch", explode)
    with pytest.raises(RuntimeError, match="boom"):
        invoke(["--verbose", "version"], home=home)


# ---------------------------------------------------------------------------
# Configuration plumbing
# ---------------------------------------------------------------------------
def test_config_flag_reaches_the_loader(home, tmp_path):
    config_file = tmp_path / "custom.yaml"
    config_file.write_text("skeptic:\n  challange_threshold: 0.9\n", encoding="utf-8")

    result = invoke(["--config", str(config_file), "version"], home=home)
    assert result.code == ExitCode.OK
    assert "unknown configuration key" in result.err, "config warnings should surface"


def test_config_warnings_do_not_pollute_stdout(home, tmp_path):
    config_file = tmp_path / "custom.yaml"
    config_file.write_text("nonsense_section: true\n", encoding="utf-8")
    result = invoke(["--config", str(config_file), "version"], home=home)
    assert result.out.strip() == f"scry {__version__}" or __version__ in result.out
    assert "unknown configuration key" not in result.out


def test_a_bad_config_file_stops_ordinary_commands(home, tmp_path):
    """Substituting defaults silently would let an ignored setting look honoured."""
    config_file = tmp_path / "custom.yaml"
    config_file.write_text("skeptic:\n  batch_size: ten\n", encoding="utf-8")

    result = invoke(["--config", str(config_file), "version"], home=home)
    assert result.code == ExitCode.ERROR
    assert "batch_size" in result.err
    assert "scry doctor" in result.err
    assert result.out == ""


def test_a_bad_config_file_does_not_stop_doctor(home, tmp_path):
    """Refusing to start the diagnostic because its subject is broken is absurd."""
    config_file = tmp_path / "custom.yaml"
    config_file.write_text("skeptic:\n  batch_size: ten\n", encoding="utf-8")

    result = invoke(["--config", str(config_file), "doctor"], home=home)
    assert result.code == ExitCode.ERROR  # the config is genuinely broken
    assert "Environment" in result.out, "doctor still produced a full report"


# ---------------------------------------------------------------------------
# Lazy dispatch
# ---------------------------------------------------------------------------
def test_selecting_one_command_does_not_import_the_others():
    """What keeps `scry why` fast once twenty commands exist."""
    code = (
        "import sys\n"
        "from scry.cli.main import main\n"
        "main(['--json', 'version'])\n"
        "loaded = [m for m in sys.modules if m.startswith('scry.cli.commands.')]\n"
        "extra = [m for m in loaded if not m.endswith('.version')]\n"
        "sys.exit(1 if extra else 0)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, f"extra command modules imported: {result.stdout}"


def test_the_cli_does_not_import_leaf_dependencies():
    code = (
        "import sys\n"
        "from scry.cli.main import main\n"
        "main(['--json', 'version'])\n"
        "heavy = [m for m in ('jinja2', 'networkx', 'textual') if m in sys.modules]\n"
        "sys.exit(1 if heavy else 0)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0
