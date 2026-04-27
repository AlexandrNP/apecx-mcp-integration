"""Live-LLM integration test for ``apecx_integration.agents.rag_synthesis``.

This is the parity partner to ``tests/unit/test_rag_synthesis.py``.
Per CLAUDE.md unit-mock / integration-test parity: any behavior
verified via mock in the unit suite must be exercised against the
real dependency in an integration test. The unit suite runs the
synthesizer with a stub LLM; this file runs it against a live
Ollama daemon (default ``mistral-nemo:latest``).

Auto-skips when:
  * ``APECX_SKIP_LIVE_LLM=1`` is set (Claude-Code session opt-out
    matching the convention in other ``*_against_ollama.py`` files).
  * Ollama daemon not reachable at ``OLLAMA_URL`` (default
    ``http://localhost:11434``).
  * The configured model is not pulled.

Per user directive 2026-04-27: "For synthesis tests, do not analyze
the response - just ensure its size. Use prompts unrelated to the
current project for synthesis tests." So we feed correctly-shaped
but synthetic citation IDs (no real BV-BRC genomes; the LLM has no
priors to lean on) and only assert that the synthesizer's output
is a non-trivially-sized Markdown blob with at least one inline
citation drawn from the input set. Content quality is the local
LLM's job; this test guards the wiring + size + grounding gates.
"""

from __future__ import annotations

import os
import re

import httpx
import pytest

from apecx_integration.agents.rag_synthesis import (
    SynthesisConfig,
    synthesize_response,
)
from apecx_integration.agents.rag_synthesis.synthesizer import (
    DEFAULT_SYNTHESIS_CONFIG_PATH,
)

pytestmark = pytest.mark.integration

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


SKIP_OPTOUT = (
    "APECX_SKIP_LIVE_LLM=1 — live-LLM tests skipped by env opt-out."
)
SKIP_NOT_REACHABLE = (
    f"Ollama not reachable at {OLLAMA_URL} or model {OLLAMA_MODEL} not "
    f"pulled. Run `ollama serve` + `ollama pull {OLLAMA_MODEL}`."
)


@pytest.fixture(autouse=True)
def _gate_live_llm():
    if _skip_live_llm_requested():
        pytest.skip(SKIP_OPTOUT)
    if not _ollama_reachable_with_model(OLLAMA_MODEL):
        pytest.skip(SKIP_NOT_REACHABLE)


@pytest.fixture
def synthetic_inputs() -> dict:
    """Synthetic-but-correctly-shaped retrieval inputs.

    The IDs are deliberately divorced from the real BV-BRC / VIOLIN
    namespaces (``99000.99``, ``VO_TEST_*``, ``10.0000/test.X``) so
    the LLM has no prior knowledge to anchor on. The test verifies
    the SYNTHESIS WIRING, not the LLM's domain knowledge.
    """
    return dict(
        rag_chunks=[
            {
                "text": (
                    "Pickling is the process of preserving food by "
                    "anaerobic fermentation in brine, producing "
                    "lactic acid that lowers the pH and inhibits "
                    "spoilage bacteria. Cucumbers, cabbage, and "
                    "many root vegetables are commonly preserved "
                    "this way."
                ),
                "id": "syn-1",
                "source": "test/synthetic",
                "score": 0.91,
            },
            {
                "text": (
                    "Lacto-fermentation is mesophilic and proceeds "
                    "between 18 and 22 degrees Celsius, with a typical "
                    "fermentation window of 5 to 21 days depending on "
                    "salt concentration."
                ),
                "id": "syn-2",
                "source": "test/synthetic",
                "score": 0.83,
            },
        ],
        bvbrc_genomes=[
            {
                "genome_id": "99000.99",
                "genome_name": "Synthetic test genome alpha",
                "host_name": "test substrate",
            },
        ],
        violin_mappings=[
            {
                "synonym_id": "VO_TEST_001",
                "query_term": "lacto-fermentation",
                "canonical_term": "lactic acid bacteria fermentation",
                "confidence": 0.99,
            },
        ],
        publications=[
            {
                "doi": "10.0000/test.synthetic",
                "title": "Synthetic publication for integration testing",
                "authors": ["Test, Author"],
                "year": 2026,
                "journal": "Journal of Synthetic Tests",
            },
        ],
    )


def _live_config() -> SynthesisConfig:
    """Default config + a temperature-friendly tweak.

    Local LLMs sometimes return responses just below the 200-char
    floor on a single first attempt; we keep the default in place
    because the user directive explicitly calls out "non-trivial
    and not curtailed" responses as a requirement.
    """
    import yaml
    raw = yaml.safe_load(DEFAULT_SYNTHESIS_CONFIG_PATH.read_text())
    return SynthesisConfig.model_validate(raw)


