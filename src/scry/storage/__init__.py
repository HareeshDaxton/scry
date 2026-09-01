"""The workspace database.

One SQLite file per workspace, holding both session state and the knowledge
graph.

Spec sections 6.1 and 11.2 show two files, ``session.db`` and ``graph.db``.
They are merged here deliberately, because Scry's most valuable outputs are
cross-domain joins: salience multiplies churn by complexity by ownership by
exposure; CR-1 joins hotspots against test co-change; CR-9 joins Oracle's CVEs
against Cartographer's call graph. Split across two files, every one of those
becomes an ATTACH dance or a manual join in Python. In one file they are
ordinary SQL, and a transaction can span them. The only thing the split bought
was "delete the graph and re-analyse", which is a DELETE statement.

Public API::

    from scry.storage import initialise_database, reader, writer

    initialise_database(workspace.paths.database)

    with writer(workspace.paths.database) as conn:
        conn.execute("BEGIN")
        ...
        conn.execute("COMMIT")

    with reader(workspace.paths.database) as conn:
        conn.execute("SELECT ...").fetchall()
"""

from scry.storage.db import (
    BUSY_TIMEOUT_MS,
    DATABASE_FILENAME,
    connect_reader,
    connect_writer,
    enable_wal,
    foreign_keys_enabled,
    journal_mode,
    reader,
    writer,
)
from scry.storage.migrate import (
    Migration,
    apply_migration,
    current_version,
    discover_migrations,
    initialise_database,
    latest_version,
    migrate,
    split_statements,
)

__all__ = [
    "BUSY_TIMEOUT_MS",
    "DATABASE_FILENAME",
    "Migration",
    "apply_migration",
    "connect_reader",
    "connect_writer",
    "current_version",
    "discover_migrations",
    "enable_wal",
    "foreign_keys_enabled",
    "initialise_database",
    "journal_mode",
    "latest_version",
    "migrate",
    "reader",
    "split_statements",
    "writer",
]
