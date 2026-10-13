"""Harvest → stamp glue: the thin seam that turns a search term into stamped
literature records so the pipeline can run on real papers.

Reuses only: :func:`agents.literature.harvest.harvest_pubmed` (bounded Entrez
harvest → ``{pmid, title, abstract}``) and
:func:`agents.literature.stamped_corpus.stamp_abstract` (adds ``iris``). No new
network or dictionary logic lives here.
"""

from __future__ import annotations

import asyncio
import logging

from apecx_integration.agents.literature.gazetteer import Gazetteer
from apecx_integration.agents.literature.harvest import harvest_pubmed
from apecx_integration.agents.literature.stamped_corpus import stamp_abstract

log = logging.getLogger(__name__)


def harvest_and_stamp(
    term: str, gazetteer: Gazetteer, *, max_papers: int = 20, with_fulltext: bool = True
) -> list[dict]:
    """Harvest up to ``max_papers`` PubMed records for ``term``, stamp each with its taxon
    IRIs, and (best-effort) attach open-access full text.

    Returns records each carrying ``{pmid, pmcid, title, abstract, iris}`` (plus present-only
    ``authors`` / ``year`` / ``journal``) and — for the open-access subset — ``full_text`` (the Europe PMC body, bounded). Abstracts routinely
    omit the epitope-residue / animal-model detail specialized questions need; the full-text
    body carries it, so downstream synthesis grounds on ``full_text`` when present.

    ``with_fulltext=False`` skips the network full-text fetch (used by tests and callers that
    only need the stamped abstracts).
    """
    records = [
        stamp_abstract(rec, gazetteer) for rec in harvest_pubmed(term, max_papers=max_papers)
    ]
    if with_fulltext:
        _attach_fulltext(records)
    return records


def _attach_fulltext(records: list[dict]) -> None:
    """Populate ``record["full_text"]`` for records with a PMCID (open-access subset).

    Degrade-loud: a systemic Europe PMC outage logs a WARNING and leaves the records with
    their abstracts (never raises, never strands the harvest). Per-paper misses/errors are
    already tolerated inside :func:`fetch_fulltext_many`.
    """
    from apecx_integration.agents.literature.fulltext import (
        FulltextUnavailableError,
        fetch_fulltext_many,
    )

    pmcids = [str(r.get("pmcid") or "") for r in records if r.get("pmcid")]
    if not pmcids:
        return
    try:
        bodies = asyncio.run(fetch_fulltext_many(pmcids))
    except FulltextUnavailableError as exc:
        log.warning("full-text retrieval unavailable (%s); grounding on abstracts only", exc)
        return
    for r in records:
        body = bodies.get(str(r.get("pmcid") or ""))
        if body:
            r["full_text"] = body
