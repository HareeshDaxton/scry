"""``scry <name>-<uid>`` — resume a workspace.

Spec section 5 makes a bare workspace token a command in its own right. It has no
name of its own, so the router dispatches here whenever the first argument is not
a known command but does identify a workspace.

This is also the first path that runs the whole stack end to end: the router
resolves the workspace with section 1.4, then reads its session state from the
database section 1.5 created.
"""

from __future__ import annotations

import argparse

from scry.cli.context import Context


def run(args: argparse.Namespace, ctx: Context) -> int:
    from scry.storage import reader
    from scry.workspace import resolve_workspace

    workspace = resolve_workspace(args.token, home=ctx.home)
    state = _session_state(workspace, reader)

    if ctx.json_output:
        ctx.console.json(
            {
                "id": workspace.id,
                "name": workspace.name,
                "target_path": str(workspace.target_path),
                "created_at": workspace.marker.created_at,
                "mode": workspace.marker.mode,
                "root": str(workspace.root),
                "session": state,
            }
        )
        return 0

    console = ctx.console
    console.out(console.style(workspace.id, "highlight", bold=True))
    console.out(f"  target     {workspace.target_path}")
    console.out(f"  workspace  {workspace.root}")
    console.out(f"  created    {workspace.marker.created_at}")
    console.out(f"  mode       {workspace.marker.mode}")

    if state is None:
        console.out(f"  status     {console.style('not initialised', 'warning')}")
        ctx.logger.info("workspace %s has no database yet", workspace.id)
        return 0

    console.out(f"  status     {state['status']}")
    console.out(f"  llm calls  {state['llm_calls_used']}")
    if state["last_analyzed_commit"]:
        console.out(f"  analysed   {state['last_analyzed_commit']}")
    return 0


def _session_state(workspace, reader) -> dict | None:
    """Mutable state from the database, or None when it does not exist yet.

    A workspace can legitimately exist without a database: section 1.4 writes the
    marker, and only ``scry init`` (1.8) creates the file. Reporting that plainly
    beats failing on a workspace that is merely young.
    """
    if not workspace.paths.database.exists():
        return None
    with reader(workspace.paths.database) as connection:
        row = connection.execute(
            "SELECT status, llm_calls_used, llm_cost_usd, last_analyzed_commit"
            " FROM session_state WHERE id = 1"
        ).fetchone()
    return dict(row) if row is not None else None
