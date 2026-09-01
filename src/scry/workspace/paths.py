"""The directory layout of a single workspace.

Every path is derived from ``root``, so the layout is described in exactly one
place and a ``WorkspacePaths`` carries no state that could disagree with the
filesystem.

Nothing here points inside the repository being analysed. Scry writes only under
its own home directory — spec section 13.1 sets ``read_only_by_default``, and the
realistic case is a developer studying a repository they do not own and may not
have write access to.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from scry.storage.db import DATABASE_FILENAME
from scry.util.paths import scry_home

MARKER_FILENAME = ".scry"
WORKSPACES_DIRNAME = "workspaces"


def workspaces_dir(home: Path | None = None) -> Path:
    """Return ``<scry_home>/workspaces``."""
    return (home if home is not None else scry_home()) / WORKSPACES_DIRNAME


@dataclass(frozen=True)
class WorkspacePaths:
    """Every location inside one workspace, derived from its root."""

    root: Path

    @classmethod
    def for_id(cls, workspace_id: str, home: Path | None = None) -> WorkspacePaths:
        return cls(root=workspaces_dir(home) / workspace_id)

    @property
    def marker(self) -> Path:
        return self.root / MARKER_FILENAME

    @property
    def database(self) -> Path:
        """The single SQLite file: session state and knowledge graph together.

        Spec sections 6.1 and 11.2 show ``session.db`` and ``graph.db``
        separately. Section 1.5 merged them, because the analyses this product
        exists for are cross-domain joins — salience over churn and ownership and
        exposure, CR-9 over CVEs and the call graph — and those are ordinary SQL
        in one file and an ATTACH dance across two.
        """
        return self.root / DATABASE_FILENAME

    @property
    def config(self) -> Path:
        return self.root / "config.yaml"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def cache_ast(self) -> Path:
        return self.cache / "ast"

    @property
    def cache_embeddings(self) -> Path:
        return self.cache / "embeddings"

    @property
    def cache_git(self) -> Path:
        return self.cache / "git"

    @property
    def vector_store(self) -> Path:
        return self.root / "vector_store"

    @property
    def exports(self) -> Path:
        return self.root / "exports"

    @property
    def checkpoints(self) -> Path:
        return self.root / "checkpoints"

    def directories(self) -> tuple[Path, ...]:
        """Directories created when a workspace is made, parents before children.

        ``config.yaml`` is deliberately absent: section 1.2 established that a
        missing config file means "use the defaults", and that Scry never writes
        one as a side effect of being run.
        """
        return (
            self.root,
            self.cache,
            self.cache_ast,
            self.cache_embeddings,
            self.cache_git,
            self.vector_store,
            self.exports,
            self.checkpoints,
        )
