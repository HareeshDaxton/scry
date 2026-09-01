"""``scry doctor`` — diagnose the install, and fix what can be fixed by adding.

Written for two readers. The person whose Scry is not working, and us reading
their bug report: ``scry doctor --json`` in one paste tells us the whole
environment instead of four rounds of "what Python? is git on PATH? how much
RAM?".
"""

from __future__ import annotations

import argparse

from scry.cli.context import Context
from scry.diagnostics import GROUP_ORDER, Diagnosis, Status, repair, run_checks
from scry.util.errors import ExitCode

# Warnings are informational: "3.9 GB of RAM, below the recommended 4" should not
# break somebody's CI. Only a genuine fault fails.
_STATUS_STYLE = {
    Status.OK: ("OK  ", "success"),
    Status.WARN: ("WARN", "warning"),
    Status.FAIL: ("FAIL", "error"),
    Status.SKIP: ("--  ", "muted"),
}


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repair",
        action="store_true",
        help="fix what can be fixed by adding; never deletes anything",
    )


def run(args: argparse.Namespace, ctx: Context) -> int:
    config_path = ctx.config_path
    repaired: list[str] = []

    if args.repair:
        repaired = repair(home=ctx.home)
        for action in repaired:
            ctx.logger.info("repair: %s", action)

    diagnosis = run_checks(home=ctx.home, config_path=config_path)

    if ctx.json_output:
        ctx.console.json(
            {
                "status": str(diagnosis.status),
                "repaired": repaired,
                "checks": [result.as_dict() for result in diagnosis.results],
            }
        )
    else:
        _render(diagnosis, repaired, ctx)

    return ExitCode.OK if diagnosis.healthy else ExitCode.ERROR


def _render(diagnosis: Diagnosis, repaired: list[str], ctx: Context) -> None:
    console = ctx.console

    if repaired:
        console.out(console.style("Repaired", "success", bold=True))
        for action in repaired:
            console.out(f"  {action}")
        console.out()

    width = max((len(r.name) for r in diagnosis.results), default=0)

    for group in GROUP_ORDER:
        results = [r for r in diagnosis.results if r.group == group]
        if not results:
            continue
        console.out(console.style(group, "highlight", bold=True))
        for result in results:
            label, kind = _STATUS_STYLE[result.status]
            console.out(
                f"  {console.style(label, kind)}  {result.name.ljust(width)}  {result.detail}"
            )
            if result.remedy and result.status is not Status.OK:
                console.out(f"        {console.style('-> ' + result.remedy, 'muted')}")
        console.out()

    failures = len(diagnosis.failures)
    warnings = len(diagnosis.warnings)

    if failures:
        console.out(console.style(f"{failures} problem(s) found.", "error", bold=True))
    elif warnings:
        console.out(console.style(f"No problems. {warnings} warning(s).", "warning"))
    else:
        console.out(console.style("No problems found.", "success", bold=True))
