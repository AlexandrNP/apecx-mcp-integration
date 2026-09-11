"""Paper-coverage aggregation over a gazetteer-tagged corpus (skeleton).

Purely in-memory over an injected list of abstract dicts — NO network. The
real Globus/PubMed harvest that feeds this is a documented ``NotImplementedError``
stub (``harvest_and_build``).
"""

from __future__ import annotations

from collections.abc import Iterable

from apecx_integration.agents.literature.gazetteer import Gazetteer, _normalize

_MAX_EXAMPLES = 3


def build_coverage(abstracts: Iterable[dict], gazetteer: Gazetteer) -> dict:
    """Aggregate matched surface forms across ``abstracts``.

    Each abstract is a dict ``{pmid, title, abstract}``. Returns a map
    ``normalized_surface -> {iri, doc_count, example_pmids: [..<=3]}`` where
    ``doc_count`` counts DISTINCT documents mentioning the surface.
    """
    coverage: dict[str, dict] = {}
    for record in abstracts:
        pmid = record.get("pmid")
        text = f"{record.get('title', '')} {record.get('abstract', '')}"
        seen_here: set[str] = set()
        for tag in gazetteer.tag(text):
            key = _normalize(tag.surface)
            if key in seen_here:
                continue
            seen_here.add(key)
            entry = coverage.setdefault(key, {"iri": tag.iri, "doc_count": 0, "example_pmids": []})
            entry["doc_count"] += 1
            examples = entry["example_pmids"]
            if pmid is not None and len(examples) < _MAX_EXAMPLES and pmid not in examples:
                examples.append(pmid)
    return coverage


def harvest_and_build(*args, **kwargs) -> dict:
    """Harvest abstracts (Globus/PubMed) then build coverage (TODO).

    Deferred: the real harvest hits Globus/PubMed. Not yet implemented; smoke
    tests inject an in-memory abstract list into ``build_coverage`` instead.
    """
    raise NotImplementedError(
        "harvest_and_build: the Globus/PubMed abstract harvest is a later task; "
        "call build_coverage(injected_abstracts, gazetteer) for now."
    )
