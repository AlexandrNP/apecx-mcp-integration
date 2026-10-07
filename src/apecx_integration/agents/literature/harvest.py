"""Bounded PubMed harvest → literature-record normalization.

Reuses the existing Entrez client (``composition/steps/_pubmed_helpers.harvest``,
eSearch → eFetch → parsed publication dicts) rather than re-authoring the
network path. This module only NORMALIZES the helper's rich publication dict
down to the literature-record contract the rest of ``agents.literature`` uses:
``{"pmid": str, "pmcid": str, "title": str, "abstract": str}`` (``pmcid`` is ""
when the paper is not in PMC; it drives open-access full-text retrieval downstream).

Import stays light: the helper (and its ``apecx_harvesters`` deps) is
lazy-imported inside :func:`harvest_pubmed`.
"""

from __future__ import annotations

import asyncio


def harvest_pubmed(term: str, *, max_papers: int = 20) -> list[dict]:
    """Harvest up to ``max_papers`` PubMed records for ``term`` (bounded).

    Drives the async Entrez helper on a fresh event loop, then normalizes each
    publication dict (keys ``doi/title/authors/year/journal/pmid/pmcid/abstract``)
    to the literature-record contract ``{"pmid", "pmcid", "title", "abstract"}``.
    Records with no abstract are dropped — coverage tagging over a title alone is
    too noisy to be useful.
    """
    from apecx_integration.composition.steps import _pubmed_helpers

    publications = asyncio.run(_pubmed_helpers.harvest(term, max_papers=max_papers))

    records: list[dict] = []
    for pub in publications:
        abstract = (pub.get("abstract") or "").strip()
        if not abstract:
            continue
        records.append(
            {
                "pmid": str(pub.get("pmid") or ""),
                "pmcid": str(pub.get("pmcid") or ""),
                "title": pub.get("title") or "",
                "abstract": abstract,
            }
        )
    return records
