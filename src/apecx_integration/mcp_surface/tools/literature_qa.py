"""MCP tool — ``literature_qa``: organism-scoped, cited literature Q&A.

Exposes the merged literature RAG pipeline as ONE scientist-facing MCP tool.
Given an ``organism`` name and a free-text ``question``, it:

  1. Resolves the organism name to its canonical NCBITaxon PURL IRI
     (:func:`agents.literature.resolve.resolve_organism_to_iri`).
  2. Harvests up to ``max_papers`` PubMed records for the organism term and
     stamps each with the taxa it mentions
     (:func:`agents.literature.pipeline.harvest_and_stamp`, over a gazetteer built
     once from the synonym dictionary).
  3. Applies the IDENTIFIER FILTER: keeps only papers whose stamped ``iris``
     contain the resolved IRI. This is what drops synonym-collision look-alikes —
     papers that matched the search term textually but are about a DIFFERENT
     organism that shares an abbreviation never carry the resolved IRI, so they
     fall away.
  4. Reads the filtered papers into a grounded, PMID-cited answer with the
     ``LiteratureRagStep`` (retrieve top-k → synthesize → cite).

Degrade-loud contract (never a fake "ok"):
  - organism does not resolve            -> ``status="unresolved_organism"``
  - no papers survive the IRI filter     -> ``status="no_evidence"``
  - synthesis/gate failure in the reader -> ``status="synthesis_failed"``
  - a grounded, cited answer             -> ``status="ok"``

Clean-install rule: this module imports NO optional ``rag`` extra
(faiss / sentence-transformers) at module scope. The organism resolver, the
harvest→stamp glue, and the gazetteer are all base-dep clean; the heavy
``LiteratureRagStep`` (which lazy-loads sentence-transformers) is imported only
inside :func:`_get_reader_step`, on first use.

Gazetteer load cost (documented, one-time, module-cached): building the pathogen
gazetteer from the full synonym dictionary reads ~1.34M ``inverse_index`` rows —
about 20 s and ~440 MB resident on first call, then cached for the process
lifetime. Override the row bound with ``APECX_LITERATURE_QA_GAZETTEER_LIMIT``
(a ``LIMIT`` on the streamed rows) for a cheaper build; note some organism
surfaces sit past row 1M in the shipped dict, so a low limit can miss the
queried organism and silently under-tag. Default is the full (unbounded) build.
"""

from __future__ import annotations

import asyncio
import logging
import os

# Base-dep-clean imports (no faiss / sentence-transformers). Bound at module top
# so tests can monkeypatch them as module attributes.
from apecx_integration.agents.literature.pipeline import harvest_and_stamp
from apecx_integration.agents.literature.resolve import resolve_organism_to_iri

log = logging.getLogger(__name__)

# Process-lifetime caches. The gazetteer build is expensive (see module docstring);
# the reader step loads sentence-transformers on first synthesis. Both are built
# lazily and reused across calls on the long-lived MCP server.
_GAZETTEER = None
_READER_STEP = None


def _dict_path() -> str:
    """Resolve the synonym dictionary SQLite path (same source as the resolver)."""
    from apecx_integration.synonym_dictionary.loader import default_dictionary_path

    return str(default_dictionary_path())


def _gazetteer_limit() -> int | None:
    """Row bound for the gazetteer build; ``None`` (full) unless overridden."""
    raw = os.environ.get("APECX_LITERATURE_QA_GAZETTEER_LIMIT", "").strip()
    return int(raw) if raw else None


def _get_gazetteer():
    """Build (once) + cache the pathogen gazetteer over the synonym dictionary."""
    global _GAZETTEER
    if _GAZETTEER is None:
        from apecx_integration.agents.literature.gazetteer import build_from_dictionary

        limit = _gazetteer_limit()
        log.info(
            "literature_qa: building pathogen gazetteer from %s (limit=%s) — "
            "one-time, ~20s/~440MB for the full dict",
            _dict_path(),
            limit,
        )
        _GAZETTEER = build_from_dictionary(_dict_path(), entity_type="pathogen", limit=limit)
    return _GAZETTEER


def _get_reader_step():
    """Load (once) + cache the LiteratureRagStep via ``from_config`` (framework rule).

    Imported here, not at module scope, so importing this tool module never pulls
    in the optional ``rag`` extra (clean-install rule).
    """
    global _READER_STEP
    if _READER_STEP is None:
        from pathlib import Path

        from apecx_integration.composition.workflows.literature_rag.steps import (
            literature_rag_step as lrs,
        )

        step_yaml = Path(lrs.__file__).resolve().parent / "literature_rag_step.yml"
        _READER_STEP = lrs.LiteratureRagStep.from_config(str(step_yaml))
    return _READER_STEP


