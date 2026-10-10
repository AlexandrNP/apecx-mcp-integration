"""Live-LLM parity test for the LiteratureRagStep GRADED LLM+CE FUSION stage.

CLAUDE.md unit-mock / integration-test parity: tests/unit/test_literature_rag_llm_fusion.py
exercises the same ``_hybrid_rank`` fusion path with a STUBBED LLM; this file drives it through
the SAME path against a REAL apecx LLM (Ollama) — no mocks. It asserts the real model returns a
PARSEABLE graded score and the fusion path runs end-to-end, keeping a SANE subset.

SYNTHETIC toy text only (per the standing synthesis-test directive): the "question" is a trivial
banana-mention check and the "papers" are fabricated one-liners — NO real scientific question, NO
PMID. We assert only that fusion returns a SANE subset with the floor+cap invariants held, and
that the real model's replies parse into graded floats in [0, 1].

Auto-skips when ``APECX_SKIP_LIVE_LLM=1`` or Ollama is unreachable / the model is not pulled.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.integration

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("APECX_LLM_MODEL", os.environ.get("OLLAMA_MODEL", "medgemma:latest"))


def _skip_live_llm_requested() -> bool:
    return os.environ.get("APECX_SKIP_LIVE_LLM") == "1"


def _ollama_reachable_with_model(model: str) -> bool:
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=2.0)
        r.raise_for_status()
        names = {m["name"] for m in r.json().get("models", [])}
        stem = model.split(":", 1)[0]
        return any(n == model or n.split(":", 1)[0] == stem for n in names)
    except Exception:
        return False


@pytest.fixture(autouse=True)
def _gate():
    if _skip_live_llm_requested():
        pytest.skip("APECX_SKIP_LIVE_LLM=1 — live-LLM tests skipped.")
    if not _ollama_reachable_with_model(OLLAMA_MODEL):
        pytest.skip(f"Ollama not reachable at {OLLAMA_URL} or model {OLLAMA_MODEL} not pulled.")


@pytest.fixture(autouse=True)
def _enable_fusion(monkeypatch):
    monkeypatch.setenv("APECX_ENABLE_LLM_FUSION", "1")
    monkeypatch.setenv("APECX_LLM_MODEL", OLLAMA_MODEL)
    # The CE stage is a separate opt-in; keep it OFF so fusion uses the bi-encoder cosine as the
    # retrieval leg and this test isolates the fusion stage.
    monkeypatch.delenv("APECX_ENABLE_CROSS_ENCODER", raising=False)
    monkeypatch.delenv("APECX_ENABLE_LLM_VERIFY", raising=False)


def _rag_step():
    import apecx_integration.composition.workflows.literature_rag.steps as pkg
    from apecx_integration.composition.workflows.literature_rag.steps.literature_rag_step import (
        LiteratureRagStep,
    )

    yml = str(Path(pkg.__file__).resolve().parent / "literature_rag_step.yml")
    step = LiteratureRagStep.from_config(yml)
    # Neutralize the embedding tiebreak/bar + cross-encoder so coverage keeps all toy records and
    # the REAL LLM graded score is the sole discriminator (fused with a flat zero retrieval leg).
    step._embed_sims = lambda q, recs: [0.0] * len(recs)
    step._get_cross_encoder = lambda: (_ for _ in ()).throw(RuntimeError("CE off for this test"))
    return step


_TOY_RECORDS = [
    {
        "pmid": "toy1",
        "title": "A banana is a yellow fruit",
        "abstract": "This text mentions a banana growing on a tree.",
    },
    {
        "pmid": "toy2",
        "title": "Bananas in a fruit bowl",
        "abstract": "The banana sat beside an apple; this text mentions a banana.",
    },
    {
        "pmid": "toy3",
        "title": "Ripe banana smoothie recipe",
        "abstract": "Blend one banana with milk; this text clearly mentions a banana.",
    },
    {
        "pmid": "toy4",
        "title": "A paper about bicycles and traffic",
        "abstract": "This text discusses urban cycling lanes and mentions no fruit at all.",
    },
]


def test_real_llm_returns_parseable_graded_scores():
    """The REAL model, prompted by fusion_prompt.md, returns replies that parse into graded
    floats in [0, 1] — exercises _llm_relevance_scores + _parse_fusion_score end-to-end."""
    step = _rag_step()
    question = "Which texts mention a banana?"
    scores = step._llm_relevance_scores(question, _TOY_RECORDS)
    assert len(scores) == len(_TOY_RECORDS)
    assert any(s is not None for s in scores)  # not every call errored
    for s in scores:
        assert s is None or (isinstance(s, float) and 0.0 <= s <= 1.0)


def test_real_llm_fusion_keeps_sane_subset():
    """A trivial banana-mention criterion on synthetic one-liners: the REAL fusion stage runs
    end-to-end and keeps a sane subset, honouring floor+cap."""
    step = _rag_step()
    question = "Which texts mention a banana?"

    ranked = step._hybrid_rank(question, _TOY_RECORDS, 15)
    pmids = {r["pmid"] for r in ranked}

    # Sane-subset invariants (do NOT over-assert a specific LLM-graded cut):
    assert 0 < len(ranked) <= 15  # cap holds, never empty
    assert pmids.issubset({"toy1", "toy2", "toy3", "toy4"})  # only supplied records come back
    # At least one true banana record survives the real graded fusion.
    assert pmids & {"toy1", "toy2", "toy3"}
