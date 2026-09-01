"""Diagnostic checks.

**No check may raise.** Every one runs inside a wrapper that turns an exception
into a failure naming it, because this command is the diagnostic of last resort
— it gets run precisely when something is already broken. A doctor that crashes
while diagnosing a broken install turns one confusing failure into two.

``SKIP`` exists so a check that genuinely cannot run on this platform says so,
rather than pretending to pass.
"""

from __future__ import annotations

import platform
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from scry.diagnostics.system import (
    BYTES_PER_GB,
    check_writable,
    cpu_count,
    free_disk_bytes,
    git_version,
    human_bytes,
    total_memory_bytes,
)
from scry.util.paths import scry_home

# Spec section 14.3's lite-mode floor, and section 19.5's disk target.
MINIMUM_PYTHON = (3, 11)
RECOMMENDED_MEMORY_BYTES = 4 * BYTES_PER_GB
RECOMMENDED_FREE_DISK_BYTES = 1 * BYTES_PER_GB

ENVIRONMENT = "Environment"
RESOURCES = "Resources"
STORAGE = "Storage"
BACKEND = "Backend"
WORKSPACES = "Workspaces"

GROUP_ORDER = (ENVIRONMENT, RESOURCES, STORAGE, BACKEND, WORKSPACES)


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True)
class CheckResult:
    group: str
    name: str
    status: Status
    detail: str
    remedy: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "group": self.group,
            "name": self.name,
            "status": str(self.status),
            "detail": self.detail,
            "remedy": self.remedy,
        }


@dataclass(frozen=True)
class Diagnosis:
    results: tuple[CheckResult, ...]

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.status is Status.FAIL)

    @property
    def warnings(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.status is Status.WARN)

    @property
    def healthy(self) -> bool:
        return not self.failures

    @property
    def status(self) -> Status:
        if self.failures:
            return Status.FAIL
        return Status.WARN if self.warnings else Status.OK


def _safe(group: str, name: str, check: Callable[[], object]) -> tuple[CheckResult, ...]:
    """Run a check, converting any exception into a failure rather than a crash."""
    try:
        outcome = check()
    except Exception as exc:
        return (
            CheckResult(
                group,
                name,
                Status.FAIL,
                f"the check itself failed: {type(exc).__name__}: {exc}",
                remedy="This is a bug in Scry. Please report it with this output.",
            ),
        )
    if isinstance(outcome, CheckResult):
        return (outcome,)
    return tuple(outcome)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
def check_python() -> CheckResult:
    current = sys.version_info[:3]
    detail = f"{'.'.join(map(str, current))} (requires >= {'.'.join(map(str, MINIMUM_PYTHON))})"
    if current[:2] < MINIMUM_PYTHON:
        return CheckResult(
            ENVIRONMENT,
            "python",
            Status.FAIL,
            detail,
            remedy=f"Install Python {'.'.join(map(str, MINIMUM_PYTHON))} or newer.",
        )
    return CheckResult(ENVIRONMENT, "python", Status.OK, detail)


def check_git() -> CheckResult:
    available, detail = git_version()
    if not available:
        return CheckResult(
            ENVIRONMENT,
            "git",
            Status.FAIL,
            detail,
            remedy="Install git and make sure it is on PATH. History analysis needs it.",
        )
    return CheckResult(ENVIRONMENT, "git", Status.OK, detail)


def check_platform() -> CheckResult:
    return CheckResult(
        ENVIRONMENT,
        "platform",
        Status.OK,
        f"{platform.system()} {platform.release()} ({platform.machine()})",
    )


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------
def check_memory() -> CheckResult:
    total = total_memory_bytes()
    if total is None:
        return CheckResult(
            RESOURCES, "memory", Status.SKIP, "could not be determined on this platform"
        )
    detail = f"{human_bytes(total)} (recommended >= {human_bytes(RECOMMENDED_MEMORY_BYTES)})"
    if total < RECOMMENDED_MEMORY_BYTES:
        return CheckResult(
            RESOURCES,
            "memory",
            Status.WARN,
            detail,
            remedy="Analysis will still run, but large repositories may be slow.",
        )
    return CheckResult(RESOURCES, "memory", Status.OK, human_bytes(total))


