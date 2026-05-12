"""Pin the ``APECX_LLM_*`` env-var override contract.

These tests cover ``_apply_llm_env_overrides`` directly — a pure
function that mutates a raw config dict before pydantic validates.
We pin:

  1. Each documented env var maps onto its config key.
  2. Numeric casting fires (temperature/max_tokens/max_validation_retries).
  3. Unset env vars leave YAML defaults untouched.

The most recent addition, ``APECX_LLM_MAX_VALIDATION_RETRIES``
(2026-05-11), lets operators dial the C1 retry budget per-model
without editing the YAML — needed because gemma4 benefits from
budget=2 while mistral-nemo is fine with the default 1.
"""

from __future__ import annotations

import pytest

from apecx_integration.composition.composer import _apply_llm_env_overrides


def _with_env(monkeypatch, **kwargs):
    for k, v in kwargs.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, str(v))


def test_model_env_overrides_yaml(monkeypatch):
    _with_env(monkeypatch, APECX_LLM_MODEL="gemma4:latest")
    raw = {"llm_model": "mistral-nemo:latest"}
    _apply_llm_env_overrides(raw)
    assert raw["llm_model"] == "gemma4:latest"


def test_base_url_env_overrides_yaml(monkeypatch):
    _with_env(monkeypatch, APECX_LLM_BASE_URL="http://example.test/v1")
    raw = {"llm_base_url": "http://localhost:11434/v1"}
    _apply_llm_env_overrides(raw)
    assert raw["llm_base_url"] == "http://example.test/v1"


def test_temperature_env_casts_float(monkeypatch):
    _with_env(monkeypatch, APECX_LLM_TEMPERATURE="0.7")
    raw: dict = {}
    _apply_llm_env_overrides(raw)
    assert raw["temperature"] == pytest.approx(0.7)
    assert isinstance(raw["temperature"], float)


def test_max_tokens_env_casts_int(monkeypatch):
    _with_env(monkeypatch, APECX_LLM_MAX_TOKENS="4096")
    raw: dict = {}
    _apply_llm_env_overrides(raw)
    assert raw["max_tokens"] == 4096
    assert isinstance(raw["max_tokens"], int)


def test_max_validation_retries_env_overrides_default(monkeypatch):
    """The C1 retry budget knob added 2026-05-11. Without this,
    operators trying to dial up retries for gemma4 (which benefits
    from more rounds) would have to edit composer_config.yml —
    a real friction point on shared deployments."""
    _with_env(monkeypatch, APECX_LLM_MAX_VALIDATION_RETRIES="2")
    raw: dict = {}
    _apply_llm_env_overrides(raw)
    assert raw["max_validation_retries"] == 2
    assert isinstance(raw["max_validation_retries"], int)


def test_unset_env_leaves_yaml_value_untouched(monkeypatch):
    for env in (
        "APECX_LLM_MODEL",
        "APECX_LLM_BASE_URL",
        "APECX_LLM_TEMPERATURE",
        "APECX_LLM_MAX_TOKENS",
        "APECX_LLM_MAX_VALIDATION_RETRIES",
    ):
        monkeypatch.delenv(env, raising=False)
    raw = {
        "llm_model": "mistral-nemo:latest",
        "llm_base_url": "http://localhost:11434/v1",
        "max_validation_retries": 1,
    }
    _apply_llm_env_overrides(raw)
    assert raw == {
        "llm_model": "mistral-nemo:latest",
        "llm_base_url": "http://localhost:11434/v1",
        "max_validation_retries": 1,
    }


def test_empty_env_value_leaves_yaml_value_untouched(monkeypatch):
    """An empty string env var must NOT clobber the YAML default —
    that's a known ``export FOO=`` foot-gun where an empty value
    silently zeroes a numeric setting."""
    _with_env(
        monkeypatch,
        APECX_LLM_MAX_VALIDATION_RETRIES="",
        APECX_LLM_MAX_TOKENS="",
    )
    raw = {
        "max_validation_retries": 1,
        "max_tokens": 4096,
    }
    _apply_llm_env_overrides(raw)
    assert raw["max_validation_retries"] == 1
    assert raw["max_tokens"] == 4096
