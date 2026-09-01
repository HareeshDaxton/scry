"""Loads and validates configuration from its five layers.

Precedence, lowest to highest::

    1. built-in defaults
    2. ~/.scry/config.yaml          global, machine-wide
    3. <workspace>/config.yaml      per-project overrides
    4. SCRY_* environment variables
    5. command-line flags

Two design points are worth stating, because they are easy to get subtly wrong.

**The merge is deep.** If a global file sets ``skeptic.batch_size`` and a
workspace file sets ``skeptic.challenge_threshold``, a shallow merge would
replace the whole ``skeptic`` block and silently discard ``batch_size``. The
user would have set a value, in a file, that does nothing — with no way to
notice. Dicts therefore merge key by key; scalars and lists replace wholesale,
because overriding a list means *this list*, not "append to whatever was there".

**Layers merge as plain dicts, and the typed Config is built exactly once at the
end.** Building a typed object per layer would make "the user explicitly set 60"
indistinguishable from "60 is the default", so a later layer's default would
overwrite an earlier layer's explicit value. Merging raw dicts keeps "unset"
represented by key absence, which is what makes precedence correct.
"""

from __future__ import annotations

import dataclasses
import re
import warnings
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml

from scry.config import defaults as d
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
from scry.util.errors import ConfigError

# libyaml-backed loader where available; both are safe. Never yaml.load, which
# can construct arbitrary Python objects and would turn a config file into
# remote code execution.
try:  # pragma: no cover - depends on the installed PyYAML build
    from yaml import CSafeLoader as _SafeLoader
except ImportError:  # pragma: no cover
    from yaml import SafeLoader as _SafeLoader  # type: ignore[assignment]

DEFAULTS_SOURCE = "built-in defaults"
CLI_SOURCE = "command line"

