"""T02 / memo-08: unit tests for nanobrain's ConfigBase env-var
interpolation patch.

These live in apecx-mcp-integration because nanobrain isn't a git repo
in this workspace — we own the tests that exercise the patches we
apply under the carve-out. The parity-rule requirement (workspace
CLAUDE.md, 2026-04-21) is satisfied by
``tests/integration/test_nanobrain_configbase_env_vars_integration.py``
— a real from_config call that proves the same interpolation behavior
exercises real Agent YAMLs (not just a unit helper).
"""

from __future__ import annotations

import textwrap

import pytest

from nanobrain.core.config.config_base import (
    _interpolate_env_vars,
    _ENV_VAR_PATTERN,
)

pytestmark = pytest.mark.unit


# -- Leaf string behavior ----------------------------------------------------


def test_bare_var_substitutes_when_set(monkeypatch) -> None:
    monkeypatch.setenv("NB_TEST_VAR", "hello")
    assert _interpolate_env_vars("${NB_TEST_VAR}") == "hello"


def test_bare_var_raises_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("NB_TEST_VAR", raising=False)
    with pytest.raises(ValueError, match="NB_TEST_VAR.*not set"):
        _interpolate_env_vars("${NB_TEST_VAR}")


def test_default_form_uses_default_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("NB_TEST_VAR", raising=False)
    assert (
        _interpolate_env_vars("${NB_TEST_VAR:-fallback}") == "fallback"
    )


def test_default_form_uses_default_when_empty(monkeypatch) -> None:
    """POSIX ``:-`` treats an empty env var as 'unset for fallback
    purposes.' Matches docker-compose, GitHub Actions, shell behavior."""
    monkeypatch.setenv("NB_TEST_VAR", "")
    assert _interpolate_env_vars("${NB_TEST_VAR:-fallback}") == "fallback"


def test_default_form_uses_env_value_when_set(monkeypatch) -> None:
    monkeypatch.setenv("NB_TEST_VAR", "real-value")
    assert _interpolate_env_vars("${NB_TEST_VAR:-fallback}") == "real-value"


def test_default_can_be_empty_string(monkeypatch) -> None:
    monkeypatch.delenv("NB_TEST_VAR", raising=False)
    assert _interpolate_env_vars("${NB_TEST_VAR:-}") == ""


def test_escape_preserves_literal_dollar_brace(monkeypatch) -> None:
    """A prompt that legitimately contains ``${something}`` (LaTeX
    notation, shell-example-in-docs, etc.) can be preserved as-is by
    escaping the leading dollar. Without this, any system_prompt
    containing shell examples or math would fail to load."""
    monkeypatch.delenv("NB_TEST_VAR", raising=False)
    # The escape prevents interpolation; the final value has one $ left.
    assert _interpolate_env_vars("$${NB_TEST_VAR}") == "${NB_TEST_VAR}"


def test_mixed_literal_and_interpolation(monkeypatch) -> None:
    monkeypatch.setenv("NB_HOST", "localhost")
    monkeypatch.setenv("NB_PORT", "11434")
    assert (
        _interpolate_env_vars("http://${NB_HOST}:${NB_PORT}/v1")
        == "http://localhost:11434/v1"
    )


def test_missing_var_in_composite_string_still_fails(monkeypatch) -> None:
    """A fail-loud that only catches pure-var strings would be worse
    than none — real configs embed vars in URL fragments, prefixes, etc."""
    monkeypatch.delenv("NB_MISSING", raising=False)
    monkeypatch.setenv("NB_HOST", "localhost")
    with pytest.raises(ValueError, match="NB_MISSING"):
        _interpolate_env_vars("http://${NB_HOST}:${NB_MISSING}/v1")


# -- Recursion into dict / list ---------------------------------------------


def test_interpolates_inside_nested_dict(monkeypatch) -> None:
    monkeypatch.setenv("NB_URL", "http://localhost:11434/v1")
    data = {
        "llm": {
            "base_url": "${NB_URL}",
            "temperature": 0.7,  # non-string leaf passes through
        },
        "name": "agent",
    }
    assert _interpolate_env_vars(data) == {
        "llm": {"base_url": "http://localhost:11434/v1", "temperature": 0.7},
        "name": "agent",
    }


