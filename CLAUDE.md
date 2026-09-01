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
uv sync                                        # create/refresh the env, installs dev deps too
uv run python -m pytest                        # full suite
uv run python -m pytest tests/test_config.py   # one file
uv run python -m pytest -k redact              # one pattern
uv run python -m pytest -q -x                  # quiet, stop at first failure
uv run python -m ruff check .                  # lint
uv run python -m ruff format .                 # format (before finishing any section)
uv run python -m ruff format --check .         # verify formatting
uv run python -m scry                          # run the CLI
```

**Always invoke through `python -m`, never the bare `scry` / `pytest` / `ruff` commands.**
Those are unsigned `.exe` shims generated at install time, and Windows Smart App Control
(enforced on the dev machine, `VerifiedAndReputablePolicyState = 1`) blocks them with
`os error 4551`. The block is intermittent — a shim can work until `uv sync` regenerates it —
so `python -m` is the only reliable form. This is exactly why `src/scry/__main__.py` exists.

It also affects end users on Windows 11. The fix is code-signing at distribution time, which is
not yet scoped.

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
- **Update this file at the end of every section — before reporting completion.** Add the
  section's entry to *Implementation log*, flip its row in the phase table, move the ▶ marker to
  the next section, and add any newly discovered trap to *Gotchas already paid for*. The log is
  what lets a fresh session know what already exists without reading every file, so it is part
  of the section's deliverable, not an afterthought.

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

### Scry never writes into the repository it analyses

Workspaces live under `~/.scry/workspaces/`, never beside the target repo. The
spec contradicts itself here — §5 implies a marker in the repo, §6.1 puts it in
the workspace — and §6.1 wins, because the realistic user is studying a
repository they do not own. "Which workspace am I in?" is answered by matching
cwd against each marker's recorded `target_path`, not by walking up for a file.
`tests/test_workspace.py` snapshots the target directory and asserts it is
byte-identical afterwards.

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
| `PRAGMA foreign_keys` defaults to **OFF**, per connection | Every `REFERENCES` clause is decorative on any connection that missed it; orphan rows insert silently | Applied in `_apply_connection_pragmas`, which every connection goes through. A test asserts a violation actually raises |
| `Connection.executescript` COMMITs first | It discards the transaction wrapping a migration, so a failure leaves the schema half-changed | `split_statements()` uses `sqlite3.complete_statement` and executes one statement at a time inside an explicit BEGIN |
| `git log --follow` does not survive `--reverse` | Rename following lives in the history walk, so reversing it silently returns only the commits after the rename. Cost an hour chasing a fixture that was correct | `log()` in `tests/test_fixtures.py` takes `reverse=` rather than always adding it |
| The spec's example id `legacy-monolith-a7f3k9m2` is **not a valid Scry id** | It contains `9`, which base32 excludes, so it cannot be generated and confuses any pattern matching real ids | Use ids from the real alphabet (`a-z`, `2-7`) in docs and fixtures |
| `os.access(path, W_OK)` lies on Windows | It reports the read-only *attribute* and ignores ACLs, so it returns True for a directory you cannot write to | `diagnostics/system.check_writable()` writes a probe file and deletes it |
| **Never round-trip a source file through PowerShell** | `Get-Content -Raw` reads UTF-8 as the system ANSI codepage and `Set-Content` writes the mangled bytes back, turning `—` into `â€"` silently. It corrupted `router.py` once already | Use Edit/Write for file content. Reserve PowerShell for running commands |
| argparse calls `sys.exit()` on a parse error | Escapes the router, so `main(argv)` raises instead of returning, and its message bypasses the Console and `--no-color` | `CommandParser` in `cli/router.py` overrides `exit` and `_print_message` |
| Smart App Control blocks generated `.exe` shims | `uv run pytest` fails with `os error 4551`, intermittently — a shim can work until `uv sync` regenerates it | Always `uv run python -m <tool>` |
| A context manager entered without being held | `writer(path).__enter__()` leaves the manager unreferenced, so GC closes the connection under you — surfaced as `Cannot operate on a closed database` | Use `connect_writer()` when a connection must outlive a `with` block |
| `spawn` children cannot import the test module | Concurrency tests died with `ModuleNotFoundError: No module named 'tests'` before unpickling the target function | `pythonpath = ["."]` in pytest config — spawn hands the parent's `sys.path` to the child |
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
| `cli/registry.py` | Command **metadata** only; the module is a string and nothing is imported until a command is selected. Register new commands here as their sections land, so `--help` lists only what works. |
| `cli/router.py` | Global flags, dispatch, and failure→exit-code translation. Configures logging with `console=False` so `Console` owns all terminal output. |
| `cli/output.py` | The results-on-stdout / everything-else-on-stderr rule. |
| `workspace/ids.py` | Names normalise to lowercase (Windows filesystems are case-insensitive) and **reject Windows reserved device names** (`con`, `nul`, `com1`…), which otherwise fail as an unrelated filesystem error. Ids use base32, whose alphabet omits `0`,`1`,`8`,`9` — no `0`/`O` confusion when retyping. |
| `workspace/manager.py` | Creation writes the marker **last**, so the marker's presence *is* the definition of a complete workspace and an interrupted run leaves detectable debris. Id collisions are handled by checking the directory and regenerating, not by bigger numbers. |

