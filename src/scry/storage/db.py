"""SQLite connections, configured for one writer and many concurrent readers.

SQLite handles our access pattern well, but three of its defaults are wrong for
it and none of them fail loudly:

* **journal_mode** defaults to a rollback journal, where a writer blocks every
  reader. WAL lets one writer and many readers proceed at once, which is the
  only reason the eight worker processes of section 1.11 can run at all.
* **foreign_keys** defaults to *off*, per connection. Miss it anywhere and every
  ``REFERENCES`` clause in the schema is decorative on that connection: orphan
  rows insert happily and nothing complains.
* **busy_timeout** defaults to zero, so a connection that finds the database
  busy fails immediately with ``database is locked`` rather than retrying.

Only ``journal_mode`` is persistent — it is stored in the file and set once.
The rest are per-connection and must be applied to every connection handed out,
which is why they live in one function here rather than at each call site.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from scry.util.errors import StorageError

DATABASE_FILENAME = "scry.db"

# Five seconds of retrying before giving up. With eight workers contending, the
# difference between this and the default of zero is the difference between
# "works" and "fails intermittently under load".
BUSY_TIMEOUT_MS = 5000

WarningSink = Callable[[str], None]


def _apply_connection_pragmas(connection: sqlite3.Connection) -> None:
    """Settings that are per-connection and must be re-applied every time."""
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA foreign_keys = ON")
    # NORMAL is durable against a process crash under WAL; only power loss can
    # cost the last few transactions. FULL fsyncs constantly and would show up
    # directly in the section 2.12 performance gate. This database is a cache
    # that `scry map` can rebuild, so the trade is clearly worth it.
    connection.execute("PRAGMA synchronous = NORMAL")


def _database_uri(path: Path, *, read_only: bool) -> str:
    """Build a SQLite URI.

    ``Path.as_uri`` percent-encodes the path, which is what makes workspaces
    under directories containing spaces work without any escaping of our own.
    """
    uri = path.resolve().as_uri()
    return f"{uri}?mode=ro" if read_only else uri


def connect_writer(path: Path) -> sqlite3.Connection:
    """Open the single writable connection.

    ``isolation_level=None`` disables the sqlite3 module's implicit transaction
    handling, so transactions are started and ended explicitly. Predictable
    beats convenient here: section 1.6 batches many inserts into one
    transaction, and implicit boundaries would make that hard to reason about.
    """
    try:
        connection = sqlite3.connect(
            _database_uri(path, read_only=False),
            uri=True,
            isolation_level=None,
        )
    except sqlite3.Error as exc:
        raise StorageError(f"could not open database: {exc}", path=path) from exc

    connection.row_factory = sqlite3.Row
    _apply_connection_pragmas(connection)
    return connection


def connect_reader(path: Path) -> sqlite3.Connection:
    """Open a read-only connection.

    Read-only is enforcement, not optimisation. It makes the single-writer
    discipline a property of the database rather than a rule people remember:
    a worker that accidentally tries to INSERT gets an immediate error naming
    the problem, instead of silently breaking the invariant that section 1.6's
    merge design rests on.
    """
    if not path.exists():
        raise StorageError("database does not exist", path=path)
    try:
        connection = sqlite3.connect(
            _database_uri(path, read_only=True),
            uri=True,
            isolation_level=None,
        )
    except sqlite3.Error as exc:
        raise StorageError(f"could not open database read-only: {exc}", path=path) from exc

    connection.row_factory = sqlite3.Row
    _apply_connection_pragmas(connection)
    return connection


@contextmanager
def writer(path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect_writer(path)
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def reader(path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect_reader(path)
    try:
        yield connection
    finally:
        connection.close()


def enable_wal(connection: sqlite3.Connection, *, on_warning: WarningSink | None = None) -> str:
    """Switch the database to WAL and return the mode actually in effect.

    The return value is checked rather than assumed. WAL is unavailable on many
    network filesystems, and ``~/.scry`` on a roaming enterprise profile is a
    realistic situation. Degrading silently would turn "eight workers" into
    "eight workers serialising on each other" with nothing to explain why.
    """
    row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
    mode = str(row[0]).lower() if row else "unknown"
    if mode != "wal" and on_warning is not None:
        on_warning(
            f"could not enable WAL journal mode (got {mode!r}). Concurrent reads and "
            f"writes will contend, which will slow analysis considerably. This usually "
            f"means the database is on a network filesystem."
        )
    return mode


def journal_mode(connection: sqlite3.Connection) -> str:
    row = connection.execute("PRAGMA journal_mode").fetchone()
    return str(row[0]).lower() if row else "unknown"


def foreign_keys_enabled(connection: sqlite3.Connection) -> bool:
    row = connection.execute("PRAGMA foreign_keys").fetchone()
    return bool(row[0]) if row else False
