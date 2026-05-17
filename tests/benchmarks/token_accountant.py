"""Thread-local token accountant for the benchmark harness — G100 (2026-05-17).

Captures per-codegen LLM call counts + prompt / completion token totals
so the benchmark runner can record them in ``RunResult``. Wall time is a
noisy cost proxy (subprocess startup, network latency, model warmup);
token counts are the true cost signal for "did this pattern actually
spend more LLM resources to gain its lift?"

Design choices:

* **Thread-local accumulator** — the runner processes one problem at
  a time per worker; thread-local is safe + low-overhead. Concurrent
  problems (if the runner ever parallelizes) would each get their own
  state.

* **Context-manager API** — ``with count_tokens() as totals:`` resets
  the counter on enter, yields a live snapshot reference, and freezes
  it on exit. Callers read ``totals.prompt_tokens`` etc. from outside
  the ``with`` block AFTER it exits.

* **Install-once on each LLM** — ``install_on_llm(llm)`` adds a
  callback to the LLM's ``callbacks`` list. The callback writes to
  the thread-local on every ``on_llm_end`` event. Idempotent: calling
  ``install_on_llm`` twice on the same LLM doesn't double-count.

* **Best-effort token extraction** — LangChain surfaces token usage
  differently across model providers. For Ollama via the OpenAI-compat
  endpoint, ``response.llm_output["token_usage"]`` typically carries
  ``prompt_tokens`` + ``completion_tokens``. We fall back through
  several candidate locations and record 0 when none are present —
  callers can distinguish "we didn't capture" from "0 tokens used"
  via the ``n_calls`` field (>0 if calls happened).

Honest scope limitations:
  * Ollama in some configurations doesn't return token counts at all.
    We log a warning once per (codegen, problem) when n_calls > 0 but
    total tokens stay 0 — that's the signal the endpoint isn't
    cooperating.
  * Cached LLMs (the ``_LLM_CACHE`` in direct.py) get the callback
    installed at cache-miss time. Subsequent cache hits return the
    already-instrumented instance — the same callback fires for them
    too. This is the correct behavior.
  * Some codegens spawn subprocesses (the sandbox); subprocess Python
    runs are NOT counted because they're not LLM calls. This is also
    correct — the sandbox cost belongs to a separate dimension.
  * **YAML-workflow codegens** (tdr_yaml, best_of_n_yaml) construct
    their LLMs via build_chat_llm (in src/) which does NOT install
    the callback — the import-resolution hook bans src/ → tests/
    dependencies. Result: YAML codegen token counts will be 0 with
    n_llm_calls > 0 (extraction_misses non-zero). To instrument
    YAML codegens, ``install_on_workflow(workflow)`` can be called
    by the codegen driver after Workflow.from_config — but the
    CodeWriteStep's LLM is built lazily per call inside _invoke_llm,
    so this is a known limitation we accept rather than refactor
    CodeWriteStep.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

_local = threading.local()


@dataclass
class TokenTotals:
    """Live snapshot of token usage in the current count_tokens() scope."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    n_calls: int = 0
    # Internal: which LLM responses we couldn't extract tokens from.
    # Used to surface the Ollama-no-counts warning at most once per scope.
    extraction_misses: int = 0


def _get_or_init_local() -> TokenTotals:
    """Return the thread-local TokenTotals, creating a fresh one if
    we're not currently inside a ``count_tokens()`` scope.

    Outside a scope, we still capture into a 'detached' totals so
    that direct ``_default_llm_for`` callers (e.g., tests that build
    an LLM directly without a context manager) don't crash. The
    detached totals are discarded at the next ``count_tokens()`` reset.
    """
    if not hasattr(_local, "totals"):
        _local.totals = TokenTotals()
    return _local.totals


@contextmanager
def count_tokens():
    """Reset thread-local token totals + yield a live snapshot.

    Usage:
        with count_tokens() as totals:
            do_llm_work()
        # totals.prompt_tokens etc. now reflect the work done above.

    The yielded ``TokenTotals`` is a reference to the same object the
    callback writes to; reading it AFTER the with-block exits gives
    the final captured counts.
    """
    fresh = TokenTotals()
    prior = getattr(_local, "totals", None)
    _local.totals = fresh
    try:
        yield fresh
    finally:
        # Restore the prior (typically None) so nested scopes don't
        # leak. The yielded ``fresh`` reference is still held by the
        # caller — they can still read it.
        if prior is None:
            del _local.totals
        else:
            _local.totals = prior


