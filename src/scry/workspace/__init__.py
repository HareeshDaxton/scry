"""Workspaces: Scry's private working directory for one analysed repository.

A workspace lives under ``~/.scry/workspaces/``, never inside the repository it
points at. Everything Scry produces — databases, caches, exports, checkpoints —
lands there, so the repository under analysis is only ever read.

Public API::

    from scry.workspace import create_workspace, resolve_workspace, list_workspaces

    ws = create_workspace("legacy-monolith", "E:/work/legacy-monolith")
    ws.id                                  # legacy-monolith-a7f3k9m2
    ws.paths.exports                       # where the Onboarding Brief lands

    resolve_workspace("legacy-monolith-a7f3k9m2")   # by id
    resolve_workspace("legacy-monolith")            # by name, if unambiguous
    resolve_workspace()                             # by current directory
"""

from scry.workspace.ids import (
    MAX_NAME_LENGTH,
    MIN_NAME_LENGTH,
    RESERVED_NAMES,
    UID_LENGTH,
    generate_uid,
    generate_workspace_id,
    looks_like_workspace_id,
    parse_workspace_id,
    validate_workspace_name,
)
from scry.workspace.manager import (
    Workspace,
    create_workspace,
    find_incomplete_workspaces,
    iter_workspaces,
    list_workspaces,
    resolve_workspace,
    same_path,
)
from scry.workspace.marker import MARKER_SCHEMA_VERSION, VALID_MODES, WorkspaceMarker
from scry.workspace.paths import MARKER_FILENAME, WorkspacePaths, workspaces_dir

__all__ = [
    "MARKER_FILENAME",
    "MARKER_SCHEMA_VERSION",
    "MAX_NAME_LENGTH",
    "MIN_NAME_LENGTH",
    "RESERVED_NAMES",
    "UID_LENGTH",
    "VALID_MODES",
    "Workspace",
    "WorkspaceMarker",
    "WorkspacePaths",
    "create_workspace",
    "find_incomplete_workspaces",
    "generate_uid",
    "generate_workspace_id",
    "iter_workspaces",
    "list_workspaces",
    "looks_like_workspace_id",
    "parse_workspace_id",
    "resolve_workspace",
    "same_path",
    "validate_workspace_name",
    "workspaces_dir",
]
