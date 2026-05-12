"""CGU-P0-T1 — multi-model Ollama inventory smoke test.

The composer codegen-uplift effort uses three local models via Ollama:

* ``mistral-nemo:latest``      (12B, drafter / direct-codegen baseline)
* ``nemotron-3-nano:4b``       (4B, planner / reviewer; emits ``<think>``)
* ``gemma4:latest``            (8B, fallback / pluggability target)

This test is the canary: each model is reachable through the
OpenAI-compatible ``/v1`` chat endpoint and returns a non-empty
completion. If a model is missing from ``ollama list``, the test for
that specific model is skipped (per-model, not whole-suite) — that
way one stale model on a developer machine does not silently disable
the entire codegen-uplift CI signal.

Auto-skip rules:

* ``APECX_SKIP_LIVE_LLM=1`` → whole module skipped (offline session).
* Ollama daemon unreachable → whole module skipped with a clear
  message naming the daemon URL.
* Daemon reachable but model not pulled → that single parametrized
  case is skipped; sibling models still run.

The test deliberately does NOT pin specific output strings — the
contract is "model answers", not "model says X". String-equality
gates would make the test brittle against quantization differences
or Ollama version upgrades.
"""

from __future__ import annotations

import os

import httpx
import pytest

from apecx_integration.agents._llm_factory import build_chat_llm

pytestmark = pytest.mark.integration

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

# Models the codegen-uplift effort depends on. Adding a new entry
# here implicitly extends the smoke contract: any new model the
# composer uses must answer the smoke prompt or be explicitly
# allowed to fail in this manifest.
EXPECTED_MODELS: tuple[str, ...] = (
    "mistral-nemo:latest",
    "nemotron-3-nano:4b",
    "gemma4:latest",
)


def _skip_live_llm_requested() -> bool:
    return os.environ.get("APECX_SKIP_LIVE_LLM") == "1"


def _installed_models() -> set[str]:
    """Return the set of ``name`` fields from ``/api/tags``.

    Empty set on any reachability error. Caller treats empty as
    "daemon unreachable" and skips the whole module.
    """
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=2.0)
        r.raise_for_status()
    except Exception:
        return set()
    return {m["name"] for m in r.json().get("models", [])}


SKIP_OPTOUT = "APECX_SKIP_LIVE_LLM=1 — live-LLM tests explicitly skipped."
SKIP_DAEMON = (
    f"Ollama daemon not reachable at {OLLAMA_URL}. "
    "Run `ollama serve` to enable the inventory smoke test."
)


@pytest.fixture(scope="module")
def installed() -> set[str]:
    if _skip_live_llm_requested():
        pytest.skip(SKIP_OPTOUT)
    inventory = _installed_models()
    if not inventory:
        pytest.skip(SKIP_DAEMON)
    return inventory


@pytest.mark.parametrize("model", EXPECTED_MODELS)
def test_model_answers_smoke_prompt(model: str, installed: set[str]) -> None:
    """Each expected model returns a non-empty completion.

    Per-model skip rather than whole-suite skip — a stale developer
    machine missing one model should not erase the signal from the
    other two.
    """
    if model not in installed:
        pytest.skip(f"{model!r} not pulled. Run `ollama pull {model}` to enable this case.")

    llm = build_chat_llm(
        temperature=0.0,
        max_tokens=64,
        model=model,
        base_url=f"{OLLAMA_URL}/v1",
    )
    from langchain_core.messages import HumanMessage, SystemMessage  # noqa: PLC0415

    response = llm.invoke(
        [
            SystemMessage(content="You reply with a single short word."),
            HumanMessage(content="Reply with the word: OK"),
        ]
    )
    text = response.content if hasattr(response, "content") else str(response)

    # The smoke contract: non-empty, parseable string. We do NOT
    # assert text == "OK" — nemotron in particular sometimes wraps
    # the answer in <think>...</think> + the word, and quantization
    # versions occasionally produce "Ok." or "OK." The brittle pin
    # is worse than no pin.
    assert isinstance(text, str), f"{model}: response.content not a string: {response!r}"
    assert text.strip(), f"{model}: returned empty completion"


def test_inventory_covers_expected_models(installed: set[str]) -> None:
    """Diagnostic: how many of the expected models are actually pulled?

    Does NOT fail when models are missing — that's the per-model
    parametrized case's job. This test instead surfaces the
    inventory state as part of the pytest output so a developer
    can see at a glance whether their local Ollama matches the
    codegen-uplift assumptions.
    """
    missing = [m for m in EXPECTED_MODELS if m not in installed]
    if missing:
        # Reported as XFAIL rather than FAIL — diagnostic, not gating.
        pytest.xfail(
            f"Missing models on this host (run `ollama pull <name>` to fix): {', '.join(missing)}"
        )
    # All present → assertion holds trivially. The test name documents
    # the happy-path expectation.
    assert set(EXPECTED_MODELS).issubset(installed)
