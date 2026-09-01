"""Creating, finding and listing workspaces."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from scry.util.errors import WorkspaceError
from scry.workspace.ids import generate_workspace_id, validate_workspace_name
from scry.workspace.marker import VALID_MODES, WorkspaceMarker
from scry.workspace.paths import WorkspacePaths, workspaces_dir

WarningSink = Callable[[str], None]

# Ids embed 20 random bits, which is deliberately modest; collisions are handled
# by checking whether the directory already exists and trying again. Eight
# attempts failing in a row means something is wrong with the filesystem, not
# that we were unlucky.
MAX_ID_ATTEMPTS = 8


@dataclass(frozen=True)
class Workspace:
    """A workspace that exists on disk."""

    marker: WorkspaceMarker
    paths: WorkspacePaths

    @property
    def id(self) -> str:
        return self.marker.id

    @property
    def name(self) -> str:
        return self.marker.name

    @property
    def target_path(self) -> Path:
        return self.marker.target

    @property
    def root(self) -> Path:
        return self.paths.root


def _normcase(path: Path) -> Path:
    """Normalise a path for comparison, honouring the platform's case rules.

    ``os.path.normcase`` lowercases and unifies separators on Windows and is a
    no-op on POSIX, so ``E:\\Work\\Repo`` and ``e:\\work\\repo`` compare equal on
    Windows while staying distinct on Linux — which is how each filesystem
    actually behaves.
    """
    return Path(os.path.normcase(str(path)))


def _covers(target: Path, cwd: Path) -> bool:
    """True if ``cwd`` is ``target`` or lies inside it."""
    norm_target = _normcase(target)
    norm_cwd = _normcase(cwd)
    return norm_cwd == norm_target or norm_target in norm_cwd.parents


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------
def create_workspace(
    name: str,
    target_path: Path | str,
    *,
    mode: str = "auto",
    home: Path | None = None,
) -> Workspace:
    """Create a workspace for ``target_path`` and return it.

    The whole directory tree is built first and the marker written **last**, so
    an interrupted run leaves a tree with no marker — detectable debris rather
    than something that looks complete and is not.

    Nothing is written inside ``target_path``. The repository being analysed is
    only ever read.
    """
    validated_name = validate_workspace_name(name)

    if mode not in VALID_MODES:
        raise WorkspaceError(f"unknown mode {mode!r}. Choose one of: {', '.join(VALID_MODES)}")

    target = Path(target_path).expanduser()
    try:
        target = target.resolve(strict=True)
    except (OSError, FileNotFoundError) as exc:
        raise WorkspaceError(f"target path does not exist: {target}") from exc
    if not target.is_dir():
        raise WorkspaceError("target path is not a directory", path=target)

    for _ in range(MAX_ID_ATTEMPTS):
        workspace_id = generate_workspace_id(validated_name)
        paths = WorkspacePaths.for_id(workspace_id, home)
        if paths.root.exists():
            continue  # astronomically unlikely; regenerate rather than fail

        try:
            for directory in paths.directories():
                directory.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise WorkspaceError(f"could not create workspace: {exc}", path=paths.root) from exc

        marker = WorkspaceMarker(
            id=workspace_id,
            name=validated_name,
            target_path=str(target),
            mode=mode,
        )
        try:
            marker.write(paths.marker)
        except OSError as exc:
            raise WorkspaceError(
                f"could not write workspace marker: {exc}", path=paths.marker
            ) from exc

        return Workspace(marker=marker, paths=paths)

    raise WorkspaceError(
        f"could not allocate a unique workspace id after {MAX_ID_ATTEMPTS} attempts",
        path=workspaces_dir(home),
    )


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------
def iter_workspaces(
    home: Path | None = None,
    *,
    on_warning: WarningSink | None = None,
) -> Iterator[Workspace]:
    """Yield every complete workspace.

    Directories without a marker are skipped silently — they are incomplete
    creations, reported separately by :func:`find_incomplete_workspaces`. A
    directory whose marker is corrupt is reported through ``on_warning`` and
    skipped, so one damaged workspace cannot break a listing of the others.
    """
    root = workspaces_dir(home)
    if not root.is_dir():
        return

    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        paths = WorkspacePaths(root=entry)
        if not paths.marker.is_file():
            continue
        try:
            marker = WorkspaceMarker.read(paths.marker)
        except WorkspaceError as exc:
            if on_warning is not None:
                on_warning(str(exc))
            continue
        yield Workspace(marker=marker, paths=paths)


def list_workspaces(
    home: Path | None = None,
    *,
    on_warning: WarningSink | None = None,
) -> list[Workspace]:
    """Every complete workspace, oldest first, with a stable tiebreak."""
    return sorted(
        iter_workspaces(home, on_warning=on_warning),
        key=lambda w: (w.marker.created_at, w.id),
    )


def find_incomplete_workspaces(home: Path | None = None) -> list[Path]:
    """Directories that look like workspaces but have no marker.

    These are the residue of an interrupted creation. Section 1.9's ``scry
    doctor`` reports them.
    """
    root = workspaces_dir(home)
    if not root.is_dir():
        return []
    return [
        entry
        for entry in sorted(root.iterdir())
        if entry.is_dir() and not (entry / WorkspacePaths(root=entry).marker.name).is_file()
    ]


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def resolve_workspace(
    token: str | None = None,
    *,
    cwd: Path | None = None,
    home: Path | None = None,
) -> Workspace:
    """Find a workspace by id, by name, or by the current directory.

    Args:
        token: an exact workspace id (``legacy-monolith-a7f3k9m2``) or a bare
            name (``legacy-monolith``). When omitted, the current directory is
            matched against each workspace's recorded target path.
        cwd: directory to resolve from, defaulting to the process's own.
        home: Scry home, for tests.

    Raises:
        WorkspaceError: when nothing matches, or when a name is ambiguous. Both
            messages name the candidates, because silently picking one would
            answer questions about the wrong repository with no way to notice.
    """
    if token:
        return _resolve_token(token, home=home)
    return _resolve_from_cwd(Path.cwd() if cwd is None else Path(cwd), home=home)


def _resolve_token(token: str, *, home: Path | None) -> Workspace:
    # An exact directory match first. This also settles the shape ambiguity in
    # parse_workspace_id: a name like `my-projects` parses as id-shaped, but
    # unless a directory of that name exists we fall through to the name search.
    paths = WorkspacePaths.for_id(token, home)
    if paths.marker.is_file():
        return Workspace(marker=WorkspaceMarker.read(paths.marker), paths=paths)

    wanted = token.strip().lower()
    matches = [w for w in iter_workspaces(home) if w.name == wanted]

    if len(matches) == 1:
        return matches[0]

    if not matches:
        known = [w.id for w in iter_workspaces(home)]
        detail = (
            "\n  existing workspaces:\n" + "\n".join(f"    {i}" for i in known)
            if known
            else "\n  no workspaces exist yet. Create one with `scry init <name>`."
        )
        raise WorkspaceError(f"no workspace matches {token!r}{detail}")

    listed = "\n".join(f"    {w.id}  ->  {w.target_path}" for w in matches)
    raise WorkspaceError(f"{token!r} matches {len(matches)} workspaces. Use the full id:\n{listed}")


def _resolve_from_cwd(cwd: Path, *, home: Path | None) -> Workspace:
    candidates = [w for w in iter_workspaces(home) if _covers(w.target_path, cwd)]

    if not candidates:
        raise WorkspaceError(
            f"no workspace covers {cwd}.\n"
            f"  Create one with `scry init <name>`, or name an existing workspace "
            f"explicitly."
        )

    # Most specific wins: with workspaces for both E:\work and E:\work\repo, a
    # cwd inside the latter should resolve to the latter.
    candidates.sort(key=lambda w: len(str(_normcase(w.target_path))), reverse=True)
    return candidates[0]
