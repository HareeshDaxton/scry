"""Default configuration values, collected in one readable place.

``schema.py`` defines the *shape* of the configuration; this module defines the
*values*. Keeping them apart means the tunables a user or a benchmark sweep
would actually want to change are all visible in a single file, instead of
scattered through dataclass definitions.
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
LOG_LEVEL = "INFO"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5

# --------------------------------------------------------------------------
# Agent runtime (spec section 18.1, "agents")
# --------------------------------------------------------------------------
AGENTS_MAX_CONCURRENT = 10
AGENT_TIMEOUT_SECONDS = 300
HEARTBEAT_INTERVAL_SECONDS = 10
HEARTBEAT_TIMEOUT_SECONDS = 30

# --------------------------------------------------------------------------
# Archivist (section 2.2 - 2.9)
# --------------------------------------------------------------------------
CHURN_HALF_LIFE_DAYS = 90.0

# Bot filtering is on by default and is not cosmetic: left in, automated
# accounts dominate churn and ownership on most real repositories and would
# poison hotspot ranking, bus factor, and knowledge-loss detection alike.
EXCLUDE_BOTS = True
BOT_PATTERNS: tuple[str, ...] = (
    "*[bot]",
    "dependabot*",
    "renovate*",
    "greenkeeper*",
    "github-actions*",
    "snyk-bot*",
    "imgbot*",
    "allcontributors*",
    "*-ci",
    "*-bot",
)

# `git blame` costs one subprocess per file, so it is budgeted rather than run
# across the whole tree. Coverage is recorded so the Onboarding Brief can state
# what it actually analysed instead of implying whole-repository blame.
BLAME_BUDGET_FILES = 500

# Merge commits double-count changes in numstat output, so they are excluded
# from churn by default.
INCLUDE_MERGE_COMMITS = False

# Word boundaries are load-bearing. The naive pattern "fix" matches "prefix"
# and "suffix", silently inflating defect density on any repository that
# mentions either word.
BUGFIX_PATTERNS: tuple[str, ...] = (
    r"\bfix(e[sd])?\b",
    r"\bbug(s|fix(es)?)?\b",
    r"\bhotfix\b",
    r"\bpatch(e[sd])?\b",
    r"\bresolve[sd]?\b",
    r"\bregression\b",
    r"\brevert(s|ed)?\b",
    r"\bcloses?\s+#\d+",
)

# Indentation-based complexity is language-agnostic; tabs are normalised to
# this many columns before depth is measured.
TAB_WIDTH = 4

# --------------------------------------------------------------------------
# Salience Engine (spec section 5.1)
#
# Exposed here precisely so section 3.9 can sweep them. Hard-coded weights
# would be unfalsifiable.
# --------------------------------------------------------------------------
W1_HOTSPOT = 0.30
W2_COUPLING_CENTRALITY = 0.20
W3_KNOWLEDGE_RISK = 0.20
W4_DEFECT_DENSITY = 0.15
W5_EXPOSURE = 0.15
W6_CALL_CENTRALITY = 0.00  # activated in section 7.6, when Cartographer lands

# A 500-file refactor commit yields 124,750 co-change pairs of pure noise and
# would dominate the coupling graph. Commits touching more than this many files
# contribute no pairs at all.
COUPLING_MAX_FILES_PER_COMMIT = 20
COUPLING_MIN_SUPPORT = 5
COUPLING_MIN_CONFIDENCE = 0.30

# Scores are normalised by percentile rather than min-max, because these
# distributions are heavy-tailed: with min-max, one 10,000-commit file flattens
# every other file to approximately zero.
WINSORIZE_PERCENTILE = 0.99

# --------------------------------------------------------------------------
# Skeptic (spec section 2.6)
# --------------------------------------------------------------------------
CHALLENGE_THRESHOLD = 0.85
SKEPTIC_BATCH_SIZE = 10
SKEPTIC_BATCH_INTERVAL_SECONDS = 30

# --------------------------------------------------------------------------
# Storage (spec section 18.1, "storage")
# --------------------------------------------------------------------------
MAX_WORKSPACE_SIZE_MB = 2048
CHECKPOINT_INTERVAL_MINUTES = 5
MAX_MEMORY_MB = 4096

# --------------------------------------------------------------------------
# Security and privacy (spec sections 8, 13.4, 13.5)
# --------------------------------------------------------------------------
READ_ONLY = True
SANDBOX_TOOLS = True
REDACT_SECRETS = True

# The graph-facts-only privacy invariant. When false (the default), no raw file
# content ever leaves the machine — only graph facts, paths, symbol names, line
# numbers and commit shas. Enabling this requires explicit consent (6.4) and
# still never permits transmitting secret values.
INCLUDE_SNIPPETS = False

# --------------------------------------------------------------------------
# Terminal UI (spec sections 7, 22)
# --------------------------------------------------------------------------
TUI_THEME = "dark"
TUI_THEMES = ("dark",)  # spec section 7.1: dark mode only
TUI_REFRESH_RATE_HZ = 10
TUI_COLOR = True
TUI_SHOW_AGENT_COLORS = True
TUI_SHOW_TIMESTAMPS = True

# --------------------------------------------------------------------------
# LLM backends
#
# Deliberately minimal. Spec section 7 replaced cost tiers with *detected*
# backends, and the real shape of backend configuration is not settled until
# section 6.1. Inventing it now would only mean rewriting it then.
#
# There is no api_key field anywhere, by design: keys live in the OS keyring
# (spec section 8), never in a configuration file.
# --------------------------------------------------------------------------
LLM_PRIMARY_PROVIDER = "ollama"
LLM_PROVIDERS = ("ollama", "openai", "anthropic", "none")
OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_MODEL = "codellama:7b"
OLLAMA_EMBEDDING_MODEL = "nomic-embed-text"
LLM_CACHE_ENABLED = True
LLM_CACHE_TTL_SECONDS = 3600
LLM_MAX_CALLS_PER_MINUTE = 60

CONFIG_VERSION = "1.0"