---

## Implementation log

What each completed section actually delivered, and what it leaves behind for later ones to
use. Appended to as each section finishes.

*Maintenance rule: keep full entries for the phase in progress. When a phase completes, compress
its entries into a single paragraph so this section does not grow without bound across 93
sections.*

---

### ✅ 1.1 — Project scaffolding & repo hygiene · 8 tests

**Built:** `src/` layout, hatchling build, `scry` console script and `python -m scry`,
ruff + pytest config, `.gitignore`, `.gitattributes`, `README.md`.

**Decided:** `requires-python >=3.11` although development is on 3.13, with
`ruff target-version = "py311"` as the net that stops 3.12+ syntax reaching users · all four
runtime dependencies declared up front, so a missing wheel surfaces now rather than mid-Phase 5
(all resolved cleanly on 3.13) · `.gitattributes` pins LF, which the golden-file tests from 2.11
and the byte-identical brief from 3.6 both depend on.

**Leaves behind:** `scry.__version__` · a working `main()` stub at `scry.cli.main` for 1.7 to
replace · the import-hygiene test that every later section inherits.

---

### ✅ 1.2 — Config system · +45 tests (53)

**Built:** `config/{defaults,schema,loader}.py` — five-layer merge (defaults → global yaml →
workspace yaml → `SCRY_*` env → CLI flags), frozen dataclasses, per-value provenance.
Also created `util/errors.py` with `ScryError` + `ConfigError`, since the loader needed them
before 1.3 existed.

**Decided:** layers merge as **plain dicts** and the typed `Config` is built once at the end —
per-layer construction would make "explicitly set" indistinguishable from "defaulted" ·
provenance tracking, which gives error messages their filename for free · unknown keys warn
rather than fail · **no `api_key` field anywhere**, overruling §18.1 in favour of §8's
keyring-only rule · salience weights accept any non-negative values and normalise at use, so
3.9's sweep is "vary one number".

**Leaves behind:** `load_config()` with a `workspace_path` parameter 1.8 will fill, a
`cli_overrides` parameter 1.7 will fill, and an `on_warning` sink 1.3 routes to the logger ·
every tunable for Archivist, Salience, Skeptic, storage, security and TUI already named and
validated · a pickle round-trip test that 1.11 depends on.

---

### ✅ 1.3 — Logging, error taxonomy, redaction filter · +81 tests (134)

**Built:** `util/redact.py` (`RedactingFilter` + provider patterns), `util/logging.py`
(`setup_logging`, `reset_logging`, `bootstrap`), and the remaining five exception types with
`ExitCode`.

