"""Tests for logging setup (section 1.3).

The load-bearing test in this file is
``test_secret_from_a_deep_child_logger_is_redacted``. It proves the redaction
filter is attached to handlers rather than loggers — the placement that actually
works. Attached to a logger, the filter would scrub records from ``scry`` itself
and silently miss every record from every agent, while looking perfectly
installed.
"""

from __future__ import annotations

import dataclasses
import logging
import sys

import pytest

from scry.config import load_config
from scry.util.logging import ROOT_LOGGER_NAME, bootstrap, reset_logging, scry_home, setup_logging

# Assembled rather than written as a literal, for the reason documented at the
# top of tests/test_redact.py: a credential-shaped literal in committed source
# is rejected outright by GitHub push protection.
AWS_KEY = "AKIA" + ("EXAMPLEONLY" + "0" * 16)[:16]
OPENAI_KEY = "sk-proj-" + ("EXAMPLEONLY" + "0" * 24)[:24]


@pytest.fixture(autouse=True)
def isolated_logging():
    """Logger objects are process-global; without this, config leaks between tests."""
    reset_logging()
    yield
    reset_logging()


@pytest.fixture
def config():
    return load_config(env={})


def read_log(log_dir) -> str:
    return (log_dir / "scry.log").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# scry_home
# ---------------------------------------------------------------------------
def test_scry_home_defaults_to_dot_scry_in_the_user_home():
    assert scry_home(env={}).name == ".scry"


def test_scry_home_honours_the_environment_override(tmp_path):
    assert scry_home(env={"SCRY_HOME": str(tmp_path)}) == tmp_path


# ---------------------------------------------------------------------------
# Handler wiring
# ---------------------------------------------------------------------------
def test_creates_the_log_file(config, tmp_path):
    logger = setup_logging(config, log_dir=tmp_path, console=False)
    logger.info("hello")
    assert (tmp_path / "scry.log").exists()
    assert "hello" in read_log(tmp_path)


def test_does_not_configure_the_root_logger(config, tmp_path):
    """Scry must not hijack logging for an application that imports it."""
    before = list(logging.getLogger().handlers)
    setup_logging(config, log_dir=tmp_path, console=False)
    assert logging.getLogger().handlers == before


def test_scry_logger_does_not_propagate(config, tmp_path):
    setup_logging(config, log_dir=tmp_path, console=False)
    assert logging.getLogger(ROOT_LOGGER_NAME).propagate is False


def test_setup_is_idempotent(config, tmp_path, capsys):
    """Calling twice must not double every line, nor reconfigure behind our back."""
    logger = setup_logging(config, log_dir=tmp_path, console=False)
    setup_logging(config, log_dir=tmp_path, console=True)  # no force: a no-op
    logger.warning("logged once")

    assert read_log(tmp_path).count("logged once") == 1
    assert capsys.readouterr().err == "", "the second call should not have added a console"


def test_force_rebuilds_handlers(config, tmp_path, capsys):
    setup_logging(config, log_dir=tmp_path, console=False)
    logger = setup_logging(config, log_dir=tmp_path, console=True, force=True)
    logger.warning("now on the console")
    assert "now on the console" in capsys.readouterr().err


def test_console_can_be_disabled_for_the_tui(config, tmp_path, capsys):
    """The TUI takes over the terminal; stray log lines would corrupt it."""
    logger = setup_logging(config, log_dir=tmp_path, console=False)
    logger.warning("must not reach the terminal")
    assert capsys.readouterr().err == ""
    assert "must not reach the terminal" in read_log(tmp_path)


def test_configures_even_when_a_foreign_handler_is_already_attached(config, tmp_path):
    """Regression: idempotency must key on *our* handlers, not on any handler.

    Other parties legitimately attach handlers to the `scry` logger — pytest's
    logging plugin does it for any non-propagating logger. A guard that skipped
    setup because some handler existed left Scry silently unconfigured, both
    under pytest and in any host application with its own handler.
    """
    foreign = logging.NullHandler()
    logging.getLogger(ROOT_LOGGER_NAME).addHandler(foreign)
    try:
        logger = setup_logging(config, log_dir=tmp_path, console=False)
        logger.info("configured anyway")
        assert "configured anyway" in read_log(tmp_path)
    finally:
        logging.getLogger(ROOT_LOGGER_NAME).removeHandler(foreign)


def test_reset_leaves_foreign_handlers_alone(config, tmp_path):
    """Teardown must never remove a handler Scry did not install."""
    foreign = logging.NullHandler()
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    logger.addHandler(foreign)
    try:
        setup_logging(config, log_dir=tmp_path, console=False)
        reset_logging()
        assert foreign in logger.handlers
    finally:
        logger.removeHandler(foreign)


