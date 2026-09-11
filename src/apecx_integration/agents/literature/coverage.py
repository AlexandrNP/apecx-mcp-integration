"""Paper-coverage aggregation over a gazetteer-tagged corpus.

``build_coverage`` is pure and in-memory over a list of abstract dicts (NO
network), so it stays trivially testable. ``harvest_and_build`` wires it to a
real live-PubMed harvest (``harvest_pubmed``) plus gazetteer tagging.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from apecx_integration.agents.literature.gazetteer import Gazetteer, _normalize

_MAX_EXAMPLES = 3


def build_coverage(abstracts: Iterable[dict], gazetteer: Gazetteer) -> list[dict]:
    """Aggregate matched surface forms across ``abstracts`` (deliverable-1 shape).

    Each abstract is a dict ``{pmid, title, abstract}``. Returns one entry per
    matched surface form, ``{term, iri, doc_count, example_pmids: [..<=3]}``,
    where ``doc_count`` counts DISTINCT documents mentioning the surface. The
    list is sorted by ``doc_count`` descending, then ``term`` ascending. Pure
    over the injected list — NO network.
    """
    aggregated: dict[str, dict] = {}
    for record in abstracts:
        pmid = record.get("pmid")
        text = f"{record.get('title', '')} {record.get('abstract', '')}"
        seen_here: set[str] = set()
        for tag in gazetteer.tag(text):
            term = _normalize(tag.surface)
            if term in seen_here:
                continue
            seen_here.add(term)
            entry = aggregated.setdefault(
                term, {"term": term, "iri": tag.iri, "doc_count": 0, "example_pmids": []}
            )
            entry["doc_count"] += 1
            examples = entry["example_pmids"]
            if pmid is not None and len(examples) < _MAX_EXAMPLES and pmid not in examples:
                examples.append(pmid)
    return sorted(aggregated.values(), key=lambda e: (-e["doc_count"], e["term"]))


def coverage_to_json(coverage: list[dict]) -> str:
    """Serialize coverage entries as pretty JSON with stable key order.

    Writes the ``literature_term_coverage.json`` artifact. Entry order (by
    ``doc_count``) is preserved; keys within each entry are sorted so the
    artifact diffs cleanly across runs.
    """
    return json.dumps(coverage, indent=2, sort_keys=True)


def harvest_and_build(term: str, gazetteer: Gazetteer, *, max_papers: int = 20) -> list[dict]:
    """Harvest PubMed abstracts for ``term`` then build coverage (deliverable-1).

    The real path: :func:`harvest_pubmed` runs a bounded Entrez harvest and
    normalizes each hit to ``{pmid, title, abstract}``; :func:`build_coverage`
    aggregates gazetteer surface-form matches across those abstracts. Returns
    the coverage-entry list (see :func:`build_coverage`).
    """
    from apecx_integration.agents.literature.harvest import harvest_pubmed

    abstracts = harvest_pubmed(term, max_papers=max_papers)
    return build_coverage(abstracts, gazetteer)
