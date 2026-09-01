"""The single-writer merge: draining the claim log into the claims table.

Exactly one process runs this. Everything subtle about combining claims
therefore executes single-threaded, with no concurrency to reason about — which
is the entire reason the append-only log exists.

**Scope.** This merges by identity and recency only. Spec section 3.1's
corroboration rules — noisy-OR across independent producers, ``max`` within one
evidence family — need the confidence provenance table and land in section 5.7.
Building half of that here would only mean rewriting it there.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from scry.storage.claims import Claim
from scry.util.clock import utc_timestamp
from scry.util.errors import StorageError

# One COMMIT per claim would be far too slow for the tens of thousands a real
# repository produces. A larger batch only means more work replayed after a
# crash, and replay is idempotent, so the cost is time rather than correctness.
DEFAULT_BATCH_SIZE = 500

BatchCallback = Callable[[int, int], None]


# `merged_from_seq` carries the log position each row came from, which is what
# makes the update rule expressible in SQL: a lower sequence never overwrites a
# higher one, so replaying an earlier batch after a crash cannot undo later work.
#
# `created_at` is preserved on update — the claim was first seen when it was
# first seen. `status` is preserved only while the assertion and confidence are
# unchanged: if the underlying computation produced something different, any
# earlier adjudication by the Skeptic is stale and the claim goes back to
# pending. Section 5.8 owns the adjudication policy and may refine this.
_UPSERT = """
INSERT INTO claims (
    id, agent_name, claim_type, target_file, target_symbol, target_line,
    assertion, confidence, status, evidence_json, created_at, updated_at, merged_from_seq
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    agent_name      = excluded.agent_name,
    claim_type      = excluded.claim_type,
    target_file     = excluded.target_file,
    target_symbol   = excluded.target_symbol,
    target_line     = excluded.target_line,
    assertion       = excluded.assertion,
    confidence      = excluded.confidence,
    evidence_json   = excluded.evidence_json,
    updated_at      = excluded.updated_at,
    merged_from_seq = excluded.merged_from_seq,
    status          = CASE
                          WHEN claims.assertion = excluded.assertion
                           AND claims.confidence = excluded.confidence
                          THEN claims.status
                          ELSE 'pending'
                      END
WHERE excluded.merged_from_seq > claims.merged_from_seq
"""


@dataclass(frozen=True)
class MergeResult:
    merged: int
    batches: int
    last_seq: int


def merge_checkpoint(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT last_seq FROM merge_checkpoint WHERE id = 1").fetchone()
    if row is None:
        raise StorageError("merge_checkpoint row is missing; the database is not initialised")
    return int(row[0])


def merge_claims(
    connection: sqlite3.Connection,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches: int | None = None,
    on_batch: BatchCallback | None = None,
) -> MergeResult:
    """Drain the claim log into ``claims`` and return what was done.

    Each batch merges its rows **and** advances the checkpoint inside one
    transaction. That is what makes crash recovery free: a crash rolls back both,
    so a restart resumes from the last committed checkpoint having neither lost a
    claim nor applied one twice. Were the checkpoint updated separately, a crash
    between the two would push it past claims that had been rolled back, and
    those claims would be gone with nothing to indicate it.

    Args:
        on_batch: called with ``(batch_number, last_seq)`` after each committed
            batch. Exists so tests can inject a crash at a known point without
            monkeypatching.
    """
    if batch_size < 1:
        raise StorageError(f"batch_size must be at least 1, got {batch_size}")

    merged = 0
    batches = 0
    last_seq = merge_checkpoint(connection)

    while max_batches is None or batches < max_batches:
        rows = connection.execute(
            "SELECT seq, payload FROM claim_log WHERE seq > ? ORDER BY seq LIMIT ?",
            (last_seq, batch_size),
        ).fetchall()
        if not rows:
            break

        batch_last_seq = int(rows[-1]["seq"])
        now = utc_timestamp()

        parameters = []
        for row in rows:
            claim = Claim.from_payload(row["payload"])
            parameters.append(
                (
                    claim.id,
                    claim.agent,
                    claim.claim_type,
                    claim.target_file,
                    claim.target_symbol,
                    claim.target_line,
                    claim.assertion,
                    claim.confidence,
                    _evidence_json(claim),
                    now,
                    now,
                    int(row["seq"]),
                )
            )

        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.executemany(_UPSERT, parameters)
            connection.execute(
                "UPDATE merge_checkpoint SET last_seq = ?, updated_at = ? WHERE id = 1",
                (batch_last_seq, now),
            )
            connection.execute("COMMIT")
        except sqlite3.Error as exc:
            connection.execute("ROLLBACK")
            raise StorageError(f"claim merge failed and was rolled back: {exc}") from exc

        merged += len(rows)
        batches += 1
        last_seq = batch_last_seq

        if on_batch is not None:
            on_batch(batches, last_seq)

    return MergeResult(merged=merged, batches=batches, last_seq=last_seq)


def _evidence_json(claim: Claim) -> str | None:
    if not claim.evidence:
        return None
    return json.dumps([e.to_dict() for e in claim.evidence], sort_keys=True, separators=(",", ":"))