**Decided:** the filter attaches to **handlers, not loggers**, the only placement that sees
child loggers' records · records are formatted eagerly so secrets are caught in `msg`, `args`
**and tracebacks** · precise provider patterns with no entropy, so commit shas and UUIDs survive
· exit codes live on the exception classes so a future subclass cannot be forgotten from a
mapping table · `bootstrap()` buffers config warnings and replays them once the logger exists.

**Leaves behind:** `bootstrap()` as the standard startup path for 1.7 · `console=False` for the
TUI in 5.12 · `GitError(command=, stderr=)` ready for 2.1 · a docstring warning that 1.11 must
add `setup_worker_logging(queue)` before spawning.

---

### ✅ 1.4 — Workspace model, ID generation, resolution · +80 tests (214)

**Built:** `workspace/{ids,paths,marker,manager}.py` — create, resolve by three routes, list,
and detect incomplete workspaces. Moved `scry_home()` into `util/paths.py`.

**Decided:** the marker lives **workspace-side**, resolving the §5/§6.1 contradiction in favour
of never writing into the analysed repo · 8-character base32 ids per the documented format, with
collisions handled by checking the directory rather than by more entropy · names lowercase and
Windows device names refused · the marker is written **last**, so its presence defines a
complete workspace · `mode` defaults to `"auto"` because §7 makes backends detected, not fixed
at creation.

**Leaves behind:** `Workspace.paths.session_db` and `.graph_db` for 1.5 to create ·
`resolve_workspace()` for 1.7's router · `create_workspace()` for 1.8's `scry init` ·
`find_incomplete_workspaces()` for 1.9's `doctor` · `workspace.paths.config` as the
`workspace_path` 1.2's loader has been waiting for.

---

### ✅ 1.5 — Storage A: schema, connection, migrations · +29 tests (243)

**Built:** `storage/{db,migrate}.py` and `migrations/001_core.sql` — WAL-configured connection
factory, forward-only migration runner, and the core tables (`session_state`, `agent_state`,
`claim_log`, `claims`, `merge_checkpoint`).

**Decided:** **one database file, not two.** §6.1/§11.2 show `session.db` and `graph.db`
separately; merged because every high-value analysis is a cross-domain join — salience over
churn × complexity × ownership × exposure, CR-1, CR-9 — which is ordinary SQL in one file and an
ATTACH dance across two. `paths.session_db`/`graph_db` became `paths.database` (`scry.db`) ·
read-only connections make single-writer discipline structural, not remembered · migrations are
forward-only, since a rebuildable cache does not justify reversible-migration machinery ·
domain tables wait for the sections that own them, so Phase 2 becomes the runner's first real
test.

**Leaves behind:** `initialise_database()` for 1.8's `scry init` · `writer()`/`reader()` context
managers and the `claim_log` → `claims` → `merge_checkpoint` trio for 1.6 · `agent_state` for
1.11/1.12 · `session_state.last_analyzed_commit` as the anchor for 2.10 and 8.5 · a proven
migration path for Phase 2's `002`.

---

### ✅ 1.6 — Storage B: claim log & single-writer merge · +45 tests (288)

**Built:** `storage/{claims,merge}.py` — the `Claim`/`Evidence` model, `append_claim(s)`,
backpressure, and the batched drain from `claim_log` into `claims`. Added `util/clock.py` so the
timestamp format is defined once.