WarningSink = Callable[[str], None]

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# An explicit table rather than a generic SCRY_A_B_C -> a.b.c rule. Generic
# mapping is ambiguous because underscores are both the path separator and part
# of key names: SCRY_MAX_MEMORY_MB could mean max.memory.mb or max_memory_mb.
# That ambiguity turns into bug reports. This table doubles as documentation.
#
# Deliberately absent:
#   SCRY_HOME           - resolves data directories, not a config value (1.4)
#   OPENAI_API_KEY      - read at call time from keyring or env (6.4), never
#   ANTHROPIC_API_KEY     stored in configuration
_ENV_MAP: dict[str, tuple[str, str]] = {
    "SCRY_LOG_LEVEL": ("logging.level", "str"),
    "SCRY_LLM_PROVIDER": ("llm.primary_provider", "str"),
    "SCRY_MAX_MEMORY_MB": ("storage.max_memory_mb", "int"),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def load_config(
    *,
    global_path: Path | None = None,
    workspace_path: Path | None = None,
    env: Mapping[str, str] | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
    on_warning: WarningSink | None = None,
) -> Config:
    """Compose all five layers and return a validated, frozen Config.

    Args:
        global_path: ``~/.scry/config.yaml``. Missing is not an error — the
            built-in defaults apply and no file is created. A tool that writes
            to your home directory as a side effect of being run is rude, and
            it makes behaviour depend on whether it happened to run before.
            Generating a starter file is an explicit ``scry config --init``.
        workspace_path: per-workspace ``config.yaml``. Section 1.4 supplies
            this path once workspaces exist; the plumbing is here already.
        env: environment mapping, defaulting to ``os.environ``.
        cli_overrides: dotted key to value, for flags on this invocation.
        on_warning: receives non-fatal problems such as unknown keys. Defaults
            to :func:`warnings.warn`; section 1.3 routes it to the logger and
            section 1.9 collects them for ``scry doctor``.

    Raises:
        ConfigError: on unreadable YAML, an unresolvable ``${VAR}``, or any
            value that fails validation. The message names the source file,
            the dotted key, what was expected and what was found.
    """
    warn = on_warning if on_warning is not None else _default_warn
    if env is None:
        import os

        env = os.environ

    data = _as_dict(Config())
    provenance: dict[str, str] = {}

    for path in (global_path, workspace_path):
        if path is None:
            continue
        loaded = _read_yaml(path, env, warn)
        if loaded is None:
            continue
        _warn_unknown_keys(loaded, _known_shape(Config()), str(path), warn)
        _warn_api_key_in_file(loaded, str(path), warn)
        _deep_merge(data, loaded, str(path), provenance)

    _apply_env(data, provenance, env)
    _apply_cli(data, provenance, cli_overrides)

    return _build(data, provenance)


def _default_warn(message: str) -> None:
    warnings.warn(message, UserWarning, stacklevel=3)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def _read_yaml(path: Path, env: Mapping[str, str], warn: WarningSink) -> dict[str, Any] | None:
    """Read one YAML file. Returns None when the file simply does not exist."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigError("<file>", source=str(path), detail=f"could not be read: {exc}") from exc

    try:
        # _SafeLoader is CSafeLoader/SafeLoader — never the unsafe default loader.
        loaded = yaml.load(text, Loader=_SafeLoader)
    except yaml.YAMLError as exc:
        raise ConfigError("<file>", source=str(path), detail=f"is not valid YAML: {exc}") from exc

    if loaded is None:
        return None
    if not isinstance(loaded, dict):
        raise ConfigError(
            "<file>",
            source=str(path),
            expected="a mapping at the top level",
            got=loaded,
        )
    return _expand_env_vars(loaded, env, str(path))


def _expand_env_vars(value: Any, env: Mapping[str, str], source: str, key: str = "") -> Any:
    """Expand ``${VAR}`` in string values.

    An undefined variable is a hard error rather than a silent empty string.
    Silent expansion to "" produces mystery failures far from their cause.
    """
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in env:
                raise ConfigError(
                    key or "<value>",
                    source=source,
                    detail=f"refers to ${{{name}}}, which is not set in the environment",
                )
            return env[name]

        return _ENV_VAR_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        return {
            k: _expand_env_vars(v, env, source, f"{key}.{k}" if key else str(k))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_expand_env_vars(v, env, source, key) for v in value]
    return value


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------
def _deep_merge(
    base: dict[str, Any],
    overlay: Mapping[str, Any],
    source: str,
    provenance: dict[str, str],
    prefix: str = "",
) -> None:
    for key, value in overlay.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value, source, provenance, f"{dotted}.")
        else:
            base[key] = value
            provenance[dotted] = source


def _apply_env(data: dict[str, Any], provenance: dict[str, str], env: Mapping[str, str]) -> None:
    for name, (dotted, kind) in _ENV_MAP.items():
        raw = env.get(name)
        if raw is None:
            continue
        _set(data, dotted, _coerce_env(name, raw, kind))
        provenance[dotted] = f"environment variable {name}"

    # Follows the no-color.org convention: present and non-empty disables
    # colour, regardless of the value.
    no_color = env.get("SCRY_NO_COLOR")
    if no_color:
        _set(data, "tui.color", False)
        provenance["tui.color"] = "environment variable SCRY_NO_COLOR"


def _coerce_env(name: str, raw: str, kind: str) -> Any:
    source = f"environment variable {name}"
    if kind == "int":
        try:
            return int(raw)
        except ValueError:
            raise ConfigError(name, source=source, expected="an integer", got=raw) from None
    return raw


def _apply_cli(
    data: dict[str, Any],
    provenance: dict[str, str],
    overrides: Mapping[str, Any] | None,
) -> None:
    if not overrides:
        return
    for dotted, value in overrides.items():
        _set(data, dotted, value)
        provenance[dotted] = CLI_SOURCE


# ---------------------------------------------------------------------------
# Shape introspection
# ---------------------------------------------------------------------------
def _as_dict(obj: Any) -> dict[str, Any]:
    """Flatten a dataclass instance into nested plain dicts."""
    out: dict[str, Any] = {}
    for f in dataclasses.fields(obj):
        if f.name == "sources":
            continue
        value = getattr(obj, f.name)
        out[f.name] = _as_dict(value) if dataclasses.is_dataclass(value) else value
    return out


def _known_shape(obj: Any) -> dict[str, Any]:
    """Nested map of valid keys; leaves are None.

    Derived from a default *instance* rather than from type annotations, which
    would be plain strings under ``from __future__ import annotations``.
    """
    shape: dict[str, Any] = {}
    for f in dataclasses.fields(obj):
        if f.name == "sources":
            continue
        value = getattr(obj, f.name)
        shape[f.name] = _known_shape(value) if dataclasses.is_dataclass(value) else None
    return shape


def _warn_unknown_keys(
    data: Mapping[str, Any],
    shape: Mapping[str, Any],
    source: str,
    warn: WarningSink,
    prefix: str = "",
) -> None:
    """Warn about unrecognised keys without failing.

    The two failure modes are asymmetric. Silently ignoring a typo'd key means
    a user sets a value, sees no effect, and has nothing to diagnose.
    Hard-failing means a file written by a newer Scry breaks an older one,
    which is hostile on downgrade. Warning catches the typo and keeps
    forward-compatibility; ``scry doctor`` (1.9) surfaces these as a check.
    """
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if key not in shape:
            warn(f"{source}: unknown configuration key {dotted!r} (ignored)")
            continue
        child = shape[key]
        if isinstance(child, dict) and isinstance(value, Mapping):
            _warn_unknown_keys(value, child, source, warn, f"{dotted}.")


def _warn_api_key_in_file(data: Mapping[str, Any], source: str, warn: WarningSink) -> None:
    """Flag API keys found in a configuration file.

    Spec section 18.1 showed ``api_key`` in config.yaml; spec section 8
    requires keys in the OS keyring and never in a file. Section 8 wins, so no
    such field exists — but a user following the older documentation deserves
    to be told their key is both ignored and exposed. The realistic outcome of
    a key sitting in a YAML file is that it gets committed to a repository.
    """
    llm = data.get("llm")
    if not isinstance(llm, Mapping):
        return
    for path, node in (("llm", llm), *((f"llm.{k}", v) for k, v in llm.items())):
        if isinstance(node, Mapping) and "api_key" in node:
            warn(
                f"{source}: {path}.api_key is ignored. API keys are read from the OS "
                f"keyring, never from a configuration file. Remove it — a key in a "
                f"YAML file will eventually be committed to a repository."
            )


# ---------------------------------------------------------------------------
# Dotted-path access
# ---------------------------------------------------------------------------
def _set(data: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def _lookup(data: Mapping[str, Any], dotted: str) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _source(provenance: Mapping[str, str], dotted: str) -> str:
    return provenance.get(dotted, DEFAULTS_SOURCE)


def _range_text(kind: str, minimum: Any, maximum: Any) -> str:
    if minimum is not None and maximum is not None:
        return f"{kind} between {minimum} and {maximum}"
    if minimum is not None:
        return f"{kind} >= {minimum}"
    if maximum is not None:
        return f"{kind} <= {maximum}"
    return kind


def _int(
    data: Mapping[str, Any],
    dotted: str,
    prov: Mapping[str, str],
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    value = _lookup(data, dotted)
    expected = _range_text("integer", minimum, maximum)
    # bool is a subclass of int in Python, so `batch_size: true` would sail
    # through a bare isinstance check and become 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(dotted, source=_source(prov, dotted), expected=expected, got=value)
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise ConfigError(dotted, source=_source(prov, dotted), expected=expected, got=value)
    return value


def _float(
    data: Mapping[str, Any],
    dotted: str,
    prov: Mapping[str, str],
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    value = _lookup(data, dotted)
    expected = _range_text("number", minimum, maximum)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(dotted, source=_source(prov, dotted), expected=expected, got=value)
    value = float(value)
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise ConfigError(dotted, source=_source(prov, dotted), expected=expected, got=value)
    return value


def _bool(data: Mapping[str, Any], dotted: str, prov: Mapping[str, str]) -> bool:
    value = _lookup(data, dotted)
    if not isinstance(value, bool):
        raise ConfigError(dotted, source=_source(prov, dotted), expected="true or false", got=value)
    return value


def _str(
    data: Mapping[str, Any],
    dotted: str,
    prov: Mapping[str, str],
    *,
    choices: tuple[str, ...] | None = None,
    case_insensitive: bool = False,
) -> str:
    value = _lookup(data, dotted)
    expected = "one of " + ", ".join(choices) if choices else "a string"
    if not isinstance(value, str):
        raise ConfigError(dotted, source=_source(prov, dotted), expected=expected, got=value)
    candidate = value.upper() if case_insensitive else value
    if choices is not None and candidate not in choices:
        raise ConfigError(dotted, source=_source(prov, dotted), expected=expected, got=value)
    return candidate


def _str_tuple(data: Mapping[str, Any], dotted: str, prov: Mapping[str, str]) -> tuple[str, ...]:
    value = _lookup(data, dotted)
    if isinstance(value, tuple):
        value = list(value)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(
            dotted, source=_source(prov, dotted), expected="a list of strings", got=value
        )
    return tuple(value)


def _build(data: Mapping[str, Any], prov: dict[str, str]) -> Config:
    """Construct the typed Config, validating every field exactly once."""
    salience = SalienceConfig(
        w1_hotspot=_float(data, "salience.w1_hotspot", prov, minimum=0.0),
        w2_coupling_centrality=_float(data, "salience.w2_coupling_centrality", prov, minimum=0.0),
        w3_knowledge_risk=_float(data, "salience.w3_knowledge_risk", prov, minimum=0.0),
        w4_defect_density=_float(data, "salience.w4_defect_density", prov, minimum=0.0),
        w5_exposure=_float(data, "salience.w5_exposure", prov, minimum=0.0),
        w6_call_centrality=_float(data, "salience.w6_call_centrality", prov, minimum=0.0),
        coupling_max_files_per_commit=_int(
            data, "salience.coupling_max_files_per_commit", prov, minimum=2
        ),
        coupling_min_support=_int(data, "salience.coupling_min_support", prov, minimum=1),
        coupling_min_confidence=_float(
            data, "salience.coupling_min_confidence", prov, minimum=0.0, maximum=1.0
        ),
        winsorize_percentile=_float(
            data, "salience.winsorize_percentile", prov, minimum=0.5, maximum=1.0
        ),
    )
    if sum(salience.raw_weights.values()) <= 0:
        raise ConfigError(
            "salience",
            source=_source(prov, "salience.w1_hotspot"),
            expected="at least one weight greater than zero",
            detail="all salience weights are zero, which makes the ranking meaningless",
        )

    return Config(
        version=_str(data, "version", prov),
        logging=LoggingConfig(
            level=_str(data, "logging.level", prov, choices=d.LOG_LEVELS, case_insensitive=True),
            max_bytes=_int(data, "logging.max_bytes", prov, minimum=1024),
            backup_count=_int(data, "logging.backup_count", prov, minimum=0),
        ),
        agents=AgentsConfig(
            max_concurrent=_int(data, "agents.max_concurrent", prov, minimum=1, maximum=64),
            timeout_seconds=_int(data, "agents.timeout_seconds", prov, minimum=1),
            heartbeat_interval_seconds=_int(
                data, "agents.heartbeat_interval_seconds", prov, minimum=1
            ),
            heartbeat_timeout_seconds=_int(
                data, "agents.heartbeat_timeout_seconds", prov, minimum=1
            ),
        ),
        archivist=ArchivistConfig(
            churn_half_life_days=_float(data, "archivist.churn_half_life_days", prov, minimum=1.0),
            exclude_bots=_bool(data, "archivist.exclude_bots", prov),
            bot_patterns=_str_tuple(data, "archivist.bot_patterns", prov),
            blame_budget_files=_int(data, "archivist.blame_budget_files", prov, minimum=0),
            include_merge_commits=_bool(data, "archivist.include_merge_commits", prov),
            bugfix_patterns=_str_tuple(data, "archivist.bugfix_patterns", prov),
            tab_width=_int(data, "archivist.tab_width", prov, minimum=1, maximum=16),
        ),
        salience=salience,
        skeptic=SkepticConfig(
            challenge_threshold=_float(
                data, "skeptic.challenge_threshold", prov, minimum=0.0, maximum=1.0
            ),
            batch_size=_int(data, "skeptic.batch_size", prov, minimum=1),
            batch_interval_seconds=_int(data, "skeptic.batch_interval_seconds", prov, minimum=1),
        ),
        storage=StorageConfig(
            max_workspace_size_mb=_int(data, "storage.max_workspace_size_mb", prov, minimum=1),
            checkpoint_interval_minutes=_int(
                data, "storage.checkpoint_interval_minutes", prov, minimum=1
            ),
            max_memory_mb=_int(data, "storage.max_memory_mb", prov, minimum=256),
        ),
        security=SecurityConfig(
            read_only=_bool(data, "security.read_only", prov),
            sandbox_tools=_bool(data, "security.sandbox_tools", prov),
            redact_secrets=_bool(data, "security.redact_secrets", prov),
            include_snippets=_bool(data, "security.include_snippets", prov),
        ),
        tui=TuiConfig(
            theme=_str(data, "tui.theme", prov, choices=d.TUI_THEMES),
            refresh_rate_hz=_int(data, "tui.refresh_rate_hz", prov, minimum=1, maximum=120),
            color=_bool(data, "tui.color", prov),
            show_agent_colors=_bool(data, "tui.show_agent_colors", prov),
            show_timestamps=_bool(data, "tui.show_timestamps", prov),
        ),
        llm=LlmConfig(
            primary_provider=_str(data, "llm.primary_provider", prov, choices=d.LLM_PROVIDERS),
            ollama=OllamaConfig(
                base_url=_str(data, "llm.ollama.base_url", prov),
                model=_str(data, "llm.ollama.model", prov),
                embedding_model=_str(data, "llm.ollama.embedding_model", prov),
            ),
            cache_enabled=_bool(data, "llm.cache_enabled", prov),
            cache_ttl_seconds=_int(data, "llm.cache_ttl_seconds", prov, minimum=0),
            max_calls_per_minute=_int(data, "llm.max_calls_per_minute", prov, minimum=1),
        ),
        sources=dict(prov),
    )
