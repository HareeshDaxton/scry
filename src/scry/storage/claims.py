"""Claims, and the append-only log workers write them to.

Eight worker processes discover facts at once. If they each wrote to ``claims``
directly, three things would go wrong: SQLite permits one writer at a time, so
they would spend their time waiting rather than analysing; two workers updating
the same row would silently lose one of the updates; and every merge rule would
have to be correct under concurrency.

Appending to a log dissolves all three. A worker does nothing but INSERT — no
read, no update, minimal time holding the write lock. A single process later
drains that log into ``claims`` **single-threaded**, so the merge logic never has
to reason about concurrency at all.

The discipline is therefore "only the merger writes ``claims``", not "only one
process may ever write". Workers legitimately hold writable connections; they
simply never touch the merged table.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from scry.util.clock import utc_timestamp
from scry.util.errors import StorageError
from scry.util.redact import redact

# Unit separator. Using a character that cannot appear in a path or an
# identifier means two different field splits can never hash to the same id.
_FIELD_SEPARATOR = "\x1f"

ID_LENGTH = 32


@dataclass(frozen=True)
class Evidence:
    """One piece of support for a claim.

    Snippets are redacted on construction. Pathologist will find hardcoded
    credentials, and a snippet containing one would otherwise be written into the
    database — from where spec section 8 permits graph facts to reach a cloud
    model. Redacting here means a secret never enters the graph at all, rather
    than relying on section 6.2's boundary to catch it on the way out. The
    patterns are precise enough that ordinary code passes through untouched.
    """

    kind: str
    summary: str
    snippet: str | None = None
    tool: str | None = None
    rule_id: str | None = None

    def __post_init__(self) -> None:
        if not self.kind.strip():
            raise StorageError("evidence kind is empty")
        if not self.summary.strip():
            raise StorageError("evidence summary is empty")
        if self.snippet is not None:
            object.__setattr__(self, "snippet", redact(self.snippet))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "summary": self.summary,
            "snippet": self.snippet,
            "tool": self.tool,
            "rule_id": self.rule_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Evidence:
        return cls(
            kind=str(data["kind"]),
            summary=str(data["summary"]),
            snippet=data.get("snippet"),
            tool=data.get("tool"),
            rule_id=data.get("rule_id"),
        )


@dataclass(frozen=True)
class Claim:
    """A single assertion an agent makes about the codebase.

    Frozen and picklable, because section 1.11 spawns workers on Windows and
    every claim crosses a process boundary as pickled bytes.
    """

    agent: str
    claim_type: str
    assertion: str
    confidence: float
    target_file: str | None = None
    target_symbol: str | None = None
    target_line: int | None = None
    evidence: tuple[Evidence, ...] = ()
    metadata: tuple[tuple[str, str], ...] = field(default=())

    def __post_init__(self) -> None:
        for name in ("agent", "claim_type", "assertion"):
            if not str(getattr(self, name)).strip():
                raise StorageError(f"claim {name} is empty")

        if not isinstance(self.confidence, (int, float)) or isinstance(self.confidence, bool):
            raise StorageError(f"claim confidence must be a number, got {self.confidence!r}")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise StorageError(f"claim confidence must be within 0..1, got {self.confidence!r}")
        object.__setattr__(self, "confidence", float(self.confidence))

        if self.target_line is not None and self.target_line < 1:
            raise StorageError(f"claim target_line must be positive, got {self.target_line!r}")

        object.__setattr__(self, "evidence", tuple(self.evidence))

        # Accepted as a mapping for callers' convenience, stored as a sorted
        # tuple so the value is immutable and its serialisation deterministic.
        if isinstance(self.metadata, Mapping):
            object.__setattr__(
                self,
                "metadata",
                tuple(sorted((str(k), str(v)) for k, v in self.metadata.items())),
            )
        else:
            object.__setattr__(self, "metadata", tuple(self.metadata))

    @property
    def id(self) -> str:
        """A stable identifier derived from what this claim is *about*.

        Confidence, evidence and metadata are deliberately excluded: they are the
        claim's *value*, not its *identity*. Recomputing a fact therefore yields
        the same id with a new value, and the merge updates one row instead of
        adding a second.

        That is what makes agent restarts free. Section 1.12's Conductor
        respawns crashed agents and section 8.5 re-runs Archivist over changed
        files; both re-derive facts they already found. With random ids each
        re-run would duplicate every claim and `scry hotspots` would quietly
        double-count. Here idempotency is a property of the identifier rather
        than a deduplication pass someone has to remember to write.

        The agent name is part of the hash on purpose. One agent restating a
        fact collapses to one row; two different agents claiming about the same
        file stay separate, because that is corroboration and combining it is
        section 5.7's job under the confidence provenance table.
        """
        parts = (
            self.agent,
            self.claim_type,
            self.target_file or "",
            self.target_symbol or "",
            str(self.target_line or 0),
            self.assertion,
        )
        digest = hashlib.sha256(_FIELD_SEPARATOR.join(parts).encode("utf-8"))
        return digest.hexdigest()[:ID_LENGTH]

    @property
    def metadata_dict(self) -> dict[str, str]:
        return dict(self.metadata)

    def to_payload(self) -> str:
        """Serialise for the log. Sorted keys keep the encoding deterministic."""
        return json.dumps(
            {
                "agent": self.agent,
                "claim_type": self.claim_type,
                "assertion": self.assertion,
                "confidence": self.confidence,
                "target_file": self.target_file,
                "target_symbol": self.target_symbol,
                "target_line": self.target_line,
                "evidence": [e.to_dict() for e in self.evidence],
                "metadata": list(self.metadata),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_payload(cls, payload: str) -> Claim:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise StorageError(f"claim payload is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise StorageError("claim payload must be a JSON object")

        return cls(
            agent=str(data["agent"]),
            claim_type=str(data["claim_type"]),
            assertion=str(data["assertion"]),
            confidence=float(data["confidence"]),
            target_file=data.get("target_file"),
            target_symbol=data.get("target_symbol"),
            target_line=data.get("target_line"),
            evidence=tuple(Evidence.from_dict(e) for e in data.get("evidence", ())),
            metadata=tuple((str(k), str(v)) for k, v in data.get("metadata", ())),
        )


# ---------------------------------------------------------------------------
# Appending
# ---------------------------------------------------------------------------
def append_claim(connection: sqlite3.Connection, claim: Claim) -> int:
    """Append one claim to the log and return its sequence number."""
    return append_claims(connection, (claim,))


def append_claims(
    connection: sqlite3.Connection,
    claims: Iterable[Claim],
    *,
    max_pending: int | None = None,
    wait_timeout: float = 30.0,
) -> int:
    """Append claims in a single transaction and return the highest sequence.

    One transaction rather than one per claim: each COMMIT is expensive, and an
    agent typically emits a claim per file, so batching is the difference
    between a fast pass and a slow one.

    Args:
        max_pending: when set, wait for the merger to drain below this depth
            before appending. Off by default — section 1.11 owns the process
            pool and is the only layer that knows how many workers exist and
            what the memory budget is, so it supplies the policy while this
            provides the lever.
    """
    batch: Sequence[Claim] = tuple(claims)
    if not batch:
        return 0

    if max_pending is not None:
        wait_for_capacity(connection, max_pending, timeout=wait_timeout)

    now = utc_timestamp()
    rows = [(claim.id, claim.agent, claim.to_payload(), now) for claim in batch]

    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.executemany(
            "INSERT INTO claim_log (claim_id, agent_name, payload, appended_at)"
            " VALUES (?, ?, ?, ?)",
            rows,
        )
        highest = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.execute("COMMIT")
    except sqlite3.Error as exc:
        connection.execute("ROLLBACK")
        raise StorageError(f"could not append claims: {exc}") from exc

    return highest


def pending_depth(connection: sqlite3.Connection) -> int:
    """How many appended claims the merger has not yet drained."""
    row = connection.execute(
        "SELECT COUNT(*) FROM claim_log"
        " WHERE seq > (SELECT last_seq FROM merge_checkpoint WHERE id = 1)"
    ).fetchone()
    return int(row[0]) if row else 0


def wait_for_capacity(
    connection: sqlite3.Connection,
    max_pending: int,
    *,
    timeout: float = 30.0,
    poll_interval: float = 0.05,
) -> None:
    """Block until the log has drained below ``max_pending``.

    Without this the log grows unbounded when workers outrun the merger, which
    on a repository of a hundred thousand files is a real amount of disk.
    """
    deadline = time.monotonic() + timeout
    while pending_depth(connection) >= max_pending:
        if time.monotonic() >= deadline:
            raise StorageError(
                f"claim log still has {pending_depth(connection)} unmerged entries after "
                f"{timeout:g}s (limit {max_pending}). The merger is not keeping up."
            )
        time.sleep(poll_interval)
