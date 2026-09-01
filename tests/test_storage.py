"""Tests for the workspace database (section 1.5).

The acceptance test is ``test_concurrent_readers_and_writer_never_lock``. Every
other test here checks a setting; that one checks the property all the settings
exist to produce, using real processes rather than threads because real
processes are what section 1.11 will spawn.
"""

from __future__ import annotations

import multiprocessing
import sqlite3
import time

import pytest

from scry.storage import (
    BUSY_TIMEOUT_MS,
    Migration,
    apply_migration,
    connect_reader,
    current_version,
    discover_migrations,
    foreign_keys_enabled,
    initialise_database,
    journal_mode,
    latest_version,
    migrate,
    reader,
    split_statements,
    writer,
)
from scry.util.errors import ScryError, StorageError

TABLES = frozenset(
    {"schema_version", "session_state", "agent_state", "claim_log", "claims", "merge_checkpoint"}
)


@pytest.fixture
def database(tmp_path):
    """An initialised database in a temporary directory."""
    path = tmp_path / "scry.db"
    initialise_database(path)
    return path


def table_names(connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


# ---------------------------------------------------------------------------
# Creation and schema
# ---------------------------------------------------------------------------
def test_initialise_creates_every_core_table(database):
    with reader(database) as connection:
        assert table_names(connection) >= TABLES


def test_initialise_is_idempotent(tmp_path):
    path = tmp_path / "scry.db"
    assert initialise_database(path) == latest_version()
    assert initialise_database(path) == latest_version()

    with reader(path) as connection:
        applied = connection.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    assert applied == len(discover_migrations()), "a migration was applied twice"


def test_initialise_refuses_a_missing_directory(tmp_path):
    with pytest.raises(StorageError, match="directory does not exist"):
        initialise_database(tmp_path / "nope" / "scry.db")


def test_storage_error_is_a_scry_error():
    assert issubclass(StorageError, ScryError)


def test_session_state_starts_with_exactly_one_row(database):
    with reader(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM session_state").fetchone()[0] == 1


def test_session_state_refuses_a_second_row(database):
    """One row is enforced by the schema, not by convention."""
    with writer(database) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO session_state (id, created_at) VALUES (2, 'now')")


def test_claim_status_uncertain_is_accepted(database):
    """`uncertain` is terminal, not an oversight: lite mode has no model to escalate to."""
    with writer(database) as connection:
        connection.execute(
            "INSERT INTO claims (id, agent_name, claim_type, assertion, confidence, status,"
            " created_at) VALUES ('c1', 'Skeptic', 'dead_code', 'x', 0.7, 'uncertain', 'now')"
        )
        row = connection.execute("SELECT status FROM claims WHERE id = 'c1'").fetchone()
    assert row["status"] == "uncertain"


def test_invalid_claim_status_is_rejected(database):
    with writer(database) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO claims (id, agent_name, claim_type, assertion, confidence, status,"
            " created_at) VALUES ('c2', 'Skeptic', 'dead_code', 'x', 0.7, 'vibes', 'now')"
        )


def test_confidence_outside_zero_to_one_is_rejected(database):
    with writer(database) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO claims (id, agent_name, claim_type, assertion, confidence, status,"
            " created_at) VALUES ('c3', 'Skeptic', 'dead_code', 'x', 1.4, 'pending', 'now')"
        )


def test_rows_are_dict_like(database):
    with reader(database) as connection:
        row = connection.execute("SELECT status, llm_calls_used FROM session_state").fetchone()
    assert row["status"] == "created"
    assert row["llm_calls_used"] == 0


# ---------------------------------------------------------------------------
# Pragmas
# ---------------------------------------------------------------------------
def test_wal_is_actually_in_effect(database):
    """Assert what the pragma returns, not that we asked for it."""
    with writer(database) as connection:
        assert journal_mode(connection) == "wal"


def test_wal_survives_reopening(database):
    """journal_mode is persistent: stored in the file, not per connection."""
    with reader(database) as connection:
        assert journal_mode(connection) == "wal"


def test_foreign_keys_are_on_for_both_connection_kinds(database):
    """SQLite defaults this OFF, per connection. Missed anywhere, REFERENCES is decorative."""
    with writer(database) as connection:
        assert foreign_keys_enabled(connection)
    with reader(database) as connection:
        assert foreign_keys_enabled(connection)


def test_foreign_key_violations_are_actually_enforced(database):
    """The pragma being on is only interesting if it changes behaviour."""
    with writer(database) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO claims (id, agent_name, claim_type, assertion, confidence,"
            " created_at, merged_from_seq)"
            " VALUES ('c4', 'Archivist', 'churn', 'x', 0.9, 'now', 999999)"
        )


def test_busy_timeout_is_set(database):
    """Without it a busy database fails immediately instead of retrying."""
    with writer(database) as connection:
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS
    with reader(database) as connection:
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS


def test_paths_containing_spaces_work(tmp_path):
    """Path.as_uri percent-encodes, which is what makes this work with no escaping."""
    directory = tmp_path / "a workspace with spaces"
    directory.mkdir()
    path = directory / "scry.db"
    initialise_database(path)
    with reader(path) as connection:
        assert table_names(connection) >= TABLES


# ---------------------------------------------------------------------------
# Read-only enforcement
# ---------------------------------------------------------------------------
def test_read_only_connections_refuse_to_write(database):
    """Single-writer discipline enforced by the database, not by remembering."""
    with reader(database) as connection, pytest.raises(sqlite3.OperationalError, match="readonly"):
        connection.execute("INSERT INTO agent_state (agent_name) VALUES ('Archivist')")


def test_read_only_connection_to_a_missing_database_is_refused(tmp_path):
    with pytest.raises(StorageError, match="does not exist"):
        connect_reader(tmp_path / "absent.db")


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------
def test_migrations_are_discoverable_as_package_data():
    """importlib.resources behaves the same in an editable install and a wheel.

    If a .sql file ever fails to be packaged, this fails here rather than on a
    user's machine after `pip install`.
    """
    migrations = discover_migrations()
    assert migrations, "no migrations found — are the .sql files packaged?"
    assert migrations[0].version == 1
    assert "CREATE TABLE" in migrations[0].sql


def test_migration_versions_are_ordered_and_unique():
    versions = [m.version for m in discover_migrations()]
    assert versions == sorted(versions)
    assert len(set(versions)) == len(versions)


def test_fresh_database_reports_version_zero(tmp_path):
    path = tmp_path / "fresh.db"
    with writer(path) as connection:
        assert current_version(connection) == 0


def test_version_is_recorded_after_migrating(database):
    with reader(database) as connection:
        assert current_version(connection) == latest_version()


def test_a_database_from_a_future_version_is_refused(database):
    with writer(database) as connection:
        connection.execute(
            "INSERT INTO schema_version (version, name, applied_at)"
            " VALUES (999, 'from_the_future', 'now')"
        )
        with pytest.raises(StorageError, match="newer version"):
            migrate(connection)


def test_a_failing_migration_leaves_the_schema_untouched(database):
    """SQLite DDL is transactional, so a partial migration must not survive."""
    broken = Migration(
        version=900,
        name="broken",
        sql=(
            "CREATE TABLE half_applied (id INTEGER);\n"
            "CREATE TABLE this_one_is_not_valid (id INTEGER REFERENCES nowhere_at_all(id));\n"
            "INSERT INTO nowhere_at_all (id) VALUES (1);\n"
        ),
    )
    with writer(database) as connection:
        before = table_names(connection)
        with pytest.raises(StorageError, match="rolled back"):
            apply_migration(connection, broken)

        assert table_names(connection) == before, "a partial migration survived"
        assert current_version(connection) == latest_version(), "version was advanced anyway"


# ---------------------------------------------------------------------------
# Statement splitting
# ---------------------------------------------------------------------------
def test_split_statements_handles_a_semicolon_inside_a_string():
    """Naive splitting on ';' would cut this in half."""
    script = "INSERT INTO t (a) VALUES ('one; two');\nSELECT 1;\n"
    assert list(split_statements(script)) == ["INSERT INTO t (a) VALUES ('one; two');", "SELECT 1;"]


def test_split_statements_skips_leading_comments():
    script = "-- a comment\n-- another\nSELECT 1;\n"
    assert list(split_statements(script)) == ["SELECT 1;"]


def test_split_statements_rejects_an_incomplete_tail():
    with pytest.raises(StorageError, match="incomplete statement"):
        list(split_statements("SELECT 1;\nCREATE TABLE unfinished ("))


def test_split_statements_on_an_empty_script():
    assert list(split_statements("\n-- nothing here\n")) == []


# ---------------------------------------------------------------------------
# Concurrency — the acceptance test
# ---------------------------------------------------------------------------
def _read_until(path, seconds, results):  # pragma: no cover - runs in a child process
    errors = []
    deadline = time.monotonic() + seconds
    reads = 0
    while time.monotonic() < deadline:
        try:
            with reader(path) as connection:
                connection.execute("SELECT COUNT(*) FROM claim_log").fetchone()
            reads += 1
        except sqlite3.OperationalError as exc:
            errors.append(f"reader: {exc}")
    results.put((reads, errors))


def _write_rows(path, count, results):  # pragma: no cover - runs in a child process
    errors = []
    written = 0
    try:
        with writer(path) as connection:
            for n in range(count):
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO claim_log (claim_id, agent_name, payload, appended_at)"
                    " VALUES (?, ?, ?, ?)",
                    (f"claim-{n}", "Archivist", "{}", "now"),
                )
                connection.execute("COMMIT")
                written += 1
    except sqlite3.OperationalError as exc:
        errors.append(f"writer: {exc}")
    results.put((written, errors))


def test_concurrent_readers_and_writer_never_lock(database):
    """Two reader processes and one writer, at once, with zero lock errors.

    Real processes rather than threads, using the spawn start method, because
    that is exactly what section 1.11 will do on Windows.
    """
    context = multiprocessing.get_context("spawn")
    results = context.Queue()

    processes = [
        context.Process(target=_read_until, args=(database, 2.0, results)),
        context.Process(target=_read_until, args=(database, 2.0, results)),
        context.Process(target=_write_rows, args=(database, 300, results)),
    ]
    for process in processes:
        process.start()

    collected = [results.get(timeout=60) for _ in processes]
    for process in processes:
        process.join(timeout=60)
        assert process.exitcode == 0, "a worker process died"

    errors = [message for _, messages in collected for message in messages]
    assert not errors, f"database contention: {errors}"

    counts = [count for count, _ in collected]
    assert all(count > 0 for count in counts), f"a worker did no work: {counts}"

    with reader(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM claim_log").fetchone()[0] == 300
