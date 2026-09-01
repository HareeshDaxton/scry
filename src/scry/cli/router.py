"""Argument parsing, dispatch, and the translation of failures into exit codes."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from scry import __version__
from scry.cli.colors import supports_color
from scry.cli.context import Context
from scry.cli.output import Console
from scry.cli.registry import COMMANDS, RESUME_MODULE, Command, find_command, load_module, suggest
from scry.util.errors import ExitCode, ScryError
from scry.util.paths import scry_home

PROGRAM = "scry"

# SIGINT's conventional exit code: 128 + 2. Scripts wrapping us can tell a
# deliberate Ctrl+C from a failure.
EXIT_INTERRUPTED = 130

USAGE_EPILOGUE = """\
Resume a workspace by naming it:

  scry <name>-<uid>          resume by id
  scry <name>                resume by name, when unambiguous

Run 'scry <command> --help' for a command's own options.
"""


class _ParserExitError(Exception):
    """Raised instead of calling sys.exit when a command's parser rejects input."""

    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(status)


class CommandParser(argparse.ArgumentParser):
    """An ArgumentParser that returns control instead of killing the process.

    ``argparse`` calls ``sys.exit()`` on a parse error and writes to the real
    ``sys.stderr``. Both are wrong here: ``main(argv)`` is documented to *return*
    an exit code, and the Console owns every character the user sees so that
    tests can capture output and ``--no-color`` is honoured. Overriding these two
    hooks routes argparse's own messages through the same path as everything
    else, and turns its exit into an ordinary return value.
    """

    def __init__(self, *args: Any, console: Console, **kwargs: Any) -> None:
        self._console = console
        super().__init__(*args, **kwargs)

    def _print_message(self, message: str | None, file: Any = None) -> None:
        if message:
            self._console.err(message.rstrip())

    def exit(self, status: int = 0, message: str | None = None) -> None:  # type: ignore[override]
        if message:
            self._print_message(message)
        raise _ParserExitError(status)


def build_global_parser() -> argparse.ArgumentParser:
    """Flags that apply to every command, wherever they appear on the line."""
    parser = argparse.ArgumentParser(prog=PROGRAM, add_help=False)
    parser.add_argument("-h", "--help", action="store_true", dest="show_help")

    # Spec section 5 assigns -v to --version. That is inverted here on purpose:
    # across essentially the whole Unix toolchain -v means verbose, and a
    # developer debugging a failing run will type it by reflex. Getting a version
    # string back, with no change in behaviour, is a trap set for exactly the
    # user this tool is built for. Both long spellings still work in full.
    parser.add_argument("-V", "--version", action="store_true", dest="show_version")
    parser.add_argument("-v", "--verbose", action="store_true")

    parser.add_argument("--config", metavar="PATH", default=None)
    parser.add_argument("--no-color", action="store_true", dest="no_color")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def render_help(console: Console, commands: Sequence[Command] = COMMANDS) -> None:
    # Deliberately ASCII. Console encodings vary on Windows, and a decorative
    # em dash is not worth a UnicodeEncodeError on somebody's legacy code page.
    console.out(f"{PROGRAM} {__version__} - terminal-native software archaeology")
    console.out()
    console.out(f"Usage: {PROGRAM} [OPTIONS] [COMMAND]")
    console.out()
    console.out("Options:")
    console.out("  -V, --version      Show version information")
    console.out("  -h, --help         Show this message")
    console.out("  -v, --verbose      Enable debug logging and full tracebacks")
    console.out("      --config PATH  Use a specific configuration file")
    console.out("      --no-color     Disable coloured output")
    console.out("      --json         Emit machine-readable output")
    console.out()
    console.out("Commands:")
    width = max((len(c.name) for c in commands), default=0)
    for command in sorted(commands, key=lambda c: c.name):
        console.out(f"  {command.name.ljust(width)}   {command.summary}")
    console.out()
    for line in USAGE_EPILOGUE.rstrip().splitlines():
        console.out(line)


def _split_argv(argv: Sequence[str]) -> tuple[list[str], str | None, list[str], str | None]:
    """Separate global flags from the command and its own arguments.

    Returns ``(global_argv, command_token, command_argv, error)``. The command
    must precede its own flags: taking the first non-flag token anywhere would
    misread ``--limit 5 hotspots`` as the command being ``5``.
    """
    parser = build_global_parser()
    try:
        _, rest = parser.parse_known_args(list(argv))
    except SystemExit:  # argparse rejected a global flag's value
        return [], None, [], "invalid option"

    if not rest:
        return list(argv), None, [], None
    if rest[0].startswith("-"):
        return list(argv), None, [], f"unrecognised option {rest[0]!r}"
    return list(argv), rest[0], rest[1:], None


def _looks_like_workspace_reference(token: str, home: Path | None) -> bool:
    """Whether a token that is not a command should be treated as a workspace.

    An id-shaped token always is. A bare name only counts when a workspace of
    that name actually exists - otherwise a mistyped command would be reported
    as a missing workspace, which explains nothing.
    """
    from scry.workspace import looks_like_workspace_id

    if looks_like_workspace_id(token):
        return True
    try:
        from scry.workspace import iter_workspaces

        return any(w.name == token.strip().lower() for w in iter_workspaces(home))
    except OSError:  # pragma: no cover - unreadable home
        return False