def test_interpolates_inside_list(monkeypatch) -> None:
    monkeypatch.setenv("NB_MODEL_A", "mistral-small:latest")
    monkeypatch.setenv("NB_MODEL_B", "mistral-nemo:latest")
    data = {"models": ["${NB_MODEL_A}", "${NB_MODEL_B}", "static-model"]}
    assert _interpolate_env_vars(data) == {
        "models": [
            "mistral-small:latest",
            "mistral-nemo:latest",
            "static-model",
        ]
    }


def test_non_string_leaves_pass_through() -> None:
    data = {"timeout": 30, "debug": True, "skip": None, "tags": []}
    assert _interpolate_env_vars(data) == data


# -- Grammar regex guardrails -----------------------------------------------


def test_var_name_must_start_with_letter_or_underscore() -> None:
    """A leading digit is not a valid env-var name. Don't match these
    so they pass through as literals (they were never going to be env
    vars anyway)."""
    assert _interpolate_env_vars("${1FOO}") == "${1FOO}"


def test_plain_dash_form_not_supported(monkeypatch) -> None:
    """POSIX has both ``${VAR-default}`` (unset-only) and ``${VAR:-default}``
    (unset-or-empty). We deliberately support only the latter. The plain-
    dash form should NOT be interpreted as a default-fallback — it falls
    back to fail-loud on the whole pattern because the grammar doesn't
    match."""
    monkeypatch.delenv("NB_TEST_VAR", raising=False)
    # The pattern requires ``:-`` for defaults; a bare ``-`` in the middle
    # makes the variable name invalid and the whole match fail.
    # Result: pattern doesn't match at all, literal string stays.
    assert (
        _interpolate_env_vars("${NB_TEST_VAR-default}")
        == "${NB_TEST_VAR-default}"
    )


# -- From-config integration (lightweight, not a full Agent spin-up) --------


def test_configbase_load_yaml_interpolates(tmp_path, monkeypatch) -> None:
    """End-to-end: an actual YAML file on disk gets its env vars
    interpolated when ``_load_yaml_file`` runs. This is the shape that
    matters for real Agent / Step YAMLs."""
    from nanobrain.core.config.config_base import ConfigBase

    monkeypatch.setenv("NB_LLM_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("NB_LLM_MODEL", "mistral-small:latest")

    yaml_path = tmp_path / "agent.yml"
    yaml_path.write_text(
        textwrap.dedent(
            """\
            name: test_agent
            model: "${NB_LLM_MODEL}"
            provider: openai_compatible
            base_url: "${NB_LLM_URL}"
            api_key: "${NB_LLM_API_KEY:-EMPTY}"
            temperature: 0.0
            """
        )
    )
    loaded = ConfigBase._load_yaml_file(yaml_path)
    assert loaded["model"] == "mistral-small:latest"
    assert loaded["base_url"] == "http://localhost:11434/v1"
    assert loaded["api_key"] == "EMPTY"  # default form, var unset
    assert loaded["temperature"] == 0.0  # non-string leaf preserved


def test_configbase_load_yaml_fails_loud_on_missing_required(
    tmp_path, monkeypatch
) -> None:
    """The load MUST raise — not silently produce an empty-string key —
    when a required env var is missing. This is the load-bearing claim
    for the memo 08 rationale."""
    from nanobrain.core.config.config_base import ConfigBase

    monkeypatch.delenv("NB_REQUIRED_SECRET", raising=False)

    yaml_path = tmp_path / "agent.yml"
    yaml_path.write_text(
        textwrap.dedent(
            """\
            name: test_agent
            api_key: "${NB_REQUIRED_SECRET}"
            """
        )
    )
    with pytest.raises(ValueError, match="NB_REQUIRED_SECRET"):
        ConfigBase._load_yaml_file(yaml_path)
