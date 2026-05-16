"""LLM factory + env-var override helpers extracted from composer.py (G78).

Two free functions that don't depend on Composer state:

* ``_default_llm_factory(**kwargs)`` — lazy import of the agents-side
  ``build_chat_llm`` builder. Used by ``Composer.__init__`` as the
  default ``llm_factory`` when the caller doesn't inject one.

* ``_apply_llm_env_overrides(raw)`` — in-place edits the
  raw-composer-config dict to honor ``APECX_LLM_*`` env vars BEFORE
  Pydantic validation. Drives the documented operator override
  surface (model / base_url / temperature / max_tokens /
  composer_mode / per-role model overrides).

Extracted 2026-05-16 from ``composer.py`` to give the env-override
contract a discoverable home + shrink composer.py. The composer
re-exports both symbols so existing
``from apecx_integration.composition.composer import _xxx`` imports
keep working without test changes.
"""

from __future__ import annotations

import os
from typing import Any


def _default_llm_factory(**kwargs: Any):
    """Default LLM client builder — imports lazily so a missing
    optional dependency (langchain_openai) does not break composer
    import for callers that inject their own ``llm_factory``.

    Tests that need a placeholder LLM should pass ``llm_factory=...``
    to the ``Composer`` constructor — that is the supported seam
    (see ``tests/integration/test_composer_phase2.py``). Direct
    monkeypatching of this factory is not supported.

    Implementation lives in ``apecx_integration.agents._llm_factory``.
    The factory honors APECX_LLM_BASE_URL / APECX_LLM_MODEL /
    APECX_LLM_API_KEY / APECX_LLM_TEMPERATURE / APECX_LLM_MAX_TOKENS
    env vars; local LLMs (Ollama, vLLM) are first-class — both expose
    an OpenAI-compatible chat-completions endpoint.
    """
    from apecx_integration.agents._llm_factory import build_chat_llm

    return build_chat_llm(**kwargs)


def _apply_llm_env_overrides(raw: dict[str, Any]) -> None:
    """Honor the ``APECX_LLM_*`` env-var contract the composer_config
    header promises. In-place edit of the raw mapping before pydantic
    validation runs.

    Mapping (env → config key):
        APECX_LLM_MODEL                  → llm_model
        APECX_LLM_BASE_URL               → llm_base_url
        APECX_LLM_TEMPERATURE            → temperature (float)
        APECX_LLM_MAX_TOKENS             → max_tokens  (int)
        APECX_LLM_MAX_VALIDATION_RETRIES → max_validation_retries (int, C1)

    Unset env vars leave the YAML value untouched. Invalid numeric
    values raise ValueError at pydantic validation; the composer
    surfaces that as ``ComposerConfigurationError``.

    The ``APECX_LLM_MAX_VALIDATION_RETRIES`` knob (2026-05-11) lets
    operators dial up the C1 retry budget per-model. mistral-nemo
    repairs reliably with the default 1; gemma4 (from observation)
    benefits from 2 because it hallucinates class paths more often
    on the first attempt. Without an env-var hook, operators would
    have to edit the YAML to experiment — a real friction point.
    """
    str_pairs = (
        ("APECX_LLM_MODEL", "llm_model"),
        ("APECX_LLM_BASE_URL", "llm_base_url"),
        # SPEC2 (2026-05-11): operators flip the composer mode
        # without editing YAML. Valid values: "monolithic" | "spec".
        ("APECX_COMPOSER_MODE", "composer_mode"),
    )
    for env, key in str_pairs:
        value = os.environ.get(env)
        if value:
            raw[key] = value

    numeric_pairs = (
        ("APECX_LLM_TEMPERATURE", "temperature", float),
        ("APECX_LLM_MAX_TOKENS", "max_tokens", int),
        (
            "APECX_LLM_MAX_VALIDATION_RETRIES",
            "max_validation_retries",
            int,
        ),
    )
    for env, key, caster in numeric_pairs:
        value = os.environ.get(env)
        if value is not None and value != "":
            raw[key] = caster(value)

    # REVIEW-AGENT (2026-05-12): APECX_COMPOSER_REVIEW flips the
    # second-pass reviewer on without YAML edits.
    review_env = os.environ.get("APECX_COMPOSER_REVIEW", "").lower()
    if review_env in ("1", "true", "yes"):
        raw["enable_review"] = True
    elif review_env in ("0", "false", "no"):
        raw["enable_review"] = False

    # BENCH-P0 (2026-05-12): per-role overrides.
    # APECX_LLM_MODEL_<ROLE_UPPER>    → model_roles.<role>.model
    # APECX_LLM_BASE_URL_<ROLE_UPPER> → model_roles.<role>.base_url
    #
    # We deliberately scan os.environ (rather than checking a fixed
    # role list) so operators can add a role like "critic" or
    # "explainer" without a code change. The role name is lower-cased
    # from the env-var suffix; the YAML model_roles dict (if present)
    # is merged keyword-by-keyword so env wins on conflict.
    role_models: dict[str, dict[str, str]] = {}
    for env_key, env_val in os.environ.items():
        if not env_val:
            continue
        if env_key.startswith("APECX_LLM_MODEL_"):
            role = env_key.removeprefix("APECX_LLM_MODEL_").lower()
            role_models.setdefault(role, {})["model"] = env_val
        elif env_key.startswith("APECX_LLM_BASE_URL_"):
            role = env_key.removeprefix("APECX_LLM_BASE_URL_").lower()
            role_models.setdefault(role, {})["base_url"] = env_val
    if role_models:
        merged = dict(raw.get("model_roles") or {})
        for role, fields in role_models.items():
            existing = dict(merged.get(role) or {})
            existing.update(fields)
            # An env-only role MUST carry a model field — base_url
            # without a model is meaningless. Skip if missing.
            if "model" not in existing:
                continue
            merged[role] = existing
        raw["model_roles"] = merged


__all__ = ["_default_llm_factory", "_apply_llm_env_overrides"]
