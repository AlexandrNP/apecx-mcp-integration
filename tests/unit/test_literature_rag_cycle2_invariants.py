"""Cycle-2 regression/invariant tests — reader is the SOLE rank+gate+cap authority.

SYNTHETIC fixtures only (fabricated pmids / nonsense tokens; no network, no LLM, no
faiss / sentence-transformers — the embedding tiebreak is monkeypatched to zeros so
ranking collapses to the deterministic IDF keyword signal). Mechanism-only.

Covers:
  * Invariant 1 — single cap authority: the tool has no cap symbols, does not
    slice/sort the record list, and hands the FULL IRI-filtered set (>15) to the reader.
  * Invariant 2 — rank+cap runs in EVERY locus BEFORE the locus branch; the desktop
    branch hands the ranked (<=15) set, not the full set.
  * Invariant 3 — nanobrain from_config wiring (loads; process defined; execute not
    overridden).
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import apecx_integration.mcp_surface.tools.literature_qa as tool
from apecx_integration.composition.runtime.execution_locus import (
    ExecutionLocus,
    get_active_locus,
    set_active_locus,
)
from apecx_integration.composition.workflows.literature_rag.steps import (
    literature_rag_step as lrs,
)

_IRI = "http://purl.obolibrary.org/obo/NCBITaxon_424242"
_TOP_K = lrs._TOP_K  # 15


def _record(pmid: str) -> dict:
    # Every record carries the same matching facets -> equal IDF keyword scores,
    # so the gate keeps all and the cap (not the gate) is what trims to _TOP_K.
    return {
        "pmid": pmid,
        "title": f"alpha beta gamma record {pmid}",
        "abstract": "alpha beta gamma delta",
        "iris": [_IRI],
    }


# ---------------------------------------------------------------------------
# Invariant 1 — single cap authority
# ---------------------------------------------------------------------------
def test_tool_has_no_cap_symbols_or_slicing():
    """Tool module defines no cap helper/constant and never slices/sorts records."""
    assert not hasattr(tool, "_rank_top_k")
    assert not hasattr(tool, "_RETURN_K")
    src = inspect.getsource(tool)
    # No list slice with a numeric bound and no sort of the record list in the tool.
    assert ".sort(" not in src
    assert "sorted(" not in src


def test_tool_hands_full_unsorted_set_to_reader(monkeypatch):
    """>15 fabricated IRI-stamped records reach the reader intact (tool imposes no cap)."""
    n = _TOP_K + 12  # 27 > 15
    fixtures = [_record(f"p{i:02d}") for i in range(n)]
    monkeypatch.setattr(tool, "resolve_organism_to_iri", lambda term, *a, **k: _IRI)
    monkeypatch.setattr(tool, "_get_gazetteer", lambda: object())
    monkeypatch.setattr(tool, "harvest_and_stamp", lambda term, gaz, *, max_papers: list(fixtures))

    spy: dict[str, object] = {}

    async def _spy_reader(question, filtered_records):
        spy["n"] = len(filtered_records)
        spy["pmids"] = [r["pmid"] for r in filtered_records]
        return {"answer": "x [PMID:p00]", "citations": ["p00"], "status": "ok"}

    monkeypatch.setattr(tool, "_read_filtered", _spy_reader)

    result = asyncio.run(tool.literature_qa("fake organism", "alpha beta gamma", max_papers=500))

    assert spy["n"] == n  # full set reached the reader — NO tool-side cap
    assert spy["pmids"] == [f"p{i:02d}" for i in range(n)]  # order + membership intact
    assert result["n_papers_after_filter"] == n
    assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# Invariant 2 — rank+cap runs in EVERY locus, BEFORE the locus branch
# ---------------------------------------------------------------------------
def test_rank_precedes_locus_branch_in_source():
    """In process(), _hybrid_rank is invoked BEFORE the get_active_locus() branch."""
    src = inspect.getsource(lrs.LiteratureRagStep.process)
    rank_pos = src.index("_hybrid_rank")
    locus_pos = src.index("get_active_locus()")
    assert rank_pos < locus_pos, "rank+cap must run before the locus branch"


def test_desktop_branch_hands_ranked_capped_set(monkeypatch):
    """Desktop locus + >15 records -> status=host_synthesizes; citations/evidence <= _TOP_K."""
    # Avoid loading the real embedding model: collapse the tiebreak to zeros so the
    # deterministic IDF keyword signal ranks (every record equal -> cap, not gate, trims).
    monkeypatch.setattr(
        lrs.LiteratureRagStep, "_embed_sims", lambda self, q, recs: [0.0] * len(recs)
    )

    n = _TOP_K + 10  # 25 > 15
    records = [_record(f"d{i:02d}") for i in range(n)]

    step_yaml = Path(lrs.__file__).resolve().parent / "literature_rag_step.yml"
    step = lrs.LiteratureRagStep.from_config(str(step_yaml))

    prev = get_active_locus()
    set_active_locus(ExecutionLocus.DESKTOP)
    try:
        result = asyncio.run(
            step.process({"question": "alpha beta gamma", "filtered_records": records})
        )
    finally:
        set_active_locus(prev)

    assert result["status"] == "host_synthesizes"
    # Cap enforced in the desktop branch: at most _TOP_K citations.
    assert 0 < len(result["citations"]) <= _TOP_K
    # Evidence body carries one "## [PMID:...]" header per handed record -> also capped.
    n_evidence_blocks = result["answer"].count("## [PMID:")
    assert n_evidence_blocks <= _TOP_K
    assert n_evidence_blocks == len(result["citations"])


# ---------------------------------------------------------------------------
# Invariant 3 — nanobrain from_config wiring
# ---------------------------------------------------------------------------
def test_reader_from_config_and_contract():
    step_yaml = Path(lrs.__file__).resolve().parent / "literature_rag_step.yml"
    step = lrs.LiteratureRagStep.from_config(str(step_yaml))
    assert step is not None
    assert "process" in vars(lrs.LiteratureRagStep)
    assert inspect.iscoroutinefunction(lrs.LiteratureRagStep.process)
    from nanobrain.core.step import BaseStep

    assert "execute" not in vars(lrs.LiteratureRagStep)
    assert lrs.LiteratureRagStep.execute is BaseStep.execute
