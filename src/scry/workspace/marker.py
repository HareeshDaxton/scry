"""The ``.scry`` marker file.

The marker records which workspace this is and what it points at. Its presence
is the definition of a complete workspace: creation builds the whole directory
tree and writes the marker **last**, so a tree without one is detectable debris
from an interrupted run rather than something that looks real and is not.

Written as JSON, not YAML. ``config.yaml`` is edited by people and so gets a
forgiving human format; the marker is written by Scry and read by Scry, never
hand-edited, so it gets a format with no surprising type coercion.

**The marker is written once and never updated.** Anything mutable — last
accessed, session status, budget spent — belongs in ``session.db`` (section 1.5).
A file rewritten on every run is a file a crash can corrupt on every run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scry.util.errors import WorkspaceError

MARKER_SCHEMA_VERSION = 1

# `auto` means "detect the best available backend at run time", which is what
# spec section 7 requires: modes are detected capability tiers, not a property
# fixed when the workspace was created. The others pin a backend explicitly.
VALID_MODES = ("auto", "lite", "local", "cloud")


def utc_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class WorkspaceMarker:
    """Immutable identity of one workspace."""

    id: str
    name: str
    target_path: str
    created_at: str = field(default_factory=utc_timestamp)
    mode: str = "auto"
    schema_version: int = MARKER_SCHEMA_VERSION

    def to_json(self) -> str:
        return (
            json.dumps(
                {
                    "schema_version": self.schema_version,
                    "id": self.id,
                    "name": self.name,
                    "target_path": self.target_path,
                    "created_at": self.created_at,
                    "mode": self.mode,
                },
                indent=2,
                sort_keys=False,
            )
            + "\n"
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, source: str) -> WorkspaceMarker:
        version = data.get("schema_version", 1)
        if not isinstance(version, int):
            raise WorkspaceError(
                f"marker has a non-integer schema_version: {version!r}", path=source
            )
        if version > MARKER_SCHEMA_VERSION:
            raise WorkspaceError(
                f"this workspace was created by a newer version of Scry "
                f"(marker schema {version}, this build understands {MARKER_SCHEMA_VERSION}). "
                f"Upgrade Scry to open it.",
                path=source,
            )

        missing = [key for key in ("id", "name", "target_path") if not data.get(key)]
        if missing:
            raise WorkspaceError(
                f"marker is missing required field(s): {', '.join(missing)}", path=source
            )

        # Unknown keys are ignored rather than rejected, so a marker written by a
        # later version with the same schema_version still opens.
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            target_path=str(data["target_path"]),
            created_at=str(data.get("created_at", "")),
            mode=str(data.get("mode", "auto")),
            schema_version=version,
        )

    @classmethod
    def read(cls, path: Path) -> WorkspaceMarker:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise WorkspaceError("workspace marker is missing", path=path) from exc
        except OSError as exc:
            raise WorkspaceError(f"workspace marker could not be read: {exc}", path=path) from exc

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise WorkspaceError(f"workspace marker is not valid JSON: {exc}", path=path) from exc

        if not isinstance(data, dict):
            raise WorkspaceError("workspace marker must contain a JSON object", path=path)

        return cls.from_dict(data, source=str(path))

    def write(self, path: Path) -> None:
        path.write_text(self.to_json(), encoding="utf-8")

    @property
    def target(self) -> Path:
        return Path(self.target_path)
