"""Tests for ``LiteratureRagStep`` — the literature_rag reading-and-synthesis step.

Two layers, per the workspace mocks policy (mocks only for wiring smoke; real
backends for correctness):

  1. OFFLINE unit (always runs, no LLM, no network): the degrade-loud
     no-evidence contract. ``filtered_records=[]`` must return
     ``status="no_evidence"`` with an empty answer and NO LLM call.

  2. Ollama-GATED integration (real LLM + real sentence-transformers retrieval):
     three CHIKV abstracts with distinct PMIDs go in; the step must return
     ``status="ok"`` with a non-empty answer and at least one cited PMID that
     traces back to a fixture PMID. The synthesizer's citation-against-inputs
     grounding gate is what makes that traceability real — a hallucinated
     citation would raise inside ``synthesize_response`` and the step would
     degrade to ``status="synthesis_failed"`` (asserted-against, never faked).

Run:
    PYTHONPATH=src .venv/bin/python -m pytest \
        tests/unit/test_literature_rag_step_smoke.py -q
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import pytest

from apecx_integration.composition.workflows.literature_rag.steps.literature_rag_step import (
    LiteratureRagStep,
)

_STEP_YAML = (
    Path(__file__).resolve().parents[1].parent
    / "src"
    / "apecx_integration"
    / "composition"
    / "workflows"
    / "literature_rag"
    / "steps"
    / "literature_rag_step.yml"
)

# Workspace synthesis baseline (12B) — far more reliable than the 4B default at
# reproducing the inline ``[RAG chunk #N]`` citation tokens the grounding gate
# requires. Overridable via the same env var the runtime honours.
_OLLAMA_URL = os.environ.get("APECX_LLM_BASE_URL", "http://localhost:11434/v1")
_OLLAMA_ROOT = _OLLAMA_URL[:-3].rstrip("/") if _OLLAMA_URL.endswith("/v1") else _OLLAMA_URL
_LLM_MODEL = os.environ.get("APECX_LLM_MODEL", "mistral-nemo:latest")


def _ollama_reachable() -> bool:
    """True iff the Ollama endpoint is reachable AND the target model is pulled."""
    try:
        r = httpx.get(f"{_OLLAMA_ROOT}/api/tags", timeout=3.0)
        r.raise_for_status()
        names = {m["name"] for m in r.json().get("models", [])}
        stem = _LLM_MODEL.split(":", 1)[0]
        return any(n == _LLM_MODEL or n.split(":", 1)[0] == stem for n in names)
    except Exception:
        return False


def _new_step() -> LiteratureRagStep:
    return LiteratureRagStep.from_config(str(_STEP_YAML))


# --------------------------------------------------------------------------- #
# 1. OFFLINE unit — no LLM, no network, always runs.
# --------------------------------------------------------------------------- #


def test_no_filtered_records_degrades_loud_no_llm():
    """Empty evidence -> status=no_evidence, empty answer, and no LLM call.

    Hermetic: the step short-circuits before any embedding or LLM work, so this
    runs on a machine with neither Ollama nor sentence-transformers.
    """
    step = _new_step()
    result = asyncio.run(
        step.process({"question": "What antibodies neutralize CHIKV?", "filtered_records": []})
    )
    assert result == {"answer": "", "citations": [], "status": "no_evidence"}


def test_no_filtered_records_via_cascade_envelope():
    """The DU-wrapped ``{rag_input: {...}}`` cascade shape unwraps the same way."""
    step = _new_step()
    result = asyncio.run(step.process({"rag_input": {"question": "q", "filtered_records": []}}))
    assert result["status"] == "no_evidence"
    assert result["answer"] == ""


# --------------------------------------------------------------------------- #
# 2. Ollama-GATED integration — real LLM + real retrieval.
# --------------------------------------------------------------------------- #

# Three representative Chikungunya-virus (CHIKV) abstracts, each with a distinct
# PMID. Realistic test fixtures (real CHIKV facts), not synthetic noise — the
# retrieval + synthesis must operate on genuine domain text.
_CHIKV_RECORDS = [
    {
        "pmid": "23300718",
        "title": "Broadly neutralizing human monoclonal antibodies against Chikungunya virus",
        "abstract": (
            "Human monoclonal antibodies isolated from convalescent Chikungunya virus (CHIKV) "
            "patients potently neutralize the virus by targeting the E2 glycoprotein. Several "
            "antibodies block virus attachment and fusion, and confer protection against lethal "
            "CHIKV challenge in mouse models, supporting antibody-based prophylaxis and therapy."
        ),
    },
    {
        "pmid": "24672035",
        "title": "A live-attenuated Chikungunya virus vaccine candidate elicits protective immunity",
        "abstract": (
            "A live-attenuated Chikungunya virus vaccine candidate induced durable neutralizing "
            "antibody titers and protected non-human primates from viremia and joint inflammation. "
            "The attenuating deletions in the nsP3 and E2 regions limited replication while "
            "preserving immunogenicity, advancing CHIKV vaccine development."
        ),
    },
    {
        "pmid": "25787825",
        "title": "Complete genome sequence and phylogenetics of an epidemic Chikungunya virus strain",
        "abstract": (
            "The complete genome of an epidemic Chikungunya virus strain revealed an A226V "
            "substitution in the E1 glycoprotein associated with enhanced transmission by Aedes "
            "albopictus. Phylogenetic analysis placed the strain within the East/Central/South "
            "African lineage, informing molecular surveillance of CHIKV outbreaks."
        ),
    },
]
_FIXTURE_PMIDS = {r["pmid"] for r in _CHIKV_RECORDS}


@pytest.mark.slow
@pytest.mark.skipif(
    not _ollama_reachable(),
    reason=f"Ollama endpoint {_OLLAMA_ROOT} / model {_LLM_MODEL} not reachable",
)
def test_literature_rag_answers_and_cites_real_pmid(monkeypatch):
    """Real end-to-end: retrieve top-k CHIKV records, synthesize a grounded,
    PMID-cited answer via the shared synthesizer + its citation gate.

    Honest degrade contract: if the synthesizer's citation/grounding gate raises
    (small model fails to cite), the step returns status="synthesis_failed" and
    THIS assertion fails cleanly — it never masquerades as ok.
    """
    pytest.importorskip("sentence_transformers")
    pytest.importorskip("faiss")

    monkeypatch.setenv("APECX_LLM_MODEL", _LLM_MODEL)
    monkeypatch.setenv("APECX_LLM_TEMPERATURE", "0")

    # This test exercises the AGENT/LLM synthesis path. The default locus is DESKTOP,
    # where LiteratureRagStep now hands evidence to the host (status=host_synthesizes)
    # instead of calling the apecx LLM — so pin AGENT locus to reach the synthesizer.
    import apecx_integration.composition.runtime.execution_locus as _loc

    monkeypatch.setattr(_loc, "_ACTIVE_LOCUS", _loc.ExecutionLocus.AGENT)

    step = _new_step()
    result = asyncio.run(
        step.process(
            {
                "question": "What antibodies neutralize Chikungunya virus?",
                "filtered_records": _CHIKV_RECORDS,
            }
        )
    )

    assert result["status"] == "ok", f"expected ok, got {result!r}"
    assert isinstance(result["answer"], str) and result["answer"].strip(), (
        f"answer must be non-empty; got {result['answer']!r}"
    )
    citations = result["citations"]
    assert isinstance(citations, list) and citations, f"expected citations, got {citations!r}"
    # The grounding gate guarantees every cited chunk was one we supplied, so
    # every returned PMID must trace to a fixture PMID.
    assert _FIXTURE_PMIDS.issuperset(citations), (
        f"returned citations {citations} must all be fixture PMIDs {sorted(_FIXTURE_PMIDS)}"
    )
    assert set(citations) & _FIXTURE_PMIDS, "at least one cited PMID must be a fixture PMID"
