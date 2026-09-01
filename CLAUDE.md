# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## What Scry is

A terminal-native tool for **software archaeology**: it maps an unknown codebase and tells you
what matters before you know what to ask. Target user is a developer dropped into a large,
undocumented, legacy repository with no mentor.

### The governing principle — read this before writing any code

> **An LLM never produces a fact that can be computed. It only produces language about facts
> that were computed.**

Nine of the eleven components are pure deterministic code — git miners, static analyzers,
dependency resolvers, graph algorithms. Facts carry **provenance**: you can point at the line
that proves them. An LLM-produced fact is unfalsifiable — a hallucinated taint path looks
identical to a real one — which is fatal for a tool selling "validated discovery". This is a
correctness argument first and a cost argument second.

Practical consequence: **the full analysis runs with no model, no API key, and no network.**
Findings and their ranking are byte-identical whether or not a model is available; only the
prose differs. Never introduce a code path where a missing model changes *what is found*.

---

## Commands

This project uses [uv](https://docs.astral.sh/uv/). Do not use bare `pip` or `python`.

```bash
uv sync                              # create/refresh the env, installs dev deps too
uv run pytest                        # full suite
uv run pytest tests/test_config.py   # one file
uv run pytest -k redact              # one pattern
uv run pytest -q -x                  # quiet, stop at first failure
uv run ruff check .                  # lint
uv run ruff format .                 # format (run before finishing any section)
uv run ruff format --check .         # verify formatting
uv run python -m scry                # run the CLI
```

**Use `python -m scry`, not `scry`.** The generated `scry.exe` shim is unsigned, and Windows
Smart App Control (enforced on the dev machine) blocks it. This is exactly why
`src/scry/__main__.py` exists. It also affects end users on Windows 11 — the eventual fix is
code-signing at distribution time, which is not yet scoped.

---

## Working agreement

- **Never run `git add`, `git commit`, or `git push`.** Write code, run tests and lint, then
  stop and report. The user reviews every diff in VS Code's Source Control panel and commits
  themselves. Read-only git (`status`, `log`, `diff`, `show`) is fine.
- **Never add `Co-Authored-By` trailers** to commit messages. GitHub credits the address as a
  repository contributor, which the user does not want.
- **One section at a time.** Work proceeds strictly 1.1 → 1.2 → … → 8.9. Before implementing,
  explain the section in detail and get explicit approval. A section is done only when its
  tests pass, lint and format are clean, and no earlier test regresses.

---

## Architecture invariants

These span multiple files and are easy to violate with innocent-looking code.

### Lazy imports for leaf dependencies

`Jinja2`, `networkx` and `textual` must be imported **inside the functions that use them**,
never at module scope. Fast commands (`scry why`, `scry owners`) promise sub-second answers and
must not pay their import cost. Enforced by `tests/test_import_hygiene.py`, which checks
`sys.modules` in a fresh subprocess — checking inside the pytest process is meaningless because
other tests will already have imported them.

Core path (`cli`, `config`, `util`, storage, runtime) stays **stdlib only**, with PyYAML as the
single deliberate exception.

### Logs go to stderr, never stdout

`scry hotspots --json | jq` must emit clean JSON. One log line on stdout corrupts it.

### Config is frozen and must stay picklable

Section 1.11 spawns worker processes. On Windows that is `spawn`, not `fork`, so each worker
receives a **pickled copy** of the config. A mutable copy would let one worker diverge silently
from the others, producing analysis that differs by process with nothing in the logs to explain
it. Every field must be a primitive, a tuple, or a nested frozen dataclass — no lambdas, no
open handles. `tests/test_config.py` asserts a pickle round-trip.

### Redaction filters attach to handlers, never to loggers

A filter on a *logger* runs only for records logged directly to that logger. A record from
`scry.agents.pathologist` propagates to the `scry` logger's **handlers**, but `scry`'s
**filters are never consulted**. A filter installed on the `scry` logger would look correct and
silently miss every agent's records.

### API keys never appear in configuration

The schema has no `api_key` field anywhere, and a test walks the dataclass tree to prove it.
Keys live in the OS keyring (section 6.4). The v2 spec showed keys in `config.yaml`; that was
deliberately overruled — a key in a YAML file eventually gets committed to a repository.

### Git access is subprocess-only

Stream `git log --numstat` and `git blame --porcelain`. No `pygit2`, no `pydriller` — pydriller
materialises a Python object per commit and cannot survive a 100k-commit history. Zero binary
dependencies keeps the install trivial on every platform.

---

## Code conventions

- **Frozen dataclasses over pydantic** on the always-loaded path.
- **`from __future__ import annotations`** at the top of every module.
- Line length **100**. Ruff target is **py311** even though we develop on 3.13 — this is the
  safety net that stops 3.12+ syntax reaching users on the 3.11 floor we promise.
- **Comments explain *why*, never *what*.** Prefer one paragraph on a non-obvious decision over
  five lines restating the code. Where a subtle trap was avoided, name the trap.
- **Tests assert behaviour, not implementation.** "Logging twice produces one line" beats
  "the logger has two handlers" — the second breaks when pytest attaches its own handler.
- Tests are inline per section through Phase 3, then batched per phase from Phase 4.

---

## Gotchas already paid for

Do not rediscover these.

| Trap | What happens | Guard |
|---|---|---|
| `bool` subclasses `int` | `batch_size: true` passes an `isinstance(v, int)` check and becomes `1` | Explicit `isinstance(v, bool)` rejection in `config/loader.py` |
| pytest attaches `LogCaptureHandler` to the `scry` logger | An "is anything already configured?" guard sees it and skips setup entirely, leaving Scry silently unlogged | Handlers are tagged `_scry_owned_handler`; idempotency and teardown key on ownership |
| Secrets hide in three places in a `LogRecord` | `msg`, `args`, and **tracebacks** — `raise ValueError(f"bad {t}")` puts the value in none of the first two | `RedactingFilter` formats eagerly and scrubs `exc_text`/`stack_info` |
| Entropy-based secret detection in logs | Would redact every commit sha and UUID, destroying the logs' usefulness | Precise provider patterns only; entropy belongs in Pathologist (4.8) |
| **Credential-shaped test fixtures block the push** | GitHub push protection rejected the first push over Slack- and Stripe-shaped strings in `tests/test_redact.py`. It also defeats same-line `"prefix-" + "body"` splitting | Fixtures are assembled from a prefix plus a generated body (`body()` in `tests/test_redact.py`) so no credential-shaped literal exists in the source. **Every Pathologist fixture from 4.8 onward must follow this.** Never click GitHub's "allow secret" link — for this project that is training yourself to ignore the exact signal you are building |
| PowerShell wraps native stderr as an error | `git push` "fails" while actually succeeding | Read the output, not just the exit status; or use the Bash tool |
| `git filter-branch` deprecation warning | Noisy but harmless | `FILTER_BRANCH_SQUELCH_WARNING=1` |

Traps already anticipated in the plan, not yet reached:

- **Coupling commit-size cap** — a 500-file refactor commit yields 124,750 co-change pairs of
  pure noise. Commits over `salience.coupling_max_files_per_commit` (default 20) contribute none.
- **Percentile, not min-max normalisation** — these distributions are heavy-tailed; min-max lets
  one 10,000-commit file flatten every other file to ~0.
- **Word boundaries in bugfix regexes** — naive `fix` matches "prefix" and "suffix". Already
  encoded in `config/defaults.py:BUGFIX_PATTERNS`.
- **Bot filtering is mandatory** — left in, dependabot dominates churn and ownership on most
  real repositories and poisons every downstream metric.
- **`RotatingFileHandler` is not multiprocess-safe** — 1.11 must add
  `setup_worker_logging(queue)` before spawning anything. See the warning block in
  `src/scry/util/logging.py`.

---

## Module map

Only what is not obvious from the tree.

| Module | Role |
|---|---|
| `config/defaults.py` | Every tunable **value**, in one place. Weights live here so the 3.9 sweep can vary them. |
| `config/schema.py` | Every tunable **shape**. Frozen dataclasses only. |
| `config/loader.py` | Five-layer merge. Layers merge as **plain dicts**, and the typed `Config` is built **once** at the end — building per layer would make "explicitly set" indistinguishable from "defaulted". |
| `util/errors.py` | `ScryError` hierarchy. **Exit codes live on the classes**, not in a CLI mapping table a new subclass could be forgotten from. |
| `util/redact.py` | Last-resort log safety net. *Not* the secret detector. |
| `util/logging.py` | `setup_logging`, `reset_logging`, and `bootstrap()` — which resolves the ordering problem that logging needs config while config wants to log. |

---

## Progress: 8 phases, 93 sections

Legend: ✅ done · ▶ next · ⬜ pending

### Phase 1 — Foundation & skeleton
| § | Section | Status |
|---|---|---|
| 1.1 | Project scaffolding & repo hygiene | ✅ |
| 1.2 | Config system (5 layers, provenance, validation) | ✅ |
| 1.3 | Logging, error taxonomy, redaction filter | ✅ |
| 1.4 | Workspace model, ID generation, resolution | ▶ |
| 1.5 | Storage A — schema, connection, WAL, migrations | ⬜ |
| 1.6 | Storage B — claim log & single-writer merge | ⬜ |
| 1.7 | CLI parser & router | ⬜ |
| 1.8 | `scry init` | ⬜ |
| 1.9 | `scry doctor` v1 | ⬜ |
| 1.10 | Test harness & synthetic git repo builder | ⬜ |
| 1.11 | Runtime harness (multiprocessing, message bus) | ⬜ |
| 1.12 | Conductor v1 (rule engine, state machine) | ⬜ |

### Phase 2 — Archivist & the git engine
| § | Section | Status |
|---|---|---|
| 2.1 | Git subprocess layer + baseline benchmark | ⬜ |
| 2.2 | Commit stream parser (constant memory) | ⬜ |
| 2.3 | File identity & rename tracking | ⬜ |
| 2.4 | Author identity, `.mailmap`, bot filtering | ⬜ |
| 2.5 | Churn computation (90-day half-life decay) | ⬜ |
| 2.6 | Indentation complexity & file filter | ⬜ |
| 2.7 | Blame & ownership (budgeted) | ⬜ |
| 2.8 | Knowledge loss | ⬜ |
| 2.9 | Bugfix commit classification | ⬜ |
| 2.10 | Archivist agent assembly | ⬜ |
| 2.11 | Commands: `why`, `owners`, `hotspots` | ⬜ |
| 2.12 | Benchmark corpus + P0 performance gate | ⬜ |

### Phase 3 — Salience Engine & Onboarding Brief · *first shippable product*
| § | Section | Status |
|---|---|---|
| 3.1 | Temporal coupling extraction | ⬜ |
| 3.2 | Coupling graph & centrality | ⬜ |
| 3.3 | Normalization framework | ⬜ |
| 3.4 | Salience Engine (+ explainability) | ⬜ |
| 3.5 | Brief data model (citations mandatory) | ⬜ |
| 3.6 | Template renderer (byte-identical output) | ⬜ |
| 3.7 | `scry map` — lite path | ⬜ |
| 3.8 | Commands: `coupled`, `risk` | ⬜ |
| 3.9 | Salience sanity eval & weight sweep | ⬜ |
| 3.10 | Export (markdown, json) | ⬜ |

### Phase 4 — Oracle & secret scanning *(tests batched at 4.12)*
| § | Section | Status |
|---|---|---|
| 4.1 | Ecosystem registry & lockfile discovery | ⬜ |
| 4.2 | Lockfile parsers A — JS & Python | ⬜ |
| 4.3 | Lockfile parsers B — JVM, Go, Rust | ⬜ |
| 4.4 | Lockfile parsers C — Ruby, PHP, .NET | ⬜ |
| 4.5 | Transitive graph & dependency centrality | ⬜ |
| 4.6 | OSV snapshot & version matching *(hardest in phase)* | ⬜ |
| 4.7 | Oracle assembly + `scry deps` | ⬜ |
| 4.8 | Secret detection engine | ⬜ |
| 4.9 | Historical blob scanning | ⬜ |
| 4.10 | Pathologist (secrets) + `scry secrets` | ⬜ |
| 4.11 | Exposure signal → salience `w5` | ⬜ |
| 4.12 | Phase 4 test & eval pass | ⬜ |

### Phase 5 — Semiotician, Skeptic, contradictions, TUI *(tests batched at 5.17)*
| § | Section | Status |
|---|---|---|
| 5.1 | Language-agnostic tokenizer/normalizer | ⬜ |
| 5.2 | Winnowing fingerprints & exact clone groups | ⬜ |
| 5.3 | Near-clone similarity & clone group model | ⬜ |
| 5.4 | Structural peer grouping & convention inference | ⬜ |
| 5.5 | Copy-paste divergence | ⬜ |
| 5.6 | Semiotician agent assembly | ⬜ |
| 5.7 | Confidence table & combination rules | ⬜ |
| 5.8 | Skeptic rule engine | ⬜ |
| 5.9 | Contradiction rules CR-1 … CR-3 | ⬜ |
| 5.10 | Contradiction rules CR-4 … CR-6 | ⬜ |
| 5.11 | Debate log model & resolution protocol | ⬜ |
| 5.12 | TUI foundation | ⬜ |
| 5.13 | TUI panels — architecture map & agent streams | ⬜ |
| 5.14 | TUI panels — debate log & system status | ⬜ |
| 5.15 | TUI command bar, `@mention`, shortcuts | ⬜ |
| 5.16 | Calibration harness & confidence gate | ⬜ |
| 5.17 | Phase 5 test & eval pass | ⬜ |

### Phase 6 — LLM layer *(tests batched at 6.10)*
| § | Section | Status |
|---|---|---|
| 6.1 | Backend detection + doctor integration | ⬜ |
| 6.2 | **Privacy boundary — built first** | ⬜ |
| 6.3 | LLM gateway (LiteLLM, retry, fallback, cache) | ⬜ |
| 6.4 | Keyring & first-run consent | ⬜ |
| 6.5 | Budget controller | ⬜ |
| 6.6 | Scribe — narratives from graph facts | ⬜ |
| 6.7 | Scribe — brief prose rendering | ⬜ |
| 6.8 | Scribe — semantic labeling of top-N functions | ⬜ |
| 6.9 | Skeptic escalation, batching, `scry query` | ⬜ |
| 6.10 | Phase 6 test & eval pass (content-identity test) | ⬜ |

### Phase 7 — Cartographer & the call graph *(tests batched at 7.11)*
| § | Section | Status |
|---|---|---|
| 7.1 | Language detection & capability matrix | ⬜ |
| 7.2 | Indexer spike: stack-graphs vs SCIP | ⬜ |
| 7.3 | Symbol & binding model + storage | ⬜ |
| 7.4 | Call graph + possible-callee sets | ⬜ |
| 7.5 | Entry point detection | ⬜ |
| 7.6 | Call centrality → salience `w6` | ⬜ |
| 7.7 | `who-calls`, `calls`, `blast-radius`, `entry-points` | ⬜ |
| 7.8 | Contradiction rules CR-7, CR-10 | ⬜ |
| 7.9 | CR-9 — CVE × reachability join | ⬜ |
| 7.10 | Dead-code detection with dynamic-load evidence | ⬜ |
| 7.11 | Phase 7 test + call-edge precision gate | ⬜ |

### Phase 8 — Hematologist, watch, second language *(tests batched at 8.9)*
| § | Section | Status |
|---|---|---|
| 8.1 | Taint engine spike: Joern vs semgrep | ⬜ |
| 8.2 | Source / sink / sanitizer models | ⬜ |
| 8.3 | Demand-driven taint runner | ⬜ |
| 8.4 | CR-8 + `scry unsafe` | ⬜ |
| 8.5 | Incremental analysis | ⬜ |
| 8.6 | `scry watch` + map-diff reporting | ⬜ |
| 8.7 | Second language, end-to-end | ⬜ |
| 8.8 | Full v1 verification sweep | ⬜ |
| 8.9 | Phase 8 test pass + release checklist | ⬜ |

**Update the status column when a section completes**, and add any newly discovered trap to
*Gotchas already paid for*.

---

## Deferred decisions and where they get closed

Resolved by measurement, not discussion. None may quietly go missing.

| Deferred | Closed by |
|---|---|
| Benchmark corpus selection | 2.12 |
| Salience weights `w1…w6` | 3.9 (re-run at 4.11 and 7.6) |
| Confidence constants calibration | 5.16 |
| semgrep native Windows support | 5.17 |
| Cartographer's first target language | 7.2 |
| Rust port of the parse/index layer | 2.12 / 8.8, only if a gate fails |
| Licence (Apache-2.0 vs MIT) | before any public release |
| Code-signing for the Windows `scry.exe` shim | distribution phase, not yet scoped |

---

## Full specification

The authoritative architecture spec and implementation plan live at
`C:\Users\hareesh\.claude\plans\and-these-are-some-snoopy-sparrow.md` — sections §1–§10 describe
the architecture, §11 the 93-section implementation plan, §12 the verification gates. Consult it
when a section's intent is unclear; this file is the summary, that file is the source.
