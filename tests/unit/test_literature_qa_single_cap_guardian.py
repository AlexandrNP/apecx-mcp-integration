"""Guardian invariant tests — single cap authority + nanobrain from_config wiring.

SYNTHETIC fixtures only (fabricated pmids / nonsense tokens; no network, no LLM,
no faiss). Mechanism-only: proves the ranking cap lives SOLELY in the reader step,
not in the ``literature_qa`` tool, and that the reader step still satisfies the
nanobrain contract (from_config loads; implements ``process``; never overrides
``execute``).
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import apecx_integration.mcp_surface.tools.literature_qa as tool

_IRI = "http://purl.obolibrary.org/obo/NCBITaxon_99999"
_N = 42  # > reader _TOP_K (35): proves the tool does NOT cap


def _stamped(pmid: str) -> dict:
    return {"pmid": pmid, "title": f"zeta {pmid}", "abstract": "eta theta", "iris": [_IRI]}


def test_tool_hands_full_set_reader_owns_cap(monkeypatch):
    """Tool passes the FULL (>35) IRI-filtered set to the reader; it never pre-caps."""
    fixtures = [_stamped(f"q{i}") for i in range(_N)]
    monkeypatch.setattr(tool, "resolve_organism_to_iri", lambda term, *a, **k: _IRI)
    monkeypatch.setattr(tool, "_get_gazetteer", lambda: object())
    monkeypatch.setattr(tool, "harvest_and_stamp", lambda term, gaz, *, max_papers: list(fixtures))

    spy: dict[str, object] = {}

    async def _spy_reader(question, filtered_records):
        spy["n"] = len(filtered_records)
        spy["pmids"] = [r["pmid"] for r in filtered_records]
        return {"answer": "x [PMID:q0]", "citations": ["q0"], "status": "ok"}

    monkeypatch.setattr(tool, "_read_filtered", _spy_reader)

    result = asyncio.run(tool.literature_qa("fake organism", "zeta eta theta", max_papers=200))

    assert spy["n"] == _N  # full set reached the reader — tool imposed NO cap
    assert spy["pmids"] == [f"q{i}" for i in range(_N)]  # order + membership preserved
    assert result["n_papers_after_filter"] == _N
    assert result["status"] == "ok"


def test_tool_defines_no_cap_symbols():
    """The retired tool-side cap helper + constant are gone (single authority = reader)."""
    assert not hasattr(tool, "_rank_top_k")
    assert not hasattr(tool, "_RETURN_K")


def test_reader_step_from_config_and_nanobrain_contract():
    """LiteratureRagStep.from_config loads; implements process; does NOT override execute."""
    from apecx_integration.composition.workflows.literature_rag.steps import (
        literature_rag_step as lrs,
    )

    step_yaml = Path(lrs.__file__).resolve().parent / "literature_rag_step.yml"
    step = lrs.LiteratureRagStep.from_config(str(step_yaml))
    assert step is not None

    # Implements its own process().
    assert "process" in vars(lrs.LiteratureRagStep)
    # Never overrides execute() — must resolve to the framework BaseStep definition.
    from nanobrain.core.step import BaseStep

    assert "execute" not in vars(lrs.LiteratureRagStep)
    assert lrs.LiteratureRagStep.execute is BaseStep.execute
    assert inspect.iscoroutinefunction(lrs.LiteratureRagStep.process)