def test_an_unwritable_log_directory_does_not_kill_the_tool(config, tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("I am a file, not a directory", encoding="utf-8")
    logger = setup_logging(config, log_dir=blocker / "logs", console=True)
    logger.warning("still working")  # must not raise


# ---------------------------------------------------------------------------
# Streams
# ---------------------------------------------------------------------------
def test_console_output_goes_to_stderr_and_stdout_stays_clean(config, tmp_path, capsys):
    """`scry hotspots --json | jq` must not find log lines in its input."""
    logger = setup_logging(config, log_dir=tmp_path, console=True)
    logger.warning("a warning")
    captured = capsys.readouterr()
    assert captured.out == "", "logs must never reach stdout"
    assert "a warning" in captured.err


def test_console_is_quiet_below_warning(config, tmp_path, capsys):
    """The file keeps a full trail; the terminal shows only what needs action."""
    logger = setup_logging(config, log_dir=tmp_path, console=True)
    logger.info("routine progress")
    assert "routine progress" not in capsys.readouterr().err
    assert "routine progress" in read_log(tmp_path)


def test_verbose_puts_debug_on_both_handlers(config, tmp_path, capsys):
    logger = setup_logging(config, log_dir=tmp_path, console=True, verbose=True)
    logger.debug("detailed trace")
    assert "detailed trace" in capsys.readouterr().err
    assert "detailed trace" in read_log(tmp_path)


# ---------------------------------------------------------------------------
# Levels and rotation
# ---------------------------------------------------------------------------
def test_level_comes_from_config(tmp_path):
    config = load_config(env={"SCRY_LOG_LEVEL": "ERROR"})
    logger = setup_logging(config, log_dir=tmp_path, console=False)
    logger.warning("should not appear")
    logger.error("should appear")
    contents = read_log(tmp_path)
    assert "should not appear" not in contents
    assert "should appear" in contents


def test_debug_level_from_the_environment_reaches_the_logger(tmp_path):
    config = load_config(env={"SCRY_LOG_LEVEL": "DEBUG"})
    logger = setup_logging(config, log_dir=tmp_path, console=False)
    logger.debug("trace detail")
    assert "trace detail" in read_log(tmp_path)


def test_rotation_produces_a_backup_file(config, tmp_path):
    small = dataclasses.replace(
        config, logging=dataclasses.replace(config.logging, max_bytes=1024, backup_count=2)
    )
    logger = setup_logging(small, log_dir=tmp_path, console=False)
    for i in range(200):
        logger.info("padding line %d %s", i, "x" * 40)
    assert (tmp_path / "scry.log.1").exists(), "rotation did not trigger at the size limit"


# ---------------------------------------------------------------------------
# Redaction, end to end
# ---------------------------------------------------------------------------
def test_secret_never_reaches_the_log_file(config, tmp_path):
    logger = setup_logging(config, log_dir=tmp_path, console=False)
    logger.info("authenticating with %s", AWS_KEY)
    contents = read_log(tmp_path)
    assert AWS_KEY not in contents
    assert "<REDACTED_aws_key>" in contents


def test_secret_from_a_deep_child_logger_is_redacted(config, tmp_path):
    """The test that proves handler-level attachment.

    A filter on the `scry` logger would not run for this record at all: it
    propagates to `scry`'s *handlers*, but `scry`'s *filters* are never
    consulted for a child's record.
    """
    setup_logging(config, log_dir=tmp_path, console=False)
    child = logging.getLogger("scry.agents.pathologist.secrets")
    child.warning("found key %s in config/legacy/auth.yaml", AWS_KEY)

    contents = read_log(tmp_path)
    assert AWS_KEY not in contents
    assert "<REDACTED_aws_key>" in contents
    assert "config/legacy/auth.yaml" in contents, "location must survive; only the value goes"


def test_secret_in_a_traceback_never_reaches_the_log_file(config, tmp_path):
    logger = setup_logging(config, log_dir=tmp_path, console=False)
    try:
        raise ValueError(f"bad credential {AWS_KEY}")
    except ValueError:
        logger.exception("authentication failed")
    assert AWS_KEY not in read_log(tmp_path)


def test_secret_never_reaches_the_console_either(config, tmp_path, capsys):
    logger = setup_logging(config, log_dir=tmp_path, console=True)
    logger.error("token %s rejected", AWS_KEY)
    assert AWS_KEY not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------
def test_bootstrap_returns_config_and_logger(tmp_path):
    config, logger = bootstrap(env={}, log_dir=tmp_path, console=False)
    assert config.skeptic.batch_size == 10
    assert logger.name == ROOT_LOGGER_NAME


def test_bootstrap_replays_config_warnings_into_the_log(tmp_path):
    """Config warns before the logger exists; nothing may be silently dropped."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("skeptic:\n  challange_threshold: 0.9\n", encoding="utf-8")

    bootstrap(global_path=config_file, env={}, log_dir=tmp_path, console=False)

    contents = read_log(tmp_path)
    assert "challange_threshold" in contents
    assert "unknown configuration key" in contents


def test_bootstrap_does_not_leak_an_api_key_found_in_a_config_file(tmp_path):
    """The warning about a misplaced key must not itself log the key."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        f"llm:\n  openai:\n    api_key: {OPENAI_KEY}\n",
        encoding="utf-8",
    )
    bootstrap(global_path=config_file, env={}, log_dir=tmp_path, console=False)

    contents = read_log(tmp_path)
    assert OPENAI_KEY not in contents
    assert "keyring" in contents


def test_bootstrap_honours_verbose(tmp_path, capsys):
    _, logger = bootstrap(env={}, log_dir=tmp_path, console=True, verbose=True)
    logger.debug("verbose trace")
    assert "verbose trace" in capsys.readouterr().err


def test_module_loggers_nest_under_the_scry_logger():
    """Modules use logging.getLogger(__name__); those names must nest correctly."""
    assert logging.getLogger("scry.agents.archivist").parent.name.startswith(ROOT_LOGGER_NAME)


def test_logging_module_does_not_write_to_stdout_on_import(capsys):
    assert capsys.readouterr().out == ""
    assert sys.stdout is not None
