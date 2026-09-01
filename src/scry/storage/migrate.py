"""Forward-only schema migrations.

There are no down migrations. Reversible migrations are a large amount of
machinery to maintain, and this database is a cache that ``scry map`` can
rebuild from nothing. If a downgrade is ever genuinely needed, deleting the
workspace is the honest answer rather than a half-tested rollback path.

A database whose recorded version is *newer* than this build understands is
refused outright, for the same reason the workspace marker refuses one in
section 1.4: opening it would mean interpreting data written under rules we do
not know.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from scry.storage.db import connect_writer, enable_wal
from scry.util.errors import StorageError

MIGRATIONS_PACKAGE = "scry.storage.migrations"
MIGRATION_FILENAME = re.compile(r"^(?P<version>\d{3})_(?P<name>[a-z0-9_]+)\.sql$")

WarningSink = Callable[[str], None]

# Created by the runner rather than by 001, since it is the runner's own
# bookkeeping and has to exist before any migration can be recorded. One row per
# applied migration, so the file carries its own history.
_SCHEMA_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER NOT NULL PRIMARY KEY,
    name       TEXT    NOT NULL,
    applied_at TEXT    NOT NULL
)
"""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def filename(self) -> str:
        return f"{self.version:03d}_{self.name}.sql"


def discover_migrations() -> tuple[Migration, ...]:
    """Load every migration shipped with the package, in version order."""
    found: list[Migration] = []
    for entry in resources.files(MIGRATIONS_PACKAGE).iterdir():
        match = MIGRATION_FILENAME.match(entry.name)
        if match is None:
            continue
        found.append(
            Migration(
                version=int(match["version"]),
                name=match["name"],
                sql=entry.read_text(encoding="utf-8"),
            )
        )

    found.sort(key=lambda m: m.version)

    versions = [m.version for m in found]
    if len(set(versions)) != len(versions):
        raise StorageError(f"duplicate migration version numbers: {versions}")

    return tuple(found)


def latest_version() -> int:
    migrations = discover_migrations()
    return migrations[-1].version if migrations else 0


def current_version(connection: sqlite3.Connection) -> int:
    """Return the highest applied migration version, or 0 for a fresh database."""
    connection.execute(_SCHEMA_VERSION_DDL)
    row = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def split_statements(script: str) -> Iterator[str]:
    """Yield complete SQL statements from a migration file.

    Splitting on semicolons naively would break on a semicolon inside a string
    literal or inside a trigger body. ``sqlite3.complete_statement`` is the
    parser-aware check for "is this a whole statement yet", so accumulating
    lines until it returns True is correct for anything we might write.

    This exists because ``Connection.executescript`` cannot be used: it issues an
    implicit COMMIT before running, which would discard the transaction wrapping
    the migration and leave a failure half-applied.
    """
    buffer = ""
    for line in script.splitlines(keepends=True):
        stripped = line.strip()
        if not buffer and (not stripped or stripped.startswith("--")):
            continue
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                yield statement
            buffer = ""

    if buffer.strip():
        raise StorageError(f"migration ends with an incomplete statement: {buffer.strip()[:80]!r}")


def apply_migration(connection: sqlite3.Connection, migration: Migration) -> None:
    """Apply one migration inside a transaction, or leave the schema untouched.

    SQLite's DDL is transactional, so a failure part-way through rolls the whole
    migration back rather than leaving a half-changed schema that neither the old
    code nor the new code understands.
    """
    connection.execute("BEGIN")
    try:
        for statement in split_statements(migration.sql):
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
            (migration.version, migration.name, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        connection.execute("COMMIT")
    except sqlite3.Error as exc:
        connection.execute("ROLLBACK")
        raise StorageError(
            f"migration {migration.filename} failed and was rolled back: {exc}"
        ) from exc


def migrate(connection: sqlite3.Connection, *, on_warning: WarningSink | None = None) -> int:
    """Bring the database up to the latest schema and return its version."""
    applied = current_version(connection)
    available = discover_migrations()
    newest = available[-1].version if available else 0

    if applied > newest:
        raise StorageError(
            f"this workspace's database is at schema version {applied}, but this build of "
            f"Scry only understands {newest}. It was created by a newer version — upgrade "
            f"Scry to open it."
        )

    for migration in available:
        if migration.version > applied:
            apply_migration(connection, migration)

    return newest


def initialise_database(path: Path, *, on_warning: WarningSink | None = None) -> int:
    """Create or upgrade the workspace database, and return its schema version.

    Safe to call repeatedly: an already-current database is left alone.
    """
    if not path.parent.is_dir():
        raise StorageError("database directory does not exist", path=path.parent)

    connection = connect_writer(path)
    try:
        enable_wal(connection, on_warning=on_warning)
        return migrate(connection, on_warning=on_warning)
    finally:
        connection.close()