**Decided:** claim ids are a **hash of what the claim is about** — agent, type, target,
assertion — and exclude confidence and evidence, which are its value rather than its identity.
That makes 1.12's agent respawns and 8.5's incremental re-runs idempotent for free instead of
needing a dedup pass · the agent is *in* the hash, so one agent restating collapses to one row
while two agents claiming about the same file stay separate for 5.7 to corroborate · each batch
merges rows **and** advances the checkpoint in one transaction, which is what makes crash
recovery free · highest `seq` wins; §3.1's noisy-OR needs the confidence table and waits for 5.7
· `status` survives an unchanged restatement but returns to `pending` when the assertion or
confidence moved · **evidence snippets are redacted at construction**, so a secret never enters
the graph at all rather than relying on 6.2 to catch it leaving · named `merge.py`, not the
plan's `writer.py`, which would collide with `db.writer()`.

**Leaves behind:** `append_claims()` for every Phase 2+ agent · `merge_claims()` for 1.11 to run
as a supervised process · `pending_depth()`/`wait_for_capacity()` as the backpressure lever 1.11
sets policy on · `Claim`/`Evidence` as the wire format for 1.11's message bus.

---

### ✅ 1.7 — CLI parser & router · +35 tests (323)

**Built:** `cli/{registry,router,context,output,colors}.py`, `cli/commands/{version,resume}.py`,
and a thin `main.py`. Global flags, exit-code translation, colour detection, and the
`scry <name>-<uid>` resume path.

**Decided:** dispatch is a **registry of metadata with lazily imported handlers**, not argparse
subparsers — subparsers need every command's arguments at startup, so `scry why` would import
textual and NetworkX before printing a line · **`-v` is `--verbose`, `-V` is `--version`**,
inverting §5 because `-v` means verbose across the whole toolchain and the spec's mapping is a
trap for the exact user we build for · commands win a name collision with a workspace, with the
full id as the escape hatch · an id-shaped token that misses exits 3, a non-id-shaped one exits
2, so a typo reads as a typo · the router does load-config → setup-logging → replay itself
rather than calling `bootstrap()`, with `console=False`, so the Console owns every character the
user sees and tracebacks never reach the terminal · unexpected exceptions print a pointer to the
log, and `--verbose` re-raises · CLI output stays ASCII, since console encodings vary on Windows.

**Leaves behind:** `COMMANDS` for every later section to register into · `Context(config,
logger, console, home, json_output, verbose)` as the handler contract · `Console` enforcing
results-on-stdout · `AGENT_COLORS`/`SEMANTIC_COLORS` for the TUI in 5.12 · a `--json` path
already proven on two commands.

---

### ✅ 1.8 — `scry init` · +43 tests (366)

**Built:** `cli/commands/init.py`, `RESERVED_COMMAND_NAMES` in the registry,
`workspace.same_path()`, and `CommandParser` in the router.

**Decided:** the **database is created after the marker**, and that is the model rather than a
gap: the marker is identity (written once, presence = complete workspace), the database is a
rebuildable cache `initialise_database()` can recreate at any time · **`--force` adds a
workspace and never overwrites one** — a flag that could silently destroy completed analysis
with no undo is not worth having · reserved names cover all ~21 *planned* commands, not just the
one registered, so a workspace named `doctor` today cannot shadow `scry doctor` next week ·
`--mode standard|pro` gets a migration message naming what replaced it · a non-git target warns
rather than refuses, since Oracle, Pathologist and Semiotician all work without git · name
errors return exit 2, not `WorkspaceError`'s exit 3, because a typo in an argument is not a
missing workspace · §5's `Session DB` and `Knowledge Graph` lines collapse to one `Database`
line, and `Mode: auto` says "backend detected at run time" rather than inventing a claim.

**Fixed in the router:** argparse called `sys.exit()` on a parse error, escaping the router and
writing to the real stderr — so `main(argv)` raised instead of returning and the message bypassed
`--no-color`. `CommandParser` overrides `exit` and `_print_message` so every usage error now
returns a code through the Console.

**Leaves behind:** working `scry init` for 1.9's doctor to diagnose and 3.7's `scry map` to
consume · `CommandParser` for every later command's arguments · `RESERVED_COMMAND_NAMES` to
extend as commands land.

---

### ✅ 1.9 — `scry doctor` v1 · +39 tests (405)

