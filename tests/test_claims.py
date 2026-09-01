"""Tests for the claim log and the single-writer merge (section 1.6).

Two acceptance tests carry this section. ``test_eight_processes_append_ten_thousand_claims``
proves the concurrency discipline holds under the load section 1.11 will
produce, and ``test_a_crash_mid_merge_loses_and_duplicates_nothing`` proves
recovery works when a merger dies without cleanup.
"""

from __future__ import annotations

import multiprocessing
import os
import pickle

import pytest

from scry.storage import (
    Claim,
    Evidence,
    append_claim,
    append_claims,
    connect_writer,
    initialise_database,
    merge_checkpoint,
    merge_claims,
    pending_depth,
    reader,
    wait_for_capacity,
    writer,
)
from scry.util.errors import StorageError

AWS_KEY = "AKIA" + ("EXAMPLEONLY" + "0" * 16)[:16]


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "scry.db"
    initialise_database(path)
    return path


def make_claim(**overrides) -> Claim:
    defaults = {
        "agent": "Archivist",
        "claim_type": "hotspot",
        "assertion": "payment.py changed 42 times in 3 months",
        "confidence": 0.95,
        "target_file": "src/payment.py",
    }
    return Claim(**{**defaults, **overrides})


def claim_count(path) -> int:
    with reader(path) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0])


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def test_identical_claims_share_an_id():
    assert make_claim().id == make_claim().id


def test_id_ignores_confidence_and_evidence():
    """Those are the claim's value, not its identity — that is what makes re-runs free."""
    plain = make_claim(confidence=0.5)
    richer = make_claim(
        confidence=0.99,
        evidence=(Evidence(kind="git_log", summary="42 commits"),),
        metadata={"severity": "high"},
    )
    assert plain.id == richer.id


def test_id_changes_with_the_agent():
    """Two agents claiming about one file stay separate rows: that is corroboration (5.7)."""
    assert make_claim(agent="Archivist").id != make_claim(agent="Pathologist").id


@pytest.mark.parametrize(
    "field",
    ["claim_type", "assertion", "target_file", "target_symbol", "target_line"],
)
def test_id_changes_with_every_identity_field(field):
    changed = {"target_line": 99}.get(field, "something-else")
    assert make_claim().id != make_claim(**{field: changed}).id


def test_ids_cannot_collide_across_field_boundaries():
    """A separator that cannot occur in a path or identifier prevents this."""
    first = make_claim(target_file="a", target_symbol="b")
    second = make_claim(target_file="ab", target_symbol="")
    assert first.id != second.id


# ---------------------------------------------------------------------------
# Validation and immutability
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [-0.1, 1.1, "high", True, None])
def test_invalid_confidence_is_refused(bad):
    with pytest.raises(StorageError, match="confidence"):
        make_claim(confidence=bad)


@pytest.mark.parametrize("field", ["agent", "claim_type", "assertion"])
def test_empty_required_fields_are_refused(field):
    with pytest.raises(StorageError, match=field):
        make_claim(**{field: "   "})


def test_non_positive_line_numbers_are_refused():
    with pytest.raises(StorageError, match="target_line"):
        make_claim(target_line=0)


def test_claims_are_frozen():
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        make_claim().confidence = 0.1


def test_claims_survive_a_pickle_round_trip():
    """Section 1.11 spawns workers, so every claim crosses a process boundary."""
    claim = make_claim(
        evidence=(Evidence(kind="git_log", summary="42 commits", tool="git"),),
        metadata={"language": "python"},
    )
    restored = pickle.loads(pickle.dumps(claim))
    assert restored == claim
    assert restored.id == claim.id


def test_metadata_accepts_a_mapping_and_stores_it_deterministically():
    one = make_claim(metadata={"b": "2", "a": "1"})
    two = make_claim(metadata={"a": "1", "b": "2"})
    assert one.metadata == two.metadata
    assert one.metadata_dict == {"a": "1", "b": "2"}


# ---------------------------------------------------------------------------
# Evidence redaction
# ---------------------------------------------------------------------------
def test_evidence_snippets_are_redacted_on_construction():
    """A secret must never enter the graph, not merely be caught on the way out."""
    evidence = Evidence(kind="secret", summary="hardcoded key", snippet=f"KEY = '{AWS_KEY}'")
    assert AWS_KEY not in evidence.snippet
    assert "<REDACTED_aws_key>" in evidence.snippet


def test_ordinary_code_snippets_are_left_alone():
    code = "def process_payment(order_id: int) -> None:\n    return None\n"
    assert Evidence(kind="ast", summary="function", snippet=code).snippet == code