def check_cpus() -> CheckResult:
    count = cpu_count()
    return CheckResult(RESOURCES, "cpus", Status.OK, f"{count} logical")


def check_disk(home: Path) -> CheckResult:
    free = free_disk_bytes(home)
    if free is None:
        return CheckResult(RESOURCES, "disk", Status.SKIP, "could not be determined")
    detail = f"{human_bytes(free)} free at {home}"
    if free < RECOMMENDED_FREE_DISK_BYTES:
        return CheckResult(
            RESOURCES,
            "disk",
            Status.WARN,
            detail,
            remedy="A workspace can use hundreds of megabytes on a large repository.",
        )
    return CheckResult(RESOURCES, "disk", Status.OK, detail)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def check_home(home: Path) -> CheckResult:
    writable, detail = check_writable(home)
    if not writable:
        return CheckResult(
            STORAGE,
            "scry home",
            Status.FAIL,
            f"{home}: {detail}",
            remedy="Fix the permissions, or set SCRY_HOME to a writable directory.",
        )
    return CheckResult(STORAGE, "scry home", Status.OK, f"{home} ({detail})")


def check_log_directory(home: Path) -> CheckResult:
    writable, detail = check_writable(home / "logs")
    status = Status.OK if writable else Status.WARN
    remedy = None if writable else "Logging will be unavailable; Scry will otherwise work."
    return CheckResult(STORAGE, "log directory", status, f"{home / 'logs'} ({detail})", remedy)


