"""Live-LLM integration test for ``RagSynthesisStep`` (the nanobrain
wrapper around ``synthesize_response``).

CLAUDE.md unit-mock / integration-test parity: ``test_rag_synthesis_step.py``
exercises the wrapper with monkeypatched ``synthesize_response``;
this file exercises it via the canonical ``from_config`` boot path
+ a real Ollama backend. Auto-skips when:

  * ``APECX_SKIP_LIVE_LLM=1`` is set,
  * Ollama daemon not reachable / model not pulled.

Per user directive 2026-04-27: synthesis-test fixtures use synthetic
IDs and a non-domain prompt; assert size + grounded citation only.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import pytest

from apecx_integration.composition.steps.rag_synthesis_step import (
    RagSynthesisStep,
)


pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER_YAML = (
    REPO_ROOT / "src" / "apecx_integration" / "composition"
    / "workflows" / "violin_bvbrc" / "steps" / "rag_synthesis.yml"
)


OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get(
    "APECX_LLM_MODEL",
    os.environ.get("OLLAMA_MODEL", "mistral-nemo:latest"),
)


def _skip_live_llm_requested() -> bool:
    return os.environ.get("APECX_SKIP_LIVE_LLM") == "1"


def _ollama_reachable_with_model(model: str) -> bool:
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=2.0)
        r.raise_for_status()
        names = {m["name"] for m in r.json().get("models", [])}
        return model in names
    except Exception:
        return False


SKIP_OPTOUT = "APECX_SKIP_LIVE_LLM=1 — live-LLM tests skipped."
SKIP_NOT_REACHABLE = (
    f"Ollama not reachable at {OLLAMA_URL} or model {OLLAMA_MODEL} "
    f"not pulled."
)


@pytest.fixture(autouse=True)
def _gate():
    if _skip_live_llm_requested():
        pytest.skip(SKIP_OPTOUT)
    if not _ollama_reachable_with_model(OLLAMA_MODEL):
        pytest.skip(SKIP_NOT_REACHABLE)


def test_step_runs_against_ollama_with_synthetic_inputs():
    """The full from_config boot path + async process() against a
    real Ollama backend produces a sized + grounded synthesis."""
    step = RagSynthesisStep.from_config(str(WRAPPER_YAML))

    inputs = {
        "query": (
            "Briefly explain how pickling preserves food, drawing on "
            "the retrieved context."
        ),
        "rag_chunks": [
            {
                "text": (
                    "Pickling is anaerobic fermentation in brine that "
                    "produces lactic acid, lowering pH and inhibiting "
                    "spoilage bacteria. Cucumbers, cabbage, and many "
                    "root vegetables are commonly preserved this way."
                ),
                "id": "syn-1",
                "source": "test/synthetic",
                "score": 0.92,
            },
            {
                "text": (
                    "Lacto-fermentation proceeds at mesophilic "
                    "temperatures (18-22°C) with a 5-21 day window."
                ),
                "id": "syn-2",
                "source": "test/synthetic",
            },
        ],
        "bvbrc_genomes": [
            {
                "genome_id": "99000.99",
                "genome_name": "Synthetic test genome alpha",
            },
        ],
        "violin_mappings": [
            {
                "synonym_id": "VO_TEST_001",
                "query_term": "lacto-fermentation",
                "canonical_term": "lactic acid bacteria fermentation",
            },
        ],
        "publications": [
            {
                "doi": "10.0000/test.synthetic.pickle",
                "title": "Synthetic pickling integration test",
                "authors": ["Test, A."],
                "year": 2026,
                "journal": "Journal of Synthetic Tests",
            },
        ],
    }

    out = asyncio.run(step.process(inputs))
    assert isinstance(out, dict)
    assert "synthesis" in out
    body = out["synthesis"]
    # Size check (per user directive — no content analysis).
    assert isinstance(body, str)
    assert len(body.strip()) >= 200, f"len={len(body.strip())}"
    # Grounding spot-check — at least one allowed citation token
    # surfaced. The synthesizer's gates would have raised before this
    # assertion if no citation appeared, so this is a safety check.
    allowed = {
        "[BV-BRC genome 99000.99]",
        "[VIOLIN VO_TEST_001]",
        "[10.0000/test.synthetic.pickle]",
        "[RAG chunk #1]",
        "[RAG chunk #2]",
    }
    found = {tok for tok in allowed if tok in body}
    assert found, f"no allowed citation found in:\n{body}"


def test_step_pre_llm_empty_retrieval_gate_holds_via_step():
    """Empty inputs → fail-fast BEFORE the LLM round-trip. Verify by
    timing the call (real Ollama latency is >100ms; sub-500ms = no
    LLM contact)."""
    import time
    step = RagSynthesisStep.from_config(str(WRAPPER_YAML))
    t0 = time.monotonic()
    with pytest.raises(ValueError, match="every retrieval input is empty"):
        asyncio.run(step.process({"query": "any query"}))
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"elapsed={elapsed:.3f}s — likely hit LLM"


def test_step_async_does_not_block_the_event_loop():
    """asyncio.to_thread offload: the LLM call must run on a worker
    thread so other async tasks proceed.

    Property under test: a 50ms ``asyncio.sleep`` running concurrently
    with ``step.process()`` MUST elapse in roughly its nominal duration
    (< 500ms) — regardless of whether process() succeeds or raises.
    A blocked event loop would delay sleep until process() returns
    (and Ollama round-trips are >1s on mistral-nemo).

    Decoupled from synth success: even a synthesizer ValueError
    (hallucinated citation, etc.) must not affect the property, since
    the LLM is on the worker thread either way. Robustness here also
    means we don't flake on LLM content quality.
    """
    step = RagSynthesisStep.from_config(str(WRAPPER_YAML))

    inputs = {
        "query": (
            "Briefly explain how pickling preserves food using "
            "[BV-BRC genome 99000.99] and [RAG chunk #1] and "
            "[10.0000/test.x] as your inline citations."
        ),
        "rag_chunks": [{"text": "Some chunk text about pickling."}],
        "bvbrc_genomes": [
            {"genome_id": "99000.99", "name": "Synthetic test genome"},
        ],
        "violin_mappings": [],
        "publications": [
            {"doi": "10.0000/test.x", "title": "T"}
        ],
    }

    import time
    sleep_elapsed = None
    synth_done = False

    async def _runner():
        nonlocal sleep_elapsed, synth_done

        async def _sleep_task():
            nonlocal sleep_elapsed
            t0 = time.monotonic()
            await asyncio.sleep(0.05)
            sleep_elapsed = time.monotonic() - t0

        async def _synth_task():
            nonlocal synth_done
            try:
                await step.process(inputs)
            except Exception:
                # Tolerate synthesizer ValueError — the property
                # under test is event-loop liveness, not synth
                # success. A failure here doesn't invalidate the
                # async-non-blocking property.
                pass
            synth_done = True

        await asyncio.gather(_sleep_task(), _synth_task())

    asyncio.run(_runner())
    assert sleep_elapsed is not None
    # Sleep nominal is 50ms; allow 500ms slack. If the event loop
    # was blocked by a synchronous LLM call inside process(), this
    # would be >>1s.
    assert sleep_elapsed < 0.5, (
        f"asyncio.sleep(0.05) took {sleep_elapsed:.3f}s — process() "
        f"likely blocked the event loop (LLM call not offloaded)"
    )
    assert synth_done, "synth task did not complete"