def test_a_redacted_secret_never_reaches_the_database(database):
    claim = make_claim(
        evidence=(Evidence(kind="secret", summary="key", snippet=f"k={AWS_KEY}"),),
    )
    with writer(database) as connection:
        append_claim(connection, claim)
        merge_claims(connection)

    assert AWS_KEY not in database.read_bytes().decode("utf-8", errors="ignore")


def test_empty_evidence_fields_are_refused():
    with pytest.raises(StorageError, match="kind"):
        Evidence(kind="", summary="x")
    with pytest.raises(StorageError, match="summary"):
        Evidence(kind="x", summary="")


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------
def test_payload_round_trips():
    claim = make_claim(
        target_line=42,
        target_symbol="process_order",
        evidence=(Evidence(kind="ast", summary="f-string in SQL", rule_id="py.sql.injection"),),
        metadata={"severity": "critical"},
    )
    assert Claim.from_payload(claim.to_payload()) == claim


def test_payload_encoding_is_deterministic():
    assert make_claim(metadata={"b": "2", "a": "1"}).to_payload() == (
        make_claim(metadata={"a": "1", "b": "2"}).to_payload()
    )


def test_malformed_payload_is_refused():
    with pytest.raises(StorageError, match="not valid JSON"):
        Claim.from_payload("{ nope")


# ---------------------------------------------------------------------------
# Appending
# ---------------------------------------------------------------------------
def test_append_writes_to_the_log_not_to_claims(database):
    with writer(database) as connection:
        append_claim(connection, make_claim())
        logged = connection.execute("SELECT COUNT(*) FROM claim_log").fetchone()[0]
        merged = connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    assert (logged, merged) == (1, 0)


def test_batch_append_returns_the_highest_sequence(database):
    claims = [make_claim(target_file=f"src/f{n}.py") for n in range(10)]
    with writer(database) as connection:
        assert append_claims(connection, claims) == 10


def test_appending_nothing_is_a_no_op(database):
    with writer(database) as connection:
        assert append_claims(connection, []) == 0


def test_pending_depth_tracks_the_backlog(database):
    with writer(database) as connection:
        assert pending_depth(connection) == 0
        append_claims(connection, [make_claim(target_file=f"f{n}") for n in range(5)])
        assert pending_depth(connection) == 5
        merge_claims(connection)
        assert pending_depth(connection) == 0


def test_backpressure_raises_when_the_merger_never_catches_up(database):
    with writer(database) as connection:
        append_claims(connection, [make_claim(target_file=f"f{n}") for n in range(10)])
        with pytest.raises(StorageError, match="not keeping up"):
            wait_for_capacity(connection, max_pending=2, timeout=0.2, poll_interval=0.01)


def test_backpressure_is_off_by_default(database):
    """Section 1.11 owns the pool and therefore the policy; this only provides the lever."""
    with writer(database) as connection:
        append_claims(connection, [make_claim(target_file=f"f{n}") for n in range(50)])
        append_claims(connection, [make_claim(target_file="late.py")])  # must not block


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------
def test_merge_drains_the_log_into_claims(database):
    claims = [make_claim(target_file=f"src/f{n}.py") for n in range(25)]
    with writer(database) as connection:
        append_claims(connection, claims)
        result = merge_claims(connection)

    assert result.merged == 25
    assert claim_count(database) == 25


def test_merge_advances_the_checkpoint(database):
    with writer(database) as connection:
        append_claims(connection, [make_claim(target_file=f"f{n}") for n in range(7)])
        assert merge_checkpoint(connection) == 0
        merge_claims(connection)
        assert merge_checkpoint(connection) == 7


def test_merging_twice_changes_nothing(database):
    with writer(database) as connection:
        append_claims(connection, [make_claim(target_file=f"f{n}") for n in range(10)])
        merge_claims(connection)
        second = merge_claims(connection)

    assert second.merged == 0
    assert claim_count(database) == 10


def test_restating_a_claim_updates_one_row(database):
    """What makes agent restarts and incremental re-runs idempotent."""
    with writer(database) as connection:
        append_claim(connection, make_claim(confidence=0.5))
        merge_claims(connection)
        append_claim(connection, make_claim(confidence=0.9))
        merge_claims(connection)

        row = connection.execute("SELECT confidence FROM claims").fetchone()

    assert claim_count(database) == 1
    assert row["confidence"] == 0.9, "the later restatement should win"


def test_an_unchanged_restatement_keeps_its_adjudicated_status(database):
    """Re-validating a claim nothing changed about would be pointless churn."""
    with writer(database) as connection:
        append_claim(connection, make_claim())
        merge_claims(connection)
        connection.execute("UPDATE claims SET status = 'validated'")

        append_claim(connection, make_claim())
        merge_claims(connection)
        status = connection.execute("SELECT status FROM claims").fetchone()["status"]

    assert status == "validated"