async def _read_filtered(question: str, filtered_records: list[dict]) -> dict:
    """Read the IRI-filtered records into a cited answer via the reader step.

    A single injectable seam: the offline test monkeypatches THIS function with a
    fake reader so no LLM / faiss is touched. Returns the step's
    ``{answer, citations, status}`` envelope verbatim.
    """
    step = _get_reader_step()
    return await step.process({"question": question, "filtered_records": filtered_records})


async def literature_qa(organism: str, question: str, max_papers: int = 20) -> dict:
    """Answer a question about ``organism`` from organism-filtered, cited literature.

    Resolves the organism to its NCBITaxon IRI, harvests + taxon-stamps up to
    ``max_papers`` PubMed papers for it, keeps only those actually mentioning the
    resolved organism (the identifier filter that drops synonym-collision
    look-alikes), and synthesizes a grounded answer citing real PMIDs.

    Parameters
    ----------
    organism:
        Organism name, abbreviation, or synonym (e.g. ``"Chikungunya virus"``,
        ``"CHIKV"``). Resolved through the synonym dictionary.
    question:
        Free-text scientific question to answer over the filtered papers.
    max_papers:
        Upper bound on PubMed records harvested (bounded Entrez fetch).

    Returns
    -------
    dict with:
      - ``answer``: grounded Markdown answer with inline ``[PMID:<id>]`` citations
        (empty on any non-``ok`` status).
      - ``citations``: list of cited PMIDs (numeric strings), each provably from a
        supplied paper.
      - ``n_papers_considered``: papers harvested + stamped before filtering.
      - ``n_papers_after_filter``: papers retained after the IRI filter.
      - ``organism_iri``: resolved NCBITaxon IRI, or ``None`` when unresolved.
      - ``status``: ``"ok" | "no_evidence" | "unresolved_organism" |
        "synthesis_failed"``.

    Raises
    ------
    ValueError
        If ``question`` is empty — a wiring bug, not degradable data (mirrors the
        reader step's own contract). An empty ``organism`` degrades to
        ``status="unresolved_organism"`` (nothing to resolve).
    """
    if not isinstance(question, str) or not question.strip():
        raise ValueError("literature_qa: 'question' must be a non-empty string")
    question = question.strip()

    def _empty(status: str, iri: str | None, considered: int, after: int) -> dict:
        return {
            "answer": "",
            "citations": [],
            "n_papers_considered": considered,
            "n_papers_after_filter": after,
            "organism_iri": iri,
            "status": status,
        }

    # 1. Resolve organism -> NCBITaxon IRI. Degrade-loud on a miss.
    iri = (
        resolve_organism_to_iri(organism)
        if isinstance(organism, str) and organism.strip()
        else None
    )
    if iri is None:
        log.info("literature_qa: organism %r did not resolve to an NCBITaxon IRI", organism)
        return _empty("unresolved_organism", None, 0, 0)

    # 2. Harvest + stamp. Both the harvest (its own asyncio.run) and the ~1.34M-row
    #    gazetteer build are blocking; run them off the event loop so the MCP server
    #    stays responsive and we never call asyncio.run() inside a running loop.
    gazetteer = _get_gazetteer()
    stamped = await asyncio.to_thread(
        harvest_and_stamp, organism, gazetteer, max_papers=int(max_papers)
    )
    n_considered = len(stamped)

    # 3. Identifier filter: keep only papers whose stamped iris carry the resolved
    #    IRI — this drops the synonym-collision look-alikes.
    filtered = [r for r in stamped if iri in (r.get("iris") or [])]
    n_after = len(filtered)
    if not filtered:
        log.info(
            "literature_qa: %d/%d papers survived the IRI filter for %s — no_evidence",
            n_after,
            n_considered,
            iri,
        )
        return _empty("no_evidence", iri, n_considered, 0)

    # 4. Read the filtered papers into a cited answer. The reader step returns the
    #    honest status (ok / no_evidence / synthesis_failed) — pass it straight
    #    through; never fabricate an "ok".
    result = await _read_filtered(question, filtered)
    status = result.get("status") or "synthesis_failed"
    return {
        "answer": result.get("answer") or "",
        "citations": list(result.get("citations") or []),
        "n_papers_considered": n_considered,
        "n_papers_after_filter": n_after,
        "organism_iri": iri,
        "status": status,
    }


__all__ = ["literature_qa"]
