"""Tests for the layered configuration system (section 1.2).

The emphasis is on the things that are easy to get subtly wrong and expensive
to discover later: precedence between layers, deep merges that must not discard
sibling keys, and error messages that name the file and key at fault.
"""

from __future__ import annotations

import dataclasses
import pickle
from pathlib import Path

import pytest
import yaml

from scry.config import Config, load_config
from scry.config.loader import DEFAULTS_SOURCE
from scry.util.errors import ConfigError, ScryError


def write_yaml(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def collect_warnings() -> tuple[list[str], object]:
    messages: list[str] = []
    return messages, messages.append


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
def test_loads_defaults_with_no_files_at_all():
    config = load_config(env={})
    assert config.skeptic.batch_size == 10
    assert config.archivist.churn_half_life_days == 90.0
    assert config.security.include_snippets is False


def test_missing_global_config_is_not_an_error_and_creates_nothing(tmp_path):
    missing = tmp_path / "config.yaml"
    config = load_config(global_path=missing, env={})
    assert config == Config()
    assert not missing.exists(), "loading config must never create a file as a side effect"


def test_default_values_report_their_provenance():
    config = load_config(env={})
    assert config.source_of("skeptic.batch_size") == DEFAULTS_SOURCE


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------
def test_global_file_overrides_defaults(tmp_path):
    path = write_yaml(tmp_path / "config.yaml", {"skeptic": {"batch_size": 25}})
    config = load_config(global_path=path, env={})
    assert config.skeptic.batch_size == 25
    assert config.source_of("skeptic.batch_size") == str(path)


def test_workspace_file_overrides_global(tmp_path):
    global_path = write_yaml(tmp_path / "global.yaml", {"skeptic": {"batch_size": 25}})
    workspace_path = write_yaml(tmp_path / "ws.yaml", {"skeptic": {"batch_size": 40}})
    config = load_config(global_path=global_path, workspace_path=workspace_path, env={})
    assert config.skeptic.batch_size == 40
    assert config.source_of("skeptic.batch_size") == str(workspace_path)


def test_environment_overrides_files(tmp_path):
    path = write_yaml(tmp_path / "config.yaml", {"storage": {"max_memory_mb": 1024}})
    config = load_config(global_path=path, env={"SCRY_MAX_MEMORY_MB": "8192"})
    assert config.storage.max_memory_mb == 8192
    assert "SCRY_MAX_MEMORY_MB" in config.source_of("storage.max_memory_mb")


def test_cli_overrides_everything(tmp_path):
    path = write_yaml(tmp_path / "config.yaml", {"storage": {"max_memory_mb": 1024}})
    config = load_config(
        global_path=path,
        env={"SCRY_MAX_MEMORY_MB": "8192"},
        cli_overrides={"storage.max_memory_mb": 512},
    )
    assert config.storage.max_memory_mb == 512
    assert config.source_of("storage.max_memory_mb") == "command line"


def test_all_five_layers_compose_in_order(tmp_path):
    global_path = write_yaml(
        tmp_path / "global.yaml",
        {"skeptic": {"batch_size": 11}, "tui": {"refresh_rate_hz": 20}},
    )
    workspace_path = write_yaml(tmp_path / "ws.yaml", {"skeptic": {"batch_size": 12}})
    config = load_config(
        global_path=global_path,
        workspace_path=workspace_path,
        env={"SCRY_LOG_LEVEL": "DEBUG"},
        cli_overrides={"tui.color": False},
    )
    assert config.archivist.tab_width == 4  # layer 1: default
    assert config.tui.refresh_rate_hz == 20  # layer 2: global
    assert config.skeptic.batch_size == 12  # layer 3: workspace beats global
    assert config.logging.level == "DEBUG"  # layer 4: env
    assert config.tui.color is False  # layer 5: cli


# ---------------------------------------------------------------------------
# Deep merge
# ---------------------------------------------------------------------------
def test_partial_override_does_not_discard_sibling_keys(tmp_path):
    """The case a shallow merge silently breaks."""
    global_path = write_yaml(tmp_path / "global.yaml", {"skeptic": {"batch_size": 20}})
    workspace_path = write_yaml(tmp_path / "ws.yaml", {"skeptic": {"challenge_threshold": 0.90}})
    config = load_config(global_path=global_path, workspace_path=workspace_path, env={})
    assert config.skeptic.challenge_threshold == 0.90
    assert config.skeptic.batch_size == 20, "sibling key was discarded by the merge"


def test_lists_replace_rather_than_append(tmp_path):
    path = write_yaml(tmp_path / "config.yaml", {"archivist": {"bot_patterns": ["only-me"]}})
    config = load_config(global_path=path, env={})
    assert config.archivist.bot_patterns == ("only-me",)


def test_nested_sections_merge_independently(tmp_path):
    path = write_yaml(tmp_path / "config.yaml", {"llm": {"ollama": {"model": "llama3"}}})
    config = load_config(global_path=path, env={})
    assert config.llm.ollama.model == "llama3"
    assert config.llm.ollama.base_url == "http://localhost:11434"


# ---------------------------------------------------------------------------
# Validation and error messages
# ---------------------------------------------------------------------------
def test_wrong_type_names_file_key_expected_and_got(tmp_path):
    path = write_yaml(tmp_path / "config.yaml", {"skeptic": {"batch_size": "ten"}})
    with pytest.raises(ConfigError) as excinfo:
        load_config(global_path=path, env={})

    message = str(excinfo.value)
    assert str(path) in message
    assert "skeptic.batch_size" in message
    assert "integer >= 1" in message
    assert "'ten'" in message
    assert "str" in message


def test_out_of_range_value_is_rejected(tmp_path):
    path = write_yaml(tmp_path / "config.yaml", {"skeptic": {"challenge_threshold": 1.5}})
    with pytest.raises(ConfigError, match="challenge_threshold"):
        load_config(global_path=path, env={})


def test_booleans_are_not_accepted_as_integers(tmp_path):
    """bool subclasses int in Python, so a bare isinstance check would let this through."""
    path = write_yaml(tmp_path / "config.yaml", {"skeptic": {"batch_size": True}})
    with pytest.raises(ConfigError, match="batch_size"):
        load_config(global_path=path, env={})


def test_integers_are_not_accepted_as_booleans(tmp_path):
    path = write_yaml(tmp_path / "config.yaml", {"security": {"read_only": 1}})
    with pytest.raises(ConfigError, match="read_only"):
        load_config(global_path=path, env={})


def test_invalid_choice_lists_the_valid_ones(tmp_path):
    path = write_yaml(tmp_path / "config.yaml", {"logging": {"level": "CHATTY"}})
    with pytest.raises(ConfigError) as excinfo:
        load_config(global_path=path, env={})
    assert "DEBUG" in str(excinfo.value)


def test_log_level_is_case_insensitive(tmp_path):
    path = write_yaml(tmp_path / "config.yaml", {"logging": {"level": "debug"}})
    assert load_config(global_path=path, env={}).logging.level == "DEBUG"


def test_config_error_is_a_scry_error():
    assert issubclass(ConfigError, ScryError)


def test_malformed_yaml_names_the_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("skeptic: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(global_path=path, env={})
    assert str(path) in str(excinfo.value)
    assert "not valid YAML" in str(excinfo.value)


def test_top_level_must_be_a_mapping(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(global_path=path, env={})


def test_empty_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("", encoding="utf-8")
    assert load_config(global_path=path, env={}) == Config()


# ---------------------------------------------------------------------------
# YAML safety
# ---------------------------------------------------------------------------
def test_python_object_payload_is_refused(tmp_path):
    """yaml.load would make a config file remote code execution; safe_load must not."""
    path = tmp_path / "config.yaml"
    path.write_text("evil: !!python/object/apply:os.system ['echo pwned']\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(global_path=path, env={})


# ---------------------------------------------------------------------------
# Unknown keys
# ---------------------------------------------------------------------------
def test_unknown_key_warns_and_does_not_fail(tmp_path):
    path = write_yaml(tmp_path / "config.yaml", {"skeptic": {"challange_threshold": 0.9}})
    messages, sink = collect_warnings()
    config = load_config(global_path=path, env={}, on_warning=sink)

    assert config.skeptic.challenge_threshold == 0.85
    assert any("challange_threshold" in m for m in messages)
    assert any(str(path) in m for m in messages)


def test_unknown_top_level_section_warns(tmp_path):
    path = write_yaml(tmp_path / "config.yaml", {"cartographer": {"max_depth": 20}})
    messages, sink = collect_warnings()
    load_config(global_path=path, env={}, on_warning=sink)
    assert any("cartographer" in m for m in messages)


def test_api_key_in_file_is_warned_about_loudly(tmp_path):
    path = write_yaml(
        tmp_path / "config.yaml", {"llm": {"openai": {"api_key": "sk-not-a-real-key"}}}
    )
    messages, sink = collect_warnings()
    load_config(global_path=path, env={}, on_warning=sink)
    assert any("api_key" in m and "keyring" in m for m in messages)


def test_config_has_no_api_key_field_anywhere():
    """Spec section 8 wins over 18.1: keys live in the keyring, never in config."""

    def field_names(obj) -> set[str]:
        names = set()
        for f in dataclasses.fields(obj):
            names.add(f.name)
            value = getattr(obj, f.name)
            if dataclasses.is_dataclass(value):
                names |= field_names(value)
        return names

    assert not any("api_key" in name for name in field_names(Config()))


# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------
def test_log_level_env_var():
    assert load_config(env={"SCRY_LOG_LEVEL": "WARNING"}).logging.level == "WARNING"


def test_llm_provider_env_var():
    assert load_config(env={"SCRY_LLM_PROVIDER": "openai"}).llm.primary_provider == "openai"


def test_no_color_env_var_disables_colour():
    assert load_config(env={"SCRY_NO_COLOR": "1"}).tui.color is False


def test_empty_no_color_leaves_colour_on():
    """no-color.org: the variable disables colour when present *and non-empty*."""
    assert load_config(env={"SCRY_NO_COLOR": ""}).tui.color is True


def test_uncoercible_env_var_names_the_variable():
    with pytest.raises(ConfigError) as excinfo:
        load_config(env={"SCRY_MAX_MEMORY_MB": "lots"})
    assert "SCRY_MAX_MEMORY_MB" in str(excinfo.value)


def test_api_key_env_vars_are_not_config_values():
    config = load_config(env={"OPENAI_API_KEY": "sk-nope", "ANTHROPIC_API_KEY": "sk-nope"})
    assert "sk-nope" not in repr(config)


# ---------------------------------------------------------------------------
# ${VAR} expansion
# ---------------------------------------------------------------------------
def test_env_var_expansion_in_string_values(tmp_path):
    path = write_yaml(tmp_path / "config.yaml", {"llm": {"ollama": {"model": "${MY_MODEL}"}}})
    config = load_config(global_path=path, env={"MY_MODEL": "qwen2.5-coder"})
    assert config.llm.ollama.model == "qwen2.5-coder"


def test_undefined_expansion_is_an_error_not_an_empty_string(tmp_path):
    path = write_yaml(tmp_path / "config.yaml", {"llm": {"ollama": {"model": "${NOT_SET}"}}})
    with pytest.raises(ConfigError) as excinfo:
        load_config(global_path=path, env={})
    assert "NOT_SET" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Salience weights
# ---------------------------------------------------------------------------
def test_weights_normalise_to_one():
    weights = load_config(env={}).salience.normalized_weights()
    assert sum(weights.values()) == pytest.approx(1.0)


def test_arbitrary_weights_are_accepted_and_normalised(tmp_path):
    """Sweeping must be 'vary one number', not 'renormalise every combination'."""
    path = write_yaml(tmp_path / "config.yaml", {"salience": {"w1_hotspot": 3.0}})
    weights = load_config(global_path=path, env={}).salience.normalized_weights()
    assert sum(weights.values()) == pytest.approx(1.0)
    assert weights["hotspot"] > 0.7


def test_all_zero_weights_are_rejected(tmp_path):
    path = write_yaml(
        tmp_path / "config.yaml",
        {
            "salience": {
                "w1_hotspot": 0.0,
                "w2_coupling_centrality": 0.0,
                "w3_knowledge_risk": 0.0,
                "w4_defect_density": 0.0,
                "w5_exposure": 0.0,
                "w6_call_centrality": 0.0,
            }
        },
    )
    with pytest.raises(ConfigError, match="meaningless"):
        load_config(global_path=path, env={})


def test_negative_weight_is_rejected(tmp_path):
    path = write_yaml(tmp_path / "config.yaml", {"salience": {"w1_hotspot": -1.0}})
    with pytest.raises(ConfigError, match="w1_hotspot"):
        load_config(global_path=path, env={})


def test_call_centrality_weight_starts_at_zero():
    """w6 activates in section 7.6, when Cartographer exists to feed it."""
    assert load_config(env={}).salience.w6_call_centrality == 0.0


# ---------------------------------------------------------------------------
# Immutability and picklability (pre-flight for section 1.11)
# ---------------------------------------------------------------------------
def test_config_is_frozen():
    config = load_config(env={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.skeptic.batch_size = 99


def test_config_survives_a_pickle_round_trip(tmp_path):
    """Windows spawns workers, so every worker receives a pickled copy."""
    path = write_yaml(tmp_path / "config.yaml", {"skeptic": {"batch_size": 33}})
    config = load_config(global_path=path, env={})
    restored = pickle.loads(pickle.dumps(config))
    assert restored == config
    assert restored.skeptic.batch_size == 33
    assert restored.archivist.bot_patterns == config.archivist.bot_patterns


def test_provenance_is_excluded_from_equality(tmp_path):
    """Two configs with the same values are the same config, whatever their origin."""
    path = write_yaml(tmp_path / "config.yaml", {"skeptic": {"batch_size": 10}})
    assert load_config(global_path=path, env={}) == load_config(env={})