def test_a_changed_restatement_returns_to_pending(database):
    """If the computation produced something different, prior adjudication is stale."""
    with writer(database) as connection:
        append_claim(connection, make_claim(confidence=0.5))
        merge_claims(connection)
        connection.execute("UPDATE claims SET status = 'validated'")

        append_claim(connection, make_claim(confidence=0.8))
        merge_claims(connection)
        status = connection.execute("SELECT status FROM claims").fetchone()["status"]

    assert status == "pending"


def test_merge_respects_the_batch_size(database):
    with writer(database) as connection:
        append_claims(connection, [make_claim(target_file=f"f{n}") for n in range(10)])
        result = merge_claims(connection, batch_size=3)

    assert result.merged == 10
    assert result.batches == 4  # 3 + 3 + 3 + 1


def test_evidence_is_stored_as_json(database):
    with writer(database) as connection:
        append_claim(
            connection,
            make_claim(evidence=(Evidence(kind="git_log", summary="42 commits"),)),
        )
        merge_claims(connection)
        stored = connection.execute("SELECT evidence_json FROM claims").fetchone()[0]

    assert "42 commits" in stored


def test_invalid_batch_size_is_refused(database):
    with writer(database) as connection, pytest.raises(StorageError, match="batch_size"):
        merge_claims(connection, batch_size=0)


# ---------------------------------------------------------------------------
# Acceptance: concurrency
# ---------------------------------------------------------------------------
def _append_worker(path, worker_id, count, results):  # pragma: no cover - child process
    try:
        with writer(path) as connection:
            append_claims(
                connection,
                [
                    Claim(
                        agent=f"Worker{worker_id}",
                        claim_type="churn",
                        assertion=f"file {n} changed",
                        confidence=0.9,
                        target_file=f"src/w{worker_id}/f{n}.py",
                    )
                    for n in range(count)
                ],
            )
        results.put((count, None))
    except Exception as exc:
        results.put((0, f"{type(exc).__name__}: {exc}"))


def test_eight_processes_append_ten_thousand_claims(database):
    """The acceptance test: eight concurrent appenders, then one merge."""
    workers, per_worker = 8, 1250
    context = multiprocessing.get_context("spawn")
    results = context.Queue()

    processes = [
        context.Process(target=_append_worker, args=(database, n, per_worker, results))
        for n in range(workers)
    ]
    for process in processes:
        process.start()

    collected = [results.get(timeout=120) for _ in processes]
    for process in processes:
        process.join(timeout=120)
        assert process.exitcode == 0

    errors = [error for _, error in collected if error]
    assert not errors, f"appenders failed: {errors}"

    with reader(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM claim_log").fetchone()[0] == 10_000

    with writer(database) as connection:
        result = merge_claims(connection)

    assert result.merged == 10_000
    assert claim_count(database) == 10_000, "duplicates or losses in the merge"


def _merge_then_crash(path, batch_size, batches_before_crash):  # pragma: no cover - child
    # connect_writer rather than the context manager: this process is meant to
    # die without unwinding, so there must be nothing that could close the
    # connection cleanly on the way out.
    connection = connect_writer(path)

    def on_batch(batch_number, _last_seq):
        if batch_number >= batches_before_crash:
            # A real crash: no unwinding, no cleanup, no rollback handler.
            os._exit(9)

    merge_claims(connection, batch_size=batch_size, on_batch=on_batch)


def test_a_crash_mid_merge_loses_and_duplicates_nothing(database):
    """A merger killed without cleanup must leave the database consistent.

    Each batch commits its rows and its checkpoint together, so an interrupted
    merge simply resumes from the last committed batch.
    """
    total, batch_size, crash_after = 1000, 100, 3

    with writer(database) as connection:
        append_claims(connection, [make_claim(target_file=f"src/f{n}.py") for n in range(total)])

    context = multiprocessing.get_context("spawn")
    crasher = context.Process(target=_merge_then_crash, args=(database, batch_size, crash_after))
    crasher.start()
    crasher.join(timeout=120)
    assert crasher.exitcode == 9, "the child did not crash as intended"

    # Exactly the committed batches survived: no partial batch, no lost work.
    with reader(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == (
            batch_size * crash_after
        )
        assert connection.execute("SELECT last_seq FROM merge_checkpoint").fetchone()[0] == (
            batch_size * crash_after
        )

    with writer(database) as connection:
        resumed = merge_claims(connection, batch_size=batch_size)

    assert resumed.merged == total - batch_size * crash_after
    assert claim_count(database) == total, "resuming lost or duplicated claims"