**Built:** `diagnostics/{checks,system}.py` and `cli/commands/doctor.py` — environment,
resources, storage, backend and per-workspace checks, plus additive `--repair`.

**Decided:** **no check may raise** — `_safe()` turns any exception into a FAIL, because a doctor
that crashes while diagnosing turns one confusing failure into two · **`--repair` only adds**:
it creates missing databases and directories and never deletes, so an incomplete workspace is
reported with its path for the user to remove themselves · a **missing LLM backend is `OK`, not
`WARN`** — lite is fully supported and yellow would teach every user they are degraded ·
writability is tested by *writing*, since `os.access` reports the read-only attribute and ignores
ACLs on Windows · RAM probed per-platform with stdlib rather than adding `psutil` · warnings do
not fail the exit code, only failures do · a minimal `git --version` call here, with 2.1 owning
the real git layer.

**Fixed in the router:** an unloadable `config.yaml` crashed before dispatch, so `scry doctor`
could not diagnose the one thing users most need it for. The failure is now captured on the
Context; doctor alone proceeds, every other command stops with the error rather than silently
running on substituted defaults.

**Leaves behind:** `run_checks()`/`Diagnosis` for 6.1 to add real backend detection to and 1.12's
Conductor to consume · `repair()` closing the missing-database gap 1.8 leaves open ·
`Context.config_path`/`config_error` · `system.py` probes for the 2.12 benchmark harness.

---

### ✅ 1.10 — Test harness & fixtures · +30 tests (435)

**Built:** `tests/fixtures/{gitrepo,cli,golden}.py` and `tests/conftest.py` — a synthetic git
repository builder, shared CLI invocation helpers, and golden-file comparison. Removed the
duplicated fixtures from `test_cli`, `test_init`, `test_doctor`, `test_logging` and
`test_workspace`.

**Decided:** repositories are built with **`git fast-import`**, one process for a whole history —
per-commit subprocesses cost 20-40 ms each on Windows and would put fifty commits at three to
five seconds against a one-second target · built with **real git** rather than hand-written
objects, so our fixtures cannot diverge from what git actually reads · timestamps are
**`days_ago` relative to a fixed `REFERENCE_TIME`**, because a fixture on absolute dates would
make 2.5's decay assertions drift with the calendar · `core.autocrlf=false` in every built repo,
or Windows rewrites the bytes every content assertion compares · the builder is verified **by
`git log`**, never by our own parser, since checking a git-writer with a git-reader we also wrote
proves nothing.

**Leaves behind:** `build_repo()`/`Commit`/`linear_history()` for every Phase 2 test ·
`REFERENCE_TIME`, which **2.5's churn must accept as an injectable clock** · `invoke()`/`Result`/
`snapshot()` shared by the command tests · `assert_golden()` with scrubbing for 2.11 and 3.6 ·
`make_repo`, `workspace`, `initialised_workspace` fixtures · `--update-golden`.

---

## Progress: 8 phases, 93 sections

Legend: ✅ done · ▶ next · ⬜ pending

### Phase 1 — Foundation & skeleton
| § | Section | Status |
|---|---|---|
| 1.1 | Project scaffolding & repo hygiene | ✅ |
| 1.2 | Config system (5 layers, provenance, validation) | ✅ |
| 1.3 | Logging, error taxonomy, redaction filter | ✅ |
| 1.4 | Workspace model, ID generation, resolution | ✅ |
| 1.5 | Storage A — schema, connection, WAL, migrations | ✅ |
| 1.6 | Storage B — claim log & single-writer merge | ✅ |
| 1.7 | CLI parser & router | ✅ |
| 1.8 | `scry init` | ✅ |
| 1.9 | `scry doctor` v1 | ✅ |
| 1.10 | Test harness & synthetic git repo builder | ✅ |
| 1.11 | Runtime harness (multiprocessing, message bus) | ▶ |
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
