-- Core schema.
--
-- Only the tables sections 1.6, 1.11 and 1.12 need immediately. Domain tables
-- arrive with the sections that own them and know their real columns: files,
-- authors and churn in phase 2; coupling and salience in phase 3; dependencies
-- and secrets in phase 4. Guessing their shape now would only mean migrating
-- twice, and it means phase 2 exercises the migration runner on a genuine case
-- rather than leaving it untested until someone needs it in anger.


-- Mutable workspace state.
--
-- Identity — id, name, target_path, created_at, mode — lives in the .scry
-- marker and is never rewritten. This table holds only what changes, so the two
-- cannot drift apart. Spec section 11.3 put both in one table; section 1.4
-- established the split.
--
-- Exactly one row, enforced by the schema rather than by convention.
CREATE TABLE session_state (
    id                   INTEGER PRIMARY KEY CHECK (id = 1),
    status               TEXT    NOT NULL DEFAULT 'created'
                         CHECK (status IN ('created', 'exploring', 'paused',
                                           'consensus', 'exported', 'degraded', 'failed')),
    last_accessed        TEXT,
    llm_calls_used       INTEGER NOT NULL DEFAULT 0 CHECK (llm_calls_used >= 0),
    llm_cost_usd         REAL    NOT NULL DEFAULT 0.0 CHECK (llm_cost_usd >= 0.0),
    -- Anchor for the incremental re-analysis of sections 2.10 and 8.5.
    last_analyzed_commit TEXT,
    created_at           TEXT    NOT NULL
);

INSERT INTO session_state (id, created_at)
VALUES (1, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));


-- Per-agent runtime state: heartbeats, crash counts, current work.
-- Read by the Conductor's rules in section 1.12.
CREATE TABLE agent_state (
    agent_name     TEXT PRIMARY KEY,
    status         TEXT    NOT NULL DEFAULT 'idle'
                   CHECK (status IN ('idle', 'running', 'paused',
                                     'crashed', 'completed', 'error')),
    current_task   TEXT,
    belief_state   TEXT,
    last_heartbeat TEXT,
    crash_count    INTEGER NOT NULL DEFAULT 0 CHECK (crash_count >= 0),
    progress       REAL    CHECK (progress IS NULL OR (progress >= 0.0 AND progress <= 1.0))
);


-- Append-only claim log.
--
-- Worker processes only ever append here; they never touch `claims`. A single
-- writer process drains this into the merged registry (section 1.6). That is
-- what keeps eight concurrent writers from contending on the same rows.
CREATE TABLE claim_log (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id    TEXT NOT NULL,
    agent_name  TEXT NOT NULL,
    payload     TEXT NOT NULL,          -- JSON, shaped per spec section 9.3.1
    appended_at TEXT NOT NULL
);

CREATE INDEX idx_claim_log_claim_id ON claim_log (claim_id);


-- The merged claim registry. Written only by the single writer process.
--
-- `uncertain` is a first-class terminal status, not an oversight: in lite mode
-- the Skeptic has no model to escalate ambiguous claims to, and such claims must
-- be surfaced to the user rather than silently dropped.
CREATE TABLE claims (
    id              TEXT    PRIMARY KEY,
    agent_name      TEXT    NOT NULL,
    claim_type      TEXT    NOT NULL,
    target_file     TEXT,
    target_symbol   TEXT,
    target_line     INTEGER CHECK (target_line IS NULL OR target_line > 0),
    assertion       TEXT    NOT NULL,
    confidence      REAL    NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    status          TEXT    NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'challenged', 'validated',
                                      'retracted', 'uncertain')),
    evidence_json   TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT,
    merged_from_seq INTEGER REFERENCES claim_log (seq)
);

CREATE INDEX idx_claims_status ON claims (status);
CREATE INDEX idx_claims_target_file ON claims (target_file);
CREATE INDEX idx_claims_type ON claims (claim_type);


-- How far the writer has drained the claim log. Lets a merge interrupted by a
-- crash resume exactly where it stopped instead of replaying from the start.
CREATE TABLE merge_checkpoint (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    last_seq   INTEGER NOT NULL DEFAULT 0 CHECK (last_seq >= 0),
    updated_at TEXT
);

INSERT INTO merge_checkpoint (id, last_seq) VALUES (1, 0);
