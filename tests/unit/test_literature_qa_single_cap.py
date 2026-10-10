"""Unit test: the literature_qa tool no longer pre-caps — the reader is the SINGLE cap.

SYNTHETIC fixtures only (fabricated pmids, nonsense tokens); no network, no LLM.
Verifies that every IRI-filtered record is handed to the reader (``_read_filtered``),
so the one rank+cap authority lives in the reader step, not the tool.
"""

from __future__ import annotations

import asyncio

import apecx_integration.mcp_surface.tools.literature_qa as tool

_IRI = "http://purl.obolibrary.org/obo/NCBITaxon_00000"


def _stamped(pmid: str) -> dict:
    return {"pmid": pmid, "title": f"alpha {pmid}", "abstract": "beta gamma", "iris": [_IRI]}


def test_tool_hands_full_filtered_set_to_reader(monkeypatch):
    fixtures = [_stamped(f"p{i}") for i in range(180)]  # all match the resolved IRI
    monkeypatch.setattr(tool, "resolve_organism_to_iri", lambda term, *a, **k: _IRI)
    monkeypatch.setattr(tool, "_get_gazetteer", lambda: object())
    monkeypatch.setattr(tool, "harvest_and_stamp", lambda term, gaz, *, max_papers: list(fixtures))

    seen: dict[str, int] = {}

    async def _fake_reader(question, filtered_records):
        seen["n"] = len(filtered_records)
        return {"answer": "x [PMID:p0]", "citations": ["p0"], "status": "ok"}

    monkeypatch.setattr(tool, "_read_filtered", _fake_reader)

    result = asyncio.run(tool.literature_qa("nonsense organism", "alpha beta gamma", 200))

    assert seen["n"] == 180  # the tool did NOT pre-cap before the reader
    assert result["n_papers_after_filter"] == 180
    assert result["status"] == "ok"


def test_tool_has_no_pre_cap_symbols():
    # The tool-side ranker + its _RETURN_K constant were removed (single authority = reader).
    assert not hasattr(tool, "_rank_top_k")
    assert not hasattr(tool, "_RETURN_K")
