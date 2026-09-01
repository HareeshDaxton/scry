"""``scry init`` — create a workspace for a repository.

The first command that produces something durable, and where sections 1.4 and
1.5 meet: the workspace tree and marker, then the database.

Note the order. ``create_workspace`` writes the marker last, so the marker exists
before the database does, and a crash in between leaves a workspace with no
database. That is deliberate rather than a gap, because the two mean different
things. The marker is identity — written once, never rewritten, and its presence
defines a complete workspace. The database is a rebuildable cache that
``scry map`` fills from the repository and ``initialise_database`` can recreate
at any time. Section 1.7's resume already reports ``not initialised`` rather than
failing, and section 1.9's doctor will repair it. Strengthening the invariant
would buy little and would push SQLite knowledge into the workspace module.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from scry.cli.context import Context
from scry.cli.registry import RESERVED_COMMAND_NAMES
from scry.util.errors import ExitCode, WorkspaceError

# Spec section 5 documents `--mode lite|standard|pro`. Section 7 replaced cost
# tiers with detected backends, so anyone following the original spec or an older
# README will type one of these. A generic "invalid choice" would leave them
# guessing; naming the change costs one dictionary.
RETIRED_MODES: dict[str, str] = {
    "standard": "auto",
    "pro": "auto",
}


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "name",
        help="workspace name: 3-50 lowercase letters, digits and single hyphens",
    )
    parser.add_argument(
        "--target",
        metavar="PATH",
        default=None,
        help="repository to analyse (default: the current directory)",
    )
    parser.add_argument(
        "--mode",
        default="auto",
        metavar="MODE",
        help="backend preference: auto (default), lite, local or cloud",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="create an additional workspace even if one already targets this path",
    )


def run(args: argparse.Namespace, ctx: Context) -> int:
    from scry.workspace import VALID_MODES, create_workspace, validate_workspace_name

    console = ctx.console

    mode = str(args.mode).strip().lower()
    if mode in RETIRED_MODES:
        console.error(f"--mode {mode} is no longer used")
        console.hint("  Modes are now detected backends rather than cost tiers.")
        console.hint(f"  Use: {', '.join(VALID_MODES)}.")
        return ExitCode.USAGE
    if mode not in VALID_MODES:
        console.error(f"unknown mode {mode!r}")
        console.hint(f"  Use: {', '.join(VALID_MODES)}.")
        return ExitCode.USAGE

    # Validated here rather than left to create_workspace so a bad name reports
    # as a usage error. WorkspaceError carries the "workspace not found" exit
    # code, which would be a confusing thing to return for a typo in an argument.
    try:
        name = validate_workspace_name(args.name)
    except WorkspaceError as exc:
        console.error(str(exc))
        return ExitCode.USAGE

    if name in RESERVED_COMMAND_NAMES:
        console.error(f"{name!r} is a Scry command name and cannot be a workspace name")
        console.hint("  Commands take precedence, so the workspace would be unreachable by name.")
        return ExitCode.USAGE

    target = Path(args.target).expanduser() if args.target else Path.cwd()
    try:
        target = target.resolve(strict=True)
    except (OSError, FileNotFoundError):
        console.error(f"target path does not exist: {target}")
        return ExitCode.USAGE
    if not target.is_dir():
        console.error(f"target path is not a directory: {target}")
        return ExitCode.USAGE

    if (code := _check_duplicates(name, target, ctx, force=args.force)) is not None:
        return code

    _warn_about_git(target, ctx)

    workspace = create_workspace(name, target, mode=mode, home=ctx.home)
    version = _initialise_database(workspace, ctx)

    if ctx.json_output:
        ctx.console.json(
            {
                "id": workspace.id,
                "name": workspace.name,
                "mode": workspace.marker.mode,
                "target_path": str(workspace.target_path),
                "root": str(workspace.root),
                "database": str(workspace.paths.database),
                "created_at": workspace.marker.created_at,
                "schema_version": version,
            }
        )
        return ExitCode.OK

    _render(workspace, ctx)
    return ExitCode.OK


def _check_duplicates(name: str, target: Path, ctx: Context, *, force: bool) -> int | None:
    """Refuse a second workspace for the same repository; warn on a shared name.

    ``--force`` adds a workspace. It never overwrites or deletes one: a flag that
    could silently destroy completed analysis, with no undo, is not a flag worth
    having. Removing a workspace deserves its own command and its own
    confirmation.
    """
    from scry.workspace import iter_workspaces, same_path

    existing = list(iter_workspaces(ctx.home))
    duplicates = [w for w in existing if same_path(w.target_path, target)]

    if duplicates and not force:
        ctx.console.error(f"a workspace already targets {target}")
        for workspace in duplicates:
            ctx.console.hint(f"    {workspace.id}")
        ctx.console.hint("  Resume it by name, or pass --force to create another anyway.")
        return ExitCode.USAGE

    # A shared name is legitimate — two checkouts of similar projects — but it
    # makes `scry <name>` ambiguous, so say what that costs.
    if any(w.name == name for w in existing):
        ctx.console.warn(
            f"another workspace is already named {name!r}; "
            f"you will need the full id to resume either one"
        )
    return None


def _warn_about_git(target: Path, ctx: Context) -> None:
    """Phase 2 is entirely git-based, so say so — but do not refuse.

    Oracle (dependencies), Pathologist (secrets) and Semiotician (clones) all
    work without git, so a non-git target is degraded rather than useless.
    """
    if (target / ".git").exists():  # a file for worktrees, a directory otherwise
        return

    enclosing = next((p for p in target.parents if (p / ".git").exists()), None)
    if enclosing is not None:
        ctx.console.warn(f"{target} is not a repository root")
        ctx.console.hint(f"  It sits inside the repository at {enclosing}.")
        ctx.console.hint(f"  You may want: --target {enclosing}")
        return

    ctx.console.warn(f"{target} is not a git repository")
    ctx.console.hint("  History analysis will be unavailable; dependency and secret scanning")
    ctx.console.hint("  will still work.")


def _initialise_database(workspace, ctx: Context) -> int:
    from scry.storage import initialise_database

    return initialise_database(
        workspace.paths.database,
        on_warning=lambda message: (ctx.logger.warning("%s", message), ctx.console.warn(message)),
    )


def _render(workspace, ctx: Context) -> None:
    console = ctx.console
    ok = console.style("OK", "success", bold=True)
    mark = console.style("!", "warning", bold=True)

    mode = workspace.marker.mode
    described = "backend detected at run time" if mode == "auto" else "backend pinned"

    console.out(f"{ok}  Workspace created: {console.style(workspace.id, 'highlight', bold=True)}")
    console.out(f"{ok}  Mode: {mode} ({described})")
    console.out(f"{ok}  Target: {workspace.target_path}")
    console.out(f"{ok}  Storage: {workspace.root}")
    console.out(f"{ok}  Database: {workspace.paths.database}")
    console.out(f"{mark}  Save this id to return later:")
    console.out(f"     scry {workspace.id}")
