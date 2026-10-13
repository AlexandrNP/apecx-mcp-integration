"""Live parity test for code-rendered attribution (Lever 4): real PubMed harvest -> real
``LiteratureRagStep.process()`` -> real Ollama synthesis -> relabel + References.

Parity partner of ``tests/unit/test_literature_rag_attribution.py`` (which stubs the LLM). The
harvest term is a neutral, non-domain toy topic; assertions are structural only (no content):
status ``ok``, anchors carry the record's year when it has one, References cover exactly the
cited PMIDs in chronological order, and every anchor author is taken from that record.

Auto-skips when ``APECX_SKIP_LIVE_LLM=1``, Ollama/the model is unavailable, or eUtils is down.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import httpx
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("APECX_LLM_MODEL", "mistral-nemo:latest")
_EUTILS_PROBE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi"
_ANCHOR = re.compile(r"\[PMID:(\d+)(?: \| ([^\]]+))?\]")


def _ollama_has_model(model: str) -> bool:
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=2.0)
        r.raise_for_status()
        names = {m["name"] for m in r.json().get("models", [])}
        return any(n == model or n.split(":", 1)[0] == model.split(":", 1)[0] for n in names)
    except Exception:
        return False


def _eutils_reachable() -> bool:
    try:
        return httpx.get(_EUTILS_PROBE, timeout=5.0).status_code < 300
    except Exception:
        return False


@pytest.fixture(autouse=True)
def _gate(monkeypatch):
    if os.environ.get("APECX_SKIP_LIVE_LLM") == "1":
        pytest.skip("APECX_SKIP_LIVE_LLM=1 — live-LLM tests skipped.")
    if not _ollama_has_model(OLLAMA_MODEL):
        pytest.skip(f"Ollama not reachable at {OLLAMA_URL} or model {OLLAMA_MODEL} not pulled.")
    if not _eutils_reachable():
        pytest.skip("PubMed eUtils unreachable.")
    monkeypatch.setenv("APECX_LLM_MODEL", OLLAMA_MODEL)
    for flag in (
        "APECX_ENABLE_LLM_VERIFY",
        "APECX_ENABLE_CROSS_ENCODER",
        "APECX_ENABLE_LLM_FUSION",
    ):
        monkeypatch.delenv(flag, raising=False)


def test_real_harvest_and_synthesis_render_grounded_attribution(monkeypatch):
    import apecx_integration.composition.runtime.execution_locus as loc
    import apecx_integration.composition.workflows.literature_rag.steps as pkg
    from apecx_integration.agents.literature.harvest import harvest_pubmed
    from apecx_integration.composition.workflows.literature_rag.steps.literature_rag_step import (
        LiteratureRagStep,
        _named_authors,
    )

    monkeypatch.setattr(loc, "_ACTIVE_LOCUS", loc.ExecutionLocus.AGENT)
    records = harvest_pubmed("banana ripening ethylene", max_papers=6)
    assert records, "real PubMed harvest returned nothing"
    assert any("year" in r for r in records), "harvest dropped year metadata"
    by_pmid = {r["pmid"]: r for r in records}

    step = LiteratureRagStep.from_config(
        str(Path(pkg.__file__).resolve().parent / "literature_rag_step.yml")
    )
    step._embed_sims = lambda q, recs: [0.0] * len(recs)  # keyword rank only; no model download
    out = asyncio.run(
        step.process(
            {"question": "What is reported about banana ripening?", "filtered_records": records}
        )
    )

    assert out["status"] == "ok", out
    body, _, refs = out["answer"].partition("\n\n## References\n\n")
    anchors = _ANCHOR.findall(body)
    assert anchors, "no PMID anchors in the synthesized answer"
    # Non-vacuous: real PubMed records carry author/year, so >=1 anchor must be enriched.
    assert any(attribution for _, attribution in anchors), anchors
    for pmid, attribution in anchors:
        rec = by_pmid[pmid]  # KeyError == an anchor PMID that was never harvested
        if rec.get("year"):
            assert rec["year"] in attribution
        named, _ = _named_authors(rec)
        surnames = {a.split(",", 1)[0].strip() for a in named}
        for part in filter(None, attribution.split(" | ")):
            if not part.isdigit():  # author part must come from THIS record's authors
                assert part.removesuffix(" et al.") in surnames, (pmid, part)

    ref_pmids = re.findall(r"PMID:(\d+)$", refs, flags=re.MULTILINE)
    assert sorted(ref_pmids) == out["citations"]  # 100% of References cited, and vice versa
    years = [int(by_pmid[p]["year"]) for p in ref_pmids if by_pmid[p].get("year", "").isdigit()]
    assert years == sorted(years)  # foundational -> recent
