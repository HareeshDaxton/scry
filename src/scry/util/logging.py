"""Logging setup for Scry.

Two rules shape everything here.

**Logs go to stderr, never stdout.** ``scry hotspots --json | jq`` has to emit
clean JSON; one log line on stdout corrupts it.

**We configure the ``scry`` logger, never the root logger.** If Scry is imported
as a library it must not hijack the host application's logging, and we do not
want third-party DEBUG output flooding our file. The ``scry`` logger is
configured with ``propagate = False`` so records stop here.

.. warning::

   ``RotatingFileHandler`` is **not multiprocess-safe**. Section 1.11 spawns
   worker processes; two of them rotating this file simultaneously will lose
   records or corrupt it, and Windows file locking makes that worse rather than
   better. The correct architecture is a ``QueueHandler`` in each worker and a
   single ``QueueListener`` in the parent performing the only real file write.
   That is deliberately not built yet — there are no workers until 1.11 — but
   **1.11 must add ``setup_worker_logging(queue)`` before spawning anything**.
   This notice is the signpost on that trap.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING

from scry.util.redact import RedactingFilter

if TYPE_CHECKING:  # pragma: no cover
    from scry.config import Config

ROOT_LOGGER_NAME = "scry"

_FILE_FORMAT = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
_CONSOLE_FORMAT = "%(levelname)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Handlers we installed are tagged, so idempotency and teardown act on *our*
# handlers rather than on "any handler at all".
#
# This is not a cosmetic distinction. Other parties legitimately attach handlers
# to the `scry` logger: pytest's logging plugin does exactly that whenever a
# logger has propagate=False, so caplog keeps working. A guard that skipped
# setup because *some* handler existed would silently leave Scry unconfigured
# under pytest — and equally in any host application that had attached its own
# handler. Tagging also means teardown never removes a handler we did not own.
_OWNED = "_scry_owned_handler"


def _is_ours(handler: logging.Handler) -> bool:
    return getattr(handler, _OWNED, False)


# Modules obtain loggers with `logging.getLogger(__name__)`, which yields names
# like "scry.agents.archivist" that nest under ROOT_LOGGER_NAME automatically.


def scry_home(env: Mapping[str, str] | None = None) -> Path:
    """Return Scry's data directory: ``$SCRY_HOME`` or ``~/.scry``.

    Logging needs a directory before section 1.4 defines the workspace path
    model, so the minimal resolution lives here and 1.4 builds its full model
    on top — the same seam ``errors.py`` used in section 1.2.
    """
    environ = env if env is not None else os.environ
    raw = environ.get("SCRY_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".scry"


def setup_logging(
    config: Config,
    *,
    log_dir: Path | None = None,
    console: bool = True,
    verbose: bool = False,
    force: bool = False,
) -> logging.Logger:
    """Configure the ``scry`` logger and return it.

    Args:
        config: supplies level, rotation size and backup count.
        log_dir: directory for ``scry.log``. Defaults to ``<scry_home>/logs``.
            Tests pass a temporary path so they never touch a real home
            directory.
        console: attach a stderr handler. The TUI (5.12) takes over the terminal
            and stray log lines would corrupt the display, so it passes False.
        verbose: force DEBUG on both handlers. Section 1.7 wires ``--verbose``.
        force: rebuild handlers even if already configured.

    Returns:
        The configured ``scry`` logger. Idempotent — calling this twice will not
        attach duplicate handlers and double every line.
    """
    logger = logging.getLogger(ROOT_LOGGER_NAME)

    if any(_is_ours(h) for h in logger.handlers) and not force:
        return logger
    for existing in [h for h in logger.handlers if _is_ours(h)]:
        logger.removeHandler(existing)
        existing.close()

    level = logging.DEBUG if verbose else logging.getLevelNamesMapping()[config.logging.level]
    logger.setLevel(level)
    logger.propagate = False

    # One filter instance shared by both handlers. Redaction is idempotent, so
    # running twice over the same record is harmless.
    redactor = RedactingFilter()

    directory = log_dir if log_dir is not None else scry_home() / "logs"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            directory / "scry.log",
            maxBytes=config.logging.max_bytes,
            backupCount=config.logging.backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(logging.Formatter(_FILE_FORMAT, datefmt=_DATE_FORMAT))
        file_handler.addFilter(redactor)
        setattr(file_handler, _OWNED, True)
        logger.addHandler(file_handler)
    except OSError as exc:
        # An unwritable log directory must not take the whole tool down. Say so
        # once on stderr and carry on with console logging only.
        print(f"scry: cannot write logs to {directory}: {exc}", file=sys.stderr)

    if console:
        console_handler = logging.StreamHandler(sys.stderr)
        # The file keeps a full INFO trail for diagnosis; the terminal shows
        # only what the user needs to act on, so a hotspot table is not buried
        # under progress chatter.
        console_handler.setLevel(logging.DEBUG if verbose else logging.WARNING)
        console_handler.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
        console_handler.addFilter(redactor)
        setattr(console_handler, _OWNED, True)
        logger.addHandler(console_handler)

    return logger


def reset_logging() -> None:
    """Detach and close the handlers Scry installed on the ``scry`` logger.

    Handlers belonging to anyone else — a host application's, or pytest's
    capture handler — are deliberately left alone. Tests use this for isolation,
    since logger objects are process-global and configuration would otherwise
    leak between cases.
    """
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    for handler in [h for h in logger.handlers if _is_ours(h)]:
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(logging.NOTSET)


def bootstrap(
    *,
    global_path: Path | None = None,
    workspace_path: Path | None = None,
    env: Mapping[str, str] | None = None,
    cli_overrides: Mapping[str, object] | None = None,
    log_dir: Path | None = None,
    console: bool = True,
    verbose: bool = False,
) -> tuple[Config, logging.Logger]:
    """Load configuration, configure logging, then replay buffered warnings.

    Resolves a genuine ordering problem. Logging needs config for its level and
    rotation settings, but config wants to report warnings — unknown keys, an
    ``api_key`` found in a file — and the logger does not exist yet.

    The sequence is therefore: load config with warnings buffered into a list,
    configure logging from the result, then replay the buffer into the logger so
    nothing is silently dropped. Making that explicit here keeps every caller
    from having to rediscover the ordering.
    """
    from scry.config import load_config

    buffered: list[str] = []
    config = load_config(
        global_path=global_path,
        workspace_path=workspace_path,
        env=env,
        cli_overrides=cli_overrides,
        on_warning=buffered.append,
    )
    logger = setup_logging(config, log_dir=log_dir, console=console, verbose=verbose)
    for message in buffered:
        logger.warning("%s", message)
    return config, logger