def _build_context(
    options: argparse.Namespace,
    *,
    env: Mapping[str, str],
    home: Path | None,
    stdout: TextIO | None,
    stderr: TextIO | None,
) -> Context:
    """Load configuration, configure logging, and assemble the handler context.

    The three steps are done here rather than through ``bootstrap()`` so the
    router controls where warnings surface. Logging is configured with
    ``console=False`` and the Console owns every character the user sees; a log
    handler on stderr as well would print each config warning twice and, worse,
    would put an unexpected exception's whole traceback on the terminal.
    """
    from scry.config import load_config
    from scry.util.logging import setup_logging

    overrides: dict[str, Any] = {}
    if options.no_color:
        overrides["tui.color"] = False

    warnings: list[str] = []
    config_path = (
        Path(options.config) if options.config else (home or scry_home(env)) / "config.yaml"
    )
    config = load_config(
        global_path=config_path,
        env=env,
        cli_overrides=overrides,
        on_warning=warnings.append,
    )

    logger = setup_logging(
        config,
        log_dir=(home or scry_home(env)) / "logs",
        console=False,
        verbose=options.verbose,
    )

    color = config.tui.color and supports_color(
        stdout if stdout is not None else sys.stdout,
        env=env,
        force_off=options.no_color or options.json_output,
    )
    console = Console(stdout=stdout, stderr=stderr, color=color, json_mode=options.json_output)

    for message in warnings:
        logger.warning("%s", message)
        console.warn(message)

    return Context(
        config=config,
        logger=logger,
        console=console,
        home=home,
        json_output=options.json_output,
        verbose=options.verbose,
    )


def run(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Parse, dispatch, and return a process exit code.

    Every keyword argument exists so tests can drive the whole CLI without
    touching a real home directory, the real environment, or the real streams.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    environ = os.environ if env is None else env

    global_argv, token, command_argv, split_error = _split_argv(argv)
    parser = build_global_parser()
    try:
        options, _ = parser.parse_known_args(global_argv)
    except SystemExit:
        options = parser.parse_known_args([])[0]
        split_error = split_error or "invalid option"

    ctx = _build_context(options, env=environ, home=home, stdout=stdout, stderr=stderr)

    try:
        if split_error is not None:
            ctx.console.error(split_error)
            ctx.console.hint(f"Run '{PROGRAM} --help' to see available options.")
            return ExitCode.USAGE

        if token is None:
            if options.show_version:
                return _emit_version(ctx)
            # Bare `scry` prints help rather than an error: for a tool whose
            # whole point is discovery, the first run should show the way in.
            render_help(ctx.console)
            return ExitCode.OK

        return _dispatch(token, command_argv, ctx, show_help=options.show_help)

    except _ParserExitError as exc:
        return exc.status
    except ScryError as exc:
        # Already explained: print it, record it, and use its own exit code so a
        # caller can tell "no such workspace" from a general failure.
        ctx.logger.debug("command failed: %s", exc, exc_info=True)
        ctx.console.error(str(exc))
        return exc.exit_code
    except KeyboardInterrupt:
        ctx.console.err("")
        ctx.console.hint("interrupted")
        return EXIT_INTERRUPTED
    except Exception:
        # We crashed. A wall of traceback tells the target user nothing
        # actionable, so it goes to the log and they get a pointer to it.
        ctx.logger.exception("unexpected failure")
        if ctx.verbose:
            raise
        ctx.console.error("unexpected error")
        ctx.console.hint("Details written to the log. Re-run with --verbose to see them here.")
        return ExitCode.ERROR


def _emit_version(ctx: Context) -> int:
    module = load_module("scry.cli.commands.version")
    return int(module.run(argparse.Namespace(), ctx))


def _dispatch(token: str, command_argv: list[str], ctx: Context, *, show_help: bool) -> int:
    command = find_command(token)

    if command is not None:
        module = load_module(command.module)
        parser = CommandParser(
            prog=f"{PROGRAM} {command.name}",
            description=command.summary,
            console=ctx.console,
        )
        if hasattr(module, "add_arguments"):
            module.add_arguments(parser)
        if show_help:
            ctx.console.out(parser.format_help().rstrip())
            return ExitCode.OK
        args = parser.parse_args(command_argv)
        return int(module.run(args, ctx))

    # Not a command. Commands win on a name collision - a command shadowed by
    # data is far more surprising than the reverse, and the full id form is
    # always available as an unambiguous escape hatch.
    if _looks_like_workspace_reference(token, ctx.home):
        module = load_module(RESUME_MODULE)
        return int(module.run(argparse.Namespace(token=token), ctx))

    ctx.console.error(f"unknown command {token!r}")
    close = suggest(token)
    if close:
        ctx.console.hint(f"Did you mean: {', '.join(close)}?")
    ctx.console.hint(f"Run '{PROGRAM} --help' to see available commands.")
    return ExitCode.USAGE


__all__ = [
    "EXIT_INTERRUPTED",
    "PROGRAM",
    "CommandParser",
    "build_global_parser",
    "render_help",
    "run",
]
