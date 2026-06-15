"""Single source of truth for the synthesis-path LLM model + a loud preflight.

Why this module exists
----------------------
Three subsystems used to carry their OWN default model name and drifted apart:

* ``agents/_llm_factory.build_chat_llm`` — what the synthesis runtime ASKS for.
* ``cli/setup._ollama_model`` — what the installer (``apecx-setup``) PULLS.
* ``composition/composer_config.yml`` — the composer's own model.

When the installer pulled one model and ``build_chat_llm`` asked for another,
the mismatch did not surface at config time — ``ChatOpenAI`` only hit Ollama on
the first CALL and got a cryptic 404 mid-cascade. This module removes the drift
for the SYNTHESIS path: ``resolve_llm_model`` is the ONE place the default lives,
and both the factory and the installer delegate to it, so the installer pulls
exactly what the runtime asks for.

The composer is a deliberately SEPARATE tier (``composer_config.yml`` declares
``mistral-small:latest`` plus per-role bindings that were measured-best for the
structured-YAML codegen task). It always injects an explicit ``model=`` into the
factory and never touches ``resolve_llm_model`` or the synthesis preflight, so it
is not affected by — and does not constrain — the synthesis default here.

This module intentionally has NO heavy imports (no ``langchain_openai``) so the
installer can resolve a model name without dragging the runtime LLM stack in.
Nothing here hits the network at import time.
"""

from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)

# The ONE default model name for the synthesis path. ``apecx-setup`` pulls this
# and ``build_chat_llm`` asks for it. Change it here and both move in lock-step.
DEFAULT_LLM_MODEL = "nemotron-3-nano:4b"
DEFAULT_LLM_BASE_URL = "http://localhost:11434/v1"
# Client-side per-request timeout (seconds) for the synthesis ChatOpenAI client. WITHOUT
# this, the openai client default (~600s) lets a STALLED endpoint (accepts the TCP
# connection but never returns — e.g. Ollama mid-model-load, or a black-holing proxy) hang
# until the nanobrain step's execution_timeout KILLS it from OUTSIDE the step's degrade-loud
# try/except → the EnvelopeStep is stranded → run_workflow returns null (G127). With a
# bounded client timeout SHORTER than the step ceiling, the call raises INSIDE the step and
# degrades loud (the deterministic 5-section fallback) instead. Tunable per deployment.
DEFAULT_LLM_TIMEOUT = 300.0


def resolve_llm_timeout() -> float:
    """Return the LLM client request timeout: ``APECX_LLM_TIMEOUT`` env > default (300s)."""
    raw = os.environ.get("APECX_LLM_TIMEOUT")
    if not raw:
        return DEFAULT_LLM_TIMEOUT
    try:
        return float(raw)
    except ValueError:
        log.warning(
            "APECX_LLM_TIMEOUT=%r is not a number; using default %.0fs", raw, DEFAULT_LLM_TIMEOUT
        )
        return DEFAULT_LLM_TIMEOUT


def resolve_llm_model() -> str:
    """Return the synthesis-path model name: ``APECX_LLM_MODEL`` env > default.

    An empty env var falls back to the default — a non-empty model name is
    always returned, so callers never build a client with ``model=""``.
    """
    return os.environ.get("APECX_LLM_MODEL") or DEFAULT_LLM_MODEL


def resolve_llm_base_url() -> str:
    """Return the LLM base URL: ``APECX_LLM_BASE_URL`` env > default."""
    return os.environ.get("APECX_LLM_BASE_URL") or DEFAULT_LLM_BASE_URL


# (model, base_url) pairs already verified (or warned-on) this process. The
# preflight is an HTTP probe; we pay it at most once per pair so a synthesis
# loop does not re-probe Ollama on every call.
_preflight_done: set[tuple[str, str]] = set()


def _probe_model(model: str, base_url: str) -> tuple[bool, bool]:
    """Probe the endpoint's model list. Returns ``(reachable, model_pulled)``.

    Mirrors the proven reachability logic in
    ``tests/integration/test_viral_epitope_analysis.py::_llm_reachable``:
    Ollama's native list is at the ROOT (``/api/tags``); the OpenAI-compat list
    is at ``/v1/models``. We match the exact tag OR its ``name:tag`` stem so a
    ``latest`` vs pinned-digest difference does not read as "missing".
    """
    stem = model.split(":", 1)[0]
    root = base_url[:-3].rstrip("/") if base_url.endswith("/v1") else base_url
    reachable = False
    for url, list_key, name_key in (
        (root + "/api/tags", "models", "name"),
        (root + "/v1/models", "data", "id"),
        (base_url + "/models", "data", "id"),
    ):
        try:
            response = requests.get(url, timeout=3)
        except requests.RequestException:
            continue
        if not response.ok:
            continue
        reachable = True
        names = [item.get(name_key, "") for item in response.json().get(list_key, [])]
        if any(n == model or n.split(":", 1)[0] == stem for n in names):
            return True, True
    return reachable, False


def preflight_llm_model(model: str | None = None, base_url: str | None = None) -> None:
    """Fail loud, early, and clearly when the synthesis model is not pulled.

    Resolution: ``model``/``base_url`` args > the ``resolve_*`` env-or-default.

    Outcomes (cached per ``(model, base_url)`` for the life of the process):

    * Endpoint reachable AND model pulled → return (the happy path).
    * Endpoint reachable but model MISSING → raise ``RuntimeError`` naming the
      exact ``ollama pull <model>`` command and the ``APECX_LLM_MODEL`` override.
    * Endpoint UNREACHABLE → log a WARNING and return. Offline development is a
      legitimate state; a hard fail there would be too aggressive, and the
      subsequent LLM call surfaces a plain connection error if one is attempted.

    Call this ONCE at a synthesis entry seam (it caches), never per LLM call.
    """
    model = model or resolve_llm_model()
    base_url = (base_url or resolve_llm_base_url()).rstrip("/")
    cache_key = (model, base_url)
    if cache_key in _preflight_done:
        return

    reachable, pulled = _probe_model(model, base_url)
    if reachable and not pulled:
        raise RuntimeError(
            f"LLM model {model!r} is not pulled on the reachable endpoint "
            f"{base_url}. Pull it with:\n\n    ollama pull {model}\n\n"
            f"or point the synthesis runtime at an already-installed model via "
            f"the APECX_LLM_MODEL environment variable."
        )
    if not reachable:
        log.warning(
            "LLM preflight: endpoint %s is unreachable; skipping the model-pulled "
            "check for %r. If you expect a local LLM, start it (e.g. `ollama serve`) "
            "and pull the model with `ollama pull %s`.",
            base_url,
            model,
            model,
        )
    _preflight_done.add(cache_key)


__all__ = [
    "DEFAULT_LLM_MODEL",
    "DEFAULT_LLM_BASE_URL",
    "resolve_llm_model",
    "resolve_llm_base_url",
    "preflight_llm_model",
]
