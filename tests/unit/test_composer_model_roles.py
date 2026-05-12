"""BENCH-P0 (2026-05-12): role-keyed multi-model config.

Pins three things:

1. ``ComposerConfig.model_roles`` defaults to an empty dict — a
   config file without the field still loads, so T01 AC1 and every
   other composer test that uses the legacy single-model shape
   stays green.
2. ``_apply_llm_env_overrides`` translates ``APECX_LLM_MODEL_<ROLE>``
   and ``APECX_LLM_BASE_URL_<ROLE>`` env vars into the
   ``model_roles`` dict before pydantic validates.
3. ``Composer.llm_for_role`` selects the role-bound model when one
   is configured and falls back to ``llm_model`` / ``llm_base_url``
   when it isn't.

The integration test that runs the multi-model scaffold against
Ollama lives elsewhere; this file is the pure-unit pin so the
contract can't drift silently.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from apecx_integration.composition.composer import (
    Composer,
    _apply_llm_env_overrides,
)
from apecx_integration.composition.composer_schemas import (
    ComposerConfig,
    ModelRoleConfig,
)

# ---------------------------------------------------------------------------
# Config-level tests
# ---------------------------------------------------------------------------


def test_model_roles_defaults_to_empty_dict():
    """Backward-compat guard. A YAML config without ``model_roles``
    must still load and produce an empty dict — otherwise every
    existing composer_config.yml in the workspace breaks."""
    cfg = ComposerConfig(library_version="0.0.0-test", prompt_dir=Path("."))
    assert cfg.model_roles == {}


def test_model_roles_accepts_role_bindings():
    cfg = ComposerConfig(
        library_version="0.0.0-test",
        prompt_dir=Path("."),
        model_roles={
            "planner": {"model": "nemotron-3-nano:4b"},
            "drafter": {
                "model": "mistral-nemo:latest",
                "base_url": "http://localhost:11434/v1",
            },
        },
    )
    assert isinstance(cfg.model_roles["planner"], ModelRoleConfig)
    assert cfg.model_roles["planner"].model == "nemotron-3-nano:4b"
    assert cfg.model_roles["planner"].base_url is None
    assert cfg.model_roles["drafter"].model == "mistral-nemo:latest"
    assert cfg.model_roles["drafter"].base_url == "http://localhost:11434/v1"


def test_model_role_config_rejects_typo():
    """A misspelled field on ModelRoleConfig (e.g., ``mdl`` for
    ``model``) should fail loud at config-load, not silently land
    in an extras bucket — matches the workspace ``extra='forbid'``
    rule (2026-04-27)."""
    with pytest.raises(ValidationError):
        ModelRoleConfig(mdl="nemotron-3-nano:4b")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Env-var override tests
# ---------------------------------------------------------------------------


def _set_env(monkeypatch, **pairs):
    for k, v in pairs.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, str(v))


def test_env_override_creates_role_binding(monkeypatch):
    _set_env(monkeypatch, APECX_LLM_MODEL_PLANNER="nemotron-3-nano:4b")
    raw: dict = {}
    _apply_llm_env_overrides(raw)
    assert raw["model_roles"] == {"planner": {"model": "nemotron-3-nano:4b"}}


def test_env_override_supports_base_url(monkeypatch):
    _set_env(
        monkeypatch,
        APECX_LLM_MODEL_REVIEWER="nemotron-3-nano:4b",
        APECX_LLM_BASE_URL_REVIEWER="http://gpu-host:11434/v1",
    )
    raw: dict = {}
    _apply_llm_env_overrides(raw)
    assert raw["model_roles"]["reviewer"] == {
        "model": "nemotron-3-nano:4b",
        "base_url": "http://gpu-host:11434/v1",
    }


def test_env_override_drops_role_without_model(monkeypatch):
    """A bare ``APECX_LLM_BASE_URL_<ROLE>`` without a corresponding
    model is meaningless — the LLM factory needs a model. Make
    sure we don't synthesize a half-broken role binding."""
    _set_env(monkeypatch, APECX_LLM_BASE_URL_REVIEWER="http://x/v1")
    raw: dict = {}
    _apply_llm_env_overrides(raw)
    assert "model_roles" not in raw or raw["model_roles"] == {}


