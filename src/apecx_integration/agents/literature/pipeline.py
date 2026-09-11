"""Harvest → stamp glue: the thin seam that turns a search term into stamped
literature records so the pipeline can run on real papers.

Reuses only: :func:`agents.literature.harvest.harvest_pubmed` (bounded Entrez
harvest → ``{pmid, title, abstract}``) and
:func:`agents.literature.stamped_corpus.stamp_abstract` (adds ``iris``). No new
network or dictionary logic lives here.
"""

from __future__ import annotations

from apecx_integration.agents.literature.gazetteer import Gazetteer
from apecx_integration.agents.literature.harvest import harvest_pubmed
from apecx_integration.agents.literature.stamped_corpus import stamp_abstract


def harvest_and_stamp(term: str, gazetteer: Gazetteer, *, max_papers: int = 20) -> list[dict]:
    """Harvest up to ``max_papers`` PubMed records for ``term`` and stamp each
    with its taxon IRIs.

    Returns records each carrying ``{pmid, title, abstract, iris}`` — the harvest
    contract plus the ``iris`` list :func:`stamp_abstract` adds.
    """
    return [stamp_abstract(rec, gazetteer) for rec in harvest_pubmed(term, max_papers=max_papers)]
