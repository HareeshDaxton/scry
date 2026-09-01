"""The shape of Scry's configuration.

Every section is a frozen dataclass. Immutability is load-bearing rather than
stylistic: section 1.11 spawns worker processes, and on Windows that uses the
``spawn`` start method, so each worker receives a *pickled copy* of the config.
If a worker could mutate its copy it would silently diverge from every other
worker, producing analysis that differs by process with nothing in the logs to
explain it. ``frozen=True`` makes that impossible instead of merely discouraged.

The corollary is that every field must be picklable: primitives, tuples, and
nested frozen dataclasses only. ``tests/test_config.py`` asserts a pickle
round-trip so section 1.11 can rely on it.

Values live in ``defaults.py``; this module only describes structure.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from scry.config import defaults as d


@dataclass(frozen=True)
class LoggingConfig:
    level: str = d.LOG_LEVEL
    max_bytes: int = d.LOG_MAX_BYTES
    backup_count: int = d.LOG_BACKUP_COUNT


@dataclass(frozen=True)
class AgentsConfig:
    max_concurrent: int = d.AGENTS_MAX_CONCURRENT
    timeout_seconds: int = d.AGENT_TIMEOUT_SECONDS
    heartbeat_interval_seconds: int = d.HEARTBEAT_INTERVAL_SECONDS
    heartbeat_timeout_seconds: int = d.HEARTBEAT_TIMEOUT_SECONDS


@dataclass(frozen=True)
class ArchivistConfig:
    churn_half_life_days: float = d.CHURN_HALF_LIFE_DAYS
    exclude_bots: bool = d.EXCLUDE_BOTS
    bot_patterns: tuple[str, ...] = d.BOT_PATTERNS
    blame_budget_files: int = d.BLAME_BUDGET_FILES
    include_merge_commits: bool = d.INCLUDE_MERGE_COMMITS
    bugfix_patterns: tuple[str, ...] = d.BUGFIX_PATTERNS
    tab_width: int = d.TAB_WIDTH


@dataclass(frozen=True)
class SalienceConfig:
    """Composite salience weights and the coupling parameters that feed them.

    Weights may be any non-negative numbers; they are normalised at scoring
    time. Requiring them to sum to 1.0 would make section 3.9's weight sweep
    tedious and error-prone, while not normalising at all would make scores
    from different configurations incomparable and the sweep meaningless.
    """

    w1_hotspot: float = d.W1_HOTSPOT
    w2_coupling_centrality: float = d.W2_COUPLING_CENTRALITY
    w3_knowledge_risk: float = d.W3_KNOWLEDGE_RISK
    w4_defect_density: float = d.W4_DEFECT_DENSITY
    w5_exposure: float = d.W5_EXPOSURE
    w6_call_centrality: float = d.W6_CALL_CENTRALITY

    coupling_max_files_per_commit: int = d.COUPLING_MAX_FILES_PER_COMMIT
    coupling_min_support: int = d.COUPLING_MIN_SUPPORT
    coupling_min_confidence: float = d.COUPLING_MIN_CONFIDENCE
    winsorize_percentile: float = d.WINSORIZE_PERCENTILE

    @property
    def raw_weights(self) -> dict[str, float]:
        return {
            "hotspot": self.w1_hotspot,
            "coupling_centrality": self.w2_coupling_centrality,
            "knowledge_risk": self.w3_knowledge_risk,
            "defect_density": self.w4_defect_density,
            "exposure": self.w5_exposure,
            "call_centrality": self.w6_call_centrality,
        }

    def normalized_weights(self) -> dict[str, float]:
        """Weights scaled to sum to 1.0, so scores stay comparable across sweeps."""
        raw = self.raw_weights
        total = sum(raw.values())
        # The loader rejects an all-zero configuration, so this cannot divide by
        # zero for any Config it produced. Guarded anyway for directly
        # constructed instances.
        if total <= 0:
            raise ValueError("salience weights sum to zero; ranking would be meaningless")
        return {name: value / total for name, value in raw.items()}


@dataclass(frozen=True)
class SkepticConfig:
    challenge_threshold: float = d.CHALLENGE_THRESHOLD
    batch_size: int = d.SKEPTIC_BATCH_SIZE
    batch_interval_seconds: int = d.SKEPTIC_BATCH_INTERVAL_SECONDS


@dataclass(frozen=True)
class StorageConfig:
    max_workspace_size_mb: int = d.MAX_WORKSPACE_SIZE_MB
    checkpoint_interval_minutes: int = d.CHECKPOINT_INTERVAL_MINUTES
    max_memory_mb: int = d.MAX_MEMORY_MB


@dataclass(frozen=True)
class SecurityConfig:
    read_only: bool = d.READ_ONLY
    sandbox_tools: bool = d.SANDBOX_TOOLS
    redact_secrets: bool = d.REDACT_SECRETS
    include_snippets: bool = d.INCLUDE_SNIPPETS


@dataclass(frozen=True)
class TuiConfig:
    theme: str = d.TUI_THEME
    refresh_rate_hz: int = d.TUI_REFRESH_RATE_HZ
    color: bool = d.TUI_COLOR
    show_agent_colors: bool = d.TUI_SHOW_AGENT_COLORS
    show_timestamps: bool = d.TUI_SHOW_TIMESTAMPS


@dataclass(frozen=True)
class OllamaConfig:
    base_url: str = d.OLLAMA_BASE_URL
    model: str = d.OLLAMA_MODEL
    embedding_model: str = d.OLLAMA_EMBEDDING_MODEL


@dataclass(frozen=True)
class LlmConfig:
    """Backend settings.

    Deliberately minimal. Spec section 7 replaced cost tiers with *detected*
    backends and the full shape is settled in section 6.1.

    Note the absence of any ``api_key`` field. Spec section 18.1 showed keys in
    ``config.yaml``; spec section 8 requires them in the OS keyring and never in
    a file. Section 8 wins, so the field does not exist to be misused, and the
    loader warns if one appears in a user's file.
    """

    primary_provider: str = d.LLM_PRIMARY_PROVIDER
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    cache_enabled: bool = d.LLM_CACHE_ENABLED
    cache_ttl_seconds: int = d.LLM_CACHE_TTL_SECONDS
    max_calls_per_minute: int = d.LLM_MAX_CALLS_PER_MINUTE


@dataclass(frozen=True)
class Config:
    """The fully resolved configuration for one Scry invocation."""

    version: str = d.CONFIG_VERSION
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    agents: AgentsConfig = field(default_factory=AgentsConfig)
    archivist: ArchivistConfig = field(default_factory=ArchivistConfig)
    salience: SalienceConfig = field(default_factory=SalienceConfig)
    skeptic: SkepticConfig = field(default_factory=SkepticConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    tui: TuiConfig = field(default_factory=TuiConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)

    # Provenance: dotted key -> where the effective value came from. Excluded
    # from equality and repr because it is metadata about how the config was
    # assembled, not part of the configuration itself. Two configs with the
    # same values are the same config regardless of which files supplied them.
    #
    # Consumed by `scry doctor` (1.9) to explain the effective configuration,
    # and by the weight sweep (3.9) to confirm the weight it set is the weight
    # in effect rather than one shadowed by a stale global file.
    sources: dict[str, str] = field(default_factory=dict, compare=False, repr=False)

    def source_of(self, dotted_key: str) -> str:
        """Return where ``dotted_key``'s effective value came from."""
        from scry.config.loader import DEFAULTS_SOURCE

        return self.sources.get(dotted_key, DEFAULTS_SOURCE)