class _TokenAccountantCallback:
    """LangChain callback handler that writes to the thread-local
    ``TokenTotals`` on every LLM end event.

    Not a subclass of any LangChain class — we implement only the
    duck-typed methods LangChain calls. This avoids pinning to a
    specific LangChain version's BaseCallbackHandler API surface.
    """

    def on_llm_start(self, *_args, **_kwargs) -> None:
        pass

    def on_llm_end(self, response: Any, **_kwargs) -> None:
        """Extract prompt + completion tokens from the response.

        Tries multiple candidate locations in order:
          1. ``response.llm_output["token_usage"]`` — the OpenAI-compat
             classic location.
          2. ``generation.message.usage_metadata`` — LangChain
             AIMessage's normalized field (newer LangChain).
          3. ``generation.generation_info["token_usage"]`` — some
             providers stuff it here.

        If none yield a count, increments ``extraction_misses`` so the
        scope can warn once that the endpoint isn't surfacing tokens.
        """
        totals = _get_or_init_local()
        totals.n_calls += 1

        prompt = completion = 0
        captured = False

        # Path 1: llm_output["token_usage"]
        llm_out = getattr(response, "llm_output", None)
        if isinstance(llm_out, dict):
            usage = llm_out.get("token_usage") or llm_out.get("usage") or {}
            if usage:
                prompt = (usage.get("prompt_tokens") or usage.get("input_tokens") or 0) or 0
                completion = (
                    usage.get("completion_tokens") or usage.get("output_tokens") or 0
                ) or 0
                if prompt or completion:
                    captured = True

        # Path 2/3: per-generation usage_metadata
        if not captured:
            generations = getattr(response, "generations", None) or []
            for gen_list in generations:
                for gen in gen_list:
                    message = getattr(gen, "message", None)
                    usage_metadata = getattr(message, "usage_metadata", None) if message else None
                    if isinstance(usage_metadata, dict):
                        prompt += usage_metadata.get("input_tokens", 0) or 0
                        completion += usage_metadata.get("output_tokens", 0) or 0
                        captured = True
                        continue
                    gen_info = getattr(gen, "generation_info", None) or {}
                    if isinstance(gen_info, dict):
                        usage = gen_info.get("token_usage") or {}
                        if usage:
                            prompt += (usage.get("prompt_tokens") or 0) or 0
                            completion += (usage.get("completion_tokens") or 0) or 0
                            captured = True

        if captured:
            totals.prompt_tokens += int(prompt)
            totals.completion_tokens += int(completion)
        else:
            totals.extraction_misses += 1

    # Catch-all for callbacks LangChain expects but we don't care about.
    def __getattr__(self, name: str) -> Any:
        # Return a no-op function for any unrecognized callback hook.
        # Avoids AttributeError for callbacks like on_chat_model_start
        # in older LangChain versions.
        if name.startswith("on_"):
            return lambda *_a, **_k: None
        raise AttributeError(name)


# Singleton callback — one instance, installed on multiple LLMs.
# Thread-safety of the underlying _local is handled by Python's
# thread-local storage.
_CALLBACK_SINGLETON = _TokenAccountantCallback()


def install_on_llm(llm: Any) -> Any:
    """Attach the token-accountant callback to a LangChain ``ChatOpenAI``
    (or compatible) instance. Idempotent — calling twice is harmless.

    Returns the same ``llm`` for chaining: ``llm = install_on_llm(build_chat_llm())``.
    """
    callbacks = getattr(llm, "callbacks", None)
    if callbacks is None:
        # ChatOpenAI's callbacks attribute may be None or a list. If
        # neither, fall back to using the .with_config method (newer
        # LangChain). Defensive: don't crash if the attribute pattern
        # doesn't fit — token counting becomes a no-op.
        try:
            llm.callbacks = [_CALLBACK_SINGLETON]
        except Exception as e:  # noqa: BLE001
            log.warning(
                "TokenAccountant: could not install callback on %s: %s",
                type(llm).__name__,
                e,
            )
        return llm
    if _CALLBACK_SINGLETON in callbacks:
        return llm  # Already installed.
    callbacks.append(_CALLBACK_SINGLETON)
    return llm


def install_on_workflow(workflow: Any) -> Any:
    """Walk a nanobrain Workflow + install the token callback on any
    LLM-bearing step it finds. Used by the YAML codegen drivers
    (tdr_yaml, best_of_n_yaml) — their LLM instances live inside
    workflow-owned CodeWriteStep instances rather than at the
    benchmark-facing cache.

    Recursive: walks ``workflow.child_steps`` and any ``_writer``
    attribute exposed by a custom Step (the TdrIterationStep
    convention). Defensive: silently skips steps without an LLM —
    not every step has one.

    Returns the workflow for chaining.
    """
    if workflow is None:
        return workflow
    child_steps = getattr(workflow, "child_steps", None) or {}
    for step in child_steps.values():
        # The CodeWriteStep convention: ``._llm`` (private cached
        # client) OR ``._writer._llm`` for composed steps like
        # TdrIterationStep. Try both. Steps that built their LLM
        # via build_chat_llm and stored elsewhere are not covered;
        # this best-effort install catches the common case.
        for attr in ("_llm", "_writer"):
            obj = getattr(step, attr, None)
            if obj is None:
                continue
            if attr == "_writer":
                writer_llm = getattr(obj, "_llm", None)
                if writer_llm is not None:
                    install_on_llm(writer_llm)
            else:
                install_on_llm(obj)
    return workflow


__all__ = ["TokenTotals", "count_tokens", "install_on_llm", "install_on_workflow"]
