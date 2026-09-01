"""Scry's layered configuration.

Public API::

    from scry.config import Config, load_config

    config = load_config(global_path=Path.home() / ".scry" / "config.yaml")
    config.salience.normalized_weights()
    config.source_of("skeptic.batch_size")   # where did this value come from?

Stdlib plus PyYAML only. This sits on the always-loaded path — every command
including ``scry why`` pays for it — so nothing heavier belongs here.
"""

from scry.config.loader import CLI_SOURCE, DEFAULTS_SOURCE, load_config
from scry.config.schema import (
    AgentsConfig,
    ArchivistConfig,
    Config,
    LlmConfig,
    LoggingConfig,
    OllamaConfig,
    SalienceConfig,
    SecurityConfig,
    SkepticConfig,
    StorageConfig,
    TuiConfig,
)

__all__ = [
    "CLI_SOURCE",
    "DEFAULTS_SOURCE",
    "AgentsConfig",
    "ArchivistConfig",
    "Config",
    "LlmConfig",
    "LoggingConfig",
    "OllamaConfig",
    "SalienceConfig",
    "SecurityConfig",
    "SkepticConfig",
    "StorageConfig",
    "TuiConfig",
    "load_config",
]