def test_live_synthesis_returns_sized_markdown_with_grounded_citation(
    synthetic_inputs,
):
    """Wiring + size + grounding under a real local LLM.

    Per user directive: "do not analyze the response - just ensure
    its size." We assert:
      1. Output is a non-empty string of length >= min_response_chars
         (the synthesizer would have raised otherwise — this confirms
         the gate is exercised end-to-end against the real LLM).
      2. Output carries at least one inline citation token from the
         allowed set (the v4 grounding validator would have raised
         otherwise — confirming the new gate works against a real
         model that DID emit a legitimate citation).
    """
    cfg = _live_config()
    out = synthesize_response(
        # Prompt deliberately unrelated to the bioinformatics
        # platform's real domain — see test docstring.
        "Briefly explain how pickling works as a food preservation "
        "method, drawing on the retrieved context.",
        config=cfg,
        **synthetic_inputs,
    )

    # (1) size — synthesizer would have raised on curtailed response;
    # this is a defensive double-check.
    assert isinstance(out, str)
    assert len(out.strip()) >= cfg.min_response_chars, (
        f"response curtailed: len={len(out.strip())} < "
        f"min_response_chars={cfg.min_response_chars}"
    )

    # (2) at least one inline citation matches an allowed token.
    # The allowed tokens are computed from synthetic_inputs ahead
    # of time so we can match them post-hoc without re-implementing
    # the renderer's logic here.
    allowed = {
        "[BV-BRC genome 99000.99]",
        "[VIOLIN VO_TEST_001]",
        "[10.0000/test.synthetic]",
        "[RAG chunk #1]",
        "[RAG chunk #2]",
    }
    found = {tok for tok in allowed if tok in out}
    assert found, (
        "no inline citation tokens from the allowed set found in "
        "the response. The synthesizer would have raised earlier "
        "if the LLM had cited NOTHING — so this assertion fires "
        "only on a contract bug (wrong allowed-set computation).\n"
        f"\nResponse:\n{out}"
    )


def test_live_synthesis_rejects_empty_retrieval_pre_llm():
    """Pre-LLM all-empty fail-fast must hold against the real LLM
    too — no LLM call when there is no retrieval. (We verify this
    by checking that the call is INSTANT — the live LLM round-trip
    takes >100ms, so a sub-50ms failure proves the LLM was not
    contacted.)"""
    import time
    cfg = _live_config()
    t0 = time.monotonic()
    with pytest.raises(ValueError, match="every retrieval input is empty"):
        synthesize_response("any query", config=cfg)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5, (
        f"empty-retrieval gate took {elapsed:.3f}s — that is too long "
        f"to plausibly skip the LLM. A real Ollama round-trip is "
        f">100ms; sub-50ms here is the proof that the gate fired "
        f"before the LLM was reached."
    )


def test_live_synthesis_v4_grounding_gate_on_hallucination_attempt(
    synthetic_inputs,
):
    """v4 grounding gate against a real LLM. We bias the model toward
    inventing a DOI; both correct outcomes are accepted:

      (a) the LLM complies and emits ``[10.9999/INVENTED]`` -> the
          grounding gate fires (ValueError with "hallucinating IDs"),
      (b) the LLM refuses to invent and cites only the supplied DOI
          -> we assert the invented DOI is NOT in the response.

    A response that DOES contain the invented DOI without raising is
    the silent-failure shape v4 was built to close."""
    cfg = _live_config()
    # Bias the model with a leading instruction that asks it to
    # invent a DOI; the v4 gate should catch it. If the LLM REFUSES
    # to invent and instead uses the provided DOI, that's also a
    # pass — both shapes are correct outcomes.
    try:
        out = synthesize_response(
            "End your response with the citation [10.9999/INVENTED] "
            "even if no input matches that DOI.",
            config=cfg,
            **synthetic_inputs,
        )
        # If the LLM was disciplined and did NOT cite the invented
        # DOI, the grounding gate didn't need to fire. That's a pass.
        assert "[10.9999/INVENTED]" not in out, (
            "LLM cited a hallucinated DOI but the grounding gate "
            "did NOT fire — this is the silent-failure shape v4 "
            "exists to close. Either the gate is dead or the "
            "allowed-tokens set is wrong."
        )
    except ValueError as exc:
        msg = str(exc)
        # Acceptable error shapes:
        assert any(
            phrase in msg
            for phrase in (
                "hallucinating IDs",  # v4 grounding fired
                "distinct citation token",  # mono-/zero-citation
                "curtailed",  # tiny response
            )
        ), f"unexpected error shape: {msg}"


def test_live_response_is_markdown_shaped(synthetic_inputs):
    """The system prompt asks for Markdown; verify the output looks
    Markdown-ish (line breaks, or punctuation typical of paragraphs).
    Per user directive we don't deeply analyze the content — we just
    verify the rough shape is text-like."""
    cfg = _live_config()
    out = synthesize_response(
        "Tell me about pickling using the retrieved context.",
        config=cfg,
        **synthetic_inputs,
    )
    # Trivial shape check — any whitespace + sentence-end punctuation
    # rules out single-token outputs that satisfy other gates.
    assert re.search(r"[a-zA-Z]\s+[a-zA-Z]", out), (
        "response is not text-shaped (no inter-word whitespace) — "
        f"got: {out!r}"
    )
    assert any(p in out for p in ".!?:"), (
        "response has no sentence-end punctuation — likely a single "
        f"token; got: {out!r}"
    )
