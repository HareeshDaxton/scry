"""Tests for the exception taxonomy (section 1.3).

The point of putting exit codes on the exception classes is that a new error
type cannot silently fall through a mapping table someone forgot to update.
``test_every_error_type_has_a_usable_exit_code`` enforces exactly that by
discovering subclasses rather than listing them.
"""

from __future__ import annotations

from scry.util.errors import (
    AgentError,
    ConfigError,
    ExitCode,
    GitError,
    ScryError,
    SecurityError,
    StorageError,
    WorkspaceError,
)

ALL_ERRORS = (
    ConfigError,
    WorkspaceError,
    StorageError,
    GitError,
    AgentError,
    SecurityError,
)


def all_subclasses(cls) -> set[type]:
    found = set()
    for sub in cls.__subclasses__():
        found.add(sub)
        found |= all_subclasses(sub)
    return found


def test_every_error_derives_from_scry_error():
    for error in ALL_ERRORS:
        assert issubclass(error, ScryError)


def test_every_error_type_has_a_usable_exit_code():
    """Discovered, not listed — a future subclass is covered automatically."""
    for error in all_subclasses(ScryError):
        assert isinstance(error.exit_code, int)
        assert error.exit_code != ExitCode.OK, f"{error.__name__} would exit 0 on failure"


def test_default_exit_code_is_generic_failure():
    assert ScryError.exit_code == ExitCode.ERROR
    assert StorageError.exit_code == ExitCode.ERROR


def test_workspace_error_has_its_own_exit_code():
    """A script should tell 'no such workspace' from a general failure."""
    assert WorkspaceError.exit_code == ExitCode.WORKSPACE_NOT_FOUND


def test_security_error_has_the_guardrail_exit_code():
    """A guardrail refusal is a correct outcome, not a malfunction."""
    assert SecurityError.exit_code == ExitCode.GUARDRAIL


def test_exit_code_is_readable_from_an_instance():
    """Section 1.7 does `except ScryError as exc: return exc.exit_code`."""
    assert SecurityError("blocked").exit_code == ExitCode.GUARDRAIL


def test_exit_codes_are_distinct_where_they_should_be():
    assert (
        len(
            {
                ExitCode.OK,
                ExitCode.ERROR,
                ExitCode.USAGE,
                ExitCode.WORKSPACE_NOT_FOUND,
                ExitCode.GUARDRAIL,
            }
        )
        == 5
    )


# ---------------------------------------------------------------------------
# Context carried by individual types
# ---------------------------------------------------------------------------
def test_git_error_records_the_command_and_stderr():
    """A git failure with neither is nearly impossible to diagnose from a log."""
    error = GitError(
        "git log failed",
        command=["git", "log", "--numstat"],
        stderr="fatal: not a git repository\n",
    )
    assert error.command == ("git", "log", "--numstat")
    assert "git log --numstat" in str(error)
    assert "not a git repository" in str(error)


def test_git_error_is_fine_without_context():
    assert str(GitError("something went wrong")) == "something went wrong"


def test_workspace_error_includes_the_path():
    error = WorkspaceError("workspace not found", path="/tmp/nope")
    assert error.path == "/tmp/nope"
    assert "/tmp/nope" in str(error)


def test_storage_error_includes_the_database_path():
    error = StorageError("could not open session database", path="/tmp/session.db")
    assert "/tmp/session.db" in str(error)


def test_agent_error_names_the_agent():
    error = AgentError("crashed while parsing", agent="Archivist")
    assert error.agent == "Archivist"
    assert str(error).startswith("[Archivist]")


def test_config_error_still_renders_file_key_expected_and_got():
    """Section 1.2 behaviour must be unchanged by the taxonomy extension."""
    error = ConfigError(
        "skeptic.batch_size", source="/tmp/config.yaml", expected="integer >= 1", got="ten"
    )
    rendered = str(error)
    assert "/tmp/config.yaml: skeptic.batch_size" in rendered
    assert "expected  integer >= 1" in rendered
    assert "got       'ten'  (str)" in rendered


def test_errors_are_catchable_as_a_group():
    for error in ALL_ERRORS:
        try:
            raise error("boom") if error is not ConfigError else error("k", source="s")
        except ScryError:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"{error.__name__} was not caught as ScryError")