def check_config(config_path: Path, env: Mapping[str, str] | None) -> CheckResult:
    """Report whether configuration loads, and any keys it did not recognise."""
    from scry.config import load_config
    from scry.util.errors import ConfigError

    if not config_path.exists():
        return CheckResult(STORAGE, "configuration", Status.OK, "using built-in defaults")

    warnings: list[str] = []
    try:
        load_config(global_path=config_path, env=env or {}, on_warning=warnings.append)
    except ConfigError as exc:
        return CheckResult(
            STORAGE,
            "configuration",
            Status.FAIL,
            str(exc).replace("\n", " "),
            remedy=f"Fix or remove {config_path}.",
        )

    if warnings:
        return CheckResult(
            STORAGE,
            "configuration",
            Status.WARN,
            f"{config_path} loaded with {len(warnings)} unrecognised key(s)",
            remedy=warnings[0],
        )
    return CheckResult(STORAGE, "configuration", Status.OK, str(config_path))


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------
def check_backend() -> CheckResult:
    """Which LLM backend is available.

    Reported as OK, never as a warning. Lite is a fully supported mode in which
    findings and their ranking are byte-identical to cloud mode and only the
    prose differs. Rendering a missing model as yellow would teach every user
    they are running a degraded product, which contradicts the whole design.

    Section 6.1 replaces the stub with real detection.
    """
    return CheckResult(
        BACKEND,
        "llm backend",
        Status.OK,
        "none detected - lite mode (full analysis, no generated prose)",
    )


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------
def check_workspaces(home: Path) -> list[CheckResult]:
    """Summarise the workspaces, and report each one that has a problem."""
    from scry.storage import current_version, journal_mode, latest_version, reader
    from scry.workspace import find_incomplete_workspaces, iter_workspaces

    results: list[CheckResult] = []
    marker_problems: list[str] = []
    workspaces = list(iter_workspaces(home, on_warning=marker_problems.append))

    results.append(CheckResult(WORKSPACES, "workspaces", Status.OK, f"{len(workspaces)} complete"))

    for incomplete in find_incomplete_workspaces(home):
        results.append(
            CheckResult(
                WORKSPACES,
                incomplete.name,
                Status.FAIL,
                "no .scry marker: creation was interrupted",
                remedy=f"Nothing here is recoverable. Remove it: {incomplete}",
            )
        )

    for problem in marker_problems:
        results.append(
            CheckResult(
                WORKSPACES,
                "marker",
                Status.FAIL,
                problem.replace("\n", " "),
                remedy="Remove the workspace and create it again.",
            )
        )

    for workspace in workspaces:
        if not workspace.target_path.exists():
            results.append(
                CheckResult(
                    WORKSPACES,
                    workspace.id,
                    Status.FAIL,
                    f"target no longer exists: {workspace.target_path}",
                    remedy="The repository was moved or deleted. Remove this workspace.",
                )
            )

        database = workspace.paths.database
        if not database.exists():
            results.append(
                CheckResult(
                    WORKSPACES,
                    workspace.id,
                    Status.FAIL,
                    "database is missing",
                    remedy="Run: scry doctor --repair",
                )
            )
            continue

        try:
            with reader(database) as connection:
                version = current_version(connection)
                mode = journal_mode(connection)
        except Exception as exc:
            results.append(
                CheckResult(
                    WORKSPACES,
                    workspace.id,
                    Status.FAIL,
                    f"database could not be opened: {exc}",
                    remedy="Delete the database and re-run the analysis to rebuild it.",
                )
            )
            continue

        if version > latest_version():
            results.append(
                CheckResult(
                    WORKSPACES,
                    workspace.id,
                    Status.FAIL,
                    f"database schema {version} is newer than this build understands "
                    f"({latest_version()})",
                    remedy="Upgrade Scry.",
                )
            )
        elif mode != "wal":
            results.append(
                CheckResult(
                    WORKSPACES,
                    workspace.id,
                    Status.WARN,
                    f"journal mode is {mode!r}, not WAL",
                    remedy="Concurrent analysis will contend. Common on network filesystems.",
                )
            )

    return results


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def run_checks(
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
    config_path: Path | None = None,
) -> Diagnosis:
    """Run every check and collect the results. Never raises."""
    resolved_home = home if home is not None else scry_home(env)
    resolved_config = config_path if config_path is not None else resolved_home / "config.yaml"

    results: list[CheckResult] = []
    results.extend(_safe(ENVIRONMENT, "python", check_python))
    results.extend(_safe(ENVIRONMENT, "git", check_git))
    results.extend(_safe(ENVIRONMENT, "platform", check_platform))
    results.extend(_safe(RESOURCES, "memory", check_memory))
    results.extend(_safe(RESOURCES, "cpus", check_cpus))
    results.extend(_safe(RESOURCES, "disk", lambda: check_disk(resolved_home)))
    results.extend(_safe(STORAGE, "scry home", lambda: check_home(resolved_home)))
    results.extend(_safe(STORAGE, "log directory", lambda: check_log_directory(resolved_home)))
    results.extend(_safe(STORAGE, "configuration", lambda: check_config(resolved_config, env)))
    results.extend(_safe(BACKEND, "llm backend", check_backend))
    results.extend(_safe(WORKSPACES, "workspaces", lambda: check_workspaces(resolved_home)))
    return Diagnosis(tuple(results))


def repair(home: Path | None = None, env: Mapping[str, str] | None = None) -> list[str]:
    """Fix what can be fixed by *adding*. Never deletes anything.

    Section 1.8 leaves a real gap open: a crash between creating the workspace
    and creating its database leaves a marker with no database. This closes it.

    Nothing here removes a file or directory. An incomplete workspace — one with
    no marker — is deliberately left alone and merely reported, because a
    diagnostic tool that can silently delete your analysis is not one to trust
    running when you are already confused.
    """
    from scry.storage import initialise_database
    from scry.workspace import iter_workspaces

    resolved_home = home if home is not None else scry_home(env)
    actions: list[str] = []

    for workspace in iter_workspaces(resolved_home):
        for directory in workspace.paths.directories():
            if not directory.exists():
                directory.mkdir(parents=True, exist_ok=True)
                actions.append(f"created missing directory {directory}")

        if not workspace.paths.database.exists():
            initialise_database(workspace.paths.database)
            actions.append(f"created missing database for {workspace.id}")

    return actions