def test_env_does_not_collide_with_legacy_model_env(monkeypatch):
    """``APECX_LLM_MODEL`` (no suffix) MUST keep mapping to
    ``llm_model``, not to a role binding. Regression guard for
    the prefix-match heuristic in ``_apply_llm_env_overrides``."""
    _set_env(monkeypatch, APECX_LLM_MODEL="mistral-nemo:latest")
    raw: dict = {}
    _apply_llm_env_overrides(raw)
    assert raw.get("llm_model") == "mistral-nemo:latest"
    assert "model_roles" not in raw


def test_env_does_not_collide_with_max_tokens_env(monkeypatch):
    """``APECX_LLM_MAX_TOKENS`` superficially shares a prefix with
    ``APECX_LLM_MODEL_*`` patterns. Confirm the matcher only fires
    on the exact ``APECX_LLM_MODEL_`` prefix."""
    _set_env(monkeypatch, APECX_LLM_MAX_TOKENS="4096")
    raw: dict = {}
    _apply_llm_env_overrides(raw)
    assert raw.get("max_tokens") == 4096
    assert "model_roles" not in raw


def test_env_role_merges_with_yaml_role(monkeypatch):
    """When YAML declares a role and env supplies the same role,
    env should win (this is the documented contract). The merge
    must be field-level — env can override one field while leaving
    others from YAML intact."""
    _set_env(
        monkeypatch,
        APECX_LLM_BASE_URL_PLANNER="http://override/v1",
    )
    raw: dict = {
        "model_roles": {
            "planner": {"model": "nemotron-3-nano:4b", "base_url": "http://yaml/v1"},
        },
    }
    _apply_llm_env_overrides(raw)
    assert raw["model_roles"]["planner"]["model"] == "nemotron-3-nano:4b"
    assert raw["model_roles"]["planner"]["base_url"] == "http://override/v1"


# ---------------------------------------------------------------------------
# Composer.llm_for_role routing tests
# ---------------------------------------------------------------------------


def _build_composer_with_recording_factory(model_roles=None):
    """Construct a Composer with a recording llm_factory.

    Returns ``(composer, calls)`` where ``calls`` is a list of
    kwargs dicts the factory received. We don't need a real LLM —
    we only verify the routing logic picked the right model.
    """
    calls: list[dict] = []

    def fake_factory(**kwargs):
        calls.append(kwargs)
        # Returning an opaque sentinel is fine; the test inspects
        # the recorded kwargs, not the returned object.
        return object()

    # Use this file's directory as a stand-in for prompt_dir; we
    # never call compose() so the prompts aren't read.
    prompt_dir = (
        Path(__file__).parent.parent.parent
        / "src"
        / "apecx_integration"
        / "composition"
        / "composer_prompts"
    )
    cfg = ComposerConfig(
        library_version="0.0.0-test",
        prompt_dir=prompt_dir,
        llm_model="mistral-nemo:latest",
        llm_base_url="http://localhost:11434/v1",
        model_roles=model_roles or {},
    )
    composer = Composer(cfg, llm_factory=fake_factory)
    return composer, calls


def test_llm_for_role_falls_back_to_default_when_role_unconfigured():
    composer, calls = _build_composer_with_recording_factory(model_roles={})
    composer.llm_for_role("planner")
    assert len(calls) == 1
    assert calls[0]["model"] == "mistral-nemo:latest"
    assert calls[0]["base_url"] == "http://localhost:11434/v1"


def test_llm_for_role_uses_role_binding_when_configured():
    composer, calls = _build_composer_with_recording_factory(
        model_roles={"planner": {"model": "nemotron-3-nano:4b"}},
    )
    composer.llm_for_role("planner")
    assert len(calls) == 1
    assert calls[0]["model"] == "nemotron-3-nano:4b"
    # base_url fell back to default since role did not set one.
    assert calls[0]["base_url"] == "http://localhost:11434/v1"


def test_llm_for_role_uses_role_base_url_when_set():
    composer, calls = _build_composer_with_recording_factory(
        model_roles={
            "reviewer": {
                "model": "nemotron-3-nano:4b",
                "base_url": "http://gpu-host:11434/v1",
            },
        },
    )
    composer.llm_for_role("reviewer")
    assert calls[0]["base_url"] == "http://gpu-host:11434/v1"


def test_llm_for_role_passes_overrides_to_factory():
    composer, calls = _build_composer_with_recording_factory()
    composer.llm_for_role("drafter", temperature=0.42, custom_kwarg="x")
    assert calls[0]["temperature"] == 0.42
    assert calls[0]["custom_kwarg"] == "x"
