"""Shared LLM client factory for apecx_integration agents.

Single source of truth for the ``ChatOpenAI`` builder that every
agent (violin_bvbrc, rag_synthesis, composer) uses. All knobs come
from ``APECX_LLM_*`` env vars so operators can tune cost/quality
bounds without re-deploying or editing wrapper YAMLs. Local-LLM
deployments (Ollama, vLLM) are first-class — both expose an
OpenAI-compatible ``/v1`` chat-completions endpoint, no separate
client wrapper needed.

Why this lives in its own module
--------------------------------
Pre-2026-04-27 this factory lived as a private ``_build_chat_llm``
inside ``apecx_integration.agents.violin_bvbrc.agent``. Cross-agent
callers (``rag_synthesis.synthesizer``, ``composition.composer``)
imported it via the private ``_build_chat_llm`` name across module
boundaries — a string-coupled dependency that would silently break
on a violin_bvbrc rename. Promoting it here removes that literal.

The violin_bvbrc.agent module re-exports ``build_chat_llm`` as
``_build_chat_llm`` to preserve the
``monkeypatch.setattr(violin_bvbrc.agent, "_build_chat_llm", ...)``
test surface used by the wrapper-step integration tests. New
callers should import ``build_chat_llm`` from THIS module directly.

Resolution order (per knob): env var > caller kwarg > function default.
The env-var-wins rule lets ops override per-deployment without code
edits; the caller-kwarg fallback lets specialized callers (e.g., a
CSV agent that genuinely needs ``max_tokens=16384``) opt out of the
default policy when ops haven't set a bound.
"""

from __future__ import annotations

import os
from typing import Any

from langchain_openai import ChatOpenAI

from apecx_integration.agents._llm_config import (
    resolve_llm_base_url,
    resolve_llm_model,
    resolve_llm_timeout,
)


def build_chat_llm(
    temperature: float = 0.0,
    max_tokens: int = 1024,
    **overrides: Any,
) -> ChatOpenAI:
    """Build a LangChain ``ChatOpenAI`` against the configured endpoint.

    The model name and base URL come from ``resolve_llm_model`` /
    ``resolve_llm_base_url`` (``apecx_integration.agents._llm_config``) —
    the SINGLE source of truth that ``apecx-setup`` also delegates to, so
    the installer pulls exactly the model the runtime asks for. Defaults to
    a local Ollama daemon on ``http://localhost:11434/v1`` with
    ``nemotron-3-nano:4b``; override either via ``APECX_LLM_MODEL`` /
    ``APECX_LLM_BASE_URL``.

    Resolution order for ``temperature`` and ``max_tokens``:

      env var > caller kwarg (explicit) > function default

    Env vars (``APECX_LLM_TEMPERATURE`` / ``APECX_LLM_MAX_TOKENS``)
    win so that operators can bound cost/quality without redeploying
    or editing wrapper YAMLs. Caller kwargs win over function defaults
    for callers that need a specific shape (e.g., a CSV agent that
    genuinely needs ``max_tokens=16384`` regardless of operator
    policy).

    Args:
        temperature: Default sampling temperature (overridable via
            ``APECX_LLM_TEMPERATURE``).
        max_tokens: Default max-tokens cap (overridable via
            ``APECX_LLM_MAX_TOKENS``).
        **overrides: Forwarded to ``ChatOpenAI(...)`` after the env-
            and-default-resolved kwargs are assembled. Overrides win
            for any key that conflicts.

    Returns:
        A ``ChatOpenAI`` client targeting the configured endpoint.
    """
    base_url = resolve_llm_base_url()
    model = resolve_llm_model()
    api_key = os.environ.get("APECX_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or "EMPTY"
    env_temperature = os.environ.get("APECX_LLM_TEMPERATURE")
    if env_temperature is not None:
        temperature = float(env_temperature)
    env_max_tokens = os.environ.get("APECX_LLM_MAX_TOKENS")
    if env_max_tokens is not None:
        max_tokens = int(env_max_tokens)
    kwargs: dict[str, Any] = {
        "temperature": temperature,
        "model": model,
        "max_tokens": max_tokens,
        "base_url": base_url,
        "api_key": api_key,
        # Bounded per-request timeout so a STALLED endpoint raises (→ caught → degrade-loud)
        # instead of hanging until the nanobrain step kill strands the result. APECX_LLM_TIMEOUT
        # overrides; keep it BELOW the synthesis step's execution_timeout. A caller-supplied
        # `timeout`/`request_timeout` in **overrides still wins.
        "timeout": resolve_llm_timeout(),
    }
    kwargs.update(overrides)
    return ChatOpenAI(**kwargs)


__all__ = ["build_chat_llm"]
